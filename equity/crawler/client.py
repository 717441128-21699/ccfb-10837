"""仿真站 HTTP 客户端：自动取号、429 换号退避、网络错误重试。"""
from __future__ import annotations

import asyncio

import aiohttp

from ..config import SETTINGS
from .accounts import AccountPool, AccountState


class QuotaAllExhausted(Exception):
    """所有账号长期不可用（引擎层可选择等待而非失败）。"""


class SimClient:
    def __init__(self, pool: AccountPool, *, delay: float | None = None,
                 max_retries: int | None = None):
        self.pool = pool
        self.delay = SETTINGS.request_delay if delay is None else delay
        self.max_retries = SETTINGS.max_retries if max_retries is None else max_retries
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "SimClient":
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None
        return self._session

    async def _request(self, method: str, path: str,
                       params: dict | None = None,
                       json_body: dict | None = None) -> tuple[dict, AccountState]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            st = await self.pool.acquire()
            headers = {"Authorization": f"Bearer {st.api_key}"}
            url = f"{self.pool.base_url}{path}"
            try:
                async with self.session.request(
                        method, url, params=params, json=json_body,
                        headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "60"))
                        body = await resp.json()
                        await self.pool.report_quota_exhausted(
                            st, body.get("day", "?"), retry_after)
                        st.last_error = "quota_exhausted"
                        continue  # 换号重试，不增加退避惩罚
                    if resp.status >= 500:
                        st.last_error = f"http_{resp.status}"
                        await asyncio.sleep(min(2 ** attempt, 15))
                        continue
                    if resp.status == 404:
                        await self.pool.report_success(
                            st, resp.headers.get("X-Sim-Day", "?"), None)
                        raise EntityNotFound(path)
                    if resp.status >= 400:
                        text = await resp.text()
                        raise SimAPIError(resp.status, text)
                    body = await resp.json()
                    remaining = resp.headers.get("X-Quota-Remaining")
                    await self.pool.report_success(
                        st, resp.headers.get("X-Sim-Day", "?"),
                        int(remaining) if remaining is not None else None)
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    return body, st
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                st.last_error = type(exc).__name__
                await asyncio.sleep(min(2 ** attempt, 15))
        raise SimAPIError(0, f"重试 {self.max_retries} 次仍失败: {last_exc}")

    async def search(self, query: str) -> list[dict]:
        body, _ = await self._request("GET", "/api/search", params={"q": query})
        return body.get("results", [])

    async def company_detail(self, company_id: str) -> dict:
        body, _ = await self._request(
            "GET", "/api/company", params={"company_id": company_id})
        return body


class SimAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status


class EntityNotFound(SimAPIError):
    def __init__(self, path: str):
        super().__init__(404, f"实体不存在: {path}")
