"""检查或修复 MinerU 成功后仍残留降级状态的历史论文记录。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.hunter_agent import HunterAgent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写入修复；默认仅检查")
    parser.add_argument("--limit", type=int, default=500, help="最多检查的最近记录数，最大 500")
    args = parser.parse_args()
    result = HunterAgent().repair_mineru_status_metadata(apply=args.apply, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
