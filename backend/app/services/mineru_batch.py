"""MinerU 云端批量上传、轮询与结果落盘协调。"""

from __future__ import annotations

import hashlib
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from app.core.config import settings
from app.services.document_parse_repository import DocumentParseRepository
from app.services.mineru import materialize_mineru_result
from app.services.task_control import TaskCancelled, raise_if_task_cancelled


RETRYABLE_CODES = {-10001, -60001, -60007, -60009, -60010}
ACTIVE_REMOTE_STATES = {"uploaded", "waiting-file", "pending", "running", "converting"}


class MinerUBatchError(RuntimeError):
    """携带可重试语义的 MinerU 批量错误。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class MinerUBatchInput:
    source_id: str
    source_item_key: str
    attachment_key: str
    file_hash: str
    pdf_path: Path
    output_name: str
    data_id: str
    parse_key: str


@dataclass
class MinerUBatchOutcome:
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


ProgressCallback = Callable[[int, int, str], None]


def build_parse_key(file_hash: str) -> str:
    """将文件内容与会影响解析结果的配置合成为幂等键。"""
    signature = ":".join(
        [file_hash, settings.mineru_model_version, "ocr=1", "formula=1", "table=1"],
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


class MinerUCloudBatchClient:
    """只封装 MinerU v4 批量 HTTP 协议。"""

    def __init__(self, *, token: str, api_base: str | None = None) -> None:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise ValueError("未配置 MinerU API Token")
        self.token = normalized_token
        self.api_base = str(api_base or settings.mineru_api_base).rstrip("/")
        self.timeout = settings.mineru_request_timeout_seconds

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def create_batch(self, inputs: list[MinerUBatchInput]) -> tuple[str, list[str]]:
        try:
            response = requests.post(
                f"{self.api_base}/file-urls/batch",
                headers={**self.headers, "Content-Type": "application/json"},
                json={
                    "files": [
                        {
                            "name": f"zotero_{item.attachment_key}.pdf",
                            "data_id": item.data_id,
                            "is_ocr": True,
                        }
                        for item in inputs
                    ],
                    "model_version": settings.mineru_model_version,
                    "enable_formula": True,
                    "enable_table": True,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise MinerUBatchError(f"申请 MinerU 上传地址失败：{error}", retryable=True) from error
        payload = self._payload(response, "申请上传地址")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        batch_id = str(data.get("batch_id") or "").strip()
        urls = data.get("file_urls") if isinstance(data.get("file_urls"), list) else []
        if not batch_id or len(urls) != len(inputs):
            raise MinerUBatchError("MinerU 返回的 batch_id 或上传地址数量无效", retryable=True)
        return batch_id, [str(url) for url in urls]

    def upload(self, upload_url: str, pdf_path: Path) -> None:
        try:
            with pdf_path.open("rb") as pdf_file:
                response = requests.put(
                    upload_url,
                    data=pdf_file,
                    timeout=max(self.timeout, 300),
                )
        except (OSError, requests.RequestException) as error:
            raise MinerUBatchError(f"上传 PDF 失败：{error}", retryable=True) from error
        if not 200 <= response.status_code < 300:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise MinerUBatchError(
                f"上传 PDF 失败：HTTP {response.status_code} {(response.text or '')[:300]}",
                retryable=retryable,
            )

    def get_results(self, batch_id: str) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.api_base}/extract-results/batch/{batch_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise MinerUBatchError(f"查询 MinerU 批次失败：{error}", retryable=True) from error
        payload = self._payload(response, "查询批次")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        values = data.get("extract_result") or data.get("extract_results") or []
        return [value for value in values if isinstance(value, dict)]

    @staticmethod
    def _payload(response: requests.Response, action: str) -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise MinerUBatchError(
                f"MinerU {action}失败：HTTP {response.status_code} {(response.text or '')[:300]}",
                retryable=retryable,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise MinerUBatchError(f"MinerU {action}返回无效 JSON", retryable=True) from error
        if not isinstance(payload, dict):
            raise MinerUBatchError(f"MinerU {action}返回格式无效", retryable=True)
        code = payload.get("code")
        if code != 0:
            raise MinerUBatchError(
                f"MinerU {action}失败：{payload.get('msg') or code}",
                retryable=code in RETRYABLE_CODES,
            )
        return payload


class MinerUBatchCoordinator:
    """在单个业务任务内部执行有界并发，并持久化每篇文档的状态。"""

    def __init__(
        self,
        *,
        repository: DocumentParseRepository,
        client: MinerUCloudBatchClient,
        materializer: Callable[..., dict[str, Any]] = materialize_mineru_result,
    ) -> None:
        self.repository = repository
        self.client = client
        self.materializer = materializer

    def process(
        self,
        inputs: list[MinerUBatchInput],
        *,
        cancel_event=None,
        progress_callback: ProgressCallback | None = None,
    ) -> MinerUBatchOutcome:
        outcome = MinerUBatchOutcome()
        pending: list[MinerUBatchInput] = []
        resumable: dict[str, list[MinerUBatchInput]] = {}
        for item in inputs:
            task = self.repository.create_or_get(
                source_id=item.source_id,
                source_item_key=item.source_item_key,
                attachment_key=item.attachment_key,
                file_hash=item.file_hash,
                parse_key=item.parse_key,
                data_id=item.data_id,
            )
            markdown_path = Path(str(task.get("markdown_path") or ""))
            if task.get("status") == "completed" and markdown_path.is_file():
                outcome.results[item.data_id] = self._stored_result(task, item)
                continue
            batch_id = str(task.get("provider_batch_id") or "")
            if batch_id and str(task.get("status") or "") in ACTIVE_REMOTE_STATES:
                resumable.setdefault(batch_id, []).append(item)
            else:
                pending.append(item)

        for batch_id, batch_inputs in resumable.items():
            self._poll_and_materialize(
                batch_id, batch_inputs, outcome,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                total=len(inputs),
            )

        batch_size = min(200, max(1, settings.mineru_batch_size))
        for chunk in self._chunks(pending, batch_size):
            raise_if_task_cancelled(cancel_event)
            try:
                batch_id, upload_urls = self._retry(
                    lambda: self.client.create_batch(chunk),
                    cancel_event=cancel_event,
                )
            except TaskCancelled:
                raise
            except Exception as error:
                for item in chunk:
                    self._fail(item, error, outcome)
                continue

            for item in chunk:
                current = self.repository.get_by_parse_key(item.parse_key) or {}
                self.repository.update(
                    item.parse_key,
                    status="upload_url_ready",
                    provider_batch_id=batch_id,
                    provider_data_id=item.data_id,
                    attempts=int(current.get("attempts") or 0) + 1,
                    error_message="",
                )
            uploaded = self._upload_chunk(chunk, upload_urls, outcome, cancel_event=cancel_event)
            if uploaded:
                self._poll_and_materialize(
                    batch_id, uploaded, outcome,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    total=len(inputs),
                )

        if progress_callback:
            progress_callback(len(outcome.results), len(inputs), "批量解析处理完成")
        return outcome

    def _upload_chunk(
        self,
        inputs: list[MinerUBatchInput],
        urls: list[str],
        outcome: MinerUBatchOutcome,
        *,
        cancel_event=None,
    ) -> list[MinerUBatchInput]:
        uploaded: list[MinerUBatchInput] = []

        def upload(item: MinerUBatchInput, url: str) -> MinerUBatchInput:
            raise_if_task_cancelled(cancel_event)
            self.repository.update(item.parse_key, status="uploading")
            self._retry(lambda: self.client.upload(url, item.pdf_path), cancel_event=cancel_event)
            self.repository.update(item.parse_key, status="uploaded", error_message="")
            return item

        with ThreadPoolExecutor(max_workers=settings.mineru_upload_concurrency) as executor:
            futures = {
                executor.submit(upload, item, url): item
                for item, url in zip(inputs, urls, strict=True)
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    uploaded.append(future.result())
                except TaskCancelled:
                    raise
                except Exception as error:
                    self._fail(item, error, outcome)
        return uploaded

    def _poll_and_materialize(
        self,
        batch_id: str,
        inputs: list[MinerUBatchInput],
        outcome: MinerUBatchOutcome,
        *,
        cancel_event=None,
        progress_callback: ProgressCallback | None,
        total: int,
    ) -> None:
        remaining = {item.data_id: item for item in inputs}
        remote_data_ids = {
            str((self.repository.get_by_parse_key(item.parse_key) or {}).get("provider_data_id") or item.data_id): item
            for item in inputs
        }
        deadline = time.monotonic() + settings.mineru_cloud_timeout_seconds
        while remaining and time.monotonic() < deadline:
            raise_if_task_cancelled(cancel_event)
            try:
                results = self._retry(
                    lambda: self.client.get_results(batch_id),
                    cancel_event=cancel_event,
                )
            except TaskCancelled:
                raise
            except Exception as error:
                for item in remaining.values():
                    self.repository.update(item.parse_key, status="pending", error_message=str(error)[:2000])
                    outcome.errors[item.data_id] = str(error)
                return

            completed: list[tuple[MinerUBatchInput, str]] = []
            for result in results:
                data_id = str(result.get("data_id") or "")
                item = remaining.get(data_id) or remote_data_ids.get(data_id)
                if item is not None and item.data_id not in remaining:
                    item = None
                if item is None:
                    file_name = str(result.get("file_name") or "")
                    item = next(
                        (candidate for candidate in remaining.values() if file_name == f"zotero_{candidate.attachment_key}.pdf"),
                        None,
                    )
                if item is None:
                    continue
                state = str(result.get("state") or "pending").lower()
                if state == "done":
                    result_url = str(result.get("full_zip_url") or "").strip()
                    if result_url:
                        self.repository.update(
                            item.parse_key, status="downloading", result_url=result_url,
                        )
                        completed.append((item, result_url))
                    else:
                        self._fail(item, RuntimeError("MinerU 完成但未返回结果地址"), outcome)
                        remaining.pop(item.data_id, None)
                elif state == "failed":
                    self._fail(item, RuntimeError(str(result.get("err_msg") or "MinerU 解析失败")), outcome)
                    remaining.pop(item.data_id, None)
                else:
                    self.repository.update(item.parse_key, status=state, error_message="")

            if completed:
                self._materialize(completed, outcome, cancel_event=cancel_event)
                for item, _url in completed:
                    remaining.pop(item.data_id, None)
            if progress_callback:
                progress_callback(len(outcome.results), total, "正在等待 MinerU 批量解析")
            if remaining:
                delay = max(0.05, settings.mineru_poll_interval_seconds)
                if cancel_event is not None and cancel_event.wait(delay):
                    raise TaskCancelled("任务已取消")
                if cancel_event is None:
                    time.sleep(delay)

        # 超时不是永久失败，保留远端状态以便下次同步恢复。
        for item in remaining.values():
            self.repository.update(
                item.parse_key,
                status="pending",
                error_message="MinerU 批次轮询超时，等待下次同步恢复",
            )
            outcome.errors[item.data_id] = "MinerU 批次仍在处理中"

    def _materialize(
        self,
        completed: list[tuple[MinerUBatchInput, str]],
        outcome: MinerUBatchOutcome,
        *,
        cancel_event=None,
    ) -> None:
        def download(item: MinerUBatchInput, result_url: str) -> tuple[MinerUBatchInput, dict[str, Any]]:
            raise_if_task_cancelled(cancel_event)
            result = self._retry(
                lambda: self.materializer(
                    pdf_path=item.pdf_path,
                    output_name=item.output_name,
                    result_url=result_url,
                ),
                cancel_event=cancel_event,
            )
            return item, result

        with ThreadPoolExecutor(max_workers=settings.mineru_download_concurrency) as executor:
            futures = {
                executor.submit(download, item, url): item
                for item, url in completed
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    _item, result = future.result()
                    self.repository.update(
                        item.parse_key,
                        status="completed",
                        output_dir=str(result.get("outputDir") or ""),
                        markdown_path=str(result.get("markdownPath") or ""),
                        error_message="",
                    )
                    outcome.results[item.data_id] = result
                except TaskCancelled:
                    raise
                except Exception as error:
                    self._fail(item, error, outcome)

    def _retry(self, operation: Callable[[], Any], *, cancel_event=None) -> Any:
        attempts = settings.mineru_max_retries + 1
        for attempt in range(attempts):
            raise_if_task_cancelled(cancel_event)
            try:
                return operation()
            except MinerUBatchError as error:
                if not error.retryable or attempt + 1 >= attempts:
                    raise
            except requests.RequestException:
                if attempt + 1 >= attempts:
                    raise
            delay = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(0, 0.5)
            if cancel_event is not None and cancel_event.wait(delay):
                raise TaskCancelled("任务已取消")
            if cancel_event is None:
                time.sleep(delay)
        raise RuntimeError("MinerU 重试次数已耗尽")

    def _fail(self, item: MinerUBatchInput, error: Exception, outcome: MinerUBatchOutcome) -> None:
        message = str(error)[:2000]
        status = "retry_wait" if isinstance(error, MinerUBatchError) and error.retryable else "failed"
        self.repository.update(item.parse_key, status=status, error_message=message)
        outcome.errors[item.data_id] = message

    @staticmethod
    def _stored_result(task: dict[str, Any], item: MinerUBatchInput) -> dict[str, Any]:
        return {
            "success": True,
            "pdfPath": str(item.pdf_path),
            "outputDir": str(task.get("output_dir") or ""),
            "markdownPath": str(task.get("markdown_path") or ""),
            "sourcePdfPath": str(item.pdf_path),
            "reused": True,
        }

    @staticmethod
    def _chunks(values: list[MinerUBatchInput], size: int) -> Iterable[list[MinerUBatchInput]]:
        for index in range(0, len(values), size):
            yield values[index:index + size]


__all__ = [
    "MinerUBatchCoordinator",
    "MinerUBatchError",
    "MinerUBatchInput",
    "MinerUBatchOutcome",
    "MinerUCloudBatchClient",
    "build_parse_key",
]
