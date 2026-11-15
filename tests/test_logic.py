"""纯逻辑单测：环持股求解、历史回放、名称归一、代持穿透、实控人判定。

直接运行：py -3 tests/test_logic.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

from equity.analysis.graph import (EquityGraph, Stake, find_cycles,
                                   reachable_subgraph, ultimate_ownership)
from equity.analysis.controller import determine_controller
from equity.analysis.history import replay_events
from equity.analysis.linalg import solve_linear
from equity.identity import (company_key, normalize_company_name,
                             person_key)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ----------------------------------------------------------- 1. 线性代数
def test_linalg() -> None:
    print("[1] 高斯消元")
    # 2x2:  x + y = 3 ; 2x + 3y = 7 -> x=2, y=1
    x = solve_linear([[1.0, 1.0], [2.0, 3.0]], [3.0, 7.0])
    check("二元一次方程组", approx(x[0], 2.0) and approx(x[1], 1.0), str(x))
    # 环 (I-W) x = b:  [[0.6,-0.3],[-0.4,0.7]] x = [35,25]
    a = [[0.6, -0.3], [-0.4, 0.7]]
    b = [35.0, 25.0]
    x = solve_linear(a, b)
    # 手算: det=0.30, x1=32/0.3≈106.6667, x2=29/0.3≈96.6667
    check("环方程 RHS1", approx(x[0], 106.6666667, 1e-4), str(x))
    check("环方程 RHS2", approx(x[1], 96.6666667, 1e-4), str(x))
    # 残差
    resid = max(abs(sum(a[i][j] * x[j] for j in range(2)) - b[i]) for i in range(2))
    check("环方程残差趋零", resid < 1e-8, str(resid))


# ------------------------------------------------- 2. 三公司环穿透（手算）
def _ring_graph() -> EquityGraph:
    g = EquityGraph()
    for cid in ("A", "B", "C"):
        g.companies[cid] = {"company_id": cid, "name": cid}
    g.persons["P1"] = {"person_id": "P1", "name": "张三", "id_tail": "111111"}
    g.persons["P2"] = {"person_id": "P2", "name": "李四", "id_tail": "222222"}
    # A: B 持 30%，P1 持 70%
    g.stakes["A"] = [Stake("company", "B", 30, 30), Stake("person", "P1", 70, 70)]
    # B: C 持 30%，P2 持 70%
    g.stakes["B"] = [Stake("company", "C", 30, 30), Stake("person", "P2", 70, 70)]
    # C: A 持 30%，P1 持 20%，P2 持 50%
    g.stakes["C"] = [Stake("company", "A", 30, 30),
                     Stake("person", "P1", 20, 20),
                     Stake("person", "P2", 50, 50)]
    return g


def test_ring_penetration() -> None:
    print("[2] 三公司环形持股（手算答案）")
    g = _ring_graph()
    cycles = find_cycles(g, {"A", "B", "C"})
    check("检出一个环", len(cycles) == 1 and set(cycles[0]) == {"A", "B", "C"},
          str(cycles))
    ua = ultimate_ownership(g, "A")
    # 解析解（百分数）：
    # xA=0.3xB+70; xB=0.3xC; xC=0.3xA+20 (P1 分量)
    # -> xB=12.3/0.973≈12.6413, xA≈73.7924；P2 分量互补到 100
    check("A->P1 最终持股≈73.7924%", approx(ua.get("P1", 0), 73.7923947, 1e-4),
          str(ua))
    check("A->P2 最终持股≈26.2076%", approx(ua.get("P2", 0), 26.2076053, 1e-4),
          str(ua))
    check("最终权益合计≈100%", approx(sum(ua.values()), 100.0, 1e-4),
          str(sum(ua.values())))
    ub = ultimate_ownership(g, "B")
    check("B->P1≈12.6413%", approx(ub.get("P1", 0), 12.6413155, 1e-4), str(ub))
    check("B->P2≈87.3587%", approx(ub.get("P2", 0), 87.3586845, 1e-4), str(ub))
    check("B 公司权益也闭合", approx(sum(ub.values()), 100.0, 1e-4), str(ub))
    uc = ultimate_ownership(g, "C")
    check("C->P1≈42.1377%", approx(uc.get("P1", 0), 42.1377184, 1e-4), str(uc))
    check("C->P2≈57.8623%", approx(uc.get("P2", 0), 57.8622816, 1e-4), str(uc))
    check("C 公司权益也闭合", approx(sum(uc.values()), 100.0, 1e-4), str(uc))
    check("可达子图含全部环节点",
          reachable_subgraph(g, "A") == {"A", "B", "C"})


# ------------------------------------------------- 3. 线性链条（乘法路径）
def test_chain() -> None:
    print("[3] 线性控股链")
    g = EquityGraph()
    for cid in ("Top", "Mid", "Leaf"):
        g.companies[cid] = {"company_id": cid, "name": cid}
    g.persons["P"] = {"person_id": "P", "name": "王五", "id_tail": "333333"}
    g.stakes["Top"] = [Stake("person", "P", 100, 100)]
    g.stakes["Mid"] = [Stake("company", "Top", 60, 60),
                       Stake("person", "X", 40, 40)]
    g.stakes["Leaf"] = [Stake("company", "Mid", 50, 50),
                        Stake("person", "Y", 50, 50)]
    g.persons["X"] = {"person_id": "X", "name": "赵六", "id_tail": "444444"}
    g.persons["Y"] = {"person_id": "Y", "name": "钱七", "id_tail": "555555"}
    u = ultimate_ownership(g, "Leaf")
    # P: 0.5*0.6*100 = 30% ; X: 0.5*0.4*100 = 20% ; Y: 50%
    check("链条乘法 P=30%", approx(u.get("P", 0), 30.0), str(u))
    check("链条乘法 X=20%", approx(u.get("X", 0), 20.0), str(u))
    check("链条乘法 Y=50%", approx(u.get("Y", 0), 50.0), str(u))
    check("链条权益闭合", approx(sum(u.values()), 100.0), str(u))
    # 深度截断：只看一层时 Y 50% + Mid 50%（公司未展开，自然人只到 Y）
    u1 = ultimate_ownership(g, "Leaf", max_depth=0)
    check("深度0只到直接自然人", approx(u1.get("Y", 0), 50.0) and "P" not in u1,
          str(u1))


# ------------------------------------------------- 4. 交叉持股（双向边）
def test_cross_holding() -> None:
    print("[4] 两公司交叉持股")
    g = EquityGraph()
    for cid in ("X", "Y"):
        g.companies[cid] = {"company_id": cid, "name": cid}
    g.persons["Pa"] = {"person_id": "Pa", "name": "甲", "id_tail": "1"}
    g.persons["Pb"] = {"person_id": "Pb", "name": "乙", "id_tail": "2"}
    # X: Y 10%, Pa 90% ; Y: X 10%, Pb 90%
    g.stakes["X"] = [Stake("company", "Y", 10, 10), Stake("person", "Pa", 90, 90)]
    g.stakes["Y"] = [Stake("company", "X", 10, 10), Stake("person", "Pb", 90, 90)]
    cycles = find_cycles(g, {"X", "Y"})
    check("交叉持股检出环", len(cycles) == 1 and set(cycles[0]) == {"X", "Y"})
    ux = ultimate_ownership(g, "X")
    # 解: uX = 0.1*uY + 90 ; uY = 0.1*uX + 90 (Pa/Pb 分量)
    # Pa_X: 90 / 0.99 ≈ 90.9091 ; Pb_X ≈ 9.0909
    check("交叉 Pa≈90.9091%", approx(ux.get("Pa", 0), 90.9090909, 1e-4), str(ux))
    check("交叉 Pb≈9.0909%", approx(ux.get("Pb", 0), 9.0909091, 1e-4), str(ux))
    check("交叉权益闭合", approx(sum(ux.values()), 100.0, 1e-4))


# ------------------------------------------------- 5. 历史回放
def test_replay() -> None:
    print("[5] 股东变更回放")
    events = [
        {"holder_kind": "person", "holder_id": "P1", "event_date": "2024-01-01",
         "valid_to": "2025-01-01", "shares_pct": 60.0, "voting_pct": 60.0,
         "is_nominee": 0, "beneficial_holder_id": None},
        {"holder_kind": "person", "holder_id": "P2", "event_date": "2024-01-01",
         "valid_to": None, "shares_pct": 40.0, "voting_pct": 40.0,
         "is_nominee": 0, "beneficial_holder_id": None},
        {"holder_kind": "person", "holder_id": "P3", "event_date": "2025-01-01",
         "valid_to": None, "shares_pct": 60.0, "voting_pct": 60.0,
         "is_nominee": 0, "beneficial_holder_id": None},
    ]
    past = {h["holder_id"]: h["shares_pct"]
            for h in replay_events(events, "2024-06-01")}
    check("历史时点 P1=60", past.get("P1") == 60.0 and "P3" not in past, str(past))
    now = {h["holder_id"]: h["shares_pct"] for h in replay_events(events)}
    check("当前时点 P3=60,P2=40",
          now.get("P3") == 60.0 and now.get("P2") == 40.0 and "P1" not in now,
          str(now))
    # 边界：valid_to 当天事件已失效（半开区间）
    edge = {h["holder_id"]: h["shares_pct"]
            for h in replay_events(events, "2025-01-01")}
    check("半开区间边界正确", "P1" not in edge and edge.get("P3") == 60.0, str(edge))


# ------------------------------------------------- 6. 归一化与消歧
def test_identity() -> None:
    print("[6] 名称归一与自然人消歧")
    pairs = [
        ("字节跳动（北京）有限公司", "字节跳动(北京)有限责任公司"),
        (" 恒泰 科技 股份有限公司 ", "恒泰科技股份公司"),
        ("ＡＢＣ控股有限公司", "ABC控股有限责任公司"),
    ]
    for a, b in pairs:
        check(f"归一: {a} ≡ {b}",
              normalize_company_name(a) == normalize_company_name(b),
              f"{normalize_company_name(a)} vs {normalize_company_name(b)}")
    check("信用代码优先于名称",
          company_key("甲", "91XYZ") == company_key("乙", "91xyz"))
    check("同名不同尾号=不同人",
          person_key("张伟", "111111") != person_key("张伟", "222222"))
    check("同名同尾号=同一人",
          person_key("张伟", "111111") == person_key(" 张 伟 ", "111111"))
    check("无尾号退回姓名键",
          person_key("张伟", None) == "P::张伟::?")


# ------------------------------------------------- 7. 代持穿透
def test_nominee() -> None:
    print("[7] 代持穿透")
    g = EquityGraph()
    g.companies["C"] = {"company_id": "C", "name": "C"}
    g.persons["Real"] = {"person_id": "Real", "name": "实控人", "id_tail": "9"}
    g.persons["Nom"] = {"person_id": "Nom", "name": "代持人", "id_tail": "8"}
    g.stakes["C"] = [
        Stake("person", "Nom", 55, 55, is_nominee=1,
              beneficial_holder_id="Real"),
        Stake("person", "Real", 10, 10),
        Stake("person", "Other", 35, 35),
    ]
    g.persons["Other"] = {"person_id": "Other", "name": "外人", "id_tail": "7"}
    # build_graph 的代持折叠逻辑这里直接复用
    from equity.analysis.graph import _collapse_nominees
    collapsed = {s.holder_id: s.shares_pct
                 for s in _collapse_nominees(g.stakes["C"], True)}
    check("代持权益归并到实际人 Real=65",
          collapsed.get("Real") == 65.0 and "Nom" not in collapsed, str(collapsed))
    kept = {s.holder_id: s.shares_pct
            for s in _collapse_nominees(g.stakes["C"], False)}
    check("关闭穿透时代持人保留 Nom=55",
          kept.get("Nom") == 55.0 and kept.get("Real") == 10.0, str(kept))
    res = determine_controller(g, "C", {"follow_nominee": True})
    check("穿透后代持人不被当成控制人",
          res.controller_ids == ["Real"] and "Nom" not in res.controller_ids,
          str(res.controller_ids))


# ------------------------------------------------- 8. 实控人判定分级
def test_controller_levels() -> None:
    print("[8] 实控人判定分级与链条追溯")
    g = EquityGraph()
    for cid in ("Sole", "Joint", "Dispersed", "Parent", "Sub"):
        g.companies[cid] = {"company_id": cid, "name": cid}
    for pid, name in [("Ps", "独"), ("Pj1", "共1"), ("Pj2", "共2"),
                      ("Pd1", "散1"), ("Pd2", "散2"), ("Pd3", "散3"),
                      ("Pp", "母公司老板")]:
        g.persons[pid] = {"person_id": pid, "name": name, "id_tail": pid}
    g.stakes["Sole"] = [Stake("person", "Ps", 67, 67),
                        Stake("person", "Pd1", 33, 33)]
    g.stakes["Joint"] = [Stake("person", "Pj1", 45, 45),
                         Stake("person", "Pj2", 40, 40),
                         Stake("person", "Pd2", 15, 15)]
    g.stakes["Dispersed"] = [Stake("person", "Pd1", 25, 25),
                             Stake("person", "Pd2", 25, 25),
                             Stake("person", "Pd3", 25, 25),
                             Stake("person", "Pj1", 25, 25)]
    g.stakes["Parent"] = [Stake("person", "Pp", 80, 80),
                          Stake("person", "Pd3", 20, 20)]
    g.stakes["Sub"] = [Stake("company", "Parent", 70, 70),
                       Stake("person", "Pd1", 30, 30)]

    r1 = determine_controller(g, "Sole")
    check("绝对控制 sole", r1.control_type == "sole"
          and r1.controller_ids == ["Ps"], r1.control_type)
    r2 = determine_controller(g, "Joint")
    check("共同控制 joint(默认40%线)", r2.control_type == "joint"
          and set(r2.controller_ids) == {"Pj1", "Pj2"},
          f"{r2.control_type} {r2.controller_ids}")
    r3 = determine_controller(g, "Dispersed")
    check("分散股权 none", r3.control_type == "none"
          and not r3.controller_ids, r3.control_type)
    r4 = determine_controller(g, "Sub")
    check("链条追溯到自然人 Pp",
          r4.controller_ids == ["Pp"] and r4.control_type == "sole",
          f"{r4.control_type} {r4.controller_ids}")
    check("判定链有两层记录", len({step['level'] for step in r4.chain}) == 2,
          str(r4.chain))

    # 规则可调：把控制线提高到 70%，Sole(67%) 降级为相对/无控制
    r5 = determine_controller(g, "Sole", {"voting_threshold": 0.70,
                                          "joint_threshold": 0.70,
                                          "plurality_min": 0.60})
    check("调规则后 67% 不再是绝对控制", r5.control_type != "sole",
          r5.control_type)


def main() -> int:
    for fn in [test_linalg, test_ring_penetration, test_chain,
               test_cross_holding, test_replay, test_identity,
               test_nominee, test_controller_levels]:
        fn()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
