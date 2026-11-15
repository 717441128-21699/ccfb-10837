"""股权穿透采集引擎（asyncio BFS）。

- 从根公司出发按深度逐层抓股东，深度可设；
- 队列持久化在 task_queue，进程重启可续跑；
- 公司按信用代码/归一化名识别为同一家，自然人按姓名+证件尾号区分；
- direct 模式存当前股东表，change 模式存全量变更事件并回放当前股东；
- 公司型股东自动入队继续向上穿透，自然人是叶子节点。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..analysis.history import replay_events
from ..config import SETTINGS
from ..db import AsyncDB
from ..identity import (company_key, normalize_company_name,
                        normalize_person_name, person_key)
from .accounts import AccountPool
from .client import EntityNotFound, SimClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TaskStats:
    fetched: int = 0
    from_cache: int = 0
    not_found: int = 0
    persons_seen: int = 0
    companies_seen: int = 0
    requests: int = 0
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


class CrawlEngine:
    def __init__(self, db: AsyncDB, pool: AccountPool,
                 concurrency: int | None = None,
                 recrawl_age_hours: int | None = None):
        self.db = db
        self.pool = pool
        self.concurrency = concurrency or SETTINGS.concurrency
        self.recrawl_age_hours = (recrawl_age_hours
                                  if recrawl_age_hours is not None
                                  else SETTINGS.recrawl_age_hours)
        self._stop_flags: set[int] = set()
        # 每个任务运行期的内存队列与去重集合
        self._queues: dict[int, asyncio.Queue] = {}
        self._enqueued: dict[int, set[str]] = {}

    def request_stop(self, task_id: int) -> None:
        self._stop_flags.add(task_id)

    # ----------------------------------------------------------- 任务管理
    async def create_task(self, root_query: str, max_depth: int,
                          follow_nominee: bool = True) -> int:
        task_id = await self.db.execute(
            "INSERT INTO crawl_tasks(root_query,max_depth,follow_nominee,"
            "status,created_at) VALUES (?,?,?,'pending',?)",
            (root_query, max_depth, 1 if follow_nominee else 0, _now()))
        return task_id

    async def run_task(self, task_id: int, client: SimClient) -> TaskStats:
        self._stop_flags.discard(task_id)
        stats = TaskStats()
        await self.db.execute(
            "UPDATE crawl_tasks SET status='running', started_at=? WHERE task_id=?",
            (_now(), task_id))
        try:
            queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
            self._queues[task_id] = queue
            self._enqueued[task_id] = set()
            # 续跑：把上次遗留的 queued 节点灌回内存
            rows = await self.db.query(
                "SELECT company_id, depth FROM task_queue WHERE task_id=? "
                "AND status='queued' ORDER BY depth", (task_id,))
            for r in rows:
                if r["company_id"] not in self._enqueued[task_id]:
                    self._enqueued[task_id].add(r["company_id"])
                    queue.put_nowait((r["company_id"], r["depth"]))

            await self._resolve_root(task_id, client, stats)
            workers = [asyncio.create_task(self._worker(task_id, queue, client, stats))
                       for _ in range(self.concurrency)]
            await queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            status = "stopped" if task_id in self._stop_flags else "done"
            await self.db.execute(
                "UPDATE crawl_tasks SET status=?, finished_at=?, stats_json=? "
                "WHERE task_id=?", (status, _now(), stats.as_json(), task_id))
        except Exception as exc:
            stats.errors.append(repr(exc))
            await self.db.execute(
                "UPDATE crawl_tasks SET status='failed', finished_at=?, "
                "error=?, stats_json=? WHERE task_id=?",
                (_now(), repr(exc), stats.as_json(), task_id))
            raise
        finally:
            self._stop_flags.discard(task_id)
            self._queues.pop(task_id, None)
            self._enqueued.pop(task_id, None)
        return stats

    async def _resolve_root(self, task_id: int, client: SimClient,
                            stats: TaskStats) -> None:
        row = await self.db.query_one(
            "SELECT root_company_id, root_query FROM crawl_tasks WHERE task_id=?",
            (task_id,))
        if row and row["root_company_id"]:
            return
        query = row["root_query"]
        results = await client.search(query)
        stats.requests += 1
        if not results:
            raise RuntimeError(f"搜索无结果: {query}")
        # 优先精确归一化匹配，否则取第一个
        target_norm = normalize_company_name(query)
        best = next((r for r in results
                     if normalize_company_name(r["name"]) == target_norm), results[0])
        await self.db.execute(
            "UPDATE crawl_tasks SET root_company_id=? WHERE task_id=?",
            (best["company_id"], task_id))
        await self._enqueue(task_id, best["company_id"], 0)

    async def _worker(self, task_id: int, queue: asyncio.Queue,
                      client: SimClient, stats: TaskStats) -> None:
        while True:
            if task_id in self._stop_flags:
                return
            company_id, depth = await queue.get()
            try:
                task = await self.db.query_one(
                    "SELECT max_depth FROM crawl_tasks WHERE task_id=?", (task_id,))
                if depth > task["max_depth"]:
                    await self.db.execute(
                        "UPDATE task_queue SET status='skipped_depth' "
                        "WHERE task_id=? AND company_id=?", (task_id, company_id))
                    continue
                detail = await self._fetch_company(task_id, company_id, client, stats)
                if detail is None:
                    continue
                await self._store_detail(task_id, detail, stats)
                await self.db.execute(
                    "UPDATE task_queue SET status='done' WHERE task_id=? "
                    "AND company_id=?", (task_id, company_id))
                # 公司型股东入队（depth+1）
                for sh in self._current_holders(detail):
                    if sh["holder_kind"] != "company":
                        continue
                    await self._enqueue(task_id, sh["holder_id"], depth + 1)
            except EntityNotFound:
                stats.not_found += 1
                await self.db.execute(
                    "UPDATE task_queue SET status='done' WHERE task_id=? "
                    "AND company_id=?", (task_id, company_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats.errors.append(f"{company_id}: {exc!r}")
                # 保留 queued 状态以便续跑
                print(f"[engine] {company_id} 抓取失败: {exc!r}", flush=True)
            finally:
                queue.task_done()

    # ------------------------------------------------------------- 抓取
    async def _fetch_company(self, task_id: int, company_id: str,
                             client: SimClient, stats: TaskStats) -> dict | None:
        fresh = await self._is_fresh(company_id)
        if fresh:
            stats.from_cache += 1
            # 缓存命中也要把详情拼出来给股东入队逻辑用
            return await self._detail_from_cache(company_id)
        detail = await client.company_detail(company_id)
        stats.requests += 1
        stats.fetched += 1
        return detail

    async def _is_fresh(self, company_id: str) -> bool:
        row = await self.db.query_one(
            "SELECT last_synced_at FROM companies WHERE company_id=?", (company_id,))
        if not row or not row["last_synced_at"]:
            return False
        try:
            synced = datetime.fromisoformat(row["last_synced_at"])
            age_h = (datetime.now(timezone.utc) - synced).total_seconds() / 3600
            return age_h < self.recrawl_age_hours
        except ValueError:
            return False

    async def _detail_from_cache(self, company_id: str) -> dict:
        company = await self.db.query_one(
            "SELECT * FROM companies WHERE company_id=?", (company_id,))
        events = await self.db.query(
            "SELECT * FROM holding_events_raw WHERE company_id=? "
            "ORDER BY event_date, change_no", (company_id,))
        if events:
            return {"company": dict(company), "profile_mode": "change",
                    "change_records": [dict(e) for e in events]}
        holders = await self.db.query(
            "SELECT * FROM holdings_raw WHERE company_id=?", (company_id,))
        return {"company": dict(company), "profile_mode": "direct",
                "shareholders": [dict(h) for h in holders]}

    # ------------------------------------------------------------- 入库
    async def _store_detail(self, task_id: int, detail: dict,
                            stats: TaskStats) -> None:
        c = detail["company"]
        cid = c["company_id"]
        # 实体识别只认公司 id / 统一社会信用代码（需求 3）。
        # 归一化名只用于搜索召回，绝不能把信用代码不同的两家公司并成一家。
        existing = await self.db.query_one(
            "SELECT company_id FROM companies WHERE company_id=? OR "
            "(credit_code IS NOT NULL AND credit_code != '' AND credit_code=?)",
            (cid, c.get("credit_code") or ""))
        statements = []
        if not existing:
            stats.companies_seen += 1
            statements.append((
                "INSERT INTO companies(company_id,name,credit_code,normalized_name,"
                "established_date,industry,region,profile_mode,first_task_id,"
                "discovered_at,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, c["name"], c.get("credit_code"),
                 normalize_company_name(c["name"]), c.get("established_date"),
                 c.get("industry"), c.get("region"), detail["profile_mode"],
                 task_id, _now(), _now())))
        else:
            statements.append((
                "UPDATE companies SET name=?, credit_code=?, normalized_name=?, "
                "established_date=?, industry=?, region=?, profile_mode=?, "
                "last_synced_at=? WHERE company_id=?",
                (c["name"], c.get("credit_code"),
                 normalize_company_name(c["name"]), c.get("established_date"),
                 c.get("industry"), c.get("region"), detail["profile_mode"],
                 _now(), existing["company_id"])))
            if existing["company_id"] != cid:
                # 仿真站 id 与本地实体 id 对齐（同一家公司的不同写法）
                cid = existing["company_id"]
        for alias in c.get("aliases", []):
            statements.append((
                "INSERT OR IGNORE INTO company_aliases(company_id,alias) VALUES (?,?)",
                (cid, alias)))

        # 先清旧快照/事件再重写（同一公司的最新完整采集）
        statements.append(
            ("DELETE FROM holdings_raw WHERE company_id=?", (cid,)))
        statements.append(
            ("DELETE FROM holding_events_raw WHERE company_id=?", (cid,)))

        holders = self._current_holders(detail)
        for h in holders:
            name, tail = self._holder_identity(h)
            if h["holder_kind"] == "person":
                stats.persons_seen += 1
                statements.append((
                    "INSERT OR IGNORE INTO persons(person_id,name,id_tail) "
                    "VALUES (?,?,?)", (h["holder_id"], name, tail)))
            statements.append((
                "INSERT OR REPLACE INTO holdings_raw(company_id,holder_kind,"
                "holder_id,holder_name,holder_id_tail,shares_pct,voting_pct,"
                "is_nominee,beneficial_holder_id,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, h["holder_kind"], h["holder_id"], name, tail,
                 h["shares_pct"], h["voting_pct"], int(h.get("is_nominee", 0) or 0),
                 h.get("beneficial_holder_id"), _now())))

        if detail["profile_mode"] == "change":
            for ev in detail["change_records"]:
                name, tail = self._holder_identity(ev)
                if ev["holder_kind"] == "person":
                    statements.append((
                        "INSERT OR IGNORE INTO persons(person_id,name,id_tail) "
                        "VALUES (?,?,?)", (ev["holder_id"], name, tail)))
                statements.append((
                    "INSERT OR REPLACE INTO holding_events_raw(event_id,company_id,"
                    "holder_kind,holder_id,holder_name,event_date,valid_to,"
                    "shares_pct,voting_pct,is_nominee,beneficial_holder_id,"
                    "change_no,note,fetched_at) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.get("event_id"), cid, ev["holder_kind"], ev["holder_id"],
                     name, ev["event_date"], ev.get("valid_to"), ev["shares_pct"],
                     ev["voting_pct"], int(ev.get("is_nominee", 0) or 0),
                     ev.get("beneficial_holder_id"), ev.get("change_no", 0),
                     ev.get("note", ""), _now())))
        await self.db.transaction(statements)

    @staticmethod
    def _holder_identity(h: dict) -> tuple[str, str | None]:
        if h["holder_kind"] == "person":
            return h.get("name", h["holder_id"]), h.get("id_tail")
        return h.get("name", h["holder_id"]), h.get("credit_code")

    @staticmethod
    def _current_holders(detail: dict) -> list[dict]:
        if detail["profile_mode"] == "direct":
            return detail["shareholders"]
        return replay_events(detail["change_records"])

    async def _enqueue(self, task_id: int, company_id: str, depth: int) -> bool:
        """入队一个待抓公司；已在本任务出现过则返回 False。"""
        seen = self._enqueued.get(task_id)
        if seen is not None and company_id in seen:
            return False
        row = await self.db.query_one(
            "SELECT 1 FROM task_queue WHERE task_id=? AND company_id=?",
            (task_id, company_id))
        if row:
            if seen is not None:
                seen.add(company_id)
            return False
        await self.db.execute(
            "INSERT INTO task_queue(task_id,company_id,depth,status) "
            "VALUES (?,?,?,'queued')", (task_id, company_id, depth))
        if seen is not None:
            seen.add(company_id)
        queue = self._queues.get(task_id)
        if queue is not None:
            queue.put_nowait((company_id, depth))
        return True
