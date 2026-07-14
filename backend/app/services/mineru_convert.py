"""提供可直接从命令行调用的 MinerU PDF 转换入口。"""

from __future__ import annotations

import argparse
import json

from app.services.mineru import mineru_processing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="使用 MinerU 将 PDF 转换为 Markdown，并把图片、表格资源保存到 backend/storage/markdown。",
    )
    parser.add_argument("--pdf-path", help="PDF 绝对路径或 backend/storage/papers 下的文件名。")
    parser.add_argument("--file-name", help="兼容旧接口的 backend/storage/papers 下 PDF 文件名。")
    parser.add_argument("--project-id", help="可选的旧版项目 ID。")
    parser.add_argument("--output-name", help="backend/storage/markdown 下可选的输出目录名。")
    parser.add_argument("--mineru-token", help="可选的 MinerU API 令牌覆盖值。")
    args = parser.parse_args()

    result = mineru_processing(
        project_id=args.project_id,
        file_name=args.file_name,
        pdf_path=args.pdf_path,
        output_name=args.output_name,
        mineru_token=args.mineru_token,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
