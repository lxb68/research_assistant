"""优先调用 MinerU 云 API，并按显式配置回退到本地 CLI。"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import filecmp
from io import BytesIO
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional
import zipfile
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
import requests

from app.core.config import settings


MINERU_PROBE_BATCH_ID = str(UUID(int=0))
_RETRYABLE_WINDOWS_FILE_ERRORS = {5, 32, 33}
_OUTPUT_LOCKS_GUARD = threading.Lock()
_OUTPUT_LOCKS: dict[Path, tuple[threading.RLock, int]] = {}


class MinerUOutputPublishError(RuntimeError):
    """表示 MinerU 已生成结果，但结果目录未能安全发布。"""


@contextmanager
def _output_directory_lock(output_dir: Path):
    """按最终输出目录串行发布，并在无人等待后释放锁注册项。"""
    key = output_dir.resolve()
    with _OUTPUT_LOCKS_GUARD:
        lock, users = _OUTPUT_LOCKS.get(key, (threading.RLock(), 0))
        _OUTPUT_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _OUTPUT_LOCKS_GUARD:
            current_lock, users = _OUTPUT_LOCKS.get(key, (lock, 1))
            if current_lock is lock and users <= 1:
                _OUTPUT_LOCKS.pop(key, None)
            elif current_lock is lock:
                _OUTPUT_LOCKS[key] = (lock, users - 1)


def test_mineru_connection(
    *,
    token: str,
    api_base: str,
    timeout: int,
) -> None:
    """通过只读任务查询验证 MinerU 地址和 Token，不创建解析任务。"""
    if not token.strip():
        raise ValueError("未配置 MinerU API Token")
    normalized_base = api_base.strip().rstrip("/")
    if not normalized_base:
        raise ValueError("未配置 MinerU API 地址")
    try:
        response = requests.get(
            f"{normalized_base}/extract-results/batch/{MINERU_PROBE_BATCH_ID}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise RuntimeError(f"MinerU 连接失败：{error}") from error
    if response.status_code in {401, 403}:
        raise RuntimeError(f"MinerU Token 鉴权失败（HTTP {response.status_code}）")
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"MinerU 服务返回 HTTP {response.status_code}：{_response_excerpt(response)}")
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("MinerU 服务返回了非 JSON 响应，请检查 API 地址") from error
    if not isinstance(payload, dict) or "code" not in payload:
        raise RuntimeError("MinerU 服务响应格式不兼容，请检查 API 地址")
    if payload.get("code") == 0:
        return
    message = str(payload.get("msg") or "未知错误")
    normalized_message = message.lower()
    auth_markers = ("token", "auth", "unauthorized", "forbidden", "鉴权", "认证", "未授权", "无权限", "登录")
    if any(marker in normalized_message for marker in auth_markers):
        raise RuntimeError(f"MinerU Token 鉴权失败：{message}")
    # 随机任务不存在属于预期业务错误，说明服务可达且鉴权已通过。


class MinerURequest(BaseModel):
    """定义 MinerU 转换任务的输入参数。"""
    record_id: Optional[str] = Field(None, description="已保存的论文记录 ID")
    project_id: Optional[str] = Field(None, description="兼容旧接口的项目 ID")
    file_name: Optional[str] = Field(None, description="兼容旧接口的 PDF 文件名")
    pdf_path: Optional[str] = Field(None, description="PDF 绝对路径或 storage/papers 下的文件名")
    output_name: Optional[str] = Field(None, description="可选的 MinerU 输出目录名")
    mineru_token: Optional[str] = Field(None, description="可选的 MinerU API 令牌覆盖值")
    split_min_length: Optional[int] = Field(None, ge=100, description="可选的最小分块长度")
    split_max_length: Optional[int] = Field(None, ge=200, description="可选的最大分块长度")


@dataclass
class MinerUPaths:
    """保存 MinerU 转换过程涉及的输入与输出路径。"""
    pdf_path: Path
    output_dir: Path


def mineru_processing(
    project_id: str | None = None,
    file_name: str | None = None,
    update_task_callback=None,
    task_info: dict | None = None,
    *,
    pdf_path: str | None = None,
    output_name: str | None = None,
    mineru_token: str | None = None,
) -> dict:
    """把 PDF 转换为 Markdown，并将关联资源写入 storage/markdown。"""
    del update_task_callback, task_info

    paths = _resolve_paths(
        project_id=project_id,
        file_name=file_name,
        pdf_path=pdf_path,
        output_name=output_name,
    )
    paths.output_dir.parent.mkdir(parents=True, exist_ok=True)
    processing_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{paths.output_dir.name}.processing-",
            dir=paths.output_dir.parent,
        )
    )

    token = (mineru_token or settings.mineru_api_token).strip()
    cloud_error: str | None = None
    conversion_succeeded = False

    try:
        if token:
            try:
                _run_mineru_cloud_api(paths.pdf_path, processing_dir, token)
                conversion_succeeded = True
            except Exception as error:
                cloud_error = str(error)
        else:
            cloud_error = "MINERU_API_TOKEN is not configured"

        if not conversion_succeeded and settings.mineru_enable_local_cli_fallback:
            print("[MinerU] 云端转换未生成 Markdown，已启用本地 CLI 回退", flush=True)
            _reset_processing_directory(processing_dir)
            try:
                _run_local_mineru_cli(paths.pdf_path, processing_dir)
                conversion_succeeded = True
            except Exception as error:
                raise RuntimeError(
                    f"MinerU cloud conversion failed: {cloud_error}; "
                    f"local CLI fallback failed: {error}",
                ) from error

        if not conversion_succeeded:
            fallback_hint = (
                " Set MINERU_ENABLE_LOCAL_CLI_FALLBACK=true to explicitly enable local CLI fallback."
            )
            raise RuntimeError(f"MinerU cloud conversion failed: {cloud_error}.{fallback_hint}")

        processing_markdown = _select_primary_markdown(processing_dir, preferred_stem=paths.pdf_path.stem)
        if processing_markdown is None:
            raise RuntimeError("MinerU finished but no markdown output was found")
        relative_markdown = processing_markdown.relative_to(processing_dir)
        source_pdf_copy = processing_dir / paths.pdf_path.name
        if not source_pdf_copy.exists():
            shutil.copy2(paths.pdf_path, source_pdf_copy)
        resource_summary = _summarize_output(processing_dir)
        promoted = _promote_processing_output(processing_dir, paths.output_dir, paths.pdf_path)
        if not promoted:
            processing_markdown = _select_primary_markdown(
                paths.output_dir,
                preferred_stem=paths.pdf_path.stem,
            )
            if processing_markdown is None:
                raise MinerUOutputPublishError("复用结果目录时未找到有效 Markdown")
            relative_markdown = processing_markdown.relative_to(paths.output_dir)
            resource_summary = _summarize_output(paths.output_dir)
    finally:
        if processing_dir.exists():
            shutil.rmtree(processing_dir, ignore_errors=True)

    markdown_path = paths.output_dir / relative_markdown
    source_pdf_copy = paths.output_dir / paths.pdf_path.name

    return {
        "success": True,
        "pdfPath": str(paths.pdf_path),
        "outputDir": str(paths.output_dir),
        "markdownPath": str(markdown_path),
        "sourcePdfPath": str(source_pdf_copy),
        "reusedExisting": not promoted,
        "publishWarning": (
            "MinerU 新结果发布时遇到 Windows 文件占用，已复用同一 PDF 的有效历史结果"
            if not promoted else ""
        ),
        **resource_summary,
    }


def materialize_mineru_result(
    *,
    pdf_path: str | Path,
    output_name: str,
    result_url: str,
) -> dict:
    """下载并原子发布已完成的 MinerU 结果，供批量协调器复用。"""
    paths = _resolve_paths(
        project_id=None,
        file_name=None,
        pdf_path=str(pdf_path),
        output_name=output_name,
    )
    paths.output_dir.parent.mkdir(parents=True, exist_ok=True)
    processing_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{paths.output_dir.name}.processing-",
            dir=paths.output_dir.parent,
        )
    )
    try:
        _download_mineru_result(
            result_url,
            processing_dir,
            max(settings.mineru_request_timeout_seconds, 300),
        )
        markdown = _select_primary_markdown(processing_dir, preferred_stem=paths.pdf_path.stem)
        if markdown is None:
            raise RuntimeError("MinerU cloud result ZIP did not contain Markdown")
        relative_markdown = markdown.relative_to(processing_dir)
        source_pdf_copy = processing_dir / paths.pdf_path.name
        if not source_pdf_copy.exists():
            shutil.copy2(paths.pdf_path, source_pdf_copy)
        resource_summary = _summarize_output(processing_dir)
        promoted = _promote_processing_output(processing_dir, paths.output_dir, paths.pdf_path)
        if not promoted:
            markdown = _select_primary_markdown(paths.output_dir, preferred_stem=paths.pdf_path.stem)
            if markdown is None:
                raise MinerUOutputPublishError("复用结果目录时未找到有效 Markdown")
            relative_markdown = markdown.relative_to(paths.output_dir)
            resource_summary = _summarize_output(paths.output_dir)
    finally:
        if processing_dir.exists():
            shutil.rmtree(processing_dir, ignore_errors=True)

    markdown_path = paths.output_dir / relative_markdown
    return {
        "success": True,
        "pdfPath": str(paths.pdf_path),
        "outputDir": str(paths.output_dir),
        "markdownPath": str(markdown_path),
        "sourcePdfPath": str(paths.output_dir / paths.pdf_path.name),
        "reusedExisting": not promoted,
        "publishWarning": (
            "MinerU 新结果发布时遇到 Windows 文件占用，已复用同一 PDF 的有效历史结果"
            if not promoted else ""
        ),
        **resource_summary,
    }


def _resolve_paths(
    *,
    project_id: str | None,
    file_name: str | None,
    pdf_path: str | None,
    output_name: str | None,
) -> MinerUPaths:
    """解析路径。"""
    resolved_pdf = _resolve_pdf_path(project_id=project_id, file_name=file_name, pdf_path=pdf_path)
    if not resolved_pdf.exists() or not resolved_pdf.is_file():
        raise RuntimeError(f"PDF file does not exist: {resolved_pdf}")
    if resolved_pdf.suffix.lower() != ".pdf":
        raise RuntimeError(f"Only PDF files are supported: {resolved_pdf}")

    safe_output_name = _build_output_name(output_name or resolved_pdf.stem)
    output_dir = Path(settings.mineru_output_dir) / safe_output_name
    return MinerUPaths(pdf_path=resolved_pdf, output_dir=output_dir)


def _resolve_pdf_path(
    *,
    project_id: str | None,
    file_name: str | None,
    pdf_path: str | None,
) -> Path:
    """解析PDF、路径。"""
    if pdf_path:
        candidate = Path(pdf_path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path(settings.hunter_download_dir) / candidate).resolve()

    if file_name:
        if project_id:
            project_root = Path(os.getenv("PROJECT_ROOT") or settings.backend_storage_dir)
            project_candidate = project_root / project_id / "files" / file_name
            if project_candidate.exists():
                return project_candidate.resolve()
        return (Path(settings.hunter_download_dir) / file_name).resolve()

    raise RuntimeError("Missing PDF locator. Provide pdf_path or file_name")


def _build_output_name(name: str) -> str:
    """构建输出结果。"""
    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name.strip())
    normalized = normalized.strip("._") or "mineru_output"
    return normalized[:120]


def _run_local_mineru_cli(pdf_path: Path, output_dir: Path) -> None:
    """调用本地 MinerU 命令行工具转换 PDF。"""
    command = shutil.which("mineru") or shutil.which("magic-pdf")
    if not command:
        raise RuntimeError("Local mineru or magic-pdf CLI is not installed")

    candidates = [
        [command, "-p", str(pdf_path), "-o", str(output_dir)],
        [command, "-i", str(pdf_path), "-o", str(output_dir)],
        [command, str(pdf_path), "-o", str(output_dir)],
    ]

    last_error = "Unknown MinerU CLI error"
    for args in candidates:
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, timeout=600)
            if _has_markdown_output(output_dir):
                return
            last_error = f"Command succeeded but produced no markdown: {' '.join(args)}"
        except subprocess.TimeoutExpired as error:
            last_error = f"Local MinerU CLI timed out after {error.timeout}s: {' '.join(args)}"
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            stdout = (error.stdout or "").strip()
            diagnostic = stderr or stdout or str(error)
            last_error = f"Local MinerU CLI failed: {' '.join(args)} -> {diagnostic}"
        except Exception as error:
            last_error = f"Local MinerU CLI crashed: {' '.join(args)} -> {error}"

    raise RuntimeError(last_error)


def _run_mineru_cloud_api(pdf_path: Path, output_dir: Path, token: str) -> None:
    """通过 MinerU v4 签名上传接口解析本地 PDF 并解压完整结果。"""
    api_base = settings.mineru_api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    request_timeout = settings.mineru_request_timeout_seconds

    print(f"[MinerU] 正在向云端申请上传地址：{pdf_path.name}", flush=True)
    try:
        apply_response = requests.post(
            f"{api_base}/file-urls/batch",
            headers=headers,
            json={
                "files": [
                    {
                        "name": pdf_path.name,
                        "is_ocr": True,
                    },
                ],
                "model_version": settings.mineru_model_version,
                "enable_formula": True,
                "enable_table": True,
            },
            timeout=request_timeout,
        )
        apply_payload = _read_cloud_payload(apply_response, "apply upload URL")
        apply_data = apply_payload.get("data") or {}
        batch_id = str(apply_data.get("batch_id") or "").strip()
        file_urls = apply_data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise RuntimeError("MinerU cloud response did not contain batch_id and file_urls")

        print(f"[MinerU] 正在上传 PDF：batch_id={batch_id}", flush=True)
        with pdf_path.open("rb") as pdf_file:
            upload_response = requests.put(
                str(file_urls[0]),
                data=pdf_file,
                timeout=max(request_timeout, 300),
            )
        if not 200 <= upload_response.status_code < 300:
            raise RuntimeError(
                f"MinerU cloud upload failed with HTTP {upload_response.status_code}: "
                f"{_response_excerpt(upload_response)}",
            )

        result_url = _poll_mineru_cloud_result(batch_id, pdf_path.name, token)
        print("[MinerU] 云端解析完成，正在下载结果", flush=True)
        _download_mineru_result(result_url, output_dir, max(request_timeout, 300))
    except requests.RequestException as error:
        raise RuntimeError(f"MinerU cloud request failed: {error}") from error

    if not _has_markdown_output(output_dir):
        raise RuntimeError("MinerU cloud result ZIP did not contain Markdown")
    print(f"[MinerU] 云端结果已保存：{output_dir}", flush=True)


def _poll_mineru_cloud_result(batch_id: str, file_name: str, token: str) -> str:
    """轮询批量解析状态，成功时返回完整结果 ZIP 地址。"""
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + settings.mineru_cloud_timeout_seconds
    last_state = "waiting-file"

    while time.monotonic() < deadline:
        response = requests.get(
            f"{settings.mineru_api_base}/extract-results/batch/{batch_id}",
            headers=headers,
            timeout=settings.mineru_request_timeout_seconds,
        )
        payload = _read_cloud_payload(response, "poll extraction result")
        data = payload.get("data") or {}
        results = data.get("extract_result") or data.get("extract_results") or []
        result = next(
            (item for item in results if item.get("file_name") == file_name),
            results[0] if results else None,
        )

        if result:
            last_state = str(result.get("state") or "unknown").lower()
            progress = result.get("extract_progress") or {}
            extracted_pages = progress.get("extracted_pages")
            total_pages = progress.get("total_pages")
            progress_text = (
                f"，页数 {extracted_pages}/{total_pages}"
                if extracted_pages is not None and total_pages is not None
                else ""
            )
            print(f"[MinerU] 云端状态：{last_state}{progress_text}", flush=True)

            if last_state == "done":
                result_url = str(result.get("full_zip_url") or "").strip()
                if not result_url:
                    raise RuntimeError("MinerU cloud task finished without full_zip_url")
                return result_url
            if last_state == "failed":
                reason = result.get("err_msg") or "unknown cloud parsing error"
                raise RuntimeError(f"MinerU cloud task failed: {reason}")

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(settings.mineru_poll_interval_seconds, remaining))

    raise RuntimeError(
        f"MinerU cloud task timed out after {settings.mineru_cloud_timeout_seconds}s "
        f"(last state: {last_state})",
    )


def _read_cloud_payload(response: requests.Response, action: str) -> dict:
    """校验 MinerU API 的 HTTP 状态与业务状态码。"""
    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"MinerU cloud {action} failed with HTTP {response.status_code}: "
            f"{_response_excerpt(response)}",
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"MinerU cloud {action} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"MinerU cloud {action} returned a non-object JSON response")
    if payload.get("code") != 0:
        message = payload.get("msg") or "unknown MinerU API error"
        trace_id = payload.get("trace_id")
        trace_hint = f" (trace_id: {trace_id})" if trace_id else ""
        raise RuntimeError(f"MinerU cloud {action} failed: {message}{trace_hint}")
    return payload


def _response_excerpt(response: requests.Response) -> str:
    """提取有限长度的响应文本用于诊断。"""
    return (response.text or "no response body").strip()[:500]


def _download_mineru_result(result_url: str, output_dir: Path, timeout: int) -> None:
    """下载结果 ZIP；Python TLS 不兼容时在 Windows 上回退到系统 curl。"""
    try:
        response = requests.get(result_url, timeout=timeout)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"MinerU result download failed with HTTP {response.status_code}: "
                f"{_response_excerpt(response)}",
            )
        _extract_result_zip(response.content, output_dir)
        return
    except requests.exceptions.SSLError as ssl_error:
        curl_command = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_command:
            raise RuntimeError(f"MinerU result download TLS failed: {ssl_error}") from ssl_error

    print("[MinerU] Python TLS 下载失败，正在使用系统 curl 重试", flush=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="mineru-result-",
            suffix=".zip",
            dir=output_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        completed = subprocess.run(
            [
                curl_command,
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--max-time",
                str(timeout),
                "--output",
                str(temporary_path),
                result_url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout or "unknown curl error").strip()[:500]
            raise RuntimeError(f"MinerU result download via curl failed: {diagnostic}")
        _extract_result_zip_file(temporary_path, output_dir)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"MinerU result download via curl timed out after {timeout}s") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _extract_result_zip(content: bytes, output_dir: Path) -> None:
    """安全解压 MinerU 结果 ZIP，拒绝越界路径。"""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            _extract_archive_members(archive, output_dir)
    except zipfile.BadZipFile as error:
        raise RuntimeError("MinerU result download was not a valid ZIP file") from error


def _extract_result_zip_file(zip_path: Path, output_dir: Path) -> None:
    """从磁盘安全解压 MinerU 结果 ZIP，避免把完整压缩包读入内存。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _extract_archive_members(archive, output_dir)
    except zipfile.BadZipFile as error:
        raise RuntimeError("MinerU result download was not a valid ZIP file") from error


