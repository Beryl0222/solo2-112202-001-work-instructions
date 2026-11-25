"""端到端业务规则测试：作用域、进度、许可、歧义撤回、合流和追溯。"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime

from pack_check import read_tables
from workshop.engine import (
    STATUS_AWAIT_CONFIRMATION,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_ENGINEER,
    STATUS_NO_NEXT,
    STATUS_READY,
    Workshop,
)


def fresh_workshop() -> Workshop:
    return Workshop(copy.deepcopy(read_tables()))


class ScopeAndInterpretationTest(unittest.TestCase):
    def setUp(self):
        self.workshop = fresh_workshop()

    def test_note_inherits_station_and_family_and_does_not_cross_line(self):
        scope = self.workshop.kb.scopes["P11"]
        self.assertEqual("北线", scope.station)
        self.assertEqual("K2", scope.family)
        self.assertTrue(self.workshop.permit_covers_piece("P11", "K21", "2026-10-06T10:05"))
        self.assertFalse(self.workshop.permit_covers_piece("P11", "K25", "2026-10-06T10:05"))
        self.assertFalse(self.workshop.permit_covers_piece("P11", "K24", "2026-10-06T10:05"))

    def test_edge_phrase_creates_concrete_ambiguity(self):
        ambiguities = self.workshop.ambiguities()
        self.assertEqual(1, len(ambiguities))
        ambiguity = ambiguities[0]
        self.assertEqual("P11", ambiguity.source_id)
        self.assertEqual("P-r1", ambiguity.source_revision)
        self.assertEqual(["K21"], ambiguity.explicit_piece_ids)
        option_pieces = {option.key: set(option.piece_ids) for option in ambiguity.options}
        self.assertEqual({"K21"}, option_pieces["named_only"])
        self.assertIn("K24", option_pieces["include_edge_descriptors"])
        self.assertNotIn("K25", option_pieces["include_edge_descriptors"])

    def test_review_confirmation_and_withdrawal_replay(self):
        confirmed = self.workshop.decision_book("2026-10-06T10:30")
        state = next(iter(confirmed["ambiguities"].values()))
        self.assertEqual("confirmed", state["status"])
        self.assertEqual("named_only", state["selected_option"]["key"])
        self.assertEqual("P-r1", state["source_revision"])

        withdrawn = self.workshop.decision_book("2026-10-06T10:50")
        state = next(iter(withdrawn["ambiguities"].values()))
        self.assertEqual("withdrawn", state["status"])
        self.assertEqual("W07", state["withdraw_event_id"])

    def test_quan_dan_expands_scope(self):
        scope = self.workshop.kb.scope_for_card(
            {"id": "X99", "parent": "", "text": "全单临时检验要求", "card": "X-r1"}
        )
        self.assertTrue(scope.all_order)
        self.assertTrue(scope.matches({"station": "南线", "assembly": "OTHER-1", "part": "任意件"}))


class FixtureBoardTest(unittest.TestCase):
    def setUp(self):
        self.workshop = fresh_workshop()
        self.board = self.workshop.board("2026-10-06T10:50")
        self.pieces = {
            piece["piece_id"]: piece
            for assembly in self.board["assemblies"]
            for piece in assembly["pieces"]
        }

    def test_pickup_without_coating_must_follow_new_revision(self):
        piece = self.workshop.piece_board("K21", "2026-10-06T09:01")
        self.assertFalse(piece["blocked"])
        self.assertEqual("乙", piece["executable_next"]["material"])
        codes = [reason["code"] for reason in piece["executable_next"]["reasons"]]
        self.assertIn("PICKUP_IS_MATERIAL_FACT_ONLY", codes)

    def test_completed_first_pass_continues_old_material(self):
        k22 = self.pieces["K22"]
        self.assertFalse(k22["blocked"])
        self.assertEqual("ORD-K22-primer_pass_2", k22["executable_next"]["id"])
        self.assertEqual("甲", k22["executable_next"]["material"])
        self.assertEqual(STATUS_READY, k22["executable_next"]["status"])
        first_pass = next(order for order in k22["orders"] if order["step_key"] == "primer_pass_1")
        self.assertEqual(STATUS_COMPLETED, first_pass["status"])
        self.assertEqual("甲", first_pass["material"])

    def test_started_order_after_withdrawal_needs_engineer_disposition(self):
        k21 = self.pieces["K21"]
        self.assertTrue(k21["blocked"])
        order = next(order for order in k21["orders"] if order["step_key"] == "primer_pass_2")
        self.assertEqual(STATUS_ENGINEER, order["status"])
        self.assertEqual("W06", order["evidence_event_id"])
        codes = [reason["code"] for reason in order["reasons"]]
        self.assertIn("STARTED_ORDER_ENGINEER_DISPOSITION", codes)
        evidence_ids = {item["event_id"] for item in k21["completion_evidence"]}
        self.assertIn("W05", evidence_ids)

    def test_claimed_unstarted_order_and_verbatim_hold_receipt_remain(self):
        k23 = self.pieces["K23"]
        claimed = next(order for order in k23["orders"] if order["step_key"] == "primer_pass_1")
        self.assertEqual(STATUS_CLAIMED, claimed["status"])
        self.assertEqual("乙", claimed["material"])
        receipts = {item["event_id"]: item["verbatim_detail"] for item in k23["receipt_readback"]}
        self.assertEqual("底涂首遍", receipts["W03"])
        self.assertEqual("等待用料确认", receipts["W08"])
        self.assertTrue(k23["blocked"])
        self.assertEqual("W08", k23["hold_reasons"][0]["reasons"][0]["event_id"])

    def test_unaffected_edge_piece_can_start_but_optional_route_is_held(self):
        k24 = self.pieces["K24"]
        self.assertFalse(k24["blocked"])
        self.assertEqual("乙", k24["executable_next"]["material"])
        self.assertEqual("primer_pass_1", k24["executable_next"]["step_key"])
        old_route = next(order for order in k24["orders"] if order["step_key"] == "old_material_pass_2")
        self.assertEqual(STATUS_AWAIT_CONFIRMATION, old_route["status"])
        hold = next(hold for hold in k24["hold_reasons"] if hold["order_id"] == old_route["id"])
        self.assertFalse(hold["blocks_execution"])

    def test_south_line_cannot_borrow_north_permit(self):
        k25 = self.pieces["K25"]
        self.assertFalse(k25["blocked"])
        self.assertEqual("甲", k25["executable_next"]["material"])
        self.assertFalse(self.workshop.permit_covers_piece("P11", "K25", "2026-10-06T11:00"))


class OrderUpdateTest(unittest.TestCase):
    def test_unclaimed_claimed_and_started_orders_follow_withdrawal(self):
        workshop = fresh_workshop()
        workshop.record_event("K24", "完工", "乙，首遍", at="2026-10-06T09:20")

        before = workshop.order_history("ORD-K24-primer_pass_2", "2026-10-06T09:21")
        self.assertEqual(STATUS_READY, before["versions"][-1]["status"])

        workshop.confirm_interpretation(
            "P11", option_key="include_edge_descriptors", at="2026-10-06T09:22"
        )
        workshop.record_event("K24", "领单", "乙底涂第二遍", at="2026-10-06T09:23")
        claimed = workshop.order_history("ORD-K24-primer_pass_2", "2026-10-06T09:23")
        self.assertEqual(STATUS_CLAIMED, claimed["versions"][-1]["status"])

        workshop.record_event("K24", "开工", "第二遍", at="2026-10-06T09:23:30")
        workshop.withdraw_confirmation("P11", at="2026-10-06T09:24")
        withdrawn = workshop.order_history("ORD-K24-primer_pass_2", "2026-10-06T09:25")
        latest = withdrawn["versions"][-1]
        self.assertEqual(STATUS_ENGINEER, latest["status"])
        self.assertIn("STARTED_ORDER_ENGINEER_DISPOSITION", latest["reason_codes"])
        self.assertGreaterEqual(latest["revision"], 3)

        piece = workshop.piece_board("K24", "2026-10-06T09:25")
        order = next(order for order in piece["orders"] if order["step_key"] == "primer_pass_2")
        self.assertEqual(STATUS_ENGINEER, order["status"])

    def test_claimed_unstarted_order_is_updated_by_withdrawal(self):
        workshop = fresh_workshop()
        workshop.record_event("K24", "完工", "乙，首遍", at="2026-10-06T09:20")
        workshop.confirm_interpretation(
            "P11", option_key="include_edge_descriptors", at="2026-10-06T09:21"
        )
        workshop.record_event("K24", "领单", "乙底涂第二遍", at="2026-10-06T09:22")
        workshop.withdraw_confirmation("P11", at="2026-10-06T09:23")

        history = workshop.order_history("ORD-K24-primer_pass_2", "2026-10-06T09:24")
        latest = history["versions"][-1]
        self.assertEqual(STATUS_CLAIMED, latest["status"])
        self.assertIn("INTERPRETATION_WITHDRAWN_UPDATE", latest["reason_codes"])
        receipts = workshop.piece_board("K24", "2026-10-06T09:24")["receipt_readback"]
        self.assertTrue(any(receipt["verbatim_detail"] == "乙底涂第二遍" for receipt in receipts))


class CompatibilityAndPermitHistoryTest(unittest.TestCase):
    def test_unknown_completed_material_keeps_evidence_and_pauses(self):
        workshop = fresh_workshop()
        workshop.record_event("K24", "完工", "丙，首遍", at="2026-10-06T10:00")
        piece = workshop.piece_board("K24", "2026-10-06T10:05")
        self.assertTrue(piece["blocked"])
        no_next = next(order for order in piece["orders"] if order["status"] == STATUS_NO_NEXT)
        self.assertEqual("primer_pass_2", no_next["step_key"])
        self.assertTrue(no_next["evidence_event_id"])
        self.assertIn("保留", no_next["instruction"])

    def test_later_expiry_does_not_make_registered_use_retroactively_unlawful(self):
        tables = copy.deepcopy(read_tables())
        tables["events.tsv"] = [row for row in tables["events.tsv"] if row["id"] != "W07"]
        workshop = Workshop(tables)
        workshop.record_event("K21", "完工", "旧料，第二遍", at="2026-10-06T11:30")

        opinion = workshop.permit_opinion(
            "P11",
            "K21",
            operation_at="2026-10-06T11:30",
            observed_at="2026-10-06T12:30",
        )
        self.assertEqual("active", opinion["validity_at_operation"]["status"])
        self.assertEqual("expired", opinion["validity_at_observation"]["status"])
        self.assertTrue(opinion["lawful_at_operation"])
        self.assertTrue(opinion["retroactive_violation_forbidden"])


class HandoffAndTraceTest(unittest.TestCase):
    def test_inspection_conflict_has_minimal_piece_scope(self):
        workshop = fresh_workshop()
        handoff = workshop.handoff_check("K2-87", "2026-10-06T10:50")
        self.assertEqual(["K23"], handoff["minimum_affected_scope"])
        self.assertEqual(["K21", "K22"], handoff["unaffected_piece_ids"])
        conflict = handoff["inspection_conflicts"][0]
        self.assertIn("P21", conflict["exception_source_ids"])
        self.assertIn("B11", conflict["general_requirement_source_ids"])
        p21_coverage = next(item for item in handoff["permit_coverage"]["permits"] if item["source_id"] == "P21")
        self.assertFalse(p21_coverage["covers_all_required"])
        self.assertFalse(workshop.permit_covers_assembly("P21", "K2-87", "2026-10-06T10:50"))
        presented = workshop.permit_set_covers_assembly(
            ["P11", "P21"], "K2-87", "2026-10-06T10:50"
        )
        self.assertTrue(presented["covers_all_required"])
        self.assertEqual([], presented["missing_piece_ids"])
        coverage = handoff["permit_coverage"]
        self.assertTrue(coverage["collective_covers_all_required"])
        self.assertEqual([], coverage["missing_permit_coverage"])

    def test_complete_assembly_can_pass(self):
        workshop = fresh_workshop()
        # 移除资料包中导致 K21 已开工单进入工艺处置的撤回事件，构造一个已完成合流场景。
        workshop.tables["events.tsv"] = [row for row in workshop.tables["events.tsv"] if row["id"] != "W07"]
        completions = [
            ("K21", "乙，首遍", "2026-10-06T09:20"),
            ("K21", "乙，第二遍", "2026-10-06T09:50"),
            ("K21", "膜厚检查完成", "2026-10-06T10:00"),
            ("K22", "甲，首遍", "2026-10-06T08:30"),
            ("K22", "甲，第二遍", "2026-10-06T09:40"),
            ("K22", "膜厚检查完成", "2026-10-06T10:10"),
            ("K23", "乙，首遍", "2026-10-06T09:40"),
            ("K23", "乙，第二遍", "2026-10-06T10:00"),
        ]
        for piece, detail, at in completions:
            workshop.record_event(piece, "完工", detail, at=at)
        workshop.record_event("K23", "检验", "合流试配完成", at="2026-10-06T10:30")
        handoff = workshop.handoff_check("K2-87", "2026-10-06T10:31")
        self.assertEqual([], handoff["blockers"])
        self.assertTrue(handoff["ready_for_handoff"])

    def test_qc_trace_returns_historical_original_sentences(self):
        workshop = fresh_workshop()
        trace = workshop.quality_trace("K2-87", "2026-10-06T10:50")
        k22 = next(piece for piece in trace["pieces"] if piece["piece_id"] == "K22")
        w02 = next(record for record in k22["records"] if record["id"] == "W02")
        self.assertTrue(any("甲底涂双遍" in sentence for sentence in w02["source_sentences_then"]))
        self.assertFalse(any("底涂改乙" in sentence for sentence in w02["source_sentences_then"]))

        k21 = next(piece for piece in trace["pieces"] if piece["piece_id"] == "K21")
        w05 = next(record for record in k21["records"] if record["id"] == "W05")
        self.assertTrue(any("领料未涂的按新版" in sentence for sentence in w05["source_sentences_then"]))
        source_texts = {source["source_id"]: source["text"] for source in trace["source_register"]}
        self.assertIn("合流仍逐件测膜厚", source_texts["B11"])
        self.assertIn("连接件可随框板做合流试配", source_texts["P21"])

    def test_engineer_rejects_permit_prospectively(self):
        workshop = fresh_workshop()
        workshop.reject_permit("P21", at="2026-10-06T10:00", reason="对象不清")
        validity = workshop.permit_validity("P21", workshop._resolve_at("2026-10-06T10:05"))
        self.assertEqual("rejected", validity["status"])
        handoff = workshop.handoff_check("K2-87", "2026-10-06T10:05")
        self.assertEqual([], handoff["inspection_conflicts"])
        k23 = workshop.piece_board("K23", "2026-10-06T10:05")
        self.assertFalse(any(order["step_key"] == "trial_fit" for order in k23["orders"]))

    def test_failed_inspection_remains_open_until_passing_record(self):
        workshop = fresh_workshop()
        workshop.record_event("K22", "完工", "甲，第二遍", at="2026-10-06T09:40")
        workshop.record_event("K22", "检验", "膜厚不合格", at="2026-10-06T10:00")
        failed = workshop.piece_board("K22", "2026-10-06T10:01")
        film = next(order for order in failed["orders"] if order["step_key"] == "film_thickness")
        self.assertEqual("inspection_failed", film["status"])
        self.assertTrue(failed["blocked"])

        workshop.record_event("K22", "检验", "膜厚复测合格", at="2026-10-06T10:20")
        passed = workshop.piece_board("K22", "2026-10-06T10:21")
        film = next(order for order in passed["orders"] if order["step_key"] == "film_thickness")
        self.assertEqual(STATUS_COMPLETED, film["status"])
        self.assertFalse(passed["blocked"])

    def test_negative_scope_rules_remain_visible(self):
        workshop = fresh_workshop()
        k22 = workshop.piece_board("K22", "2026-10-06T10:50")
        texts = " ".join(item["text"] for item in k22["constraints"])
        self.assertIn("旧料不能用于门板", texts)
        k25 = workshop.piece_board("K25", "2026-10-06T10:50")
        texts = " ".join(item["text"] for item in k25["constraints"])
        self.assertIn("不可用北线临时许可", texts)


if __name__ == "__main__":
    unittest.main()
