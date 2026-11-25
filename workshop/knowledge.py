"""从中文工艺原句中提取作用域、工序、许可和会改变行动的歧义。

这里不试图做通用中文 NLP；解析器只识别资料中工艺语句使用的稳定业务句式，
遇到无法唯一确定且会改变现场动作的指代时生成 :class:`Ambiguity`，交给工艺员
确认。来源原文始终保留，解析结果只作为索引。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from pack_check import DEFAULT_DATA, read_tables

from .timeutil import parse_dt

STATIONS = ("北线", "南线", "东线", "西线")
MATERIAL_WORDS = ("甲", "乙", "丙", "丁")
EDGE_PART_RE = re.compile(r"(外缘|边缘|靠边|侧边|边部)")
TARGET_RE = re.compile(r"\b([A-Za-z]+\d+)\b")
FAMILY_RE = re.compile(r"([A-Z]\d+)")


@dataclass(frozen=True)
class Scope:
    station: str | None = None
    family: str | None = None
    process: str | None = None
    all_order: bool = False
    title: str = ""

    def matches(self, ticket: dict[str, str]) -> bool:
        if self.all_order:
            return True
        if self.station and ticket.get("station") != self.station:
            return False
        assembly = ticket.get("assembly", "")
        if self.family:
            if assembly != self.family and not assembly.startswith(self.family + "-"):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "family": self.family,
            "process": self.process,
            "all_order": self.all_order,
            "title": self.title,
        }


@dataclass
class Option:
    key: str
    label: str
    piece_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "piece_ids": self.piece_ids}


@dataclass
class Ambiguity:
    id: str
    source_id: str
    source_revision: str
    kind: str
    question: str
    options: list[Option]
    # 明确写出、不因歧义而失效的对象
    explicit_piece_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "kind": self.kind,
            "question": self.question,
            "options": [option.to_dict() for option in self.options],
            "explicit_piece_ids": self.explicit_piece_ids,
        }


@dataclass
class Sentence:
    uid: str
    source_id: str
    revision: str
    index: int
    raw: str
    scope: Scope
    tags: set[str] = field(default_factory=set)
    materials: list[str] = field(default_factory=list)
    pass_no: int | None = None
    coat_count: int | None = None
    deadline: datetime | None = None
    object_names: list[str] = field(default_factory=list)
    descriptors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "source_id": self.source_id,
            "revision": self.revision,
            "index": self.index,
            "raw": self.raw,
            "scope": self.scope.to_dict(),
            "tags": sorted(self.tags),
            "materials": self.materials,
            "pass_no": self.pass_no,
            "coat_count": self.coat_count,
            "deadline": self.deadline.isoformat(timespec="minutes") if self.deadline else None,
            "object_names": self.object_names,
            "descriptors": self.descriptors,
        }


def split_sentences(text: str) -> list[str]:
    # 工艺句常在前半句规定对象、后半句规定例外；按逗号/分号拆成可定位条款，
    # 但来源登记仍保留整行原文。
    parts = re.split(r"(?<=[。；;！？!?，,])", text)
    return [part.strip() for part in parts if part and part.strip()]


def _unique_materials(text: str) -> list[str]:
    found: list[str] = []
    for material in MATERIAL_WORDS:
        # 单字材料代号在合成资料中以“甲料/甲，/甲底涂”等形式出现。
        if material in text and material not in found:
            found.append(material)
    return found


def _pass_no(text: str) -> int | None:
    numbers: list[int] = []
    if "首遍" in text:
        numbers.append(1)
    if "次遍" in text:
        numbers.append(2)
    table = {"一": 1, "二": 2, "三": 3, "四": 4}
    for token, value in table.items():
        if f"第{token}遍" in text:
            numbers.append(value)
    for digit in re.findall(r"第([1234])遍", text):
        numbers.append(int(digit))
    if "双遍" in text or "两遍" in text:
        numbers.append(2)
    return max(numbers) if numbers else None


def _coat_count(text: str) -> int | None:
    if "双遍" in text or "两遍" in text:
        return 2
    match = re.search(r"([一二三四1234])遍", text)
    if match:
        token = match.group(1)
        table = {"一": 1, "二": 2, "三": 3, "四": 4}
        return table.get(token, int(token) if token.isdigit() else None)
    return None


def _deadline(text: str, effective: datetime | None) -> datetime | None:
    if not effective:
        return None
    if "今天午前" in text or "今日午前" in text:
        return effective.replace(hour=12, minute=0, second=0, microsecond=0)
    if "明天午前" in text or "次日午前" in text:
        return effective.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    match = re.search(r"今天\s*(\d{1,2})[:：](\d{2})", text)
    if match:
        return effective.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    match = re.search(r"(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2})", text)
    if match:
        return parse_dt(match.group(1))
    return None


class KnowledgeBase:
    def __init__(self, tables: dict[str, list[dict[str, str]]]):
        self.tables = tables
        self.cards = tables.get("cards.tsv", [])
        self.tickets = tables.get("tickets.tsv", [])
        self.rules = tables.get("assembly_rules.tsv", [])
        self.cards_by_id = {row["id"]: row for row in self.cards}
        self.tickets_by_id = {row["id"]: row for row in self.tickets}
        self.rules_by_id = {row["id"]: row for row in self.rules}
        self.scopes: dict[str, Scope] = {}
        self.sentences: list[Sentence] = []
        self.sentences_by_source: dict[str, list[Sentence]] = {}
        self.ambiguities: list[Ambiguity] = []
        self._parse()

    @classmethod
    def from_directory(cls, directory: Path = DEFAULT_DATA) -> "KnowledgeBase":
        return cls(read_tables(Path(directory)))

    def scope_for_card(self, row: dict[str, str]) -> Scope:
        if row["id"] in self.scopes:
            return self.scopes[row["id"]]

        chain: list[dict[str, str]] = []
        current = row
        seen: set[str] = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(current)
            parent_id = current.get("parent", "")
            if not parent_id:
                break
            current = self.cards_by_id.get(parent_id, {})

        root = next((item for item in reversed(chain) if not item.get("parent")), chain[-1])
        title = root.get("text", "")
        context = " ".join(item.get("text", "") for item in chain)
        station = next((word for word in STATIONS if word in title or word in context), None)
        family_match = FAMILY_RE.search(title) or FAMILY_RE.search(context)
        family = family_match.group(1) if family_match else None
        process = None
        process_match = re.search(r"构件\s*([^，,。；;]+)", title)
        if process_match:
            process = process_match.group(1).strip()
        scope = Scope(
            station=station,
            family=family,
            process=process,
            all_order="全单" in context,
            title=title,
        )
        self.scopes[row["id"]] = scope
        return scope

    def _known_part_names(self) -> list[str]:
        names = [ticket["part"] for ticket in self.tickets if ticket.get("part")]
        return sorted(dict.fromkeys(names), key=len, reverse=True)

    def _parse_sentence(
        self,
        *,
        uid: str,
        source_id: str,
        revision: str,
        index: int,
        text: str,
        scope: Scope,
        effective: datetime | None,
    ) -> Sentence:
        tags: set[str] = set()
        if "底涂" in text or "底漆" in text:
            tags.add("primer")
        if "改" in text and any(word in text for word in MATERIAL_WORDS):
            tags.add("material_change")
        if "旧料" in text:
            tags.add("old_material")
        if "已完成" in text and "沿用" in text:
            tags.add("transition_completed")
        if "领料未涂" in text or ("领料" in text and "未涂" in text):
            tags.add("transition_picked_uncoated")
        if "按新版" in text or "新版" in text:
            tags.add("new_version")
        if "膜厚" in text:
            tags.update({"inspection", "film_thickness"})
        if "实测" in text or "逐件测" in text or "每件测" in text:
            tags.update({"inspection", "actual_measurement"})
        if "测点" in text:
            tags.update({"inspection", "fixed_points"})
        if "合流试配" in text or "试配" in text:
            tags.update({"inspection", "trial_fit"})
        if "合流前" in text:
            tags.add("before_handoff")
        if "合流" in text:
            tags.add("handoff")
        if "每件" in text or "逐件" in text or "所有必需件" in text or "全部必需件" in text:
            tags.add("every_piece")
        if re.search(r"不可|不能|不得|不准|禁止", text):
            tags.add("prohibition")
        if re.search(r"(?<!不)可(?:以)?", text):
            tags.add("permission")
        if re.search(r"必须|须|应|要", text):
            tags.add("requirement")
        if re.search(r"除外|例外|但|改做|可随", text):
            tags.add("exception")
        if "返工" in text:
            tags.add("rework")
        if "复检" in text or "重新检验" in text:
            tags.add("reinspection")
        if "先涂后测" in text:
            tags.update({"coat_before_measure", "sequence"})
        if "限" in text and ("午前" in text or "前" in text):
            tags.add("deadline")

        object_names = [name for name in self._known_part_names() if name and name in text]
        descriptors = [token for token in ("靠边的", "靠边件", "边缘件") if token in text]
        if EDGE_PART_RE.search(text) and not descriptors:
            # 规则句中的“外缘/边缘”通常是明确对象名，已能在 parts 中匹配；
            # 只有“靠边的”这类指代需要额外歧义处理。
            pass

        return Sentence(
            uid=uid,
            source_id=source_id,
            revision=revision,
            index=index,
            raw=text,
            scope=scope,
            tags=tags,
            materials=_unique_materials(text),
            pass_no=_pass_no(text),
            coat_count=_coat_count(text),
            deadline=_deadline(text, effective),
            object_names=object_names,
            descriptors=descriptors,
        )

    def _parse(self) -> None:
        for card in self.cards:
            effective = parse_dt(card.get("effective"))
            scope = self.scope_for_card(card)
            sentences: list[Sentence] = []
            for index, text in enumerate(split_sentences(card["text"])):
                sentence = self._parse_sentence(
                    uid=f"{card['id']}:s{index + 1}",
                    source_id=card["id"],
                    revision=card.get("card", ""),
                    index=index + 1,
                    text=text,
                    scope=scope,
                    effective=effective,
                )
                sentences.append(sentence)
                self.sentences.append(sentence)
            self.sentences_by_source[card["id"]] = sentences

        for rule in self.rules:
            scope = Scope(station=rule.get("station"), family=rule.get("assembly_family"), process="交接条件")
            self.scopes[rule["id"]] = scope
            effective = None
            sentences = []
            for index, text in enumerate(split_sentences(rule["handoff"])):
                sentence = self._parse_sentence(
                    uid=f"{rule['id']}:s{index + 1}",
                    source_id=rule["id"],
                    revision=rule["id"],
                    index=index + 1,
                    text=text,
                    scope=scope,
                    effective=effective,
                )
                sentences.append(sentence)
                self.sentences.append(sentence)
            self.sentences_by_source[rule["id"]] = sentences

        self._build_ambiguities()

    def _scope_tickets(self, scope: Scope) -> list[dict[str, str]]:
        return [ticket for ticket in self.tickets if scope.matches(ticket)]

    def _build_ambiguities(self) -> None:
        for sentence in self.sentences:
            if "靠边的" not in sentence.descriptors:
                continue
            in_scope = self._scope_tickets(sentence.scope)
            explicit = [ticket["id"] for ticket in in_scope if ticket["part"] in sentence.object_names]
            # “北侧弧段……靠边的也可”中的明确对象来自同一来源的前一句。
            source_sentences = self.sentences_by_source[sentence.source_id]
            for other in source_sentences:
                if other is sentence:
                    continue
                for name in other.object_names:
                    for ticket in in_scope:
                        if ticket["part"] == name and ticket["id"] not in explicit:
                            explicit.append(ticket["id"])

            edge_candidates = [
                ticket["id"]
                for ticket in in_scope
                if EDGE_PART_RE.search(ticket.get("part", "")) and ticket["id"] not in explicit
            ]
            all_named = explicit + edge_candidates
            question = (
                f"{sentence.source_id} 原句“{sentence.raw}”中的指代可能只指明确写出的"
                f"对象，也可能扩大到靠边/外缘件 {('、'.join(edge_candidates) or '无候选')}；"
                "两种读法会改变旧料许可的适用物件。"
            )
            self.ambiguities.append(
                Ambiguity(
                    id=f"AMB-{sentence.source_id}-edge-scope",
                    source_id=sentence.source_id,
                    source_revision=sentence.revision,
                    kind="object_scope",
                    question=question,
                    explicit_piece_ids=explicit,
                    options=[
                        Option(
                            key="named_only",
                            label="仅适用于原条明确点到的区段/物件，不扩大到其他外缘或靠边分件",
                            piece_ids=explicit,
                        ),
                        Option(
                            key="include_edge_descriptors",
                            label="还适用于“靠边的”所指的外缘/靠边分件",
                            piece_ids=all_named,
                        ),
                    ],
                )
            )

    def source(self, source_id: str) -> dict[str, str]:
        if source_id in self.cards_by_id:
            return self.cards_by_id[source_id]
        if source_id in self.rules_by_id:
            return self.rules_by_id[source_id]
        raise KeyError(f"未知来源记录: {source_id}")

    def source_revision(self, source_id: str) -> str:
        row = self.source(source_id)
        return row.get("card") or row.get("id", "")

    def applicable_sentences(
        self,
        ticket: dict[str, str],
        *,
        at: datetime | None = None,
        include_rules: bool = True,
    ) -> list[Sentence]:
        result = []
        for sentence in self.sentences:
            if not sentence.scope.matches(ticket):
                continue
            source_id = sentence.source_id
            if source_id in self.cards_by_id:
                effective = parse_dt(self.cards_by_id[source_id].get("effective"))
                if at and effective and effective > at:
                    continue
            elif not include_rules:
                continue
            result.append(sentence)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "scopes": {key: value.to_dict() for key, value in self.scopes.items()},
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "ambiguities": [item.to_dict() for item in self.ambiguities],
        }


def load_knowledge(directory: Path = DEFAULT_DATA) -> KnowledgeBase:
    return KnowledgeBase.from_directory(Path(directory))