def _extract_archive_members(archive: zipfile.ZipFile, output_dir: Path) -> None:
    """解压 ZIP 成员并拒绝路径穿越。"""
    for member in archive.infolist():
        normalized_name = member.filename.replace("\\", "/")
        relative_path = Path(normalized_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"Unsafe path in MinerU result ZIP: {member.filename}")
        target_path = output_dir / relative_path
        if member.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target_path.open("wb") as target:
            shutil.copyfileobj(source, target)


def _has_markdown_output(output_dir: Path) -> bool:
    """判断Markdown、输出结果。"""
    return any(path.is_file() and path.suffix.lower() == ".md" for path in output_dir.rglob("*.md"))


def _select_primary_markdown(output_dir: Path, *, preferred_stem: str = "") -> Path | None:
    """优先选择标准 full.md，其次选择同名或正文体积最大的 Markdown。"""
    markdown_files = sorted(path for path in output_dir.rglob("*.md") if path.is_file())
    if not markdown_files:
        return None
    full_markdown = [path for path in markdown_files if path.name.lower() == "full.md"]
    if full_markdown:
        return max(full_markdown, key=lambda path: path.stat().st_size)
    normalized_stem = preferred_stem.strip().lower()
    same_name = [path for path in markdown_files if normalized_stem and path.stem.lower() == normalized_stem]
    return max(same_name or markdown_files, key=lambda path: path.stat().st_size)


