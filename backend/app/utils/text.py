"""清理第三方接口返回的文本与简单 HTML。"""

import re


def clean_text(value: str | None) -> str:
    """清理接口返回里的多余空白和简单 HTML 标签。"""
    if not value:
        return ""

    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", without_tags).strip()
