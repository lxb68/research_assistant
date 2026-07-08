import os
import json
import time
import zipfile
import io
from pathlib import Path

import requests
from pydantic import BaseModel

# -------------------- 常量 --------------------
MINERU_API_BASE = "https://mineru.net/api/v4"
POLL_INTERVAL = 3        # 秒
MAX_POLL_ATTEMPTS = 90
PROCESSING_STATES = {
    "DONE": "done",
    "FAILED": "failed"
}

# -------------------- 请求模型 --------------------
class MinerURequest(BaseModel):
    project_id: str
    file_name: str
    # 可选：若 token 不从配置读取，也可直接传入
    # mineru_token: Optional[str] = None

# -------------------- 辅助函数 --------------------
def make_http_request(url: str, method: str = "GET", headers: dict = None, body: dict = None) -> dict:
    """发送 HTTP 请求，返回 JSON 响应"""
    headers = headers or {}
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method.upper() == "POST":
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"HTTP request failed: {e}") from e

def upload_file(file_path: Path, upload_url: str) -> None:
    """使用 PUT 将文件上传至指定 URL"""
    with open(file_path, "rb") as f:
        # 注意：某些服务需要指定 Content-Type，此处简单处理
        headers = {"Content-Type": "application/octet-stream"}
        resp = requests.put(upload_url, data=f, headers=headers, timeout=120)
        resp.raise_for_status()
        # 有些服务返回 200，有些返回 201，只要 2xx 即可
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Upload failed with status {resp.status_code}: {resp.text}")

def download_and_extract_zip(zip_url: str, target_dir: Path, file_name: str) -> None:
    """下载 ZIP，解压其中的 .md 文件并保存"""
    # 下载 ZIP
    resp = requests.get(zip_url, timeout=60)
    resp.raise_for_status()
    zip_data = io.BytesIO(resp.content)

    # 解压
    with zipfile.ZipFile(zip_data, "r") as zf:
        # 只提取 .md 文件（不区分大小写）
        md_files = [name for name in zf.namelist() if name.lower().endswith(".md")]
        if not md_files:
            raise RuntimeError("No .md file found in the downloaded zip.")
        # 只取第一个，或可遍历处理，这里按原逻辑只处理第一个
        for md_name in md_files:
            content = zf.read(md_name).decode("utf-8", errors="replace")
            # 输出文件名：将 .pdf 替换为 .md
            output_name = Path(file_name).stem + ".md"
            output_path = target_dir / output_name
            output_path.write_text(content, encoding="utf-8")
            print(f"Extracted to: {output_path}")
            # 原逻辑只处理一个，若需多个可调整
            break  # 仅处理第一个

# -------------------- 核心业务 --------------------
def mineru_processing(project_id: str, file_name: str, update_task_callback=None, task_info: dict = None) -> dict:
    """
    核心处理函数
    - project_id: 项目 ID
    - file_name: PDF 文件名（包含扩展名）
    - update_task_callback: 可选回调，用于更新任务进度（如写入数据库）
    - task_info: 任务相关信息，包含 processedPage 等
    返回成功标志
    """
    print("Executing PDF MinerU conversion strategy...")

    # 1. 获取项目根目录（此处模拟，实际可能从配置或数据库读取）
    #    建议将项目根路径作为环境变量或配置传入
    project_root = Path(os.getenv("PROJECT_ROOT", "./data"))
    project_path = project_root / project_id
    file_path = project_path / "files" / file_name

    # 2. 读取任务配置（包含 minerU token）
    task_config_path = project_path / "task-config.json"
    if not task_config_path.exists():
        raise RuntimeError("Task config not found, please check mineru token configuration.")

    with open(task_config_path, "r", encoding="utf-8") as f:
        task_config = json.load(f)

    token = task_config.get("minerUToken")
    if not token:
        raise RuntimeError("MinerU token missing in task configuration.")

    # 3. 获取上传 URL
    print("Getting upload URL...")
    request_payload = {
        "enable_formula": True,
        "layout_model": "doclayout_yolo",
        "enable_table": True,
        "files": [{"name": file_name, "is_ocr": True, "data_id": "abcd"}]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    url_response = make_http_request(
        f"{MINERU_API_BASE}/file-urls/batch",
        method="POST",
        headers=headers,
        body=request_payload
    )

    if url_response.get("code") != 0:
        raise RuntimeError(f"Failed to get upload URL: {url_response}")

    file_urls = url_response.get("data", {}).get("file_urls", [])
    if not file_urls:
        raise RuntimeError("No file URL returned.")
    upload_url = file_urls[0]
    batch_id = url_response["data"].get("batch_id")
    if not batch_id:
        raise RuntimeError("No batch_id returned.")

    # 4. 上传文件
    print("Uploading file...")
    upload_file(file_path, upload_url)
    print("File uploaded.")

    # 5. 轮询结果
    print("Polling for results...")
    current_page = 0
    total_page = 0
    completed_count = 0  # 若需要累加，可从 task_info 获取

    # 轮询次数计数（以防无限循环）
    attempts = 0
    while True:
        attempts += 1
        if attempts > MAX_POLL_ATTEMPTS:
            raise RuntimeError("Polling timeout exceeded.")

        # 查询进度
        result_response = make_http_request(
            f"{MINERU_API_BASE}/extract-results/batch/{batch_id}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"}
        )

        result_data = result_response.get("data", {})
        extract_results = result_data.get("extract_result", [])
        if not extract_results:
            time.sleep(POLL_INTERVAL)
            continue

        first_result = extract_results[0]
        state = first_result.get("state")
        extract_progress = first_result.get("extract_progress", {})

        if extract_progress:
            current_page = extract_progress.get("extracted_pages", 0)
            total_page = extract_progress.get("total_pages", 1)
        else:
            # 若没有进度信息，则视为已完成
            current_page = total_page

        # 更新进度回调（如果提供）
        if update_task_callback and task_info is not None:
            task_info["processedPage"] = current_page
            task_info["stepInfo"] = f"Processing {file_name} {current_page}/{total_page} pages progress: {(current_page/total_page)*100:.1f}%"
            # 假设 update_task_callback 接受 task_id 和更新数据
            # 此处可根据实际调整
            # update_task_callback(task_id, {"completedCount": current_page + completed_count, "detail": json.dumps(task_info)})

        print(f"MinerU {file_name} progress: {current_page}/{total_page}, state: {state}")

        # 完成
        if state == PROCESSING_STATES["DONE"]:
            zip_url = first_result.get("full_zip_url")
            if not zip_url:
                raise RuntimeError("No zip URL in completed result.")
            save_path = project_path / "files"
            save_path.mkdir(parents=True, exist_ok=True)
            download_and_extract_zip(zip_url, save_path, file_name)
            break

        # 失败
        if state == PROCESSING_STATES["FAILED"]:
            raise RuntimeError(f"Task processing failed: {result_response}")

        # 等待下一次轮询
        time.sleep(POLL_INTERVAL)

    print("MinerU conversion completed.")
    return {"success": True}
