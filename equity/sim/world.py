"""仿真关系网构造（纯内存、确定性随机）。

产出的关系网里刻意埋入：
- 多层集团（创始人 → 控股平台 → 产业公司，3~6 层）
- 交叉持股 / 环形持股（含一个三家公司纯环，无实控人）
- 代持（名义股东 + 实际权益人）
- 表决权与股权不一致（一致行动/委托）
- 同名不同人的自然人（证件尾号区分）
- 公司名变体（全角半角括号、有限公司/有限责任公司）
- 股东变更历史（部分公司只给变更记录，需回放才能得到当前股东）
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import date, timedelta

GENESIS = date(2022, 1, 1)
TODAY = date(2026, 11, 15)

SURNAMES = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾"
                "肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方")
GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "军", "洋", "勇", "艳", "杰", "娟",
         "涛", "明", "超", "霞", "平", "刚", "桂英", "建国", "志强", "海燕", "晓明",
         "雪梅", "文博", "子轩", "梓涵", "浩然", "若曦", "承恩", "嘉懿", "立诚", "振华"]
INDUSTRIES = ["科技", "制造", "贸易", "建筑", "医药", "能源", "传媒", "物流", "农业", "金融"]
REGIONS = ["北京市朝阳区", "上海市浦东新区", "深圳市南山区", "杭州市西湖区", "广州市天河区",
           "成都市高新区", "武汉市光谷", "苏州市工业园区", "西安市曲江新区", "南京市江宁区"]
GROUP_PREFIX = ["恒泰", "瑞丰", "锦绣", "云启", "中天", "瀚海", "星桥", "广汇", "隆基",
                "正和", "宏图", "德润", "联创", "华曜", "启明", "百川", "昆仑", "嘉禾",
                "鼎盛", "融信", "蓝湾", "金石", "天工", "悦达", "清源", "宏远", "安泰",
                "新锐", "博远", "朗润"]
QUALIFIERS = ["控股", "投资", "实业", "科技", "发展", "资本", "集团", "国际"]
SUFFIX_VARIANTS = ["有限公司", "有限责任公司", "股份有限公司"]


@dataclass
class Person:
    person_id: str
    name: str
    id_tail: str
    gender: str


@dataclass
class Company:
    company_id: str
    name: str
    credit_code: str
    established_date: str
    registered_capital: float
    industry: str
    region: str
    profile_mode: str = "direct"
    aliases: list[str] = field(default_factory=list)
    preset_type: str = "none"          # person / joint / none
    preset_controller_id: str | None = None
    preset_rationale: str = ""


@dataclass
class Event:
    company_id: str
    holder_kind: str
    holder_id: str
    event_date: str
    valid_to: str | None
    shares_pct: float
    voting_pct: float
    is_nominee: int = 0
    beneficial_holder_id: str | None = None
    change_no: int = 0
    note: str = ""


class TargetReached(Exception):
    """公司数已达目标，停止扩张。"""


class NetworkBuilder:
    def __init__(self, target: int = 3000, seed: int = 20261115):
        self.target = target
        self.rng = random.Random(seed)
        self.persons: dict[str, Person] = {}
        self.companies: dict[str, Company] = {}
        self.events: list[Event] = []
        self.used_names: set[str] = set()
        self.used_codes: set[str] = set()
        self._seq = 0
        # 记录同名不同人：name -> [person_id]
        self.duplicate_names: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ id
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:06d}"

    def _credit_code(self) -> str:
        alphabet = string.digits + "ABCDEFGHJKLMNPQRTUWXY"
        while True:
            code = "91" + "".join(self.rng.choices(alphabet, k=16))
            if code not in self.used_codes:
                self.used_codes.add(code)
                return code

    # -------------------------------------------------------------- persons
    def _id_tail(self) -> str:
        return f"{self.rng.randrange(10**6):06d}"

    def make_person(self, name: str | None = None) -> Person:
        if name is None:
            name = self.rng.choice(SURNAMES) + self.rng.choice(GIVEN)
        pid = self._next_id("P")
        p = Person(pid, name, self._id_tail(), self.rng.choice(["男", "女"]))
        self.persons[pid] = p
        self.duplicate_names.setdefault(name, []).append(pid)
        return p

    def make_duplicate_person(self, name: str) -> Person:
        """制造一个与指定姓名相同、但证件尾号不同的自然人。"""
        existing_tails = {self.persons[pid].id_tail
                          for pid in self.duplicate_names.get(name, [])}
        pid = self._next_id("P")
        tail = self._id_tail()
        while tail in existing_tails:
            tail = self._id_tail()
        p = Person(pid, name, tail, self.rng.choice(["男", "女"]))
        self.persons[pid] = p
        self.duplicate_names.setdefault(name, []).append(pid)
        return p

    # ------------------------------------------------------------- companies
    def _unique_company_name(self, base: str) -> str:
        suffix = self.rng.choice(SUFFIX_VARIANTS)
        name = f"{base}{suffix}"
        n = 2
        while name in self.used_names:
            name = f"{base}（{n}）{suffix}"
            n += 1
        self.used_names.add(name)
        return name

    def make_company(self, base_name: str, established: date | None = None,
                     profile_mode: str = "direct") -> Company:
        if len(self.companies) >= self.target:
            raise TargetReached
        cid = self._next_id("C")
        name = self._unique_company_name(base_name)
        established = established or (GENESIS + timedelta(days=self.rng.randrange(0, 900)))
        c = Company(
            company_id=cid,
            name=name,
            credit_code=self._credit_code(),
            established_date=established.isoformat(),
            registered_capital=float(self.rng.choice([100, 300, 500, 1000, 3000, 5000, 10000])),
            industry=self.rng.choice(INDUSTRIES),
            region=self.rng.choice(REGIONS),
            profile_mode=profile_mode,
        )
        self.companies[cid] = c
        return c

    def add_alias_variants(self, c: Company) -> None:
        """加一到两个写法不同的别名。"""
        base = c.name
        for suf in SUFFIX_VARIANTS:
            if base.endswith(suf):
                core = base[: -len(suf)]
                break
        else:
            core = base
        other_suffix = self.rng.choice([s for s in SUFFIX_VARIANTS if not base.endswith(s)])
        v1 = core + other_suffix
        if "（" in base:
            v2 = base.replace("（", "(").replace("）", ")")
        elif "(" in base:
            v2 = base.replace("(", "（").replace(")", "）")
        else:
            v2 = core + "（集团）" + other_suffix
        for v in (v1, v2):
            if v != c.name and v not in c.aliases and v not in self.used_names:
                c.aliases.append(v)

    # ---------------------------------------------------------------- events
    def add_event(self, company_id: str, holder_kind: str, holder_id: str,
                  event_date: date, shares: float, voting: float | None = None,
                  *, valid_to: date | None = None, is_nominee: int = 0,
                  beneficial_holder_id: str | None = None, note: str = "",
                  change_no: int = 0) -> Event:
        ev = Event(
            company_id=company_id,
            holder_kind=holder_kind,
            holder_id=holder_id,
            event_date=event_date.isoformat(),
            valid_to=valid_to.isoformat() if valid_to else None,
            shares_pct=round(shares, 4),
            voting_pct=round(voting if voting is not None else shares, 4),
            is_nominee=is_nominee,
            beneficial_holder_id=beneficial_holder_id,
            change_no=change_no,
            note=note,
        )
        self.events.append(ev)
        return ev

    def _assert_closed(self, cid: str, on_date: date) -> None:
        valid = [e for e in self.events
                 if e.company_id == cid
                 and date.fromisoformat(e.event_date) <= on_date
                 and (e.valid_to is None or date.fromisoformat(e.valid_to) > on_date)]
        s = sum(e.shares_pct for e in valid)
        v = sum(e.voting_pct for e in valid)
        assert abs(s - 100.0) < 1e-6, f"{cid} shares {s:.4f} on {on_date}"
        assert abs(v - 100.0) < 1e-6, f"{cid} voting {v:.4f} on {on_date}"

    # ------------------------------------------------------------- 1. groups
    def build_group(self, idx: int) -> None:
        prefix = GROUP_PREFIX[idx]
        founder = self.make_person()
        founder2 = self.make_person()  # 联合创始人
        layers = self.rng.randint(3, 6)

        # 顶层控股公司
        top = self.make_company(f"{prefix}{self.rng.choice(QUALIFIERS)}")
        est = date.fromisoformat(top.established_date)
        # 创始人 67%（绝对控制），联合创始人 18%，员工平台 15%
        esop = self.make_company(f"{prefix}企业管理咨询")
        self.add_event(top.company_id, "person", founder.person_id, est, 67.0)
        self.add_event(top.company_id, "person", founder2.person_id, est, 18.0)
        self.add_event(top.company_id, "company", esop.company_id, est, 15.0)
        # ESOP 平台自身也要有股东：创始人作为 GP 持有 80%，两名合伙人持 20%
        esop_est = date.fromisoformat(esop.established_date)
        partner_a = self.make_person()
        partner_b = self.make_person()
        self.add_event(esop.company_id, "person", founder.person_id, esop_est, 80.0)
        self.add_event(esop.company_id, "person", partner_a.person_id, esop_est, 12.0)
        self.add_event(esop.company_id, "person", partner_b.person_id, esop_est, 8.0)
        self._assert_closed(esop.company_id, esop_est)
        top.preset_type = "person"
        top.preset_controller_id = founder.person_id
        top.preset_rationale = "创始人直接持股67%，超过控制线"
        esop.preset_type = "person"
        esop.preset_controller_id = founder.person_id
        esop.preset_rationale = f"由{top.name}创始人控制的员工持股平台"
        self._assert_closed(top.company_id, est)

        prev = top
        prev_est = est
        for layer in range(1, layers):
            n_children = self.rng.randint(1, 3)
            for _ in range(n_children):
                if len(self.companies) >= self.target:
                    return
                child = self.make_company(
                    f"{prefix}{self.rng.choice(QUALIFIERS)}{self.rng.choice(INDUSTRIES)}")
                cest = date.fromisoformat(child.established_date)
                if cest < prev_est:
                    cest = prev_est + timedelta(days=30)
                    child.established_date = cest.isoformat()
                parent_share = self.rng.choice([51.0, 55.0, 60.0, 70.0, 80.0, 100.0])
                rest = 100.0 - parent_share
                holders: list[tuple[str, str, float]] = [("company", prev.company_id, parent_share)]
                if rest > 0:
                    # 剩余分给自然人/外部公司
                    r = rest
                    while r > 0:
                        part = min(r, float(self.rng.choice([10, 15, 20, 25, 30, 100])))
                        part = round(part, 4)
                        if part > r + 1e-9:
                            part = r
                        if self.rng.random() < 0.7:
                            p = self.make_person()
                            holders.append(("person", p.person_id, part))
                        else:
                            try:
                                ext = self.make_company(
                                    f"{prefix}外部合作{self.rng.choice(INDUSTRIES)}")
                                ext.preset_type = "none"
                                holders.append(("company", ext.company_id, part))
                            except TargetReached:
                                p = self.make_person()
                                holders.append(("person", p.person_id, part))
                        r = round(r - part, 4)
                for kind, hid, pct in holders:
                    self.add_event(child.company_id, kind, hid, cest, pct)
                child.preset_type = "person"
                child.preset_controller_id = founder.person_id
                child.preset_rationale = (
                    f"链条控制：{founder.name}→{top.name}（{layer}层），"
                    f"上层公司持股{parent_share:.0f}%")
                self._assert_closed(child.company_id, cest)
                prev = child  # 部分链条继续往下长
            prev_est = date.fromisoformat(prev.established_date)

    # ----------------------------------------------------- 2. standalone 公司
    def build_standalone(self, n: int) -> None:
        for i in range(n):
            try:
                base = f"{self.rng.choice(GROUP_PREFIX)}{self.rng.choice(INDUSTRIES)}{i:03d}"
                c = self.make_company(base)
            except TargetReached:
                return
            est = date.fromisoformat(c.established_date)
            style = self.rng.choices(
                ["controlled", "joint", "dispersed"], weights=[55, 20, 25])[0]

            if style == "controlled":
                owner = self.make_person()
                main = self.rng.choice([51.0, 55.0, 60.0, 67.0, 80.0])
                self.add_event(c.company_id, "person", owner.person_id, est, main)
                rest = 100.0 - main
                self._distribute_rest(c.company_id, est, rest)
                c.preset_type = "person"
                c.preset_controller_id = owner.person_id
                c.preset_rationale = f"第一大股东直接持股{main:.0f}%，超过控制线"
            elif style == "joint":
                p1, p2 = self.make_person(), self.make_person()
                # 两人表决权均超过 40% 共同控制线（同股同权下无法双双过 50%）
                self.add_event(c.company_id, "person", p1.person_id, est, 45.0, 45.0)
                self.add_event(c.company_id, "person", p2.person_id, est, 40.0, 40.0)
                self._distribute_rest(c.company_id, est, 15.0)
                c.preset_type = "joint"
                c.preset_controller_id = "|".join(sorted([p1.person_id, p2.person_id]))
                c.preset_rationale = "两大股东表决权均超过40%共同控制线，认定共同控制"
            else:
                # 分散：没有任何股东 >=30%
                holders = []
                total = 100.0
                while total > 0:
                    part = min(total, float(self.rng.choice([5, 8, 10, 12, 15, 20, 25])))
                    if part >= 30.0:
                        part = 25.0
                    if part > total:
                        part = total
                    holders.append(part)
                    total = round(total - part, 4)
                for part in holders:
                    p = self.make_person()
                    self.add_event(c.company_id, "person", p.person_id, est, part)
                c.preset_type = "none"
                c.preset_rationale = "股权高度分散，无股东达到相对控制标准"
            self._assert_closed(c.company_id, est)

    def _distribute_rest(self, cid: str, est: date, rest: float) -> None:
        r = rest
        while r > 1e-9:
            part = min(r, float(self.rng.choice([5, 10, 15, 20, 25])))
            part = round(part, 4)
            if part > r:
                part = r
            if self.rng.random() < 0.75:
                p = self.make_person()
                self.add_event(cid, "person", p.person_id, est, part)
            else:
                try:
                    ext = self.make_company(f"外部投资{self.rng.choice(QUALIFIERS)}")
                    self.add_event(cid, "company", ext.company_id, est, part)
                except TargetReached:
                    p = self.make_person()
                    self.add_event(cid, "person", p.person_id, est, part)
            r = round(r - part, 4)

    # ------------------------------------------------------------- 3. 环
    def build_cross_holding_groups(self) -> None:
        """在集团内埋交叉持股；再造一个三家纯环（无实控人）。

        安全约束（硬性）：任何改造都不得改变一家公司“控股股东是谁”。
        实现上只从持股 <=15% 的非控股小股东手里协议转让少量股权给集团内
        另一家公司，控股股东的持股比例保持不变；改造后做控制人不变断言。
        """
        def current_holders(cid: str) -> list[Event]:
            return [e for e in self.events
                    if e.company_id == cid and e.valid_to is None]

        def controlling_holder(cid: str) -> Event | None:
            hs = current_holders(cid)
            if not hs:
                return None
            top_ev = max(hs, key=lambda e: e.shares_pct)
            return top_ev if top_ev.shares_pct >= 51.0 else None

        group_tops = [c for c in self.companies.values()
                      if c.preset_type == "person" and "控股" in c.name][:5]
        for top in group_tops:
            # 找 top 控股的二级公司 cid
            cid = None
            for e in self.events:
                if e.company_id == top.company_id:
                    continue
                if controlling_holder(e.company_id) is not None and \
                        controlling_holder(e.company_id).holder_id == top.company_id:
                    cid = e.company_id
                    break
            if cid is None:
                continue
            # cid 的可腾挪小股东：非控股、自然人、持股 8~15%
            ctrl = controlling_holder(cid)
            donors = [e for e in current_holders(cid)
                      if e.holder_id != ctrl.holder_id
                      and e.holder_kind == "person"
                      and 8.0 <= e.shares_pct <= 15.0]
            if not donors:
                continue
            # top 的可腾挪小股东：非创始人(非控制人)、公司型、>=8%
            top_ctrl = controlling_holder(top.company_id)
            top_donors = [e for e in current_holders(top.company_id)
                          if e.holder_id != top_ctrl.holder_id
                          and e.holder_kind == "company" and e.shares_pct >= 8.0]
            if not top_donors:
                continue

            d = date.fromisoformat(donors[0].event_date) + timedelta(days=400)
            # cid：小自然人转 3% 给 top（top 增持，控制权增强而非削弱）
            donor = donors[0]
            donor.valid_to = d.isoformat()
            self.add_event(cid, "person", donor.holder_id, d,
                           round(donor.shares_pct - 3.0, 4), note="协议转让3%")
            self.add_event(cid, "company", top.company_id, d, 3.0,
                           note="集团增持，形成交叉持股安排")
            # top：非控制小股东转 3% 给 cid；创始人 top_ctrl 完全不动
            td = top_donors[0]
            td.valid_to = d.isoformat()
            self.add_event(top.company_id, "company", td.holder_id, d,
                           round(td.shares_pct - 3.0, 4), note="协议转让3%")
            self.add_event(top.company_id, "company", cid, d, 3.0,
                           note="交叉持股，受让子公司股份")
            self._assert_closed(cid, d)
            self._assert_closed(top.company_id, d)
            # 硬断言：控股股东身份与持股比例均未改变
            new_ctrl_cid = controlling_holder(cid)
            new_ctrl_top = controlling_holder(top.company_id)
            assert new_ctrl_cid.holder_id == ctrl.holder_id, \
                f"交叉持股改变了 {cid} 的控股股东"
            assert new_ctrl_top.holder_id == top_ctrl.holder_id, \
                f"交叉持股改变了 {top.company_id} 的控股股东"

        # 纯环 A→B→C→A，每家另有分散自然人股东，无实控人
        ring = []
        base = date(2023, 6, 1)
        for i in range(3):
            c = self.make_company(f"环宇{i + 1}号供应链", established=base)
            ring.append(c)
        for i, c in enumerate(ring):
            nxt = ring[(i + 1) % 3]
            self.add_event(c.company_id, "company", nxt.company_id, base, 30.0,
                           note="环形持股")
            self._distribute_rest(c.company_id, base, 70.0)
            c.preset_type = "none"
            c.preset_rationale = "三家公司环形持股，控制权闭环无法追溯到最终主体"
            self._assert_closed(c.company_id, base)

    # ---------------------------------------------------------- 4. 代持
    def build_nominees(self, n: int = 8) -> None:
        candidates = [c for c in self.companies.values()
                      if c.preset_type == "person"]
        self.rng.shuffle(candidates)
        made = 0
        for c in candidates:
            if made >= n:
                break
            evs = [e for e in self.events
                   if e.company_id == c.company_id and e.valid_to is None
                   and e.holder_kind == "person" and e.shares_pct >= 40]
            if not evs:
                continue
            main = max(evs, key=lambda e: e.shares_pct)
            # 变更日必须晚于现存股东事件，否则会出现“旧股东尚未入股”的空档
            latest = max(date.fromisoformat(e.event_date) for e in evs)
            d = latest + timedelta(days=self.rng.randint(30, 200))
            if d >= TODAY:
                d = TODAY - timedelta(days=1)
            nominee = self.make_person()
            main.valid_to = d.isoformat()
            # 名义股东持股，实际权益仍是原控制人
            self.add_event(c.company_id, "person", nominee.person_id, d,
                           main.shares_pct, is_nominee=1,
                           beneficial_holder_id=main.holder_id,
                           note="股权代持安排（工商仅显示名义股东）")
            c.preset_rationale = c.preset_rationale + "；部分股权由他人代持，穿透代持后控制人不变"
            self._assert_closed(c.company_id, d)
            made += 1

    # ----------------------------------------------- 5. 表决权与股权不一致
    def build_voting_arrangements(self, n: int = 5) -> None:
        candidates = [c for c in self.companies.values() if c.preset_type == "none"]
        self.rng.shuffle(candidates)
        made = 0
        for c in candidates:
            if made >= n:
                break
            cur = [e for e in self.events
                   if e.company_id == c.company_id and e.valid_to is None]
            if len(cur) < 3:
                continue
            # 受托人必须是当前第一大股东，且拿到 20% 委托后表决权能超过 50%，
            # 否则构造不出“凭表决权控制”的有效样本
            target = max(cur, key=lambda e: e.voting_pct)
            if target.voting_pct + 20.0 <= 50.0:
                continue
            d = date(2025, self.rng.randint(1, 10), 15)
            latest = max(date.fromisoformat(e.event_date) for e in cur)
            if d <= latest:
                d = latest + timedelta(days=self.rng.randint(30, 120))
                if d >= TODAY:
                    continue
            # 目标股东收 20% 表决权委托：股权不变，表决权 +20，其他股东表决权 -20 均摊
            donors = [e for e in cur if e is not target]
            donor_total = sum(e.voting_pct for e in donors)
            cuts = {id(e): round(20.0 * (e.voting_pct / donor_total), 4)
                    for e in donors}
            # 舍入残差并入最后一个 donor，保证表决权严格闭合到 100
            residual = round(20.0 - sum(cuts.values()), 4)
            if donors:
                cuts[id(donors[-1])] = round(cuts[id(donors[-1])] + residual, 4)
            for e in cur:
                e.valid_to = d.isoformat()
            for e in cur:
                if e is target:
                    self.add_event(c.company_id, e.holder_kind, e.holder_id, d,
                                   e.shares_pct, e.voting_pct + 20.0,
                                   note="签一致行动协议，获20%表决权委托")
                else:
                    self.add_event(c.company_id, e.holder_kind, e.holder_id, d,
                                   e.shares_pct, round(e.voting_pct - cuts[id(e)], 4),
                                   note="表决权委托")
            ctrl = next((e for e in cur if e is target), None)
            c.preset_type = "person" if ctrl and ctrl.holder_kind == "person" else "none"
            c.preset_controller_id = ctrl.holder_id if ctrl else None
            c.preset_rationale = "表决权委托后有效表决权超50%，按表决权口径认定控制"
            self._assert_closed(c.company_id, d)
            made += 1

    # -------------------------------------------------- 6. 同名不同人场景
    def build_name_collisions(self, n: int = 25) -> None:
        # 找出现有高频姓名，再造同名人投到不同公司
        name_counts = {name: ids for name, ids in self.duplicate_names.items()
                       if len(ids) >= 1}
        names = list(name_counts)
        for _ in range(n):
            name = self.rng.choice(names)
            twin = self.make_duplicate_person(name)
            try:
                c = self.make_company(f"双生{self.rng.choice(INDUSTRIES)}{_:02d}")
            except TargetReached:
                return
            est = date.fromisoformat(c.established_date)
            self.add_event(c.company_id, "person", twin.person_id, est, 60.0,
                           note=f"股东{name}（证件尾号{twin.id_tail}）")
            self._distribute_rest(c.company_id, est, 40.0)
            c.preset_type = "person"
            c.preset_controller_id = twin.person_id
            c.preset_rationale = f"控制人为{name}，以证件尾号{twin.id_tail}与同名者区分"
            self._assert_closed(c.company_id, est)

    # ---------------------------------------------------------- 7. 变更历史
    def build_change_histories(self, n: int = 350) -> None:
        candidates = [c for c in self.companies.values()
                      if all(e.company_id != c.company_id or
                             (e.valid_to is None) for e in self.events)]
        self.rng.shuffle(candidates)
        for c in candidates[:n]:
            cur = [e for e in self.events
                   if e.company_id == c.company_id and e.valid_to is None]
            if len(cur) < 2:
                continue
            d = date(2025, self.rng.randint(1, 11), self.rng.randint(1, 28))
            latest = max(date.fromisoformat(e.event_date) for e in cur)
            if d <= latest:
                d = latest + timedelta(days=self.rng.randint(30, 150))
                if d >= TODAY:
                    continue
            seller = min(cur, key=lambda e: e.shares_pct)
            if seller.shares_pct < 10:
                continue
            moved = min(seller.shares_pct - 1.0,
                        float(self.rng.choice([5, 8, 10, 15])))
            moved = round(moved, 4)
            seller.valid_to = d.isoformat()
            self.add_event(c.company_id, seller.holder_kind, seller.holder_id, d,
                           round(seller.shares_pct - moved, 4),
                           round(seller.voting_pct - moved, 4), note="股权转让")
            if self.rng.random() < 0.5:
                buyer = self.make_person()
                self.add_event(c.company_id, "person", buyer.person_id, d,
                               moved, note="受让股权（新股东）")
            else:
                try:
                    buyer_c = self.make_company(f"接盘{self.rng.choice(QUALIFIERS)}")
                    self.add_event(c.company_id, "company", buyer_c.company_id, d,
                                   moved, note="机构受让（新股东）")
                except TargetReached:
                    buyer = self.make_person()
                    self.add_event(c.company_id, "person", buyer.person_id, d,
                                   moved, note="受让股权（新股东）")
            self._assert_closed(c.company_id, d)

    # -------------------------------------------------- 8. 孤儿公司补股东表
    def fill_orphan_cap_tables(self) -> int:
        """给所有被创建但没有自身股东事件的公司补一个闭合股东表。"""
        have_events = {e.company_id for e in self.events}
        orphans = [c for cid, c in self.companies.items() if cid not in have_events]
        for c in orphans:
            est = date.fromisoformat(c.established_date)
            founder = self.make_person()
            main = float(self.rng.choice([51.0, 60.0, 67.0, 80.0, 100.0]))
            self.add_event(c.company_id, "person", founder.person_id, est, main)
            r = 100.0 - main
            while r > 1e-9:
                part = min(r, float(self.rng.choice([5, 10, 15, 20])))
                part = round(part, 4)
                if part > r:
                    part = r
                p = self.make_person()
                self.add_event(c.company_id, "person", p.person_id, est, part)
                r = round(r - part, 4)
            if c.preset_type == "none":
                c.preset_type = "person"
                c.preset_controller_id = founder.person_id
                c.preset_rationale = (
                    f"补全股东表：创始人直接持股{main:.0f}%，为实际控制人")
            self._assert_closed(c.company_id, est)
        return len(orphans)

    # ------------------------------------------------------------------ build
    # 特殊结构需要的公司数预留（代持不新增公司；同名25家、纯环3家、其余新增在余量内）
    RESERVED = 60

    def build(self) -> "NetworkBuilder":
        # 集团：数量按目标反推，集团平均产出 ~30 家（含外部公司）
        group_n = 28
        for i in range(group_n):
            try:
                self.build_group(i)
            except TargetReached:
                break
        # 纯环 + 交叉持股（纯环固定 3 家）
        try:
            self.build_cross_holding_groups()
        except TargetReached:
            pass
        # 用独立公司补齐到 target - 预留量
        gap = self.target - self.RESERVED - len(self.companies)
        if gap > 0:
            self.build_standalone(gap)
        # 特殊结构（内部遇到上限会自行停止）
        self.build_nominees(8)
        self.build_voting_arrangements(5)
        self.build_name_collisions(25)
        self.build_change_histories(350)
        orphan_n = self.fill_orphan_cap_tables()
        if orphan_n:
            print(f"[world] 补全孤儿公司股东表 {orphan_n} 家")
        # 别名与详情页模式
        companies = list(self.companies.values())
        picked = self.rng.sample(companies, min(120, len(companies)))
        for c in picked:
            self.add_alias_variants(c)
        for c in companies:
            c.profile_mode = "change" if self.rng.random() < 0.5 else "direct"
        return self
