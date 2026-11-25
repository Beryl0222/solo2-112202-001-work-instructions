"""车间作业许可与在制件后续作业单引擎。"""

from pathlib import Path

from pack_check import DEFAULT_DATA

from .engine import Workshop, load_workshop
from .knowledge import KnowledgeBase, load_knowledge

__all__ = [
    "Workshop",
    "load_workshop",
    "KnowledgeBase",
    "load_knowledge",
    "build_board",
    "quality_trace",
    "handoff_check",
]


def build_board(at: str | None = None, directory: Path = DEFAULT_DATA) -> dict:
    """读取资料包并返回班长按构件组织的作业板。"""
    return load_workshop(directory).board(at)


def quality_trace(assembly: str, at: str | None = None, directory: Path = DEFAULT_DATA) -> dict:
    """从构件回溯物件工序记录和当时有效的工艺原句。"""
    return load_workshop(directory).quality_trace(assembly, at)


def handoff_check(assembly: str, at: str | None = None, directory: Path = DEFAULT_DATA) -> dict:
    """检查合流所需件覆盖、完成状态和检验冲突最小范围。"""
    return load_workshop(directory).handoff_check(assembly, at)
