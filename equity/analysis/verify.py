"""实控人核对（需求验收）：引擎结果 vs 仿真站预设答案，逐家比对。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import SETTINGS
from ..db import connect
from .service import AnalysisService


@dataclass
class VerifyDetail:
    company_id: str
    company_name: str
    preset_type: str
    preset_persons: list[str]
    computed_type: str
    computed_persons: list[str]
    match: bool
    reason: str = ""


@dataclass
class VerifyReport:
    total: int = 0
    matched: int = 0
    mismatched: int = 0
    unknown: int = 0
    details: list[VerifyDetail] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        decided = self.matched + self.mismatched
        return self.matched / decided if decided else 0.0


def _preset_map(sim_conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in sim_conn.execute(
            "SELECT * FROM preset_controllers ORDER BY company_id, rank_no"):
        slot = out.setdefault(r["company_id"], {
            "type": r["controller_type"], "persons": [],
            "rationale": r["rationale"]})
        if r["controller_id"]:
            slot["persons"].append(r["controller_id"])
    return out


def _classify(computed: dict) -> str:
    """把引擎的 control_type 归一到预设口径 person/joint/none。"""
    t = computed["control_type"]
    if t in ("sole", "relative"):
        return "person"
    if t == "joint":
        return "joint"
    return "none"


def run_verification(*, rules: dict | None = None,
                     save: bool = True,
                     limit: int | None = None) -> VerifyReport:
    sim = connect(SETTINGS.sim_db, readonly=True)
    preset = _preset_map(sim)
    sim.close()

    conn = connect(SETTINGS.warehouse_db, readonly=True)
    try:
        sql = "SELECT company_id, name FROM companies ORDER BY company_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        company_rows = conn.execute(sql).fetchall()
        active_rules = rules or AnalysisService.active_rules(conn)
        # 全库只构一次图，所有公司复用，避免 3000 次重复建图
        from .graph import build_graph
        from .controller import determine_controller
        graph = build_graph(conn, follow_nominee=active_rules["follow_nominee"])
    finally:
        conn.close()

    report = VerifyReport()
    for row in company_rows:
        cid = row["company_id"]
        report.total += 1
        p = preset.get(cid)
        if p is None:
            report.unknown += 1
            report.details.append(VerifyDetail(
                cid, row["name"], "?", [], "?", [], False,
                "仿真站无预设答案"))
            continue
        try:
            result = determine_controller(graph, cid, active_rules)
            computed = {
                "control_type": result.control_type,
                "controller_ids": result.controller_ids,
            }
        except Exception as exc:  # 单家失败不拖垮整体
            report.unknown += 1
            report.details.append(VerifyDetail(
                cid, row["name"], p["type"], p["persons"], "error", [],
                False, repr(exc)))
            continue

        computed_type = _classify(computed)
        computed_persons = computed["controller_ids"]
        preset_persons = sorted(p["persons"])

        if p["type"] == "none":
            match = computed_type == "none"
            reason = "预设无控制，引擎也判无控制" if match else \
                f"预设无控制，引擎判 {computed_type}"
        else:
            same_type = computed_type == p["type"]
            same_persons = sorted(computed_persons) == preset_persons
            match = same_type and same_persons
            if match:
                reason = "控制类型与控制人完全一致"
            elif same_type and not same_persons:
                reason = (f"类型一致但控制人不同：预设 {preset_persons} "
                          f"vs 引擎 {sorted(computed_persons)}")
            else:
                reason = (f"类型不同：预设 {p['type']} vs 引擎 {computed_type}")

        if match:
            report.matched += 1
        else:
            report.mismatched += 1
        report.details.append(VerifyDetail(
            cid, row["name"], p["type"], preset_persons,
            computed_type, sorted(computed_persons), match, reason))

    if save:
        _persist(report)
    return report


def _persist(report: VerifyReport) -> int:
    conn = connect(SETTINGS.warehouse_db)
    detail_json = json.dumps(
        [d.__dict__ for d in report.details], ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO verify_reports(created_at,scope,total,matched,"
        "mismatched,unknown,detail_json) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "all", report.total, report.matched, report.mismatched,
         report.unknown, detail_json))
    conn.commit()
    conn.close()
    return cur.lastrowid


def print_report(report: VerifyReport, *, show_mismatches: int = 30) -> None:
    print("=" * 60)
    print(f"核对企业数: {report.total}")
    print(f"  一致: {report.matched}")
    print(f"  不一致: {report.mismatched}")
    print(f"  无答案/异常: {report.unknown}")
    decided = report.matched + report.mismatched
    if decided:
        print(f"  准确率: {report.accuracy:.2%}")
    print("=" * 60)
    bad = [d for d in report.details if not d.match]
    for d in bad[:show_mismatches]:
        print(f"[不一致] {d.company_id} {d.company_name}")
        print(f"    预设: {d.preset_type} {d.preset_persons}")
        print(f"    引擎: {d.computed_type} {d.computed_persons}")
        print(f"    原因: {d.reason}")
    if len(bad) > show_mismatches:
        print(f"... 其余 {len(bad) - show_mismatches} 条见 verify_reports 表")
