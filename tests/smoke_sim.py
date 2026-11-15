"""仿真站冒烟测试：登录/搜索/两种详情页/配额/虚拟时钟。"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json

import aiohttp

from equity.config import SETTINGS
from equity.analysis.history import replay_events

BASE = SETTINGS.sim_base_url


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        # 健康检查
        async with s.get(f"{BASE}/health") as r:
            print("health:", r.status, await r.json())

        # 错误 key
        async with s.post(f"{BASE}/api/login", json={"api_key": "bad"}) as r:
            print("bad login:", r.status)

        # 正常登录
        async with s.post(f"{BASE}/api/login",
                          json={"api_key": "key-A01-secret"}) as r:
            login = await r.json()
            print("login:", login)
        headers = {"Authorization": "Bearer key-A01-secret"}

        # 无认证
        async with s.get(f"{BASE}/api/search?q=test") as r:
            print("no-auth search:", r.status)

        # 搜索（找一家 change 模式和一家 direct 模式的公司）
        async with s.get(f"{BASE}/api/search?q=环宇1号", headers=headers) as r:
            data = await r.json()
            print("search 环宇1号:", json.dumps(data["results"], ensure_ascii=False))
            ring_id = data["results"][0]["company_id"]

        # 全角/半角/后缀变体搜索（用库里的真实别名）
        import sqlite3
        db = sqlite3.connect(SETTINGS.sim_db)
        db.row_factory = sqlite3.Row
        alias_row = db.execute(
            "SELECT c.company_id, c.name, a.alias FROM company_aliases a "
            "JOIN companies c ON c.company_id=a.company_id LIMIT 1").fetchone()
        async with s.get(
                f"{BASE}/api/search",
                params={"q": alias_row["alias"]}, headers=headers) as r:
            res = await r.json()
            ids = [x["company_id"] for x in res["results"]]
            print(f"别名搜索 '{alias_row['alias']}' -> 命中 {alias_row['company_id']}:",
                  alias_row["company_id"] in ids)

        change_row = db.execute(
            "SELECT company_id FROM companies WHERE profile_mode='change' LIMIT 1"
        ).fetchone()
        direct_row = db.execute(
            "SELECT company_id FROM companies WHERE profile_mode='direct' LIMIT 1"
        ).fetchone()

        # direct 详情
        async with s.get(f"{BASE}/api/company",
                         params={"company_id": direct_row["company_id"]},
                         headers=headers) as r:
            d = await r.json()
            total = round(sum(x["shares_pct"] for x in d["shareholders"]), 4)
            print(f"direct 详情 {direct_row['company_id']}: "
                  f"{len(d['shareholders'])} 个股东, 合计 {total}%")

        # change 详情 + 本地回放
        async with s.get(f"{BASE}/api/company",
                         params={"company_id": change_row["company_id"]},
                         headers=headers) as r:
            d = await r.json()
            print(f"change 详情 {change_row['company_id']}: "
                  f"{len(d['change_records'])} 条变更, 日期 {d['change_dates']}")
            replayed = replay_events(d["change_records"])
            total = round(sum(x["shares_pct"] for x in replayed), 4)
            print("  回放得到", len(replayed), "个当前股东, 合计", total, "%")
            # 与服务端快照核对
            snap = db.execute(
                "SELECT holder_kind, holder_id, shares_pct FROM holdings "
                "WHERE company_id=?", (change_row["company_id"],)).fetchall()
            replay_map = {(x["holder_kind"], x["holder_id"]): x["shares_pct"]
                          for x in replayed}
            ok = all(abs(replay_map.get((x["holder_kind"], x["holder_id"]), -1)
                         - x["shares_pct"]) < 1e-6 for x in snap)
            print("  回放结果与快照逐笔一致:", ok)

        # 配额：把 A02 用到上限
        h2 = {"Authorization": "Bearer key-A02-secret"}
        quota = SETTINGS.daily_quota
        statuses = []
        for i in range(quota + 3):
            async with s.get(f"{BASE}/api/company",
                             params={"company_id": ring_id},
                             headers=h2) as r:
                statuses.append(r.status)
                if r.status == 429:
                    print("429 headers:", dict(r.headers))
                    body = await r.json()
                    print("429 body:", body)
                    break
        print(f"A02 连续请求: 前两次 {statuses[:2]}, 末尾 {statuses[-3:]},"
              f" 共 {len(statuses)} 次")

        # A01 仍可用（账号间互不影响）
        async with s.get(f"{BASE}/api/company",
                         params={"company_id": ring_id}, headers=headers) as r:
            print("A01 仍可用:", r.status)

        # 管理接口推进虚拟日 + 重置配额
        async with s.post(f"{BASE}/admin/clock",
                          headers={"X-Admin-Token": SETTINGS.admin_token},
                          json={"days": 1}) as r:
            print("advance clock:", await r.json())
        async with s.post(f"{BASE}/admin/reset-quota",
                          headers={"X-Admin-Token": SETTINGS.admin_token}) as r:
            print("reset quota:", await r.json())
        async with s.get(f"{BASE}/api/company",
                         params={"company_id": ring_id}, headers=h2) as r:
            print("重置后 A02 可用:", r.status,
                  "remaining:", r.headers.get("X-Quota-Remaining"))
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
