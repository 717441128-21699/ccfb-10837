"""仿真工商公示网站（aiohttp）。

特性：
- 每个账号每虚拟日有查询上限，超出返回 HTTP 429 + Retry-After；
- 虚拟时钟按 ``virtual_day_seconds`` 自动推进，也可由管理接口手动推进；
- 企业详情分两种资料模式：
  * direct：直接返回当前股东表；
  * change：只返回完整变更事件流，调用方需自行回放得到当前股东；
- 搜索支持信用代码、名称、别名、归一化名称（半角全角/后缀差异）。
"""
from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from aiohttp import web

from ..config import SETTINGS
from ..db import connect
from ..identity import normalize_company_name
from ..analysis.history import replay_events, event_timeline

routes = web.RouteTableDef()


class SimState:
    def __init__(self) -> None:
        self.db = connect(SETTINGS.sim_db, readonly=True)
        self.clock_path = Path(SETTINGS.clock_state)
        state = json.loads(self.clock_path.read_text(encoding="utf-8"))
        self.epoch = int(state.get("epoch", 0))
        self.day0 = str(state.get("day", "2026-11-15"))
        self.day_seconds = SETTINGS.virtual_day_seconds
        self.boot_real = time.time()

    # ------------------------------------------------------------ 虚拟时钟
    @property
    def today(self) -> str:
        elapsed_days = self.epoch + int((time.time() - self.boot_real) // self.day_seconds)
        y, m, d = map(int, self.day0.split("-"))
        from datetime import date, timedelta
        return (date(y, m, d) + timedelta(days=elapsed_days)).isoformat()

    def advance_day(self, n: int = 1) -> str:
        self.epoch += n
        self._persist_clock()
        return self.today

    def reset_quota(self) -> None:
        """管理接口：清零所有账号当天用量（模拟新一天的服务端重置）。"""
        db = connect(SETTINGS.sim_db)
        db.execute("DELETE FROM sim_quota_usage")
        db.commit()
        db.close()

    def _persist_clock(self) -> None:
        self.clock_path.write_text(
            json.dumps({"day": self.day0, "epoch": self.epoch},
                       ensure_ascii=False),
            encoding="utf-8")

    def close(self) -> None:
        self.db.close()


def _state(request: web.Request) -> SimState:
    return request.app["sim_state"]


# ------------------------------------------------------------- 鉴权与配额
def _authenticate(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    api_key = auth[7:].strip()
    row = _state(request).db.execute(
        "SELECT account_id FROM sim_accounts WHERE api_key=?", (api_key,)
    ).fetchone()
    return row["account_id"] if row else None


def _consume_quota(request: web.Request, account_id: str) -> tuple[bool, int]:
    """返回 (是否放行, 剩余额度)。放行会把当天用量 +1。"""
    st = _state(request)
    # 只读连接不能写，单独开一个短连接，避免读写混用
    db = connect(SETTINGS.sim_db)
    try:
        row = db.execute(
            "SELECT used FROM sim_quota_usage WHERE account_id=? AND day=?",
            (account_id, st.today)).fetchone()
        quota = db.execute(
            "SELECT daily_quota FROM sim_accounts WHERE account_id=?",
            (account_id,)).fetchone()["daily_quota"]
        used = row["used"] if row else 0
        if used >= quota:
            return False, 0
        if row:
            db.execute(
                "UPDATE sim_quota_usage SET used=used+1 WHERE account_id=? AND day=?",
                (account_id, st.today))
        else:
            db.execute(
                "INSERT INTO sim_quota_usage(account_id,day,used) VALUES (?,?,1)",
                (account_id, st.today))
        db.commit()
        return True, quota - used - 1
    finally:
        db.close()


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        return web.json_response(
            {"error": "internal_error",
             "detail": traceback.format_exc(limit=3)}, status=500)


@web.middleware
async def quota_middleware(request: web.Request, handler):
    # 健康检查与管理接口不走账号配额
    if request.path in ("/health", "/admin/clock", "/admin/reset-quota"):
        return await handler(request)
    if request.path not in ("/api/search", "/api/company"):
        return await handler(request)
    account_id = _authenticate(request)
    if account_id is None:
        return web.json_response({"error": "invalid_api_key"}, status=401)
    ok, remaining = _consume_quota(request, account_id)
    if not ok:
        # Retry-After：到下一个虚拟日的真实秒数
        st = _state(request)
        into_day = int((time.time() - st.boot_real) % st.day_seconds)
        retry_after = st.day_seconds - into_day + 1
        return web.json_response(
            {"error": "quota_exhausted",
             "message": f"账号 {account_id} 当日查询次数已用完，次日自动恢复",
             "day": st.today},
            status=429, headers={"Retry-After": str(retry_after)})
    resp = await handler(request)
    resp.headers["X-Account-Id"] = account_id
    resp.headers["X-Quota-Remaining"] = str(remaining)
    resp.headers["X-Sim-Day"] = _state(request).today
    return resp


# ------------------------------------------------------------------ 路由
@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    st = _state(request)
    return web.json_response({"status": "ok", "sim_day": st.today})


@routes.post("/api/login")
async def login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad_json"}, status=400)
    api_key = (body or {}).get("api_key", "")
    row = _state(request).db.execute(
        "SELECT account_id, owner_name, daily_quota FROM sim_accounts "
        "WHERE api_key=?", (api_key,)).fetchone()
    if not row:
        return web.json_response({"error": "invalid_api_key"}, status=401)
    return web.json_response({"account_id": row["account_id"],
                              "owner_name": row["owner_name"],
                              "daily_quota": row["daily_quota"],
                              "token": api_key})


@routes.get("/api/search")
async def search(request: web.Request) -> web.Response:
    st = _state(request)
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "missing_q"}, status=400)
    norm = normalize_company_name(q)
    # 1) 企业 id / 信用代码精确 2) 名称/别名精确 3) 归一化名匹配 4) 子串召回
    rows = st.db.execute(
        "SELECT c.company_id, c.name, c.credit_code, c.profile_mode FROM companies c "
        "WHERE c.company_id=? OR c.credit_code=? OR c.name=? OR c.company_id IN "
        "(SELECT company_id FROM company_aliases WHERE alias=?) "
        "LIMIT 10", (q, q.upper(), q, q)).fetchall()
    if not rows:
        like = f"%{q}%"
        rows = st.db.execute(
            "SELECT c.company_id, c.name, c.credit_code, c.profile_mode FROM companies c "
            "LEFT JOIN company_aliases a ON a.company_id=c.company_id "
            "WHERE c.name LIKE ? OR a.alias LIKE ? "
            "GROUP BY c.company_id ORDER BY LENGTH(c.name) LIMIT 20",
            (like, like)).fetchall()
    if not rows:
        # 归一化名兜底：把库里名称也归一后比较（量小，Python 侧过滤）
        all_rows = st.db.execute(
            "SELECT company_id, name, credit_code, profile_mode FROM companies").fetchall()
        rows = [r for r in all_rows
                if normalize_company_name(r["name"]) == norm][:20]
    return web.json_response({"query": q, "sim_day": st.today,
                              "results": [dict(r) for r in rows]})


