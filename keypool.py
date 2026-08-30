"""Tavily 多 key 聚合核心：key 池状态机、选 key、记账、熔断、配额同步、research 映射。

状态机:
    healthy  可用（默认）
    cooling  连续 429 被临时摘除，冷却到期自动恢复
    exhausted 配额耗尽（429 带配额关键字），跨月自动恢复
    disabled 401 无效 key，需人工干预

记账双轨:
    1. 精确记账: search/extract/crawl/map 强制注入 include_usage=true，从响应 usage.credits 扣减
    2. 估算兜底: 响应未带 usage 或为 0 时按端点参数保守估算（宁可多算，避免透支）
    3. /usage 校准: 启动时 + 每小时并行同步所有 key 的真实额度（该接口限流 10 次 / 10 分钟）
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import settings, persist_keys_to_file, remove_keys_from_file

logger = logging.getLogger("keypool")

TAVILY_BASE = settings.tavily_base_url
USAGE_PATH = "/usage"

# ---- 熔断 / 冷却 / 配额参数 ----
MAX_CONSECUTIVE_429 = 3
COOLDOWN_SECONDS = 300              # 冷却 5 分钟
FORBIDDEN_COOLDOWN_SECONDS = 120    # 403 暂时风控冷却 2 分钟
MIN_RESEARCH_REMAINING = 100        # research 任务要求 key 剩余额度下限（一次最多可消耗 250 credits）
DEFAULT_LIMIT = 1000                # 免费档默认上限，/usage 校准后覆盖

# research 按模型估算的单次消耗（实际消耗范围较大，靠 /usage 校准）
RESEARCH_ESTIMATE = {"mini": 15, "pro": 60, "auto": 30}

# search 按 search_depth 估算单次消耗（basic=1, advanced=2）
SEARCH_ESTIMATE = {"basic": 1, "advanced": 2, "auto": 1}

# 配额耗尽的 429 响应关键字
QUOTA_KEYWORDS = ("quota", "credit", "exhaust", "insufficient", "limit reached")


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@dataclass
class KeyState:
    name: str
    api_key: str
    status: str = "healthy"          # healthy | cooling | exhausted | disabled
    credits_used: int = 0            # 本地记账
    credits_limit: int = DEFAULT_LIMIT
    consecutive_429: int = 0
    cooldown_until: float = 0.0      # unix 时间戳
    billing_month: str = field(default_factory=_current_month)
    last_error: str | None = None

    @property
    def credits_remaining(self) -> int:
        return max(0, self.credits_limit - self.credits_used)

    def refresh_month(self) -> None:
        """跨月自动恢复：免费额度每月 1 号重置。"""
        now_month = _current_month()
        if self.billing_month != now_month:
            self.billing_month = now_month
            self.credits_used = 0
            if self.status == "exhausted":
                self.status = "healthy"
                self.last_error = None


@dataclass
class ProxyResult:
    status_code: int
    body: bytes
    key_name: str | None = None
    content_type: str | None = None


class KeyPool:
    def __init__(self, keys: list[str]) -> None:
        self._keys: list[KeyState] = [
            KeyState(name=f"key{i + 1}", api_key=k) for i, k in enumerate(keys)
        ]
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=60.0)
        self._sync_task: Optional[asyncio.Task] = None
        # research 任务映射: request_id -> (key_name, created_ts)
        self._research_map: dict[str, tuple[str, float]] = {}
        self._research_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._sync_task = asyncio.create_task(self._sync_loop(), name="keypool-sync")

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 选 key / 转发
    # ------------------------------------------------------------------
    async def forward(
        self,
        endpoint: str,
        body: dict[str, Any] | None,
        require_research: bool = False,
    ) -> ProxyResult:
        """转发请求到 Tavily：自动选 key、429/401 换 key 重试、成功后记账。"""
        if not self._keys:
            return self._fail(
                429, "key 池为空，请在 .env 配置 TAVILY_KEYS（逗号分隔多个 key）"
            )

        # 注入 include_usage 以便从响应精确记账（research 不支持该参数）
        if body is not None and endpoint in ("search", "extract", "crawl", "map"):
            body = {**body, "include_usage": True}

        attempted: set[str] = set()
        while True:
            key = self._pick_key(require_research=require_research)
            if key is None or key.name in attempted:
                return self._fail(429, "所有 key 均不可用（配额耗尽/冷却/禁用）。请查看 /admin/pool 或 /v1/usage")
            attempted.add(key.name)

            result = await self._raw_call(endpoint, body, key)
            status = result.status_code

            if status == 429:
                self._on_429(key, result.body)
                continue  # 换下一个 key 重试
            if status == 401:
                self._on_401(key)
                continue  # key 无效，换下一个
            if status == 403:
                self._on_403(key)
                continue  # 暂时风控/限流，冷却后重试，换下一个

            if status < 300:
                self._on_success(key, endpoint, body, result.body)
            return result

    async def create_research(self, body: dict[str, Any]) -> ProxyResult:
        """创建 research 任务：转发 + 记录 request_id->key 映射 + 估算记账。"""
        result = await self.forward("research", body, require_research=True)
        if result.status_code in (200, 201) and result.key_name:
            try:
                request_id = json.loads(result.body).get("request_id")
                if request_id:
                    await self.record_research(request_id, result.key_name)
                    model = (body or {}).get("model") or "auto"
                    self.deduct_research_estimate(result.key_name, model)
            except Exception:
                logger.exception("research 记账失败")
        return result

    async def forward_get_research(self, request_id: str) -> ProxyResult:
        """查询 research 任务状态：必须用创建时映射的 key（Tavily 按 key 隔离任务）。"""
        key_name = await self.get_research_key(request_id)
        if key_name is None:
            return self._fail(
                404,
                "该 research 任务不在网关记录中（服务可能已重启）。请直连 Tavily 查询，或在网关内重新创建任务。",
            )
        key = self._by_name(key_name)
        if key is None:
            return self._fail(404, f"创建该任务的 key（{key_name}）已不存在")
        url = f"{TAVILY_BASE}/research/{request_id}"
        try:
            resp = await self._client.get(
                url, headers={"Authorization": f"Bearer {key.api_key}"}
            )
            return ProxyResult(
                resp.status_code, resp.content, key.name, resp.headers.get("content-type")
            )
        except httpx.HTTPError as exc:
            return self._fail(502, f"上游请求失败: {exc}")

    # ------------------------------------------------------------------
    # 记账 / 状态维护
    # ------------------------------------------------------------------
    def _on_429(self, key: KeyState, body: bytes) -> None:
        text = body.decode("utf-8", "ignore").lower()
        if any(kw in text for kw in QUOTA_KEYWORDS):
            key.status = "exhausted"
            key.last_error = "配额耗尽 (429)"
            logger.warning("%s 标记为 exhausted", key.name)
            return
        # 纯 RPM 限流：连续触发则冷却
        key.consecutive_429 += 1
        key.last_error = "RPM 限流 (429)"
        if key.consecutive_429 >= MAX_CONSECUTIVE_429:
            key.status = "cooling"
            key.cooldown_until = time.time() + COOLDOWN_SECONDS
            key.consecutive_429 = 0
            logger.warning("%s 连续 429，冷却 %ss", key.name, COOLDOWN_SECONDS)

    def _on_401(self, key: KeyState) -> None:
        key.status = "disabled"
        key.last_error = "401 无效 key"
        logger.warning("%s 被禁用（401）", key.name)

    def _on_403(self, key: KeyState) -> None:
        """403 通常是 Tavily 的暂时风控/限流（非 key 永久失效），冷却后自动恢复。

        与 429 不同：403 立即冷却一次，冷却时间较短，避免反复打同一个被风控的 key。
        """
        key.status = "cooling"
        key.cooldown_until = time.time() + FORBIDDEN_COOLDOWN_SECONDS
        key.consecutive_429 = 0
        key.last_error = "403 暂时风控/限流"
        logger.warning("%s 收到 403，冷却 %ss", key.name, FORBIDDEN_COOLDOWN_SECONDS)

    def _on_success(
        self,
        key: KeyState,
        endpoint: str,
        req_body: dict[str, Any] | None,
        resp_body: bytes,
    ) -> None:
        key.consecutive_429 = 0
        key.credits_used += self._extract_credits(endpoint, req_body, resp_body)
        key.refresh_month()

    def _extract_credits(
        self,
        endpoint: str,
        req_body: dict[str, Any] | None,
        resp_body: bytes,
    ) -> int:
        """优先读响应 usage.credits；缺失/为 0 时按端点参数保守估算。"""
        try:
            data = json.loads(resp_body)
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                credits = usage.get("credits")
                if isinstance(credits, (int, float)) and credits > 0:
                    return int(round(credits))
        except (json.JSONDecodeError, AttributeError):
            pass

        req = req_body or {}
        if endpoint == "research":
            # research 的消耗由 create_research 的估算单独记账，
            # 响应不含 usage.credits，这里返回 0 避免与估算重复记账
            return 0
        if endpoint == "search":
            depth = req.get("search_depth", "basic")
            return SEARCH_ESTIMATE.get(depth, 1)
        if endpoint == "extract":
            urls = req.get("urls") or []
            n = len(urls) if isinstance(urls, (list, tuple)) else 1
            return max(1, math.ceil(n / 5))
        if endpoint == "map":
            limit = int(req.get("limit") or 10)
            return max(1, math.ceil(limit / 10))
        if endpoint == "crawl":
            depth = int(req.get("max_depth") or 1)
            breadth = int(req.get("max_breadth") or 10)
            pages = min(int(req.get("limit") or 50), depth * breadth)
            return max(1, pages)
        return 1

    def deduct_research_estimate(self, key_name: str, model: str) -> None:
        key = self._by_name(key_name)
        if key is None:
            return
        key.credits_used += RESEARCH_ESTIMATE.get(model, RESEARCH_ESTIMATE["auto"])
        key.refresh_month()

    # ------------------------------------------------------------------
    # /usage 配额同步（启动 + 定时）
    # ------------------------------------------------------------------
    async def _sync_loop(self) -> None:
        await self.sync_usage_all()
        while True:
            await asyncio.sleep(max(60, settings.usage_sync_interval))
            await self.sync_usage_all()

    async def sync_usage_all(self) -> None:
        async with self._lock:
            if not self._keys:
                return
            # 串行 + 间隔：并发打多个 key 的 /usage 会触发 Tavily 同 IP 风控（403），
            # 逐个请求并留出间隔可大幅降低被风控的概率。
            for key in self._keys:
                try:
                    usage, limit = await self._fetch_usage(key)
                except Exception as exc:
                    logger.debug("%s /usage 同步失败: %s", key.name, exc)
                    continue
                key.credits_used = usage
                if limit:
                    key.credits_limit = limit
                key.refresh_month()
                if key.status == "exhausted" and key.credits_remaining > 0:
                    key.status = "healthy"
                    key.last_error = None
                await asyncio.sleep(0.5)

    async def _fetch_usage(self, key: KeyState) -> tuple[int, int | None]:
        resp = await self._client.get(
            f"{TAVILY_BASE}{USAGE_PATH}",
            headers={"Authorization": f"Bearer {key.api_key}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"/usage 返回 {resp.status_code}")
        data = resp.json()
        # 真实 Tavily：dev key 的 key 级统计（key.usage/key.limit）恒为 0/None，
        # 真实用量在账户级 account.plan_usage / account.plan_limit。
        # 兼容两种结构，账户级优先。
        acct = data.get("account") or {}
        keyd = data.get("key") or {}
        usage = int(acct.get("plan_usage") or keyd.get("usage") or 0)
        limit = acct.get("plan_limit") or keyd.get("limit")
        return usage, (int(limit) if limit else None)

    # ------------------------------------------------------------------
    # research 任务映射
    # ------------------------------------------------------------------
    async def record_research(self, request_id: str, key_name: str) -> None:
        async with self._research_lock:
            # 顺带清理 24 小时前的映射，防止内存无限增长
            cutoff = time.time() - 86400
            self._research_map = {
                rid: v for rid, v in self._research_map.items() if v[1] > cutoff
            }
            self._research_map[request_id] = (key_name, time.time())

    async def get_research_key(self, request_id: str) -> str | None:
        async with self._research_lock:
            entry = self._research_map.get(request_id)
            return entry[0] if entry else None

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    def add_keys(self, new_keys: list[str]) -> list[KeyState]:
        """同步添加 key：写入内存 + 持久化到 api.txt。返回新增的 KeyState 列表。"""
        existing = {k.api_key for k in self._keys}
        added: list[KeyState] = []
        for raw in new_keys:
            k = raw.strip()
            if k and k not in existing:
                ks = KeyState(name=f"key{len(self._keys) + 1}", api_key=k)
                self._keys.append(ks)
                existing.add(k)
                added.append(ks)
        if added:
            persist_keys_to_file([ks.api_key for ks in added])
        return added

    async def sync_added_keys(self, added: list[KeyState]) -> None:
        """立即校准新增 key 的真实额度（不等下一轮定时同步）。串行 + 间隔避免 IP 风控。"""
        for ks in added:
            try:
                usage, limit = await self._fetch_usage(ks)
                ks.credits_used = usage
                if limit:
                    ks.credits_limit = limit
                ks.refresh_month()
                logger.info("%s 新 key 额度已校准: used=%s limit=%s", ks.name, usage, limit)
            except Exception as exc:
                logger.warning("%s 新 key /usage 校准失败: %s", ks.name, exc)
            await asyncio.sleep(0.5)

    async def remove_key(self, name: str) -> tuple[bool, str | None]:
        """按名称移除 key：内存移除 + api.txt 持久化移除 + 清理其 research 映射。

        返回 (是否成功, 错误信息)。
        """
        key = self._by_name(name)
        if key is None:
            return False, f"key {name} 不存在"
        remove_keys_from_file([key.api_key])
        self._keys.remove(key)
        async with self._research_lock:
            self._research_map = {
                rid: v for rid, v in self._research_map.items() if v[0] != name
            }
        logger.info("%s 已移除（内存 + api.txt）", name)
        return True, None

    def snapshot(self) -> list[dict[str, Any]]:
        """导出 key 池明细（不含真实 key，仅状态与额度）。"""
        now = time.time()
        out: list[dict[str, Any]] = []
        for key in self._keys:
            self._maybe_recover(key, now)
            out.append({
                "name": key.name,
                "status": key.status,
                "credits_used": key.credits_used,
                "credits_limit": key.credits_limit,
                "credits_remaining": key.credits_remaining,
                "consecutive_429": key.consecutive_429,
                "cooldown_seconds_left": max(0, int(key.cooldown_until - now)) if key.status == "cooling" else 0,
                "billing_month": key.billing_month,
                "last_error": key.last_error,
            })
        return out

    def total_remaining(self) -> int:
        return sum(k.credits_remaining for k in self._keys if k.status == "healthy")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _maybe_recover(self, key: KeyState, now: float) -> None:
        """冷却到期自动恢复 healthy；跨月额度重置。选 key 与快照共用，保证状态一致。"""
        key.refresh_month()
        if key.status == "cooling" and now >= key.cooldown_until:
            key.status = "healthy"
            key.last_error = None

    def _pick_key(self, require_research: bool) -> Optional[KeyState]:
        """剩余额度最高的可用 key；research 额外要求余量 >= MIN_RESEARCH_REMAINING。"""
        now = time.time()
        best: Optional[KeyState] = None
        for key in self._keys:
            self._maybe_recover(key, now)
            if key.status != "healthy":
                continue
            if require_research and key.credits_remaining < MIN_RESEARCH_REMAINING:
                continue
            if best is None or key.credits_remaining > best.credits_remaining:
                best = key
        return best

    async def _raw_call(
        self,
        endpoint: str,
        body: dict[str, Any] | None,
        key: KeyState,
    ) -> ProxyResult:
        headers = {
            "Authorization": f"Bearer {key.api_key}",
            "Content-Type": "application/json",
        }
        # 关键防护：Tavily 官方 API 优先认 body 里的 api_key 字段，会覆盖 Authorization header。
        # 调用方（SDK/curl）若在 body 里带了任何 api_key（假 key、占位符、自动附加），
        # 都会覆盖网关注入的有效 key 导致 401 熔断全池。这里统一剥离，认证只走 header。
        safe_body: dict[str, Any] | None = None
        if body is not None:
            safe_body = dict(body)
            if "api_key" in safe_body:
                logger.warning(
                    "%s 请求体含 api_key 字段（可能来自客户端误配置），已剥离，认证改用池内 key",
                    endpoint,
                )
                safe_body.pop("api_key")
        url = f"{TAVILY_BASE}/{endpoint}"
        try:
            resp = await self._client.post(url, json=safe_body, headers=headers)
            return ProxyResult(
                resp.status_code, resp.content, key.name, resp.headers.get("content-type")
            )
        except httpx.HTTPError as exc:
            logger.warning("上游请求失败 %s: %s", url, exc)
            return ProxyResult(502, json.dumps({"detail": {"error": f"上游请求失败: {exc}"}}).encode(), key.name, "application/json")

    def _by_name(self, name: str) -> Optional[KeyState]:
        for key in self._keys:
            if key.name == name:
                return key
        return None

    @staticmethod
    def _fail(status: int, message: str) -> ProxyResult:
        body = json.dumps({"detail": {"error": message}}).encode()
        return ProxyResult(status, body, None, "application/json")


# ----------------------------------------------------------------------
# 进程级单例访问（gateway / mcp 共用同一个池实例）
# ----------------------------------------------------------------------
_pool: Optional[KeyPool] = None


def set_pool(pool: Optional[KeyPool]) -> None:
    global _pool
    _pool = pool


def get_pool() -> KeyPool:
    if _pool is None:
        raise RuntimeError("keypool 未初始化（lifespan 未启动）")
    return _pool
