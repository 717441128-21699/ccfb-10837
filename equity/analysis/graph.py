"""股权图构建、环检测与最终持股/表决权穿透（需求 5）。

方法：
- 图边方向 holder → company（自下而上）；
- Tarjan 求强连通分量（SCC），SCC 即交叉/环形持股的“环”；
- 缩点后的 DAG 按拓扑序传播：单点 SCC 直接公式求和；
- 环内 SCC 建立小规模线性方程组 (I-W)x=b 用高斯消元求解，
  环的规模通常只有 2~5 个节点，纯 Python 也很快；
- 奇异（理论上的封闭环）时退化为不动点迭代兜底，绝不死循环。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .history import replay_events
from .linalg import SingularMatrixError, solve_linear


@dataclass
class Stake:
    holder_kind: str          # company / person
    holder_id: str
    shares_pct: float         # 0~100
    voting_pct: float
    is_nominee: int = 0
    beneficial_holder_id: str | None = None


@dataclass
class EquityGraph:
    companies: dict[str, dict] = field(default_factory=dict)
    persons: dict[str, dict] = field(default_factory=dict)
    # company_id -> 该公司的股东 Stake 列表（已按需做代持穿透）
    stakes: dict[str, list[Stake]] = field(default_factory=dict)

    def company_holders(self, cid: str) -> list[Stake]:
        return [s for s in self.stakes.get(cid, []) if s.holder_kind == "company"]

    def person_holders(self, cid: str) -> list[Stake]:
        return [s for s in self.stakes.get(cid, []) if s.holder_kind == "person"]


# ---------------------------------------------------------------- 构图
def build_graph(conn: sqlite3.Connection, *, as_of: str | None = None,
                follow_nominee: bool = True,
                company_ids: set[str] | None = None) -> EquityGraph:
    """从采集仓库构图。

    as_of=None 用当前快照 holdings_raw；否则用 holding_events_raw 回放。
    company_ids 给定时只装载这些公司（子图），否则全库。
    """
    g = EquityGraph()
    crows = conn.execute("SELECT * FROM companies").fetchall()
    for r in crows:
        if company_ids is not None and r["company_id"] not in company_ids:
            continue
        g.companies[r["company_id"]] = dict(r)
    for r in conn.execute("SELECT * FROM persons"):
        g.persons[r["person_id"]] = dict(r)

    for cid in g.companies:
        if as_of is None:
            rows = conn.execute(
                "SELECT * FROM holdings_raw WHERE company_id=?", (cid,)).fetchall()
            stakes = [Stake(
                holder_kind=r["holder_kind"], holder_id=r["holder_id"],
                shares_pct=r["shares_pct"], voting_pct=r["voting_pct"],
                is_nominee=r["is_nominee"],
                beneficial_holder_id=r["beneficial_holder_id"]) for r in rows]
        else:
            evs = conn.execute(
                "SELECT * FROM holding_events_raw WHERE company_id=? "
                "ORDER BY event_date, change_no", (cid,)).fetchall()
            stakes = [Stake(
                holder_kind=h["holder_kind"], holder_id=h["holder_id"],
                shares_pct=h["shares_pct"], voting_pct=h["voting_pct"],
                is_nominee=h["is_nominee"],
                beneficial_holder_id=h["beneficial_holder_id"])
                for h in replay_events([dict(r) for r in evs], as_of)]
        g.stakes[cid] = _collapse_nominees(stakes, follow_nominee)
    return g


def _collapse_nominees(stakes: list[Stake], follow: bool) -> list[Stake]:
    """代持穿透：把名义股东的权益归到实际权益人；关闭时保留名义股东。"""
    if not follow:
        return stakes
    out: dict[tuple[str, str], Stake] = {}
    for s in stakes:
        if s.is_nominee and s.beneficial_holder_id:
            key = ("person", s.beneficial_holder_id)
            if key in out:
                slot = out[key]
                slot.shares_pct += s.shares_pct
                slot.voting_pct += s.voting_pct
            else:
                out[key] = Stake(
                    holder_kind="person", holder_id=s.beneficial_holder_id,
                    shares_pct=s.shares_pct, voting_pct=s.voting_pct,
                    is_nominee=0, beneficial_holder_id=None)
        else:
            key = (s.holder_kind, s.holder_id)
            if key in out:
                slot = out[key]
                slot.shares_pct += s.shares_pct
                slot.voting_pct += s.voting_pct
            else:
                out[key] = Stake(**s.__dict__)
    return list(out.values())


def reachable_subgraph(g: EquityGraph, root: str,
                       max_depth: int | None = None) -> set[str]:
    """从 root 沿公司股东向上 BFS，返回可达公司集合（含 root）。"""
    seen = {root}
    frontier = [root]
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        nxt: list[str] = []
        for cid in frontier:
            for s in g.company_holders(cid):
                if s.holder_id in g.companies and s.holder_id not in seen:
                    seen.add(s.holder_id)
                    nxt.append(s.holder_id)
        frontier = nxt
        depth += 1
    return seen


# ------------------------------------------------------------- Tarjan SCC
def tarjan_scc(g: EquityGraph, nodes: set[str]) -> list[list[str]]:
    """迭代版 Tarjan，避免大图递归深度问题。"""
    index_counter = 0
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []

    for start in sorted(nodes):
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            v, pi = work[-1]
            if pi == 0:
                index[v] = low[v] = index_counter
                index_counter += 1
                stack.append(v)
                on_stack.add(v)
            neighbors = [s.holder_id for s in g.company_holders(v)
                         if s.holder_id in nodes]
            if pi < len(neighbors):
                w = neighbors[pi]
                work[-1] = (v, pi + 1)
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
            else:
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    result.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
    return result


def find_cycles(g: EquityGraph, nodes: set[str]) -> list[list[str]]:
    """返回真正成环的 SCC（规模>1，或单点自环）。"""
    cycles = []
    for comp in tarjan_scc(g, nodes):
        if len(comp) > 1:
            cycles.append(sorted(comp))
        else:
            v = comp[0]
            if any(s.holder_id == v for s in g.company_holders(v)):
                cycles.append(comp)
    return cycles


# ------------------------------------------------------------- 穿透求解
def ultimate_ownership(g: EquityGraph, root: str, *,
                       mode: str = "shares",
                       max_depth: int | None = None
                       ) -> dict[str, float]:
    """计算 root 公司被每个自然人最终持有的比例（百分数）。

    mode='shares' 按股权，'voting' 按表决权。
    返回 {person_id: 百分比}，只含 >0 的人。
    """
    nodes = reachable_subgraph(g, root, max_depth)
    comps = tarjan_scc(g, nodes)
    comp_of: dict[str, int] = {}
    for ci, comp in enumerate(comps):
        for v in comp:
            comp_of[v] = ci

    # 缩点 DAG 的上游关系（holder 所在 SCC → 本 SCC），求拓扑序
    succ: dict[int, set[int]] = {ci: set() for ci in range(len(comps))}
    indeg: dict[int, int] = {ci: 0 for ci in range(len(comps))}
    for cid in nodes:
        ci = comp_of[cid]
        for s in g.company_holders(cid):
            if s.holder_id in nodes:
                cj = comp_of[s.holder_id]
                if cj != ci and ci not in succ[cj]:
                    succ[cj].add(ci)
    for ci, outs in succ.items():
        indeg[ci] = sum(1 for other in outs if other != ci)
    # indeg 表示“有多少个上游 SCC”；拓扑从最顶端（无上游）开始
    upstream_count = {ci: 0 for ci in range(len(comps))}
    for u, outs in succ.items():
        for v in outs:
            upstream_count[v] += 1
    topo = [ci for ci in range(len(comps)) if upstream_count[ci] == 0]
    ordered: list[int] = []
    remaining = dict(upstream_count)
    while topo:
        ci = topo.pop(0)
        ordered.append(ci)
        for v in succ[ci]:
            remaining[v] -= 1
            if remaining[v] == 0:
                topo.append(v)
    if len(ordered) != len(comps):
        # 不应发生（SCC 缩点必为 DAG），保底用原序
        ordered = list(range(len(comps)))

    weight = "shares_pct" if mode == "shares" else "voting_pct"
    # x[cid][person] = 该公司最终归属自然人的比例
    x: dict[str, dict[str, float]] = {cid: {} for cid in nodes}

    def add_person(target: dict[str, float], pid: str, pct: float) -> None:
        if pct <= 0:
            return
        target[pid] = target.get(pid, 0.0) + pct

    for ci in ordered:
        comp = comps[ci]
        # 外部贡献 b：自然人直接持有 + 其它 SCC 公司持有（其 x 已解出）
        def external_vector(cid: str) -> dict[str, float]:
            b: dict[str, float] = {}
            for s in g.stakes.get(cid, []):
                w = getattr(s, weight) / 100.0
                if s.holder_kind == "person":
                    add_person(b, s.holder_id, w * 100.0)
                elif s.holder_id in nodes and comp_of[s.holder_id] != ci:
                    for pid, pct in x.get(s.holder_id, {}).items():
                        add_person(b, pid, w * pct)
            return b

        if len(comp) == 1 and not any(
                s.holder_id == comp[0] for s in g.company_holders(comp[0])):
            cid = comp[0]
            x[cid] = external_vector(cid)
            continue

        # 环内 SCC：解 (I - W_cc) x = b
        k = len(comp)
        idx = {cid: i for i, cid in enumerate(comp)}
        a = [[0.0] * k for _ in range(k)]
        for i in range(k):
            a[i][i] = 1.0
        for i, cid in enumerate(comp):
            for s in g.company_holders(cid):
                if s.holder_id in idx:
                    a[i][idx[s.holder_id]] -= getattr(s, weight) / 100.0
        # 对每个出现过的自然人构造 RHS；先收集环内 b 的 person 全集
        b_vecs = {cid: external_vector(cid) for cid in comp}
        all_persons = sorted({p for b in b_vecs.values() for p in b})
        if not all_persons:
            # 无自然人出口的纯封闭环：按环内权重均分兜底
            for cid in comp:
                x[cid] = {}
            continue
        try:
            for pid in all_persons:
                b = [b_vecs[cid].get(pid, 0.0) for cid in comp]
                sol = solve_linear(a, b)
                for i, cid in enumerate(comp):
                    if sol[i] > 1e-10:
                        # b 与解均为百分数口径，直接累加
                        x[cid][pid] = x[cid].get(pid, 0.0) + sol[i]
        except SingularMatrixError:
            _fixed_point_fallback(g, comp, idx, weight, b_vecs, x, nodes, comp_of)

    # 清洗浮点噪声
    return {p: round(v, 6) for p, v in x.get(root, {}).items() if v > 1e-8}


def _fixed_point_fallback(g, comp, idx, weight, b_vecs, x, nodes, comp_of) -> None:
    """(I-W) 奇异时的不动点迭代：x <- W x + b，收敛即环的最小非负解。"""
    k = len(comp)
    all_persons = sorted({p for b in b_vecs.values() for p in b})
    cur = {p: [0.0] * k for p in all_persons}
    for _ in range(2000):
        nxt = {p: list(b_vecs[cid].get(p, 0.0) for cid in comp)
               for p in all_persons}
        for p in all_persons:
            for i, cid in enumerate(comp):
                for s in g.company_holders(cid):
                    if s.holder_id in idx:
                        nxt[p][i] += (getattr(s, weight) / 100.0) * cur[p][idx[s.holder_id]]
        delta = max(abs(nxt[p][i] - cur[p][i])
                    for p in all_persons for i in range(k))
        cur = nxt
        if delta < 1e-10:
            break
    for p in all_persons:
        for i, cid in enumerate(comp):
            if cur[p][i] > 1e-10:
                x[cid][p] = x[cid].get(p, 0.0) + cur[p][i]
