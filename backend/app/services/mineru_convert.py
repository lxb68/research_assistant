from __future__ import annotations

import argparse
import json

from app.services.mineru import mineru_processing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use MinerU to convert a PDF into Markdown plus image/table assets under backend/storage/markdown.",
    )
    parser.add_argument("--pdf-path", help="Absolute PDF path or filename under backend/storage/papers.")
    parser.add_argument("--file-name", help="PDF filename under backend/storage/papers for compatibility.")
    parser.add_argument("--project-id", help="Optional legacy project id.")
    parser.add_argument("--output-name", help="Optional output directory name under backend/storage/markdown.")
    parser.add_argument("--mineru-token", help="Optional MinerU API token override.")
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
