"""事件回放、作业单生成、工艺确认、质检追溯与合流校验。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from pack_check import DEFAULT_DATA, read_tables

from .knowledge import Ambiguity, KnowledgeBase, Scope, split_sentences
from .timeutil import iso, max_timestamp, parse_dt

STATUS_READY = "ready"
STATUS_CLAIMED = "claimed_unstarted"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_WAITING = "waiting_predecessor"
STATUS_AWAIT_CONFIRMATION = "awaiting_confirmation"
STATUS_ENGINEER = "engineer_disposition_required"
STATUS_NO_NEXT = "no_compatible_next_step"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "inspection_failed"
STATUS_PERMIT_UNAVAILABLE = "permit_unavailable"

STATUS_LABELS_ZH = {
    STATUS_READY: "可领单/可施工",
    STATUS_CLAIMED: "已领单未开工",
    STATUS_IN_PROGRESS: "已开工",
    STATUS_COMPLETED: "已完成",
    STATUS_WAITING: "等待前道完成",
    STATUS_AWAIT_CONFIRMATION: "待工艺员确认",
    STATUS_ENGINEER: "已开工，须报工艺员决定处置",
    STATUS_NO_NEXT: "找不到兼容下一步，暂停",
    STATUS_BLOCKED: "暂缓",
    STATUS_FAILED: "检验不合格，须返工/复检",
    STATUS_PERMIT_UNAVAILABLE: "许可已失效或被否决",
}

MATERIAL_CHANGE_RE = re.compile(r"底涂改\s*([甲乙丙丁])")
RETAIN_MATERIAL_RE = re.compile(r"沿用\s*([甲乙丙丁])")
MATERIAL_RE = re.compile(r"[甲乙丙丁]")
PASS_KEY_RE = {
    1: "primer_pass_1",
    2: "primer_pass_2",
    3: "primer_pass_3",
}


@dataclass
class OrderVersion:
    changed_at: str | None
    revision: int
    title: str
    status: str
    instruction: str
    material: str | None
    source_ids: list[str]
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRecord:
    id: str
    piece_id: str
    step_key: str
    versions: list[OrderVersion] = field(default_factory=list)
    receipt_event_ids: list[str] = field(default_factory=list)

    def signature(self, snapshot: dict[str, Any]) -> tuple[Any, ...]:
        return (
            snapshot.get("title"),
            snapshot.get("status"),
            snapshot.get("instruction"),
            snapshot.get("material"),
            tuple(snapshot.get("source_ids", [])),
            tuple(reason["code"] for reason in snapshot.get("reasons", [])),
        )

    def append_snapshot(self, snapshot: dict[str, Any], changed_at: datetime | None) -> bool:
        signature = self.signature(snapshot)
        if self.versions:
            last = self.versions[-1]
            last_signature = (
                last.title,
                last.status,
                last.instruction,
                last.material,
                tuple(last.source_ids),
                tuple(last.reason_codes),
            )
            if last_signature == signature:
                return False
            revision = last.revision + 1
        else:
            revision = 1
        self.versions.append(
            OrderVersion(
                changed_at=iso(changed_at),
                revision=revision,
                title=snapshot["title"],
                status=snapshot["status"],
                instruction=snapshot.get("instruction", snapshot["title"]),
                material=snapshot.get("material"),
                source_ids=list(snapshot.get("source_ids", [])),
                reason_codes=[reason["code"] for reason in snapshot.get("reasons", [])],
            )
        )
        return True

    def to_dict(self) -> dict[str, Any]:
        current = self.versions[-1].to_dict() if self.versions else None
        return {
            "id": self.id,
            "piece_id": self.piece_id,
            "step_key": self.step_key,
            "current": current,
            "versions": [version.to_dict() for version in self.versions],
            "receipt_event_ids": self.receipt_event_ids,
        }


def _step_key_from_text(text: str) -> str | None:
    if "试配" in text:
        return "trial_fit"
    if "膜厚" in text or "测厚" in text or "实测" in text:
        return "film_thickness"
    if "首遍" in text or "第一遍" in text:
        return "primer_pass_1"
    if "第二遍" in text:
        return "primer_pass_2"
    if "第三遍" in text:
        return "primer_pass_3"
    if "底涂" in text:
        return "primer_pass_1"
    return None


def _materials_text_order(text: str) -> list[str]:
    return MATERIAL_RE.findall(text)


class Workshop:
    """以资料表为事实来源、可叠加运行时上报和工艺决定的车间回放器。"""

    def __init__(self, tables: dict[str, list[dict[str, str]]], directory: Path | None = None):
        self.tables = tables
        self.directory = directory
        self.kb = KnowledgeBase(tables)
        self.tickets_by_id = self.kb.tickets_by_id
        self._runtime_events: list[dict[str, str]] = []
        self._runtime_seq = 0
        self.order_records: dict[str, OrderRecord] = {}

    @classmethod
    def load(cls, directory: Path = DEFAULT_DATA) -> "Workshop":
        path = Path(directory)
        return cls(read_tables(path), path)

    # ------------------------------------------------------------------ time
    def _resolve_at(self, at: str | datetime | None) -> datetime | None:
        if at is None:
            stamps = [
                max_timestamp(self.tables.get("cards.tsv", []), ["effective"]),
                max_timestamp(self.tables.get("events.tsv", []), ["at"]),
            ]
            stamps.extend(parse_dt(event.get("at")) for event in self._runtime_events)
            values = [stamp for stamp in stamps if stamp is not None]
            return max(values) if values else None
        if isinstance(at, datetime):
            return at
        if at == "now":
            return datetime.now()
        return parse_dt(at)

    def _events(self, at: datetime | None = None) -> list[dict[str, str]]:
        events = list(self.tables.get("events.tsv", [])) + list(self._runtime_events)
        if at is not None:
            events = [event for event in events if (parse_dt(event.get("at")) or at) <= at]
        return sorted(events, key=lambda event: (event.get("at", ""), event.get("id", "")))

    # ------------------------------------------------------------- decisions
    def ambiguities(self) -> list[Ambiguity]:
        return list(self.kb.ambiguities)

    def find_ambiguity(self, source_id: str) -> Ambiguity:
        matches = [item for item in self.kb.ambiguities if item.source_id == source_id]
        if not matches:
            raise KeyError(f"来源 {source_id} 没有待确认的解释歧义")
        return matches[0]

    def decision_book(self, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        states: dict[str, dict[str, Any]] = {
            item.id: {
                "ambiguity": item.to_dict(),
                "status": "open",
                "selected_option": None,
                "decision_event_ids": [],
                "source_revision": item.source_revision,
            }
            for item in self.kb.ambiguities
        }
        permits: dict[str, dict[str, Any]] = {}
        log: list[dict[str, Any]] = []

        for event in self._events(moment):
            if event.get("event") != "review":
                continue
            detail = event.get("detail", "")
            target_match = re.search(r"(?<![A-Za-z0-9])([A-Za-z]+\d+)", detail)
            target = target_match.group(1) if target_match else None
            if not target or (
                target not in self.kb.cards_by_id and target not in self.kb.rules_by_id
            ):
                continue
            revision = self.kb.source_revision(target)
            stated_revision_match = re.search(r"([A-Za-z]+-r\d+)", detail)
            stated_revision = stated_revision_match.group(1) if stated_revision_match else None
            revision_applies = stated_revision is None or stated_revision == revision
            item = {
                "event_id": event["id"],
                "at": event.get("at"),
                "target": target,
                "source_revision": revision,
                "stated_revision": stated_revision,
                "applies_to_source_revision": revision_applies,
                "detail": detail,
            }
            log.append(item)
            if not revision_applies:
                continue

            if "否决" in detail or "驳回" in detail:
                permits[target] = {
                    "status": "rejected",
                    "at": event.get("at"),
                    "event_id": event["id"],
                    "source_revision": revision,
                    "detail": detail,
                }
                continue

            ambiguity = next((amb for amb in self.kb.ambiguities if amb.source_id == target), None)
            if ambiguity is None:
                continue
            state = states[ambiguity.id]
            state["decision_event_ids"].append(event["id"])

            if "撤回" in detail or "撤销" in detail:
                state["status"] = "withdrawn"
                state["withdrawn_at"] = event.get("at")
                state["withdraw_event_id"] = event["id"]
                continue

            if "确认" in detail:
                option_key_match = re.search(r"option=([a-z_]+)", detail)
                option_key = option_key_match.group(1) if option_key_match else None
                if option_key is None:
                    if "仅指" in detail or "仅适用" in detail or "只指" in detail or "仅" in detail:
                        option_key = "named_only"
                    elif "扩大" in detail or "也适用" in detail or "还适用" in detail:
                        option_key = "include_edge_descriptors"
                valid_keys = {option.key for option in ambiguity.options}
                if option_key not in valid_keys:
                    raise ValueError(
                        f"{target} 的确认未唯一选择解释；可选: {', '.join(sorted(valid_keys))}"
                    )
                selected = next(option for option in ambiguity.options if option.key == option_key)
                state["status"] = "confirmed"
                state["selected_option"] = selected.to_dict()
                state["confirmed_at"] = event.get("at")
                state["confirm_event_id"] = event["id"]
                state["source_revision"] = revision

        return {"as_of": iso(moment), "ambiguities": states, "permits": permits, "log": log}

    def confirm_interpretation(
        self,
        source_id: str,
        interpretation: str | None = None,
        option_key: str | None = None,
        at: str | datetime | None = None,
    ) -> "Workshop":
        ambiguity = self.find_ambiguity(source_id)
        if option_key is None and interpretation:
            if "仅" in interpretation:
                option_key = "named_only"
            elif "扩大" in interpretation or "靠边" in interpretation:
                option_key = "include_edge_descriptors"
        if option_key not in {option.key for option in ambiguity.options}:
            raise ValueError("必须用 option_key 或可识别的 interpretation 唯一指定一条解释")
        moment = self._resolve_at(at) or datetime.now()
        self._append_runtime_event(
            piece_id=ambiguity.explicit_piece_ids[0] if ambiguity.explicit_piece_ids else self.kb.tickets[0]["id"],
            event="review",
            detail=f"确认 {source_id} option={option_key} {interpretation or ''}".strip(),
            at=moment,
        )
        return self

    def withdraw_confirmation(
        self, source_id: str, at: str | datetime | None = None
    ) -> "Workshop":
        self.find_ambiguity(source_id)
        moment = self._resolve_at(at) or datetime.now()
        ambiguity = self.find_ambiguity(source_id)
        piece_id = ambiguity.explicit_piece_ids[0] if ambiguity.explicit_piece_ids else self.kb.tickets[0]["id"]
        self._append_runtime_event(
            piece_id=piece_id,
            event="review",
            detail=f"撤回 {source_id} 的确认",
            at=moment,
        )
        return self

    def reject_permit(
        self, source_id: str, at: str | datetime | None = None, reason: str = ""
    ) -> "Workshop":
        self.kb.source(source_id)
        moment = self._resolve_at(at) or datetime.now()
        ticket_id = self.kb.tickets[0]["id"]
        self._append_runtime_event(
            piece_id=ticket_id,
            event="review",
            detail=f"否决 {source_id} {reason}".strip(),
            at=moment,
        )
        return self

    def record_event(
        self,
        piece_id: str,
        event: str,
        detail: str,
        at: str | datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, str]:
        if piece_id not in self.tickets_by_id:
            raise KeyError(f"未知物件: {piece_id}")
        moment = self._resolve_at(at) or datetime.now()
        return self._append_runtime_event(piece_id, event, detail, moment, event_id=event_id)

    def report_progress(
        self,
        piece_id: str,
        event: str,
        detail: str,
        at: str | datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, str]:
        """``record_event`` 的业务别名，供班组上报开工/完工/用料。"""
        return self.record_event(piece_id, event, detail, at=at, event_id=event_id)

    def _append_runtime_event(
        self,
        piece_id: str,
        event: str,
        detail: str,
        at: datetime,
        event_id: str | None = None,
    ) -> dict[str, str]:
        self._runtime_seq += 1
        row = {
            "id": event_id or f"R{self._runtime_seq:03d}",
            "piece": piece_id,
            "at": at.isoformat(timespec="minutes"),
            "event": event,
            "detail": detail,
            "_runtime": "1",
        }
        self._runtime_events.append(row)
        return row

    # ------------------------------------------------------------- progress
    def piece_events(self, piece_id: str, at: str | datetime | None = None) -> list[dict[str, str]]:
        moment = self._resolve_at(at)
        return [event for event in self._events(moment) if event.get("piece") == piece_id]

    def progress(self, piece_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        material_issues: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        starts: dict[str, dict[str, Any]] = {}
        completions: dict[str, dict[str, Any]] = {}
        inspections: dict[str, list[dict[str, Any]]] = {"film_thickness": [], "trial_fit": []}
        hold_receipts: list[dict[str, Any]] = []

        for event in self.piece_events(piece_id, moment):
            kind = event.get("event", "")
            detail = event.get("detail", "")
            item = {
                "event_id": event["id"],
                "at": event.get("at"),
                "event": kind,
                "detail": detail,
                "raw": event,
            }
            if "不合格" in detail or "未通过" in detail or "失败" in detail:
                item["passed"] = False
            elif "合格" in detail or "通过" in detail:
                item["passed"] = True
            else:
                item["passed"] = None
            if kind == "领料":
                materials = _materials_text_order(detail)
                item["materials"] = materials
                material_issues.append(item)
            elif kind == "领单":
                step_key = _step_key_from_text(detail)
                item["step_key"] = step_key
                claims.append(item)
                if step_key is None:
                    hold_receipts.append(item)
            elif kind in {"开工", "完工"}:
                step_key = _step_key_from_text(detail)
                item["step_key"] = step_key
                item["materials"] = _materials_text_order(detail)
                if kind == "开工" and step_key:
                    starts[step_key] = item
                if kind == "完工":
                    if step_key and step_key.startswith("primer_pass"):
                        completions[step_key] = item
                    elif "试配" in detail:
                        inspections["trial_fit"].append(item)
                    elif "膜厚" in detail or "测" in detail or "实测" in detail or step_key == "film_thickness":
                        inspections["film_thickness"].append(item)
                    elif step_key and step_key.startswith("primer"):
                        completions[step_key] = item
            elif kind in {"检验", "质检", "检查"}:
                if "试配" in detail:
                    inspections["trial_fit"].append(item)
                else:
                    inspections["film_thickness"].append(item)

        return {
            "material_issues": material_issues,
            "claims": claims,
            "starts": starts,
            "completions": completions,
            "inspections": inspections,
            "hold_receipts": hold_receipts,
        }

    # ------------------------------------------------------------ sources
    def _card_base(self, revision: str) -> str:
        return revision.split("-r", 1)[0] if "-r" in revision else revision

    def current_card_source_ids(self, ticket: dict[str, str], at: datetime | None) -> set[str]:
        matching_roots = []
        for card in self.kb.cards:
            if card.get("parent"):
                continue
            scope = self.kb.scope_for_card(card)
            if not scope.matches(ticket):
                continue
            effective = parse_dt(card.get("effective"))
            if at and effective and effective > at:
                continue
            matching_roots.append(card)
        if not matching_roots:
            return set()
        latest_effective = max(parse_dt(card["effective"]) or datetime.min for card in matching_roots)
        roots = [
            card
            for card in matching_roots
            if (parse_dt(card["effective"]) or datetime.min) == latest_effective
        ]
        root_ids = {card["id"] for card in roots}
        result = set(root_ids)
        changed = True
        children = [card for card in self.kb.cards if card.get("parent")]
        while changed:
            changed = False
            for card in children:
                if card["id"] in result:
                    continue
                if card.get("parent") in result:
                    effective = parse_dt(card.get("effective"))
                    if at and effective and effective > at:
                        continue
                    result.add(card["id"])
                    changed = True
        return result

    def _sentence_applies_to_piece(
        self, sentence, ticket: dict[str, str], moment: datetime | None, decisions: dict[str, Any]
    ) -> bool:
        if not sentence.scope.matches(ticket):
            return False
        if sentence.object_names and ticket.get("part") not in sentence.object_names:
            # 被工艺员确认扩大的指代可以落到候选件；未确认时不把例外句套用到候选件。
            for ambiguity in self.kb.ambiguities:
                if ambiguity.source_id != sentence.source_id:
                    continue
                state = decisions["ambiguities"].get(ambiguity.id)
                if state and state["status"] == "confirmed":
                    option = state.get("selected_option") or {}
                    if ticket["id"] in option.get("piece_ids", []):
                        return True
            return False

        # 单独拆出的“限……”时限小句继承同一许可条前半句的明确对象，
        # 不能因为小句本身没有对象就把旧料时限套到门板/连接件。
        if not sentence.object_names and sentence.raw.startswith("限") and "deadline" in sentence.tags:
            sibling_names = {
                name
                for sibling in self.kb.sentences_by_source.get(sentence.source_id, [])
                for name in sibling.object_names
            }
            if sibling_names and ticket.get("part") not in sibling_names:
                for ambiguity in self.kb.ambiguities:
                    if ambiguity.source_id != sentence.source_id:
                        continue
                    state = decisions["ambiguities"].get(ambiguity.id)
                    option = (state or {}).get("selected_option") or {}
                    if state and state.get("status") == "confirmed" and ticket["id"] in option.get("piece_ids", []):
                        return True
                return False
        return True

    def source_ids_for_piece(
        self, ticket: dict[str, str], at: datetime | None
    ) -> list[str]:
        moment = self._resolve_at(at)
        decisions = self.decision_book(moment)
        current_cards = self.current_card_source_ids(ticket, moment)
        ids: list[str] = []
        for sentence in self.kb.applicable_sentences(ticket, at=moment):
            source_id = sentence.source_id
            if source_id in self.kb.cards_by_id and source_id not in current_cards:
                # 旧修订仍可用于历史追溯，但不再作为当前作业单依据；
                # 过渡条款明确保留的甲料第二遍由新卡 B11 自身引用。
                continue
            if not self._sentence_applies_to_piece(sentence, ticket, moment, decisions):
                continue
            if source_id not in ids:
                ids.append(source_id)
        return ids

    def applicable_clauses(
        self, piece_id_or_ticket: str | dict[str, str], at: str | datetime | None = None
    ) -> list[Any]:
        moment = self._resolve_at(at)
        decisions = self.decision_book(moment)
        ticket = (
            piece_id_or_ticket
            if isinstance(piece_id_or_ticket, dict)
            else self.tickets_by_id[piece_id_or_ticket]
        )
        current_cards = self.current_card_source_ids(ticket, moment)
        clauses = []
        for sentence in self.kb.applicable_sentences(ticket, at=moment):
            if sentence.source_id in self.kb.cards_by_id and sentence.source_id not in current_cards:
                continue
            if self._sentence_applies_to_piece(sentence, ticket, moment, decisions):
                clauses.append(sentence)
        return clauses

    def _source_object_piece_ids(
        self, source_id: str, moment: datetime | None, decisions: dict[str, Any]
    ) -> set[str]:
        row = self.kb.source(source_id)
        scope = self.kb.scopes.get(source_id) or self.kb.scope_for_card(row) if source_id in self.kb.cards_by_id else self.kb.scopes[source_id]
        in_scope = [ticket for ticket in self.kb.tickets if scope.matches(ticket)]
        source_sentences = self.kb.sentences_by_source.get(source_id, [])
        named_parts = {name for sentence in source_sentences for name in sentence.object_names}
        every = any("every_piece" in sentence.tags for sentence in source_sentences)
        result: set[str] = set()

        if not named_parts and not any(sentence.descriptors for sentence in source_sentences):
            return {ticket["id"] for ticket in in_scope}

        for ticket in in_scope:
            if ticket.get("part") in named_parts:
                result.add(ticket["id"])

        for ambiguity in self.kb.ambiguities:
            if ambiguity.source_id != source_id:
                continue
            state = decisions["ambiguities"].get(ambiguity.id)
            if state and state["status"] == "confirmed":
                option = state["selected_option"]
                if option:
                    result.update(option["piece_ids"])
        return result

    def permit_source_ids(self, at: datetime | None = None) -> list[str]:
        result = []
        for card in self.kb.cards:
            sentences = self.kb.sentences_by_source.get(card["id"], [])
            if any("permission" in sentence.tags or "deadline" in sentence.tags for sentence in sentences):
                result.append(card["id"])
        return result

    def permit_covers_piece(
        self, source_id: str, piece_id: str, at: str | datetime | None = None
    ) -> bool:
        moment = self._resolve_at(at)
        decisions = self.decision_book(moment)
        ticket = self.tickets_by_id[piece_id]
        scope = self.kb.scopes[source_id]
        if not scope.matches(ticket):
            return False
        return piece_id in self._source_object_piece_ids(source_id, moment, decisions)

    def permit_covers_assembly(
        self, source_id: str, assembly_id: str, at: str | datetime | None = None
    ) -> bool:
        required = [
            ticket["id"]
            for ticket in self.kb.tickets
            if ticket.get("assembly") == assembly_id and ticket.get("required") == "是"
        ]
        return all(self.permit_covers_piece(source_id, piece_id, at) for piece_id in required)

    def permit_set_covers_assembly(
        self,
        source_ids: Iterable[str],
        assembly_id: str,
        at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """校验合流时实际收到的一组许可是否共同覆盖全部必需件。"""
        required = [
            ticket["id"]
            for ticket in self.kb.tickets
            if ticket.get("assembly") == assembly_id and ticket.get("required") == "是"
        ]
        covered: set[str] = set()
        permits = []
        for source_id in source_ids:
            piece_ids = [pid for pid in required if self.permit_covers_piece(source_id, pid, at)]
            covered.update(piece_ids)
            permits.append({"source_id": source_id, "covers_piece_ids": piece_ids})
        missing = [pid for pid in required if pid not in covered]
        return {
            "assembly": assembly_id,
            "required_piece_ids": required,
            "permits": permits,
            "covered_piece_ids": sorted(covered),
            "missing_piece_ids": missing,
            "covers_all_required": not missing,
        }

    def permit_validity(
        self, source_id: str, when: datetime | None, decisions: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        decisions = decisions or self.decision_book(when)
        row = self.kb.source(source_id)
        effective = parse_dt(row.get("effective"))
        deadlines = [
            sentence.deadline
            for sentence in self.kb.sentences_by_source.get(source_id, [])
            if sentence.deadline
        ]
        deadline = min(deadlines) if deadlines else None
        rejection = decisions["permits"].get(source_id)
        if rejection:
            status = "rejected"
        elif when and effective and effective > when:
            status = "not_effective"
        elif when and deadline and when >= deadline:
            status = "expired"
        else:
            status = "active"
        return {
            "source_id": source_id,
            "status": status,
            "effective": iso(effective),
            "deadline": iso(deadline),
            "rejection": rejection,
        }

    def permit_opinion(
        self,
        source_id: str,
        piece_id: str,
        *,
        operation_at: str | datetime,
        observed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """比较行为发生时与事后观察时的许可状态，禁止到期后追溯违规。"""
        operation_moment = self._resolve_at(operation_at)
        observed_moment = self._resolve_at(observed_at)
        then_decisions = self.decision_book(operation_moment)
        now_decisions = self.decision_book(observed_moment)
        validity_then = self.permit_validity(source_id, operation_moment, then_decisions)
        validity_now = self.permit_validity(source_id, observed_moment, now_decisions)
        covered_then = self.permit_covers_piece(source_id, piece_id, operation_moment)
        lawful_at_operation = validity_then["status"] == "active" and covered_then
        return {
            "source_id": source_id,
            "piece_id": piece_id,
            "operation_at": iso(operation_moment),
            "observed_at": iso(observed_moment),
            "validity_at_operation": validity_then,
            "validity_at_observation": validity_now,
            "covered_at_operation": covered_then,
            "lawful_at_operation": lawful_at_operation,
            "later_expired_or_rejected": validity_now["status"] in {"expired", "rejected"}
            and validity_then["status"] == "active",
            "retroactive_violation_forbidden": lawful_at_operation,
        }

    # ------------------------------------------------------------- planning
    def _primer_source_info(
        self,
        ticket: dict[str, str],
        moment: datetime | None,
        decisions: dict[str, Any] | None = None,
    ) -> tuple[int, str | None, list[str], list]:
        decisions = decisions or self.decision_book(moment)
        source_ids = self.source_ids_for_piece(ticket, moment)
        sentences = []
        for source_id in source_ids:
            for sentence in self.kb.sentences_by_source.get(source_id, []):
                if self._sentence_applies_to_piece(sentence, ticket, moment, decisions):
                    sentences.append(sentence)
        primer = [sentence for sentence in sentences if "primer" in sentence.tags]
        coat_count = 1
        for sentence in primer:
            if sentence.coat_count:
                coat_count = max(coat_count, sentence.coat_count)
            if sentence.pass_no:
                coat_count = max(coat_count, sentence.pass_no)
        # 新卡明确提到“第二遍”但没有写总遍数时，至少要求到第二遍。
        if any(sentence.pass_no == 2 for sentence in sentences):
            coat_count = max(coat_count, 2)

        material = None
        material_source_ids: list[str] = []
        for sentence in sorted(primer, key=lambda item: item.source_id):
            change = MATERIAL_CHANGE_RE.search(sentence.raw)
            if change:
                material = change.group(1)
                material_source_ids = [sentence.source_id]
        if material is None:
            for sentence in sentences:
                materials = _materials_text_order(sentence.raw)
                if materials:
                    material = materials[0]
                    material_source_ids = [sentence.source_id]
                    break
        if not primer:
            coat_count = 0
        return coat_count, material, material_source_ids, sentences

    def _retained_material(self, sentences: list) -> tuple[str | None, Any]:
        for sentence in sentences:
            match = RETAIN_MATERIAL_RE.search(sentence.raw)
            if match:
                return match.group(1), sentence
        return None, None

    def _revision_effective(self, ticket: dict[str, str]) -> datetime | None:
        roots = [
            card
            for card in self.kb.cards
            if not card.get("parent") and self.kb.scope_for_card(card).matches(ticket)
        ]
        return max((parse_dt(card["effective"]) for card in roots if parse_dt(card["effective"])), default=None)

    def _trial_fit_sources(
        self,
        ticket: dict[str, str],
        moment: datetime | None,
        decisions: dict[str, Any],
        progress: dict[str, Any],
    ) -> list[str]:
        """返回真正可作为试配依据的来源。

        交接规则 R1 写的是“文件允许时”，因此规则本身不能凭空创设替代检查；
        必须有 P21 这类卡片偏离许可。若许可是在工序完成后才被否决/到期，
        已登记凭证仍保留，所以历史完成件继续把该来源列为依据。
        """
        piece_id = ticket["id"]
        historical_use = bool(progress["inspections"]["trial_fit"]) or any(
            claim.get("step_key") == "trial_fit" for claim in progress["claims"]
        )
        card_sources: list[str] = []
        rule_sources: list[str] = []
        for clause in self.applicable_clauses(ticket, moment):
            if "trial_fit" not in clause.tags:
                continue
            source_id = clause.source_id
            if not self.permit_covers_piece(source_id, piece_id, moment):
                continue
            validity = self.permit_validity(source_id, moment, decisions)
            if source_id in self.kb.cards_by_id:
                if validity["status"] != "rejected" or historical_use:
                    card_sources.append(source_id)
            elif source_id in self.kb.rules_by_id:
                rule_sources.append(source_id)
        if not card_sources:
            return []
        return list(dict.fromkeys(card_sources + rule_sources))

    def build_orders(self, piece_id: str, at: str | datetime | None = None) -> list[dict[str, Any]]:
        moment = self._resolve_at(at)
        decisions = self.decision_book(moment)
        ticket = self.tickets_by_id[piece_id]
        progress = self.progress(piece_id, moment)
        coat_count, standard_material, material_sources, sentences = self._primer_source_info(
            ticket, moment, decisions
        )
        source_ids = self.source_ids_for_piece(ticket, moment)
        orders: list[dict[str, Any]] = []

        # 领料事实和“领料未涂不豁免”说明
        pickup_fact = None
        if progress["material_issues"]:
            latest_pickup = progress["material_issues"][-1]
            pickup_fact = {
                "code": "PICKUP_IS_MATERIAL_FACT_ONLY",
                "message": "领料只证明物料去向；未完成涂覆前，不享受已涂首遍/旧工艺豁免。",
                "event_id": latest_pickup["event_id"],
                "source_ids": [sentence.source_id for sentence in sentences if "transition_picked_uncoated" in sentence.tags],
            }

        for pass_no in range(1, max(coat_count, 0) + 1):
            key = PASS_KEY_RE.get(pass_no, f"primer_pass_{pass_no}")
            completion = progress["completions"].get(key)
            start = progress["starts"].get(key)
            claim = next((item for item in reversed(progress["claims"]) if item.get("step_key") == key), None)
            material = standard_material
            material_note = None
            compat = True
            compat_reason = None
            step_sources = list(source_ids)

            if pass_no >= 2:
                first = progress["completions"].get("primer_pass_1")
                retained, retained_sentence = self._retained_material(sentences)
                new_effective = self._revision_effective(ticket)
                if first:
                    first_materials = first.get("materials") or _materials_text_order(first["detail"])
                    first_material = first_materials[0] if first_materials else None
                    completed_at = parse_dt(first["at"])
                    if (
                        retained
                        and first_material == retained
                        and new_effective
                        and completed_at
                        and completed_at < new_effective
                    ):
                        material = retained
                        material_note = "新版过渡条款明确：已完成首遍者沿用旧料至第二遍"
                        step_sources = list(dict.fromkeys(step_sources + [retained_sentence.source_id]))
                    elif first_material and standard_material and first_material == standard_material:
                        material = standard_material
                    elif first_material and retained and first_material == retained:
                        # 即使时间边界资料不完整，原句明确“已完成首遍”类别时仍按沿用处理。
                        material = retained
                        material_note = "按过渡条款沿用首遍材料"
                        step_sources = list(dict.fromkeys(step_sources + [retained_sentence.source_id]))
                    else:
                        compat = False
                        compat_reason = "completed_material_has_no_explicit_compatible_next_card"
                elif standard_material:
                    material = standard_material

            if completion and completion.get("materials"):
                material = completion["materials"][0]
            title_material = f"{material}料" if material else ""
            title = f"{title_material}底涂第{['一', '二', '三', '四'][pass_no - 1]}遍".replace("料底涂", "底涂")
            reasons: list[dict[str, Any]] = []
            if pass_no == 1 and not any(source_id in self.kb.cards_by_id for source_id in source_ids):
                reasons.append(
                    {
                        "code": "PARTIAL_STATION_RULE_ONLY",
                        "message": "当前只找到工位交接规则引用的材料标准，未找到完整的本线签发底涂卡；首遍材料可执行，后续道数/检查缺依据时必须暂停。",
                    }
                )
            if pickup_fact and pass_no == 1 and not completion:
                reasons.append(pickup_fact)
            if material_note:
                reasons.append({"code": "TRANSITION_CONTINUE_OLD", "message": material_note})
            if not compat:
                reasons.append(
                    {
                        "code": STATUS_NO_NEXT.upper(),
                        "message": "已有完成凭证，但找不到工艺原句明确兼容的下一材料/工序；保留凭证并暂停。",
                    }
                )

            preserved_evidence_id = None
            if completion:
                status = STATUS_COMPLETED
                instruction = "已完成并登记，保留原始凭证"
                evidence = completion
            elif not compat:
                status = STATUS_NO_NEXT
                instruction = "保留原完成凭证并暂停下一步；不得由工人自行猜测材料或工序"
                evidence = None
                previous = progress["completions"].get(PASS_KEY_RE.get(pass_no - 1, ""))
                preserved_evidence_id = previous["event_id"] if previous else None
            elif start:
                status = STATUS_IN_PROGRESS
                instruction = "工序已开工并登记"
                evidence = start
            elif claim:
                status = STATUS_CLAIMED
                instruction = "作业单已被当班领取但尚未登记开工"
                evidence = claim
            elif pass_no > 1 and not progress["completions"].get(PASS_KEY_RE.get(pass_no - 1, "")):
                status = STATUS_WAITING
                instruction = "等待前一遍完成"
                evidence = None
            else:
                status = STATUS_READY
                instruction = f"完成{title}"
                evidence = None

            order = {
                "id": f"ORD-{piece_id}-{key}",
                "piece_id": piece_id,
                "assembly": ticket["assembly"],
                "step_key": key,
                "kind": "primer",
                "pass_no": pass_no,
                "title": title,
                "material": material,
                "status": status,
                "status_zh": STATUS_LABELS_ZH[status],
                "instruction": instruction,
                "source_ids": step_sources,
                "reasons": reasons,
                "evidence_event_id": evidence["event_id"] if evidence else preserved_evidence_id,
                "preserved_evidence_event_id": preserved_evidence_id,
                "receipt_event_ids": [
                    item["event_id"] for item in progress["claims"] if item.get("step_key") == key
                ],
                "alternatives": [],
            }
            if completion and completion.get("materials"):
                order["material"] = completion["materials"][0]
            orders.append(self._apply_decision_holds(order, ticket, moment, decisions, progress))

        # 临时许可给出的可选旧料路线。它不替代标准路线，因此其范围歧义只暂缓
        # 该可选单，不阻止未受影响的首遍/标准第二遍继续领单。
        for permit_id in self.permit_source_ids(moment):
            validity = self.permit_validity(permit_id, moment, decisions)
            old_clauses = [
                sentence
                for sentence in self.kb.sentences_by_source.get(permit_id, [])
                if "old_material" in sentence.tags
                and (sentence.pass_no == 2 or "第二遍" in sentence.raw)
            ]
            if not old_clauses:
                continue
            permit_scope = self.kb.scopes[permit_id]
            if not permit_scope.matches(ticket):
                continue
            explicit_ids = self._source_object_piece_ids(permit_id, moment, decisions)
            potential_ids: set[str] = set()
            for ambiguity in self.kb.ambiguities:
                if ambiguity.source_id != permit_id:
                    continue
                for option in ambiguity.options:
                    if option.key == "include_edge_descriptors":
                        potential_ids.update(option.piece_ids)
            if piece_id not in explicit_ids and piece_id not in potential_ids:
                continue

            old_completion = None
            old_start = None
            old_claim = None
            for event in self.piece_events(piece_id, moment):
                if _step_key_from_text(event.get("detail", "")) != "primer_pass_2":
                    continue
                if "旧料" not in event.get("detail", ""):
                    continue
                if event["event"] == "完工":
                    old_completion = event
                elif event["event"] == "开工":
                    old_start = event
                elif event["event"] == "领单":
                    old_claim = event

            ambiguity = next((item for item in self.kb.ambiguities if item.source_id == permit_id), None)
            amb_state = decisions["ambiguities"].get(ambiguity.id) if ambiguity else None
            is_explicit = piece_id in (ambiguity.explicit_piece_ids if ambiguity else explicit_ids)
            broad_confirmed = bool(
                amb_state
                and amb_state["status"] == "confirmed"
                and (amb_state.get("selected_option") or {}).get("key") == "include_edge_descriptors"
            )
            scope_settled = is_explicit or broad_confirmed
            withdrawn = bool(amb_state and amb_state["status"] == "withdrawn")

            if old_completion:
                old_status = STATUS_COMPLETED
                instruction = "已按当时有效许可完成并登记；许可事后到期不构成当时违规"
                evidence = old_completion
            elif validity["status"] in {"rejected", "expired", "not_effective"}:
                old_status = STATUS_PERMIT_UNAVAILABLE
                instruction = "许可当前不允许开具旧料可选单；已完成凭证按行为发生时状态审计"
                evidence = None
            elif old_start:
                old_status = STATUS_IN_PROGRESS
                instruction = "旧料第二遍已开工"
                evidence = old_start
            elif old_claim:
                old_status = STATUS_CLAIMED if scope_settled and not withdrawn else STATUS_AWAIT_CONFIRMATION
                instruction = "已领旧料第二遍单；解释未确定前不得发料" if old_status == STATUS_AWAIT_CONFIRMATION else "可按许可使用旧料完成第二遍"
                evidence = old_claim
            elif not scope_settled or withdrawn:
                old_status = STATUS_AWAIT_CONFIRMATION
                instruction = "旧料许可是否覆盖本件存在待确认解释；仅暂缓该可选路线，标准材料路线仍可执行"
                evidence = None
            else:
                old_status = STATUS_READY
                instruction = "可在许可时限内使用旧料完成第二遍"
                evidence = None

            old_order = {
                "id": f"ORD-{piece_id}-OLD2",
                "piece_id": piece_id,
                "assembly": ticket["assembly"],
                "step_key": "old_material_pass_2",
                "kind": "primer_alternative",
                "pass_no": 2,
                "title": "旧料底涂第二遍（临时许可可选路线）",
                "material": "旧料",
                "status": old_status,
                "status_zh": STATUS_LABELS_ZH[old_status],
                "instruction": instruction,
                "source_ids": [permit_id],
                "reasons": [
                    {
                        "code": "TEMPORARY_PERMIT_OPTION",
                        "message": "临时许可只放宽其文字明确点到的工序/对象，未提及的膜厚等要求仍随原卡。",
                        "source_id": permit_id,
                    }
                ],
                "evidence_event_id": evidence["id"] if evidence else None,
                "receipt_event_ids": [old_claim["id"]] if old_claim else [],
                "alternatives": [f"ORD-{piece_id}-primer_pass_2"],
                "blocks_standard_route": False,
            }
            if validity.get("deadline"):
                old_order["permit_deadline"] = validity["deadline"]
            orders.append(self._apply_decision_holds(old_order, ticket, moment, decisions, progress))

        # 膜厚检查与合流试配
        film_sentences = [sentence for sentence in sentences if "film_thickness" in sentence.tags]
        trial_sources = self._trial_fit_sources(ticket, moment, decisions, progress)
        film_required = bool(film_sentences)
        if film_required:
            film_records = progress["inspections"]["film_thickness"]
            latest_film = film_records[-1] if film_records else None
            film_failed = bool(latest_film and latest_film.get("passed") is False)
            film_done = bool(latest_film and not film_failed)
            film_claim = next(
                (item for item in reversed(progress["claims"]) if item.get("step_key") == "film_thickness"),
                None,
            )
            predecessor_done = coat_count == 0 or all(
                progress["completions"].get(PASS_KEY_RE.get(i, "")) for i in range(1, coat_count + 1)
            )
            status = (
                STATUS_FAILED
                if film_failed
                else STATUS_COMPLETED
                if film_done
                else STATUS_CLAIMED
                if film_claim
                else STATUS_READY
                if predecessor_done
                else STATUS_WAITING
            )
            film_order = {
                "id": f"ORD-{piece_id}-FILM",
                "piece_id": piece_id,
                "assembly": ticket["assembly"],
                "step_key": "film_thickness",
                "kind": "inspection",
                "title": "合流前逐件膜厚检查",
                "material": None,
                "status": status,
                "status_zh": STATUS_LABELS_ZH[status],
                "instruction": "逐件实测膜厚；测点要求随适用原卡/交接规则"
                if status not in {STATUS_COMPLETED, STATUS_FAILED}
                else "膜厚检查已登记，结果不合格，须处置后复检"
                if status == STATUS_FAILED
                else "膜厚检查已登记",
                "source_ids": [sentence.source_id for sentence in film_sentences],
                "reasons": [],
                "evidence_event_id": latest_film["event_id"] if latest_film else None,
                "receipt_event_ids": [item["event_id"] for item in progress["claims"] if item.get("step_key") == "film_thickness"],
                "alternatives": [f"ORD-{piece_id}-FIT"] if trial_sources else [],
            }
            if trial_sources:
                film_order["reasons"].append(
                    {
                        "code": "MINIMAL_SCOPE_EXCEPTION",
                        "message": "该分件有明确许可改做合流试配；膜厚要求的替代仅影响本分件。",
                        "source_ids": trial_sources,
                    }
                )
            orders.append(self._apply_decision_holds(film_order, ticket, moment, decisions, progress))

        if trial_sources:
            fit_records = progress["inspections"]["trial_fit"]
            latest_fit = fit_records[-1] if fit_records else None
            fit_failed = bool(latest_fit and latest_fit.get("passed") is False)
            fit_done = bool(latest_fit and not fit_failed)
            fit_claim = next((item for item in reversed(progress["claims"]) if item.get("step_key") == "trial_fit"), None)
            predecessor_done = coat_count == 0 or all(
                progress["completions"].get(PASS_KEY_RE.get(i, "")) for i in range(1, coat_count + 1)
            )
            status = (
                STATUS_FAILED
                if fit_failed
                else STATUS_COMPLETED
                if fit_done
                else STATUS_CLAIMED
                if fit_claim
                else STATUS_READY
                if predecessor_done
                else STATUS_WAITING
            )
            fit_order = {
                "id": f"ORD-{piece_id}-FIT",
                "piece_id": piece_id,
                "assembly": ticket["assembly"],
                "step_key": "trial_fit",
                "kind": "inspection",
                "title": "随框板/构件做合流试配（许可替代膜厚）",
                "material": None,
                "status": status,
                "status_zh": STATUS_LABELS_ZH[status],
                "instruction": "按许可在合流时完成试配，并保留交接记录",
                "source_ids": trial_sources,
                "reasons": [
                    {
                        "code": "PERMITTED_ALTERNATIVE",
                        "message": "交接规则和偏离说明明确允许连接件改做合流试配。",
                    }
                ],
                "evidence_event_id": latest_fit["event_id"] if latest_fit else None,
                "receipt_event_ids": [item["event_id"] for item in progress["claims"] if item.get("step_key") == "trial_fit"],
                "alternatives": [f"ORD-{piece_id}-FILM"],
            }
            orders.append(self._apply_decision_holds(fit_order, ticket, moment, decisions, progress))

        # 没有当前工艺卡、但交接规则给出部分材料要求时，至少开出可执行首遍。
        if not orders and any("primer" in sentence.tags for sentence in sentences):
            # 理论上不会进入；保留防御性分支。
            pass
        if coat_count == 0:
            orders.append(
                {
                    "id": f"HOLD-{piece_id}-NO-CARD",
                    "piece_id": piece_id,
                    "assembly": ticket["assembly"],
                    "step_key": "missing_process_card",
                    "kind": "hold",
                    "title": "缺少适用的本线工艺卡",
                    "material": None,
                    "status": STATUS_AWAIT_CONFIRMATION,
                    "status_zh": STATUS_LABELS_ZH[STATUS_AWAIT_CONFIRMATION],
                    "instruction": "未找到与工位/机型匹配且已生效的底涂卡；不得借用其他工位卡片猜测道数和检查要求。",
                    "source_ids": source_ids,
                    "reasons": [{"code": "MISSING_SCOPE_CARD", "message": "请工艺员/本线签发卡片后再开单。"}],
                    "evidence_event_id": None,
                    "receipt_event_ids": [],
                    "alternatives": [],
                }
            )

        # 已完成首遍但来源没有明确给出完整道数/后续步骤时，显式生成无兼容下一步暂停单。
        route_defined = any(
            sentence.coat_count is not None or sentence.pass_no is not None for sentence in sentences
        )
        primer_completions = [key for key in progress["completions"] if key.startswith("primer_pass_")]
        if primer_completions and not route_defined and coat_count <= len(primer_completions):
            last_pass = max(int(key.rsplit("_", 1)[1]) for key in primer_completions)
            next_key = PASS_KEY_RE.get(last_pass + 1)
            if next_key and not any(order["step_key"] == next_key for order in orders):
                orders.append(
                    {
                        "id": f"HOLD-{piece_id}-NO-NEXT",
                        "piece_id": piece_id,
                        "assembly": ticket["assembly"],
                        "step_key": next_key,
                        "kind": "hold",
                        "title": "无兼容下一步",
                        "material": None,
                        "status": STATUS_NO_NEXT,
                        "status_zh": STATUS_LABELS_ZH[STATUS_NO_NEXT],
                        "instruction": "保留原完成凭证，暂停下一步，不交给工人自行猜测。",
                        "source_ids": source_ids,
                        "reasons": [{"code": "NO_COMPATIBLE_NEXT_STEP", "message": "现有卡片/许可未给出兼容的后续工序。"}],
                        "evidence_event_id": progress["completions"][f"primer_pass_{last_pass}"]["event_id"],
                        "receipt_event_ids": [],
                        "alternatives": [],
                    }
                )

        # 当班实际领到的“等待确认”纸条/单据必须成为未解除的暂缓原因。
        for receipt in progress["hold_receipts"]:
            if "等待用料确认" in receipt["detail"] or "待确认" in receipt["detail"]:
                orders.append(
                    {
                        "id": f"HOLD-{piece_id}-RECEIPT-{receipt['event_id']}",
                        "piece_id": piece_id,
                        "assembly": ticket["assembly"],
                        "step_key": "material_confirmation_receipt",
                        "kind": "hold",
                        "title": "当班领单内容：等待用料确认",
                        "material": None,
                        "status": STATUS_AWAIT_CONFIRMATION,
                        "status_zh": STATUS_LABELS_ZH[STATUS_AWAIT_CONFIRMATION],
                        "instruction": "该领单凭证尚未被后续确认事件解除；班组应等待用料确认，不自行换料。",
                        "source_ids": [],
                        "reasons": [
                            {
                                "code": "ISSUED_HOLD_RECEIPT",
                                "message": "历史领单原文必须保留：" + receipt["detail"],
                                "event_id": receipt["event_id"],
                            }
                        ],
                        "evidence_event_id": receipt["event_id"],
                        "receipt_event_ids": [receipt["event_id"]],
                        "alternatives": [],
                    }
                )

        return orders

    def _apply_decision_holds(
        self,
        order: dict[str, Any],
        ticket: dict[str, str],
        moment: datetime | None,
        decisions: dict[str, Any],
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        piece_id = ticket["id"]
        for ambiguity in self.kb.ambiguities:
            state = decisions["ambiguities"].get(ambiguity.id)
            if not state:
                continue
            source_id = ambiguity.source_id
            covers = self.permit_covers_piece(source_id, piece_id, moment)
            selected = state.get("selected_option") or {}
            covered_before_withdrawal = (
                state["status"] == "withdrawn" and piece_id in selected.get("piece_ids", [])
            )
            if (
                not covers
                and not covered_before_withdrawal
                and piece_id not in ambiguity.explicit_piece_ids
            ):
                # 仅作为潜在扩大对象时，只影响依赖该许可的可选项。
                option_piece = piece_id in {
                    pid for option in ambiguity.options for pid in option.piece_ids if option.key == "include_edge_descriptors"
                }
                if not option_piece:
                    continue
                if order["step_key"] == "primer_pass_2" and state["status"] in {"open", "withdrawn"}:
                    order["reasons"].append(
                        {
                            "code": "INTERPRETATION_WITHDRAWN_UPDATE"
                            if state["status"] == "withdrawn"
                            else "SCOPE_AMBIGUITY_PENDING",
                            "message": state["ambiguity"]["question"],
                            "ambiguity_id": ambiguity.id,
                        }
                    )
                continue

            affects_step = order["step_key"] in {"primer_pass_2", "old_material_pass_2"} or (
                order["kind"] == "primer" and order.get("pass_no") == 2
            )
            if not affects_step:
                continue

            start = progress["starts"].get(order["step_key"])
            if start is None and order["step_key"] == "old_material_pass_2":
                for event in self.piece_events(piece_id, moment):
                    if (
                        event.get("event") == "开工"
                        and _step_key_from_text(event.get("detail", "")) == "primer_pass_2"
                        and "旧料" in event.get("detail", "")
                    ):
                        start = event
                        break
            withdrawal_event = None
            if state["status"] == "withdrawn":
                withdrawal_id = state.get("withdraw_event_id")
                withdrawal_event = next(
                    (event for event in self._events(moment) if event["id"] == withdrawal_id), None
                )
            if start and withdrawal_event:
                start_at = parse_dt(start["at"])
                withdraw_at = parse_dt(withdrawal_event.get("at"))
                if start_at and withdraw_at and start_at <= withdraw_at:
                    order["status"] = STATUS_ENGINEER
                    order["status_zh"] = STATUS_LABELS_ZH[STATUS_ENGINEER]
                    order["instruction"] = "解释撤回前该遍已经开工；立即暂停继续施工/流转，保留用料和工时记录，报工艺员决定处置。"
                    order["reasons"].append(
                        {
                            "code": "STARTED_ORDER_ENGINEER_DISPOSITION",
                            "message": "解释撤回不能由班组自行改写已开工单；须工艺员决定继续、返工或复检。",
                            "source_id": source_id,
                            "event_id": withdrawal_event["id"],
                        }
                    )
            elif state["status"] in {"open", "withdrawn"}:
                order["reasons"].append(
                    {
                        "code": "INTERPRETATION_WITHDRAWN_UPDATE"
                        if state["status"] == "withdrawn"
                        else "INTERPRETATION_UPDATE_PENDING",
                        "message": "工艺解释已撤回或尚未确认；依赖该解释的未领/已领未开工单按最新解释更新，不得按旧解释发料。",
                        "source_id": source_id,
                        "ambiguity_id": ambiguity.id,
                    }
                )
                if order["status"] == STATUS_READY and order["step_key"] == "old_material_pass_2":
                    order["status"] = STATUS_AWAIT_CONFIRMATION
        return order

    # ------------------------------------------------------------- ledger
    def reconcile_orders(self, at: str | datetime | None = None) -> dict[str, OrderRecord]:
        moment = self._resolve_at(at)
        for ticket in self.kb.tickets:
            for snapshot in self.build_orders(ticket["id"], moment):
                order_id = snapshot["id"]
                record = self.order_records.get(order_id)
                if record is None:
                    record = OrderRecord(id=order_id, piece_id=ticket["id"], step_key=snapshot["step_key"])
                    self.order_records[order_id] = record
                record.receipt_event_ids = sorted(
                    set(record.receipt_event_ids) | set(snapshot.get("receipt_event_ids", []))
                )
                record.append_snapshot(snapshot, moment)
        return self.order_records

    def order_history(self, order_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        self.reconcile_orders(at)
        record = self.order_records.get(order_id)
        if record is None:
            raise KeyError(f"未知作业单: {order_id}")
        return record.to_dict()

    # ------------------------------------------------------------- boards
    def _hold_reasons(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        holds = []
        hold_statuses = {
            STATUS_AWAIT_CONFIRMATION,
            STATUS_ENGINEER,
            STATUS_NO_NEXT,
            STATUS_BLOCKED,
            STATUS_FAILED,
            STATUS_PERMIT_UNAVAILABLE,
        }
        for order in orders:
            if order["status"] in hold_statuses:
                optional_route = order.get("blocks_standard_route") is False or order["step_key"] == "old_material_pass_2"
                holds.append(
                    {
                        "order_id": order["id"],
                        "step_key": order["step_key"],
                        "status": order["status"],
                        "status_zh": order["status_zh"],
                        "title": order["title"],
                        "instruction": order["instruction"],
                        "reasons": order["reasons"],
                        "blocks_execution": not optional_route,
                    }
                )
        return holds

    def piece_board(self, piece_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        self.reconcile_orders(moment)
        ticket = self.tickets_by_id[piece_id]
        orders = self.build_orders(piece_id, moment)
        progress = self.progress(piece_id, moment)
        holds = self._hold_reasons(orders)
        deferred_reasons = []
        for order in orders:
            if order["status"] in {
                STATUS_AWAIT_CONFIRMATION,
                STATUS_ENGINEER,
                STATUS_NO_NEXT,
                STATUS_BLOCKED,
            }:
                continue
            for reason in order.get("reasons", []):
                if reason.get("code") in {
                    "SCOPE_AMBIGUITY_PENDING",
                    "INTERPRETATION_UPDATE_PENDING",
                    "INTERPRETATION_WITHDRAWN_UPDATE",
                    "MINIMAL_SCOPE_EXCEPTION",
                }:
                    deferred_reasons.append(
                        {
                            "order_id": order["id"],
                            "step_key": order["step_key"],
                            "status": order["status"],
                            **reason,
                        }
                    )

        active = [order for order in orders if order["status"] in {STATUS_CLAIMED, STATUS_IN_PROGRESS}]
        has_started_order = any(order["status"] == STATUS_IN_PROGRESS for order in orders)
        executable = None
        if not has_started_order:
            for order in orders:
                if order["status"] in {STATUS_READY, STATUS_CLAIMED}:
                    # 若同一物件已有未解除的实际等待领单，保守地不把另一张单标成可直接施工。
                    if any(
                        reason["code"] == "ISSUED_HOLD_RECEIPT"
                        for hold in holds
                        for reason in hold.get("reasons", [])
                    ):
                        order = dict(order)
                        order["executable_despite_hold_receipt"] = False
                    if executable is None:
                        executable = order
        blocking_hold = any(hold.get("blocks_execution", True) for hold in holds)
        if blocking_hold:
            executable = None

        receipts = [
            {
                "event_id": claim["event_id"],
                "at": claim["at"],
                "verbatim_detail": claim["detail"],
                "step_key": claim.get("step_key"),
            }
            for claim in progress["claims"]
        ]
        evidence = [
            {
                "event_id": item["event_id"],
                "at": item["at"],
                "event": item["event"],
                "verbatim_detail": item["detail"],
            }
            for group in [progress["completions"].values(), progress["material_issues"]]
            for item in group
        ]
        evidence.extend(
            {
                "event_id": item["event_id"],
                "at": item["at"],
                "event": item["event"],
                "verbatim_detail": item["detail"],
            }
            for items in progress["inspections"].values()
            for item in items
        )
        constraints = []
        for clause in self.applicable_clauses(ticket, moment):
            interesting = clause.tags & {
                "prohibition",
                "deadline",
                "exception",
                "coat_before_measure",
                "fixed_points",
                "rework",
                "reinspection",
            }
            if interesting:
                constraints.append(
                    {
                        "source_id": clause.source_id,
                        "clause_uid": clause.uid,
                        "tags": sorted(interesting),
                        "text": clause.raw,
                        "deadline": iso(clause.deadline),
                    }
                )
        return {
            "piece_id": piece_id,
            "assembly": ticket["assembly"],
            "part": ticket["part"],
            "station": ticket["station"],
            "required": ticket["required"] == "是",
            "as_of": iso(moment),
            "executable_next": executable,
            "active_orders": active,
            "orders": orders,
            "constraints": constraints,
            "hold_reasons": holds,
            "deferred_reasons": deferred_reasons,
            "blocked": blocking_hold,
            "receipt_readback": receipts,
            "completion_evidence": evidence,
        }

    def board(self, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        self.reconcile_orders(moment)
        assemblies: dict[str, list[dict[str, Any]]] = {}
        for ticket in self.kb.tickets:
            assemblies.setdefault(ticket["assembly"], []).append(self.piece_board(ticket["id"], moment))
        assembly_boards = []
        for assembly_id, pieces in sorted(assemblies.items()):
            handoff = self.handoff_check(assembly_id, moment)
            assembly_boards.append(
                {
                    "assembly": assembly_id,
                    "piece_count": len(pieces),
                    "blocked_piece_ids": [piece["piece_id"] for piece in pieces if piece["blocked"]],
                    "pieces": pieces,
                    "handoff": handoff,
                }
            )
        return {
            "as_of": iso(moment),
            "ambiguities": self.decision_book(moment)["ambiguities"],
            "assemblies": assembly_boards,
        }

    # ------------------------------------------------------------- handoff
    def _inspection_satisfied(self, piece_orders: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        film = next((order for order in piece_orders if order["step_key"] == "film_thickness"), None)
        fit = next((order for order in piece_orders if order["step_key"] == "trial_fit"), None)
        if fit and fit["status"] == STATUS_COMPLETED:
            return True, [fit["id"]]
        if film and film["status"] == STATUS_COMPLETED and (not fit or film["status"] == STATUS_COMPLETED):
            # 有试配许可时，膜厚完成当然也满足原检查；试配只是替代项。
            return True, [film["id"]]
        if film and not fit and film["status"] == STATUS_COMPLETED:
            return True, [film["id"]]
        return False, []

    def handoff_check(self, assembly_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        decisions = self.decision_book(moment)
        tickets = [ticket for ticket in self.kb.tickets if ticket["assembly"] == assembly_id]
        if not tickets:
            raise KeyError(f"未知构件: {assembly_id}")
        required_tickets = [ticket for ticket in tickets if ticket.get("required") == "是"]
        required_ids = [ticket["id"] for ticket in required_tickets]
        piece_boards = {ticket["id"]: self.piece_board(ticket["id"], moment) for ticket in tickets}

        piece_results = []
        blockers: list[dict[str, Any]] = []
        for piece_id in required_ids:
            board = piece_boards[piece_id]
            orders = board["orders"]
            piece_blockers = []
            for hold in board["hold_reasons"]:
                if not hold.get("blocks_execution", True):
                    continue
                piece_blockers.append({"code": hold["status"].upper(), "message": hold["instruction"], "order_id": hold["order_id"]})
            incomplete_coats = [
                order["id"]
                for order in orders
                if order["kind"] == "primer" and order["status"] != STATUS_COMPLETED
            ]
            if incomplete_coats and not any(block["code"] == STATUS_NO_NEXT.upper() for block in piece_blockers):
                piece_blockers.append(
                    {"code": "PRIMER_NOT_COMPLETE", "message": "必需底涂遍数尚未全部完成", "order_ids": incomplete_coats}
                )
            inspection_ok, evidence_orders = self._inspection_satisfied(orders)
            if not inspection_ok:
                piece_blockers.append(
                    {"code": "INSPECTION_NOT_COMPLETE", "message": "适用的膜厚检查或许可替代试配尚未完成"}
                )
            if piece_blockers:
                blockers.append({"piece_id": piece_id, "blockers": piece_blockers})
            piece_results.append(
                {
                    "piece_id": piece_id,
                    "part": self.tickets_by_id[piece_id]["part"],
                    "inspection_satisfied": inspection_ok,
                    "inspection_evidence_order_ids": evidence_orders,
                    "blockers": piece_blockers,
                }
            )

        permit_coverage = []
        collective: set[str] = set()
        for source_id in self.permit_source_ids(moment):
            covers = [
                piece_id
                for piece_id in required_ids
                if self.permit_covers_piece(source_id, piece_id, moment)
            ]
            validity = self.permit_validity(source_id, moment, decisions)
            ambiguity = next((item for item in self.kb.ambiguities if item.source_id == source_id), None)
            interpretation = None
            if ambiguity:
                state = decisions["ambiguities"].get(ambiguity.id, {})
                interpretation = {
                    "ambiguity_id": ambiguity.id,
                    "status": state.get("status"),
                    "selected_option": state.get("selected_option"),
                    "source_revision": ambiguity.source_revision,
                }
            collective.update(covers)
            permit_coverage.append(
                {
                    "source_id": source_id,
                    "status": validity["status"],
                    "interpretation": interpretation,
                    "covers_piece_ids": covers,
                    "covers_all_required": set(covers) == set(required_ids),
                    "missing_required_piece_ids": [pid for pid in required_ids if pid not in covers],
                }
            )

        authority_coverage = {}
        for piece_id in required_ids:
            ticket = self.tickets_by_id[piece_id]
            authority_coverage[piece_id] = self.source_ids_for_piece(ticket, moment)

        conflicts = self._inspection_conflicts(required_ids, moment, decisions)
        ready = not blockers and all(authority_coverage.get(pid) for pid in required_ids)
        return {
            "assembly": assembly_id,
            "as_of": iso(moment),
            "required_piece_ids": required_ids,
            "ready_for_handoff": ready,
            "merge_allowed": ready,
            "blockers": blockers,
            "pieces": piece_results,
            "permit_coverage": {
                "permits": permit_coverage,
                "collective_covered_piece_ids": sorted(collective),
                "collective_covers_all_required": set(collective) == set(required_ids),
                "missing_permit_coverage": [pid for pid in required_ids if pid not in collective],
                "authority_coverage": authority_coverage,
                "rule": "合流所依据的许可集合必须覆盖所有必需件；原卡继续覆盖未被许可放宽的要求。",
            },
            "inspection_conflicts": conflicts,
            "minimum_affected_scope": conflicts[0]["affected_piece_ids"] if conflicts else [],
            "unaffected_piece_ids": conflicts[0]["unaffected_piece_ids"] if conflicts else required_ids,
        }

    def _inspection_conflicts(
        self, required_ids: list[str], moment: datetime | None, decisions: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not required_ids:
            return []
        trial_pieces = []
        trial_sources = set()
        film_sources = set()
        for piece_id in required_ids:
            ticket = self.tickets_by_id[piece_id]
            for clause in self.applicable_clauses(ticket, moment):
                if "film_thickness" in clause.tags:
                    film_sources.add(clause.source_id)

            progress = self.progress(piece_id, moment)
            sources = self._trial_fit_sources(ticket, moment, decisions, progress)
            card_sources = [source_id for source_id in sources if source_id in self.kb.cards_by_id]
            if card_sources:
                trial_pieces.append(piece_id)
                trial_sources.update(sources)
        if not trial_pieces:
            return []
        affected = sorted(set(trial_pieces))
        unaffected = [piece_id for piece_id in required_ids if piece_id not in affected]
        return [
            {
                "kind": "film_thickness_vs_assembly_trial_fit",
                "description": "原卡要求合流前每件测膜厚；偏离说明允许特定连接件改做合流试配。",
                "general_requirement_source_ids": sorted(film_sources),
                "exception_source_ids": sorted(trial_sources),
                "affected_piece_ids": affected,
                "unaffected_piece_ids": unaffected,
                "minimum_scope_rule": "冲突只作用于明确点到的分件/检查方式，不扩大到同一构件其他必需件。",
                "status": "resolved_by_explicit_exception",
            }
        ]

    # ------------------------------------------------------------- QC trace
    def quality_trace(self, assembly_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        moment = self._resolve_at(at)
        tickets = [ticket for ticket in self.kb.tickets if ticket["assembly"] == assembly_id]
        if not tickets:
            raise KeyError(f"未知构件: {assembly_id}")
        handoff = self.handoff_check(assembly_id, moment)
        piece_traces = []
        for ticket in tickets:
            piece_id = ticket["id"]
            event_traces = []
            for event in self.piece_events(piece_id, moment):
                event_moment = parse_dt(event.get("at"))
                clauses_then = self.applicable_clauses(ticket, event_moment or moment)
                source_ids = list(dict.fromkeys(clause.source_id for clause in clauses_then))
                sources_then = [self.source_snapshot(source_id) for source_id in source_ids]
                active_sentences = [clause.raw for clause in clauses_then]
                event_traces.append(
                    {
                        **{key: value for key, value in event.items() if not key.startswith("_")},
                        "sources_effective_then": sources_then,
                        "source_sentences_then": active_sentences,
                    }
                )
            board = self.piece_board(piece_id, moment)
            piece_traces.append(
                {
                    "piece_id": piece_id,
                    "part": ticket["part"],
                    "station": ticket["station"],
                    "records": event_traces,
                    "receipt_readback": board["receipt_readback"],
                    "completion_evidence": board["completion_evidence"],
                    "orders": board["orders"],
                }
            )

        relevant_source_ids = set()
        # 登记册保留所有曾适用于本构件的修订；某道工序“当时有效”的来源由
        # 每条 records[*].sources_effective_then 单独回答。
        for source_id, scope in self.kb.scopes.items():
            if not any(scope.matches(ticket) for ticket in tickets):
                continue
            row = self.kb.source(source_id)
            effective = parse_dt(row.get("effective"))
            if moment and effective and effective > moment:
                continue
            relevant_source_ids.add(source_id)
        for ticket in tickets:
            relevant_source_ids.update(self.source_ids_for_piece(ticket, moment))
        source_register = [self.source_snapshot(source_id) for source_id in sorted(relevant_source_ids)]
        return {
            "assembly": assembly_id,
            "as_of": iso(moment),
            "source_register": source_register,
            "decision_log": self.decision_book(moment)["log"],
            "pieces": piece_traces,
            "handoff": handoff,
        }

    def source_snapshot(self, source_id: str) -> dict[str, Any]:
        row = self.kb.source(source_id)
        scope = self.kb.scopes.get(source_id)
        if scope is None and source_id in self.kb.cards_by_id:
            scope = self.kb.scope_for_card(row)
        return {
            "source_id": source_id,
            "revision": row.get("card") or row.get("id"),
            "authority": row.get("authority"),
            "effective": row.get("effective"),
            "parent": row.get("parent"),
            "text": row.get("text") or row.get("handoff"),
            "scope": scope.to_dict() if scope else None,
        }


def load_workshop(directory: Path = DEFAULT_DATA) -> Workshop:
    return Workshop.load(Path(directory))