def _reset_processing_directory(processing_dir: Path) -> None:
    """清空本次临时结果，防止云端残留污染本地 CLI 回退。"""
    shutil.rmtree(processing_dir)
    processing_dir.mkdir(parents=True)


def _is_retryable_file_lock(error: OSError) -> bool:
    """识别 Windows 文件占用及其在其他平台上的 PermissionError 等价形式。"""
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in _RETRYABLE_WINDOWS_FILE_ERRORS


def _replace_path_with_retry(source: Path, target: Path, *, attempts: int = 6) -> None:
    """对短暂文件占用执行有限退避重试，不掩盖路径或权限配置错误。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if not _is_retryable_file_lock(error) or attempt + 1 >= attempts:
                raise
            delay = min(1.6, 0.1 * (2 ** attempt))
            print(
                f"[MinerU] 结果目录暂时被占用，{delay:.1f} 秒后重试 "
                f"({attempt + 1}/{attempts - 1})：{source} -> {target}",
                flush=True,
            )
            time.sleep(delay)


def _can_reuse_existing_output(output_dir: Path, pdf_path: Path) -> bool:
    """仅当历史目录包含有效 Markdown 且 PDF 字节一致时允许复用。"""
    markdown = _select_primary_markdown(output_dir, preferred_stem=pdf_path.stem)
    managed_pdf = output_dir / pdf_path.name
    if markdown is None or not managed_pdf.is_file():
        return False
    try:
        return filecmp.cmp(pdf_path, managed_pdf, shallow=False)
    except OSError:
        return False


def _promote_processing_output(processing_dir: Path, output_dir: Path, pdf_path: Path) -> bool:
    """串行发布新结果；失败时恢复旧结果，并仅复用同一 PDF 的有效产物。"""
    backup_dir = output_dir.with_name(f".{output_dir.name}.backup-{uuid4().hex}")
    with _output_directory_lock(output_dir):
        previous_moved = False
        try:
            if output_dir.exists():
                _replace_path_with_retry(output_dir, backup_dir)
                previous_moved = True
            _replace_path_with_retry(processing_dir, output_dir)
        except OSError as error:
            if previous_moved and backup_dir.exists() and not output_dir.exists():
                try:
                    _replace_path_with_retry(backup_dir, output_dir)
                except OSError as rollback_error:
                    raise MinerUOutputPublishError(
                        f"MinerU 结果发布失败，且历史结果恢复失败：{rollback_error}",
                    ) from error
            if _can_reuse_existing_output(output_dir, pdf_path):
                print(
                    f"[MinerU] 新结果发布失败，已复用同一 PDF 的有效历史结果：{output_dir}",
                    flush=True,
                )
                return False
            raise MinerUOutputPublishError(f"MinerU 结果发布失败：{error}") from error
        else:
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            return True


def _summarize_output(output_dir: Path) -> dict:
    """汇总输出结果。"""
    all_files = [path for path in output_dir.rglob("*") if path.is_file()]
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
    table_markers = ("table", "tables")

    markdown_files = [path for path in all_files if path.suffix.lower() == ".md"]
    image_files = [path for path in all_files if path.suffix.lower() in image_exts]
    table_files = [
        path for path in all_files if any(marker in part.lower() for part in path.parts for marker in table_markers)
    ]

    return {
        "markdownCount": len(markdown_files),
        "imageCount": len(image_files),
        "tableAssetCount": len(table_files),
        "assetCount": max(0, len(all_files) - len(markdown_files)),
    }


__all__ = [
    "MinerUOutputPublishError",
    "MinerURequest",
    "materialize_mineru_result",
    "mineru_processing",
]
