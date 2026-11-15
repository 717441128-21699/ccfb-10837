"""分析服务：给网页与核对脚本提供统一的只读分析接口。"""
from __future__ import annotations

import sqlite3

from ..config import SETTINGS
from ..db import connect
from .controller import determine_controller, normalize_rules
from .graph import (EquityGraph, build_graph, find_cycles,
                    reachable_subgraph, ultimate_ownership)
from .history import event_timeline, replay_events


class AnalysisService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or SETTINGS.warehouse_db

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path, readonly=True)

    # ------------------------------------------------------------- 规则
    @staticmethod
    def active_rules(conn: sqlite3.Connection) -> dict:
        row = conn.execute(
            "SELECT rules_json FROM rule_configs WHERE is_active=1 "
            "ORDER BY config_id DESC LIMIT 1").fetchone()
        if not row:
            return normalize_rules(None)
        import json
        return normalize_rules(json.loads(row["rules_json"]))

    # ------------------------------------------------------------- 构图
    def graph_for_company(self, company_id: str, *,
                          as_of: str | None = None,
                          rules: dict | None = None,
                          max_depth: int | None = None) -> tuple[EquityGraph, set[str]]:
        conn = self._conn()
        try:
            rules = normalize_rules(rules or self.active_rules(conn))
            full = build_graph(conn, as_of=as_of,
                               follow_nominee=rules["follow_nominee"])
            if company_id not in full.companies:
                raise KeyError(company_id)
            nodes = reachable_subgraph(full, company_id, max_depth)
            return full, nodes
        finally:
            conn.close()

    # ----------------------------------------------------------- 股权图数据
    def equity_tree(self, company_id: str, *, max_depth: int | None = None,
                    as_of: str | None = None,
                    rules: dict | None = None) -> dict:
        """返回前端画图所需的 nodes/edges（含环标记与穿透比例）。"""
        g, nodes = self.graph_for_company(company_id, as_of=as_of, rules=rules)
        conn = self._conn()
        try:
            node_map = {}
            edges = []
            for cid in sorted(nodes):
                comp = g.companies.get(cid, {})
                node_map[cid] = {
                    "id": cid, "kind": "company",
                    "name": comp.get("name", cid),
                    "credit_code": comp.get("credit_code"),
                    "industry": comp.get("industry"),
                }
                for s in g.stakes.get(cid, []):
                    if s.holder_kind == "company" and s.holder_id in nodes:
                        edges.append({
                            "source": s.holder_id, "target": cid,
                            "shares_pct": round(s.shares_pct, 4),
                            "voting_pct": round(s.voting_pct, 4),
                            "is_nominee": bool(s.is_nominee),
                        })
                    elif s.holder_kind == "person":
                        pid = f"person:{s.holder_id}"
                        if pid not in node_map:
                            p = g.persons.get(s.holder_id, {})
                            node_map[pid] = {
                                "id": pid, "kind": "person",
                                "name": p.get("name", s.holder_id),
                                "id_tail": p.get("id_tail", ""),
                            }
                        edges.append({
                            "source": pid, "target": cid,
                            "shares_pct": round(s.shares_pct, 4),
                            "voting_pct": round(s.voting_pct, 4),
                            "is_nominee": bool(s.is_nominee),
                        })
            cycles = find_cycles(g, nodes)
            return {
                "root": company_id,
                "as_of": as_of,
                "nodes": list(node_map.values()),
                "edges": edges,
                "cycles": cycles,
                "node_count": len(node_map),
                "edge_count": len(edges),
            }
        finally:
            conn.close()

    def ultimate_beneficiaries(self, company_id: str, *,
                               as_of: str | None = None,
                               rules: dict | None = None) -> dict:
        g, _ = self.graph_for_company(company_id, as_of=as_of, rules=rules)
        shares = ultimate_ownership(g, company_id, mode="shares")
        voting = ultimate_ownership(g, company_id, mode="voting")
        rows = []
        for pid in sorted(set(shares) | set(voting),
                          key=lambda p: -voting.get(p, 0.0)):
            p = g.persons.get(pid, {})
            rows.append({
                "person_id": pid,
                "name": p.get("name", pid),
                "id_tail": p.get("id_tail", ""),
                "ultimate_shares_pct": round(shares.get(pid, 0.0), 4),
                "ultimate_voting_pct": round(voting.get(pid, 0.0), 4),
            })
        return {"company_id": company_id, "as_of": as_of, "beneficiaries": rows}

    # ------------------------------------------------------------- 实控人
    def controller_of(self, company_id: str, *, rules: dict | None = None,
                      as_of: str | None = None) -> dict:
        conn = self._conn()
        try:
            rules = normalize_rules(rules or self.active_rules(conn))
            g = build_graph(conn, as_of=as_of,
                            follow_nominee=rules["follow_nominee"])
            if company_id not in g.companies:
                raise KeyError(company_id)
            result = determine_controller(g, company_id, rules)
            # 补人名
            for step in result.chain:
                if step["holder_kind"] == "person":
                    step["holder_name"] = g.persons.get(
                        step["holder_id"], {}).get("name", step["holder_id"])
                else:
                    step["holder_name"] = g.companies.get(
                        step["holder_id"], {}).get("name", step["holder_id"])
            out = result.to_dict()
            out["company_name"] = g.companies[company_id].get("name")
            out["controller_names"] = [
                g.persons.get(pid, {}).get("name", pid)
                if pid in g.persons else
                g.companies.get(pid, {}).get("name", pid)
                for pid in result.controller_ids]
            return out
        finally:
            conn.close()

    def all_controllers(self, *, rules: dict | None = None,
                        limit: int | None = None) -> list[dict]:
        conn = self._conn()
        try:
            rules = normalize_rules(rules or self.active_rules(conn))
            g = build_graph(conn, follow_nominee=rules["follow_nominee"])
            out = []
            ids = sorted(g.companies)
            if limit:
                ids = ids[:limit]
            for cid in ids:
                r = determine_controller(g, cid, rules)
                out.append({
                    "company_id": cid,
                    "company_name": g.companies[cid].get("name"),
                    "control_type": r.control_type,
                    "confidence": r.confidence,
                    "controller_ids": r.controller_ids,
                    "controller_names": [
                        g.persons.get(p, {}).get("name", p)
                        if p in g.persons else
                        g.companies.get(p, {}).get("name", p)
                        for p in r.controller_ids],
                    "rationale": r.rationale,
                })
            return out
        finally:
            conn.close()

    # ------------------------------------------------------------- 历史
    def history_timeline(self, company_id: str) -> dict:
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT * FROM companies WHERE company_id=?",
                (company_id,)).fetchone()
            if not company:
                raise KeyError(company_id)
            evs = conn.execute(
                "SELECT * FROM holding_events_raw WHERE company_id=? "
                "ORDER BY event_date, change_no", (company_id,)).fetchall()
            return {
                "company_id": company_id,
                "company_name": company["name"],
                "change_dates": event_timeline([dict(r) for r in evs]),
                "events": [dict(r) for r in evs],
            }
        finally:
            conn.close()

    def cap_table_as_of(self, company_id: str, as_of: str) -> dict:
        """任意一天的股东结构回放（需求 7）。"""
        conn = self._conn()
        try:
            company = conn.execute(
                "SELECT name FROM companies WHERE company_id=?",
                (company_id,)).fetchone()
            if not company:
                raise KeyError(company_id)
            evs = conn.execute(
                "SELECT * FROM holding_events_raw WHERE company_id=? "
                "ORDER BY event_date, change_no", (company_id,)).fetchall()
            if not evs:
                # 没有变更记录的公司：只能给当前快照
                cur = conn.execute(
                    "SELECT * FROM holdings_raw WHERE company_id=?",
                    (company_id,)).fetchall()
                return {"company_id": company_id, "as_of": as_of,
                        "approximate": True,
                        "holders": [dict(r) for r in cur]}
            holders = replay_events([dict(r) for r in evs], as_of)
            return {"company_id": company_id, "as_of": as_of,
                    "approximate": False, "holders": holders}
        finally:
            conn.close()
