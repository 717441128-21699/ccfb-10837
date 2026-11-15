"""采集账号池 —— 多账号轮用配额，用完自动歇到第二天（需求 2）。

状态来源：
- 仿真站响应头 ``X-Sim-Day / X-Quota-Remaining`` 同步当天用量；
- 429 响应把账号标记为当前虚拟日耗尽；
- 后台定时打 ``/health``（不耗配额）感知虚拟日翻页，自动复活账号；
- 本地 ``quota_usage`` 表留痕，网页“账号配额”页直接展示。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import aiohttp

from ..config import SETTINGS
from ..db import AsyncDB


@dataclass
class AccountState:
    account_id: str
    api_key: str
    daily_quota: int
    enabled: bool = True
    sim_day: str | None = None
    used: int = 0
    exhausted_day: str | None = None     # 哪天被 429 打挂的
    cooldown_until: float = 0.0          # 真实时间戳
    last_error: str = ""
    rr_order: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.daily_quota - self.used)


class AccountPool:
    def __init__(self, db: AsyncDB, base_url: str,
                 refresh_interval: float = 20.0):
        self.db = db
        self.base_url = base_url.rstrip("/")
        self.refresh_interval = refresh_interval
        self.accounts: dict[str, AccountState] = {}
        self._cond = asyncio.Condition()
        self._rr = 0
        self._stopping = asyncio.Event()
        self._refresher: asyncio.Task | None = None
        self.current_sim_day: str | None = None

    # ------------------------------------------------------------- 生命周期
    async def load(self) -> None:
        rows = await self.db.query(
            "SELECT account_id, api_key, daily_quota, enabled FROM accounts "
            "ORDER BY account_id")
        for i, r in enumerate(rows):
            self.accounts[r["account_id"]] = AccountState(
                account_id=r["account_id"], api_key=r["api_key"],
                daily_quota=r["daily_quota"], enabled=bool(r["enabled"]),
                rr_order=i)
        if not self.accounts:
            raise RuntimeError("账号表为空，请先执行 equity.crawler.seed")
        usage = await self.db.query(
            "SELECT account_id, day, used FROM quota_usage")
        for r in usage:
            st = self.accounts.get(r["account_id"])
            if st and (st.sim_day is None or r["day"] >= (st.sim_day or "")):
                st.sim_day = r["day"]
                st.used = r["used"]

    async def start(self) -> None:
        await self.load()
        self._refresher = asyncio.create_task(self._clock_refresher())

    async def close(self) -> None:
        self._stopping.set()
        async with self._cond:
            self._cond.notify_all()
        if self._refresher:
            self._refresher.cancel()
            try:
                await self._refresher
            except asyncio.CancelledError:
                pass

    # ----------------------------------------------------------- 虚拟日感知
    async def _clock_refresher(self) -> None:
        """定时查 /health（不耗配额），翻日了就复活耗尽账号并落库新一天。"""
        while not self._stopping.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(f"{self.base_url}/health") as resp:
                        day = (await resp.json())["sim_day"]
                if day != self.current_sim_day:
                    await self._on_new_day(day)
            except Exception as exc:  # 仿真站临时重启不致命
                print(f"[pool] health 刷新失败: {exc}", flush=True)
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=self.refresh_interval)
            except asyncio.TimeoutError:
                pass

    async def _on_new_day(self, day: str) -> None:
        async with self._cond:
            prev_day = self.current_sim_day
            self.current_sim_day = day
            for st in self.accounts.values():
                # 只要虚拟日推进了，所有冷却/耗尽状态都应解除
                if prev_day is not None and day > prev_day:
                    st.exhausted_day = None
                    st.cooldown_until = 0.0
                    st.sim_day = day
                    st.used = 0
                elif st.sim_day != day:
                    st.sim_day = day
                    st.used = 0
            self._cond.notify_all()

    # ------------------------------------------------------------- 记账
    async def _persist_usage(self, st: AccountState) -> None:
        if not st.sim_day:
            return
        await self.db.execute(
            "INSERT INTO quota_usage(account_id,day,used) VALUES (?,?,?) "
            "ON CONFLICT(account_id,day) DO UPDATE SET used=excluded.used",
            (st.account_id, st.sim_day, st.used))

    async def report_success(self, st: AccountState, sim_day: str,
                             remaining: int | None) -> None:
        async with self._cond:
            if st.sim_day != sim_day:
                st.sim_day = sim_day
                st.used = 0
                if st.exhausted_day and st.exhausted_day < sim_day:
                    st.exhausted_day = None
            if remaining is not None:
                st.used = max(st.used, st.daily_quota - remaining)
            else:
                st.used += 1
            await self._persist_usage(st)

    async def report_quota_exhausted(self, st: AccountState, sim_day: str,
                                     retry_after: int) -> None:
        async with self._cond:
            st.sim_day = sim_day
            st.used = st.daily_quota
            st.exhausted_day = sim_day
            st.cooldown_until = time.time() + max(1, retry_after)
            await self._persist_usage(st)
            self._cond.notify_all()

    # ------------------------------------------------------------- 取号
    def _usable(self, st: AccountState) -> bool:
        if not st.enabled or st.exhausted_day is not None:
            return False
        if time.time() < st.cooldown_until:
            return False
        if st.sim_day and self.current_sim_day and \
                st.sim_day < self.current_sim_day:
            # 本地记录还是旧一天，视作新一天未用
            return True
        return st.used < st.daily_quota or st.sim_day is None

    async def acquire(self) -> AccountState:
        """阻塞直到拿到一个可用账号；所有账号歇菜时自动等冷却结束。"""
        async with self._cond:
            while True:
                ids = sorted(self.accounts.values(), key=lambda x: x.rr_order)
                n = len(ids)
                for offset in range(n):
                    st = ids[(self._rr + offset) % n]
                    if self._usable(st):
                        self._rr = (self._rr + offset + 1) % n
                        return st
                # 全部不可用：算最早恢复时间
                waitables = [s for s in self.accounts.values() if s.enabled]
                if not waitables:
                    raise RuntimeError("没有启用的采集账号")
                soon = min(s.cooldown_until for s in waitables)
                timeout = max(1.0, min(soon - time.time(), 60.0))
                print(f"[pool] 全部账号当日配额耗尽，等待 {timeout:.0f}s 或虚拟日翻页",
                      flush=True)
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass

    async def snapshot(self) -> list[dict]:
        """网页配额页用。"""
        out = []
        for st in sorted(self.accounts.values(), key=lambda x: x.rr_order):
            out.append({
                "account_id": st.account_id,
                "daily_quota": st.daily_quota,
                "sim_day": st.sim_day or self.current_sim_day,
                "used": st.used if st.sim_day else 0,
                "remaining": (st.daily_quota - st.used)
                             if st.sim_day and (not self.current_sim_day or
                                                st.sim_day == self.current_sim_day)
                             else st.daily_quota,
                "exhausted": st.exhausted_day is not None and
                             st.exhausted_day == (self.current_sim_day or st.sim_day),
                "cooldown_seconds": max(0, int(st.cooldown_until - time.time())),
                "enabled": st.enabled,
                "last_error": st.last_error,
            })
        return out

    async def set_enabled(self, account_id: str, enabled: bool) -> None:
        st = self.accounts[account_id]
        async with self._cond:
            st.enabled = enabled
            if enabled:
                st.exhausted_day = None
                st.cooldown_until = 0.0
            self._cond.notify_all()
        await self.db.execute(
            "UPDATE accounts SET enabled=? WHERE account_id=?",
            (1 if enabled else 0, account_id))
