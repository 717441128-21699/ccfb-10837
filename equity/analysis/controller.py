"""实际控制人判定（需求 6）。

判定口径（规则可调，存 rule_configs 表）：
1. 直接表决权 > voting_threshold        → 该股东单独控制；
2. 多名股东表决权同时 > joint_threshold → 共同控制；
3. 否则看相对控制：第一大股东 >= plurality_min 且领先第二大 >= plurality_margin；
4. 公司型控制股东按 chain_control 递归追溯到它的实控人（自然人终点）；
5. follow_nominee 决定是否穿透代持；
6. 都不满足 → 无实际控制人。
另外输出按穿透比例算出的最终受益人排名作为佐证。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .graph import EquityGraph, ultimate_ownership

DEFAULT_RULES = {
    "voting_threshold": 0.50,
    "joint_threshold": 0.40,
    "plurality_min": 0.30,
    "plurality_margin": 0.10,
    "chain_control": True,
    "follow_nominee": True,
    "max_chain_depth": 30,
}


@dataclass
class ControlResult:
    company_id: str
    control_type: str = ""              # sole / joint / relative / none
    controller_ids: list[str] = field(default_factory=list)
    confidence: str = ""                  # high / medium / low
    rationale: str = ""
    direct_voting: list[dict] = field(default_factory=list)
    ultimate_beneficiaries: list[dict] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    rules: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_rules(rules: dict | None) -> dict:
    merged = dict(DEFAULT_RULES)
    if rules:
        merged.update({k: v for k, v in rules.items() if k in DEFAULT_RULES})
    return merged


def determine_controller(g: EquityGraph, company_id: str,
                         rules: dict | None = None,
                         *, _depth: int = 0,
                         _trace: list[dict] | None = None) -> ControlResult:
    rules = normalize_rules(rules)
    if _trace is None:
        _trace = []
    result = ControlResult(company_id=company_id, rules=rules)

    # 直接表决权排名（公司/自然人都算）；按规则先折叠代持
    from .graph import _collapse_nominees, find_cycles, reachable_subgraph
    direct = sorted(_collapse_nominees(g.stakes.get(company_id, []),
                                       rules["follow_nominee"]),
                    key=lambda s: s.voting_pct, reverse=True)
    result.direct_voting = [{
        "holder_kind": s.holder_kind,
        "holder_id": s.holder_id,
        "voting_pct": round(s.voting_pct, 4),
        "shares_pct": round(s.shares_pct, 4),
        "is_nominee": s.is_nominee,
    } for s in direct]

    if not direct:
        result.control_type = "none"
        result.rationale = "无股东数据"
        return result

    # 环保护：本公司“自身”处于公司型强连通环，且最大股东是同环主体、
    # 又没有任何股东掌握过半数表决权时，控制权沿环闭环、无法落到最终自然人。
    # 若环内存在过半数控股股东，控制权依然明确，正常往下判定。
    reachable = reachable_subgraph(g, company_id)
    cycles = find_cycles(g, reachable)
    own_cycle = next((c for c in cycles if company_id in c), None)
    if own_cycle:
        cycle_set = set(own_cycle)
        top0 = direct[0]
        if (top0.holder_kind == "company" and top0.holder_id in cycle_set
                and top0.voting_pct <= rules["voting_threshold"] * 100.0):
            result.control_type = "none"
            result.confidence = "low"
            result.rationale = (
                "公司自身处于环形/交叉持股闭环，且无股东掌握过半数表决权，"
                "控制权沿股东链追溯会回到自身，认定无实际控制人")
            _attach_beneficiaries(g, result, company_id, rules)
            result.chain = _trace
            return result

    top = direct[0]
    vt = rules["voting_threshold"] * 100.0
    jt = rules["joint_threshold"] * 100.0
    pmin = rules["plurality_min"] * 100.0
    margin = rules["plurality_margin"] * 100.0

    over_jt = [s for s in direct if s.voting_pct >= jt]
    second = direct[1].voting_pct if len(direct) > 1 else 0.0
    top_lead = top.voting_pct - second

    # 1) 单独绝对控制：第一大表决权超过绝对控制线即单独控制。
    #    绝对控制线优先于共同控制线——哪怕第二名也超过共同线，
    #    只要第一大掌握过半数表决权，法律上即可单独决定普通决议。
    if top.voting_pct > vt:
        return _finalize(g, result, [top], "sole", "high",
                         f"直接表决权 {top.voting_pct:.2f}% 超过 "
                         f"{rules['voting_threshold']:.0%} 绝对控制线，"
                         "可单独控制",
                         rules, _depth, _trace)

    # 2) 共同控制：无人过绝对线，但多名股东达到共同控制线，
    #    且第一大不构成决定性领先
    decisive_lead = top_lead >= margin
    if len(over_jt) >= 2 and not decisive_lead:
        return _finalize(g, result, over_jt, "joint", "high",
                         f"{len(over_jt)} 名股东表决权同时超过 "
                         f"{rules['joint_threshold']:.0%} 且均未过绝对控制线、"
                         "持股接近，认定共同控制",
                         rules, _depth, _trace)

    # 3) 相对控制
    if top.voting_pct >= pmin and top_lead >= margin:
        return _finalize(g, result, [top], "relative", "medium",
                         f"第一大股东表决权 {top.voting_pct:.2f}%，"
                         f"领先第二大 {top_lead:.2f} 个百分点，"
                         f"认定相对控制",
                         rules, _depth, _trace)

    # 4) 无控制
    result.control_type = "none"
    result.confidence = "low"
    result.rationale = (
        f"第一大股东表决权仅 {top.voting_pct:.2f}%，"
        "未达绝对/共同/相对控制标准，认定无实际控制人")
    _attach_beneficiaries(g, result, company_id, rules)
    result.chain = _trace
    return result


def _finalize(g: EquityGraph, result: ControlResult, holders: list,
              control_type: str, confidence: str, rationale: str,
              rules: dict, depth: int, trace: list[dict]) -> ControlResult:
    result.control_type = control_type
    result.confidence = confidence
    result.rationale = rationale

    final_persons: list[str] = []
    # holders 已由调用方限定为“控制股东”（单独/相对=1 个，共同=多个），
    # 其余不达标股东不参与控制人认定，只能出现在受益人排名里。
    for s in holders:
        trace.append({
            "level": depth,
            "company_id": result.company_id,
            "holder_kind": s.holder_kind,
            "holder_id": s.holder_id,
            "voting_pct": round(s.voting_pct, 4),
        })
        if s.holder_kind == "person":
            final_persons.append(s.holder_id)
        elif rules["chain_control"] and depth < rules["max_chain_depth"] \
                and s.holder_id in g.companies:
            # 递归：控制股东是公司，向上追它的实控人
            sub = determine_controller(g, s.holder_id, rules,
                                       _depth=depth + 1, _trace=trace)
            if sub.controller_ids:
                final_persons.extend(sub.controller_ids)
            else:
                # 子层无控制则本层控制停在该公司，记为公司控制人
                final_persons.append(s.holder_id)
        else:
            final_persons.append(s.holder_id)

    # 去重保序
    seen: set[str] = set()
    result.controller_ids = [x for x in final_persons
                             if not (x in seen or seen.add(x))]
    _attach_beneficiaries(g, result, result.company_id, rules)
    result.chain = trace
    return result


def _attach_beneficiaries(g: EquityGraph, result: ControlResult,
                          company_id: str, rules: dict) -> None:
    try:
        shares = ultimate_ownership(g, company_id, mode="shares")
        voting = ultimate_ownership(g, company_id, mode="voting")
    except Exception as exc:  # 穿透失败不应拖垮判定
        result.rationale += f"（最终受益计算异常: {exc!r}）"
        return
    persons = set(shares) | set(voting)
    rows = [{
        "person_id": pid,
        "name": g.persons.get(pid, {}).get("name", pid),
        "id_tail": g.persons.get(pid, {}).get("id_tail", ""),
        "ultimate_shares_pct": round(shares.get(pid, 0.0), 4),
        "ultimate_voting_pct": round(voting.get(pid, 0.0), 4),
    } for pid in persons]
    result.ultimate_beneficiaries = sorted(
        rows, key=lambda r: r["ultimate_voting_pct"], reverse=True)[:20]
