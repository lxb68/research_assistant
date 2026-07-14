"""选择 MinerU SDK 或命令行工具，将 PDF 转换为 Markdown 资源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess

from pydantic import BaseModel, Field

from app.core.config import settings

try:
    from mineru import MinerU
    from mineru.exceptions import MinerUError, NoAuthClientError, TimeoutError
except ImportError:  # 可选依赖：只有无法使用命令行降级方案时才必须安装。
    MinerU = None

    class MinerUError(Exception):
        pass

    class NoAuthClientError(Exception):
        pass

    class TimeoutError(Exception):
        pass


class MinerURequest(BaseModel):
    record_id: str | None = Field(None, description="已保存的论文记录 ID")
    project_id: str | None = Field(None, description="兼容旧接口的项目 ID")
    file_name: str | None = Field(None, description="兼容旧接口的 PDF 文件名")
    pdf_path: str | None = Field(None, description="PDF 绝对路径或 storage/papers 下的文件名")
    output_name: str | None = Field(None, description="可选的 MinerU 输出目录名")
    mineru_token: str | None = Field(None, description="可选的 MinerU API 令牌覆盖值")
    split_min_length: int | None = Field(None, ge=100, description="可选的最小分块长度")
    split_max_length: int | None = Field(None, ge=200, description="可选的最大分块长度")


@dataclass(slots=True)
class MinerUPaths:
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
    paths.output_dir.mkdir(parents=True, exist_ok=True)

    cli_error: str | None = None
    try:
        _run_local_mineru_cli(paths.pdf_path, paths.output_dir)
    except Exception as error:
        cli_error = str(error)

    if not _has_markdown_output(paths.output_dir):
        token = (mineru_token or settings.mineru_api_token).strip()
        if not token:
            reason = cli_error or "Local MinerU CLI is unavailable and MINERU_API_TOKEN is not configured"
            raise RuntimeError(f"MinerU conversion failed: {reason}")
        _run_mineru_sdk(paths.pdf_path, paths.output_dir, token)

    markdown_path = _select_primary_markdown(paths.output_dir)
    if markdown_path is None:
        raise RuntimeError("MinerU finished but no markdown output was found")

    resource_summary = _summarize_output(paths.output_dir)
    source_pdf_copy = paths.output_dir / paths.pdf_path.name
    if not source_pdf_copy.exists():
        shutil.copy2(paths.pdf_path, source_pdf_copy)

    return {
        "success": True,
        "pdfPath": str(paths.pdf_path),
        "outputDir": str(paths.output_dir),
        "markdownPath": str(markdown_path),
        "sourcePdfPath": str(source_pdf_copy),
        **resource_summary,
    }


def _resolve_paths(
    *,
    project_id: str | None,
    file_name: str | None,
    pdf_path: str | None,
    output_name: str | None,
) -> MinerUPaths:
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
    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name.strip())
    normalized = normalized.strip("._") or "mineru_output"
    return normalized[:120]


def _run_local_mineru_cli(pdf_path: Path, output_dir: Path) -> None:
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


def _run_mineru_sdk(pdf_path: Path, output_dir: Path, token: str) -> None:
    if MinerU is None:
        raise RuntimeError("MinerU Python package is not installed")

    try:
        with MinerU(token=token) as client:
            result = client.extract(
                str(pdf_path),
                model="vlm",
                ocr=True,
                formula=True,
                table=True,
                timeout=1800,
            )
    except NoAuthClientError as error:
        raise RuntimeError(f"MinerU SDK authentication is not configured: {error}") from error
    except TimeoutError as error:
        raise RuntimeError(f"MinerU SDK timed out while waiting for conversion: {error}") from error
    except MinerUError as error:
        raise RuntimeError(f"MinerU SDK request failed: {error}") from error
    except Exception as error:
        raise RuntimeError(f"MinerU SDK crashed: {error}") from error

    if result.state != "done":
        message = result.error or result.err_code or result.state
        raise RuntimeError(f"MinerU SDK did not finish successfully: {message}")
    if not result.markdown:
        raise RuntimeError("MinerU SDK finished but returned no markdown content")

    try:
        result.save_all(str(output_dir))
    except Exception as error:
        raise RuntimeError(f"MinerU SDK succeeded but failed to save output files: {error}") from error


def _has_markdown_output(output_dir: Path) -> bool:
    return any(path.is_file() and path.suffix.lower() == ".md" for path in output_dir.rglob("*.md"))


def _select_primary_markdown(output_dir: Path) -> Path | None:
    markdown_files = sorted(path for path in output_dir.rglob("*.md") if path.is_file())
    if not markdown_files:
        return None
    return min(markdown_files, key=lambda path: (len(path.parts), len(path.name)))


def _summarize_output(output_dir: Path) -> dict:
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


__all__ = ["MinerURequest", "mineru_processing"]
