from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


def parse_dt(value: str | None) -> datetime | None:
    """解析资料包使用的本地时间；空单元格返回 None。"""
    if value is None or value == "":
        return None
    text = value.strip()
    if not text:
        return None
    return datetime.fromisoformat(text)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None


def max_timestamp(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> datetime | None:
    stamps = [parse_dt(row.get(field, "")) for row in rows for field in fields]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None
