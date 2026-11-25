from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from pack_check import DEFAULT_DATA

from .engine import Workshop, STATUS_LABELS_ZH


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="minutes")
    return value


def print_json(value: Any) -> None:
    json.dump(to_jsonable(value), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def render_board(workshop: Workshop, at: str | None) -> None:
    board = workshop.board(at)
    print(f"资料回放时刻：{board['as_of']}")
    open_ambiguities = [
        state for state in board["ambiguities"].values() if state["status"] in {"open", "withdrawn"}
    ]
    if open_ambiguities:
        print("\n待工艺员确认的歧义：")
        for state in open_ambiguities:
            amb = state["ambiguity"]
            print(f"- {amb['id']}（{state['status']}，来源修订 {amb['source_revision']}）")
            print(f"  {amb['question']}")

    for assembly in board["assemblies"]:
        handoff = assembly["handoff"]
        print(f"\n构件 {assembly['assembly']}  必需件 {', '.join(handoff['required_piece_ids'])}")
        scope = handoff["minimum_affected_scope"]
        if scope:
            print(
                "  交接检验冲突最小范围："
                + ", ".join(scope)
                + "；可继续的未受影响分件："
                + ", ".join(handoff["unaffected_piece_ids"])
            )
        for piece in assembly["pieces"]:
            if piece["blocked"]:
                flag = "暂缓"
            elif any(order["status"] == "in_progress" for order in piece["orders"]):
                flag = "进行中"
            else:
                flag = "可执行"
            print(f"  [{flag}] {piece['piece_id']} {piece['part']} / {piece['station']}")
            nxt = piece["executable_next"]
            if nxt:
                material = f"（材料：{nxt['material']}）" if nxt.get("material") else ""
                print(f"      下一道：{nxt['title']}{material} — {nxt['status_zh']} [{nxt['id']}]")
                for reason in nxt.get("reasons", []):
                    print(f"        · {reason['message']}")
            else:
                active = piece["active_orders"]
                if active:
                    order = active[0]
                    print(f"      当前单：{order['title']} — {order['status_zh']} [{order['id']}]")
            for hold in piece["hold_reasons"]:
                print(f"      暂缓缘由：{hold['instruction']}")
                for reason in hold["reasons"]:
                    print(f"        - {reason['code']}: {reason['message']}")
            for deferred in piece.get("deferred_reasons", []):
                print(f"      后续提示：{deferred['code']}（{deferred.get('order_id')}）")
            for receipt in piece["receipt_readback"]:
                print(f"      领单回读 {receipt['event_id']} {receipt['at']}：{receipt['verbatim_detail']}")
        print(f"  合流结论：{'可合流' if handoff['merge_allowed'] else '暂不可合流'}")


def render_piece(workshop: Workshop, piece_id: str, at: str | None) -> None:
    result = workshop.piece_board(piece_id, at)
    print_json(result)


def render_history(workshop: Workshop, piece_id: str, at: str | None) -> None:
    result = {
        "piece_id": piece_id,
        "receipts": workshop.piece_board(piece_id, at)["receipt_readback"],
        "orders": [
            workshop.order_history(order["id"], at)
            for order in workshop.build_orders(piece_id, at)
            if order["id"].startswith("ORD-")
        ],
    }
    print_json(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预制件车间在制件作业许可与后续作业单")
    parser.add_argument("command", nargs="?", default="board", choices=["board", "piece", "trace", "handoff", "history"])
    parser.add_argument("target", nargs="?", help="物件号（piece/history）或构件号（trace/handoff）")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--at", default=None, help="回放时刻 ISO 时间；默认取资料中最新时间")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    workshop = Workshop.load(args.directory)
    if args.command == "board":
        if args.json:
            print_json(workshop.board(args.at))
        else:
            render_board(workshop, args.at)
        return 0
    if not args.target:
        parser.error(f"{args.command} 需要提供物件号或构件号")
    if args.command == "piece":
        result = workshop.piece_board(args.target, args.at)
    elif args.command == "history":
        render_history(workshop, args.target, args.at)
        return 0
    elif args.command == "trace":
        result = workshop.quality_trace(args.target, args.at)
    else:
        result = workshop.handoff_check(args.target, args.at)
    if args.json:
        print_json(result)
    else:
        print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
