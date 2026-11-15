"""网页后端 API（aiohttp）。

模块：
- /api/tasks      查询任务（创建、列表、详情、停止）
- /api/companies  企业库（搜索、详情、股东表）
- /api/graph      股权穿透图（含环标记、任意日期回放）
- /api/controller 实控人判定 + 最终受益人 + 规则配置
- /api/accounts   账号配额监控与启停
- /api/verify     核对报告
爬取引擎在本进程内常驻运行，任务通过后台 asyncio task 异步执行。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

from ..analysis.controller import normalize_rules
from ..analysis.service import AnalysisService
from ..analysis.verify import run_verification
from ..config import SETTINGS
from ..crawler.accounts import AccountPool
from ..crawler.client import SimClient
from ..crawler.engine import CrawlEngine
from ..db import AsyncDB, connect
from ..identity import normalize_company_name

routes = web.RouteTableDef()
STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebContext:
    def __init__(self) -> None:
        self.db = AsyncDB(SETTINGS.warehouse_db)
        self.pool = AccountPool(self.db, SETTINGS.sim_base_url)
        self.engine: CrawlEngine | None = None
        self.service = AnalysisService()
        self.running: dict[int, object] = {}


CTX: WebContext | None = None


def ctx(request: web.Request) -> WebContext:
    return request.app["ctx"]


# ------------------------------------------------------------------ 任务
@routes.get("/api/tasks")
async def list_tasks(request: web.Request) -> web.Response:
    c = ctx(request)
    rows = await c.db.query(
        "SELECT t.*, ("
        "  SELECT COUNT(*) FROM task_queue q WHERE q.task_id=t.task_id"
        " ) total_nodes, ("
        "  SELECT COUNT(*) FROM task_queue q WHERE q.task_id=t.task_id "
        "  AND q.status='queued') queued_nodes, ("
        "  SELECT COUNT(*) FROM task_queue q WHERE q.task_id=t.task_id "
        "  AND q.status='done') done_nodes "
        "FROM crawl_tasks t ORDER BY task_id DESC LIMIT 50")
    out = []
    for r in rows:
        d = dict(r)
        if d.get("stats_json"):
            try:
                d["stats"] = json.loads(d["stats_json"])
            except json.JSONDecodeError:
                d["stats"] = None
        out.append(d)
    return web.json_response(out)


@routes.post("/api/tasks")
async def create_task(request: web.Request) -> web.Response:
    c = ctx(request)
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return web.json_response({"error": "query 必填"}, status=400)
    depth = int(body.get("max_depth", 3))
    depth = max(0, min(depth, 20))
    follow_nominee = bool(body.get("follow_nominee", True))
    task_id = await c.engine.create_task(query, depth, follow_nominee)

    async def runner() -> None:
        try:
            async with SimClient(c.pool) as client:
                await c.engine.run_task(task_id, client)
        except Exception as exc:  # 引擎已落 failed 状态
            print(f"[web] task {task_id} failed: {exc!r}", flush=True)
        finally:
            c.running.pop(task_id, None)

    c.running[task_id] = asyncio.create_task(runner())
    return web.json_response({"task_id": task_id})


@routes.post("/api/tasks/{task_id}/stop")
async def stop_task(request: web.Request) -> web.Response:
    c = ctx(request)
    task_id = int(request.match_info["task_id"])
    c.engine.request_stop(task_id)
    return web.json_response({"stopping": task_id})


# ------------------------------------------------------------------ 企业库
@routes.get("/api/companies")
async def search_companies(request: web.Request) -> web.Response:
    c = ctx(request)
    q = request.query.get("q", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    if q:
        norm = normalize_company_name(q)
        rows = await c.db.query(
            "SELECT * FROM companies WHERE name LIKE ? OR credit_code LIKE ? "
            "OR company_id=? OR normalized_name LIKE ? "
            "ORDER BY last_synced_at DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", q, f"%{norm}%", limit))
    else:
        rows = await c.db.query(
            "SELECT * FROM companies ORDER BY discovered_at DESC LIMIT ?",
            (limit,))
    return web.json_response([dict(r) for r in rows])


@routes.get("/api/companies/{company_id}")
async def company_detail(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    company = await c.db.query_one(
        "SELECT * FROM companies WHERE company_id=?", (cid,))
    if not company:
        return web.json_response({"error": "not_found"}, status=404)
    holders = await c.db.query(
        "SELECT * FROM holdings_raw WHERE company_id=? "
        "ORDER BY voting_pct DESC", (cid,))
    events = await c.db.query(
        "SELECT * FROM holding_events_raw WHERE company_id=? "
        "ORDER BY event_date, change_no", (cid,))
    aliases = await c.db.query(
        "SELECT alias FROM company_aliases WHERE company_id=?", (cid,))
    return web.json_response({
        "company": dict(company),
        "holders": [dict(h) for h in holders],
        "events": [dict(e) for e in events],
        "aliases": [r["alias"] for r in aliases],
    })


# ------------------------------------------------------------------ 股权图
@routes.get("/api/graph/{company_id}")
async def equity_graph(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    depth_raw = request.query.get("depth")
    max_depth = int(depth_raw) if depth_raw else None
    as_of = request.query.get("as_of") or None
    try:
        data = c.service.equity_tree(
            cid, max_depth=max_depth, as_of=as_of)
    except KeyError:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(data)


@routes.get("/api/companies/{company_id}/history")
async def company_history(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    try:
        return web.json_response(c.service.history_timeline(cid))
    except KeyError:
        return web.json_response({"error": "not_found"}, status=404)


@routes.get("/api/companies/{company_id}/cap-table")
async def cap_table(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    as_of = request.query.get("as_of")
    if not as_of:
        return web.json_response({"error": "as_of 必填 (YYYY-MM-DD)"},
                                 status=400)
    try:
        return web.json_response(c.service.cap_table_as_of(cid, as_of))
    except KeyError:
        return web.json_response({"error": "not_found"}, status=404)


# ------------------------------------------------------------------ 实控人
@routes.get("/api/controller/{company_id}")
async def controller_of(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    as_of = request.query.get("as_of") or None
    rules = await _active_rules(request)
    try:
        return web.json_response(
            c.service.controller_of(cid, rules=rules, as_of=as_of))
    except KeyError:
        return web.json_response({"error": "not_found"}, status=404)


@routes.get("/api/controller/{company_id}/beneficiaries")
async def beneficiaries(request: web.Request) -> web.Response:
    c = ctx(request)
    cid = request.match_info["company_id"]
    rules = await _active_rules(request)
    return web.json_response(
        c.service.ultimate_beneficiaries(cid, rules=rules))


async def _active_rules(request: web.Request) -> dict:
    c = ctx(request)
    row = await c.db.query_one(
        "SELECT rules_json FROM rule_configs WHERE is_active=1 "
        "ORDER BY config_id DESC LIMIT 1")
    if not row:
        return normalize_rules(None)
    return normalize_rules(json.loads(row["rules_json"]))


@routes.get("/api/rules")
async def get_rules(request: web.Request) -> web.Response:
    c = ctx(request)
    rows = await c.db.query(
        "SELECT config_id, name, rules_json, is_active, created_at "
        "FROM rule_configs ORDER BY config_id DESC")
    return web.json_response([dict(r) for r in rows])


@routes.post("/api/rules")
async def save_rules(request: web.Request) -> web.Response:
    c = ctx(request)
    body = await request.json()
    name = (body.get("name") or "自定义规则").strip()
    rules = normalize_rules(body.get("rules"))
    # execute/transaction 内部已加写锁，这里不能再嵌套获取（asyncio.Lock 不可重入）
    await c.db.execute("UPDATE rule_configs SET is_active=0")
    rid = await c.db.execute(
        "INSERT INTO rule_configs(name,rules_json,is_active,created_at) "
        "VALUES (?,? ,1, date('now'))",
        (name, json.dumps(rules, ensure_ascii=False)))
    return web.json_response({"config_id": rid, "rules": rules})


# ------------------------------------------------------------------ 配额
@routes.get("/api/accounts")
async def accounts(request: web.Request) -> web.Response:
    c = ctx(request)
    snap = await c.pool.snapshot()
    sim_day = c.pool.current_sim_day
    return web.json_response({"sim_day": sim_day, "accounts": snap})


@routes.post("/api/accounts/{account_id}/toggle")
async def toggle_account(request: web.Request) -> web.Response:
    c = ctx(request)
    aid = request.match_info["account_id"]
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    await c.pool.set_enabled(aid, enabled)
    return web.json_response({"account_id": aid, "enabled": enabled})


# ------------------------------------------------------------------ 核对
@routes.get("/api/verify/latest")
async def latest_verify(request: web.Request) -> web.Response:
    c = ctx(request)
    row = await c.db.query_one(
        "SELECT * FROM verify_reports ORDER BY report_id DESC LIMIT 1")
    if not row:
        return web.json_response(None)
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json"))
    return web.json_response(d)


@routes.post("/api/verify/run")
async def run_verify(request: web.Request) -> web.Response:
    report = run_verification(save=True)
    return web.json_response({
        "total": report.total, "matched": report.matched,
        "mismatched": report.mismatched, "unknown": report.unknown,
        "accuracy": report.accuracy,
        "details": [d.__dict__ for d in report.details],
    })


# ------------------------------------------------------------------ 统计
@routes.get("/api/stats")
async def stats(request: web.Request) -> web.Response:
    c = ctx(request)
    companies = (await c.db.query_one(
        "SELECT COUNT(*) n FROM companies"))["n"]
    persons = (await c.db.query_one(
        "SELECT COUNT(*) n FROM persons"))["n"]
    tasks = (await c.db.query_one(
        "SELECT COUNT(*) n FROM crawl_tasks"))["n"]
    return web.json_response({
        "companies": companies, "persons": persons, "tasks": tasks,
        "sim_day": c.pool.current_sim_day})


async def on_startup(app: web.Application) -> None:
    global CTX
    CTX = WebContext()
    app["ctx"] = CTX
    await CTX.pool.start()
    CTX.engine = CrawlEngine(CTX.db, CTX.pool)


async def on_cleanup(app: web.Application) -> None:
    c = app["ctx"]
    await c.pool.close()
    c.db.close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes(routes)
    app.router.add_static("/static", STATIC_DIR, show_index=False)

    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")
    app.router.add_get("/", index)
    return app


def main() -> None:
    web.run_app(create_app(), host=SETTINGS.web_host, port=SETTINGS.web_port)


if __name__ == "__main__":
    main()
