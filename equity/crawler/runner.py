"""采集器命令行入口。

用法：
  py -m equity.crawler.runner init                # 初始化仓库+账号
  py -m equity.crawler.runner crawl "公司名" 3     # 深度 3 的穿透任务
  py -m equity.crawler.runner list                # 查看任务
  py -m equity.crawler.runner quota               # 查看账号配额
"""
from __future__ import annotations

import argparse
import asyncio
import json

from ..config import SETTINGS
from ..db import AsyncDB
from .accounts import AccountPool
from .client import SimClient
from .engine import CrawlEngine
from .seed import init_warehouse, seed_accounts


async def cmd_crawl(query: str, depth: int, follow_nominee: bool) -> None:
    db = AsyncDB(SETTINGS.warehouse_db)
    pool = AccountPool(db, SETTINGS.sim_base_url)
    await pool.start()
    try:
        engine = CrawlEngine(db, pool)
        task_id = await engine.create_task(query, depth, follow_nominee)
        print(f"[runner] 任务 #{task_id} 已创建：root={query!r} depth={depth}")
        async with SimClient(pool) as client:
            stats = await engine.run_task(task_id, client)
        print("[runner] 完成:", json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
        print("[runner] 配额:")
        for s in await pool.snapshot():
            print(" ", s)
    finally:
        await pool.close()
        db.close()


async def cmd_list() -> None:
    db = AsyncDB(SETTINGS.warehouse_db, readonly=True)
    rows = await db.query(
        "SELECT t.*, (SELECT COUNT(*) FROM task_queue q WHERE q.task_id=t.task_id "
        "AND q.status='queued') queued FROM crawl_tasks t ORDER BY task_id DESC "
        "LIMIT 20")
    for r in rows:
        print(f"#{r['task_id']} [{r['status']}] root={r['root_query']!r} "
              f"depth={r['max_depth']} queued={r['queued']} "
              f"root_id={r['root_company_id']}")
        if r["stats_json"]:
            print("   stats:", r["stats_json"])
    db.close()


async def cmd_quota() -> None:
    db = AsyncDB(SETTINGS.warehouse_db)
    pool = AccountPool(db, SETTINGS.sim_base_url)
    await pool.start()
    try:
        for s in await pool.snapshot():
            print(json.dumps(s, ensure_ascii=False))
    finally:
        await pool.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="股权穿透采集器")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p_crawl = sub.add_parser("crawl")
    p_crawl.add_argument("query")
    p_crawl.add_argument("depth", type=int)
    p_crawl.add_argument("--no-nominee", action="store_true",
                         help="不穿透代持")
    sub.add_parser("list")
    sub.add_parser("quota")
    args = parser.parse_args()

    if args.cmd == "init":
        init_warehouse()
        n = seed_accounts()
        print(f"仓库就绪，账号 {n} 个")
    elif args.cmd == "crawl":
        asyncio.run(cmd_crawl(args.query, args.depth, not args.no_nominee))
    elif args.cmd == "list":
        asyncio.run(cmd_list())
    elif args.cmd == "quota":
        asyncio.run(cmd_quota())


if __name__ == "__main__":
    main()