def _person_map(st: SimState) -> dict[str, dict]:
    return {r["person_id"]: dict(r)
            for r in st.db.execute("SELECT person_id,name,id_tail,gender FROM persons")}


def _holder_name(st: SimState, kind: str, holder_id: str,
                 persons: dict[str, dict]) -> dict:
    if kind == "person":
        p = persons.get(holder_id, {"name": holder_id, "id_tail": ""})
        return {"holder_id": holder_id, "holder_kind": "person",
                "name": p["name"], "id_tail": p.get("id_tail", "")}
    row = st.db.execute(
        "SELECT company_id, name, credit_code FROM companies WHERE company_id=?",
        (holder_id,)).fetchone()
    if row:
        return {"holder_id": holder_id, "holder_kind": "company",
                "name": row["name"], "credit_code": row["credit_code"]}
    return {"holder_id": holder_id, "holder_kind": kind, "name": holder_id}


@routes.get("/api/company")
async def company_detail(request: web.Request) -> web.Response:
    st = _state(request)
    cid = request.query.get("company_id", "").strip()
    company = st.db.execute(
        "SELECT * FROM companies WHERE company_id=?", (cid,)).fetchone()
    if not company:
        return web.json_response({"error": "not_found"}, status=404)
    persons = _person_map(st)
    company_dict = dict(company)
    aliases = [r["alias"] for r in st.db.execute(
        "SELECT alias FROM company_aliases WHERE company_id=?", (cid,))]
    company_dict["aliases"] = aliases

    if company["profile_mode"] == "direct":
        # 直接给当前股东表
        raw = st.db.execute(
            "SELECT * FROM holdings WHERE company_id=? ORDER BY shares_pct DESC",
            (cid,)).fetchall()
        shareholders = []
        for r in raw:
            item = dict(r)
            item.update(_holder_name(st, r["holder_kind"], r["holder_id"], persons))
            if r["beneficial_holder_id"]:
                bp = persons.get(r["beneficial_holder_id"])
                item["beneficial_holder"] = (
                    {"person_id": bp["person_id"], "name": bp["name"],
                     "id_tail": bp["id_tail"]} if bp else
                    {"person_id": r["beneficial_holder_id"]})
            shareholders.append(item)
        return web.json_response({
            "company": company_dict,
            "profile_mode": "direct",
            "sim_day": st.today,
            "shareholders": shareholders,
        })

    # change 模式：只给变更记录，当前股东要自己回放
    raw_events = st.db.execute(
        "SELECT * FROM holding_events WHERE company_id=? ORDER BY event_date, change_no",
        (cid,)).fetchall()
    changes = []
    for r in raw_events:
        item = dict(r)
        item.update(_holder_name(st, r["holder_kind"], r["holder_id"], persons))
        changes.append(item)
    return web.json_response({
        "company": company_dict,
        "profile_mode": "change",
        "sim_day": st.today,
        "change_records": changes,
        "change_dates": event_timeline(raw_events),
        "hint": "本企业不直接公示股东表，请依据变更记录自行回放",
    })


# ------------------------------------------------------------------ 管理
@routes.post("/admin/clock")
async def admin_clock(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Token") != SETTINGS.admin_token:
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    days = int((body or {}).get("days", 1))
    today = _state(request).advance_day(days)
    return web.json_response({"sim_day": today, "advanced_days": days})


@routes.post("/admin/reset-quota")
async def admin_reset_quota(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Token") != SETTINGS.admin_token:
        return web.json_response({"error": "forbidden"}, status=403)
    _state(request).reset_quota()
    return web.json_response({"reset": True, "sim_day": _state(request).today})


def create_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware, quota_middleware])
    app["sim_state"] = SimState()
    app.add_routes(routes)
    return app


def main() -> None:
    app = create_app()
    web.run_app(app, host=SETTINGS.sim_host, port=SETTINGS.sim_port,
                print=lambda *a: print(*a, flush=True))


if __name__ == "__main__":
    main()
