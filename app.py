"""应用入口: uvicorn app:app

单进程同时提供:
    - HTTP 网关  : http://<host>:<port>/v1/*  (与 Tavily 官方 API 兼容)
    - MCP Server : http://<host>:<port>/mcp   (streamable-http)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from gateway import router as gateway_router
from keypool import KeyPool, set_pool
from mcp_server import mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")

# MCP 应用挂载在 /mcp（其 lifespan 负责 MCP 会话管理）
mcp_app = mcp.http_app(path="/", transport="streamable-http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 先启动 MCP 的 lifespan（会话状态管理），再启动 key 池
    async with mcp_app.lifespan(app):
        pool = KeyPool(settings.tavily_keys)
        app.state.keypool = pool
        set_pool(pool)
        await pool.start()
        logger.info(
            "key 池已启动: %d 个 key，剩余可用额度合计约 %d",
            len(settings.tavily_keys),
            pool.total_remaining(),
        )
        try:
            yield
        finally:
            await pool.stop()
            set_pool(None)
            logger.info("key 池已停止")


app = FastAPI(
    title="Tavily 多 Key 聚合网关",
    description="HTTP 网关 (/v1/*) + MCP Server (/mcp)，多 key 自动轮询、熔断、配额感知",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(gateway_router)
app.mount("/mcp", mcp_app)
# Web 控制台（可选）：webui/ 目录存在时挂载 /ui。
# 服务器最小化部署可不携带 webui/，网关照常运行（API + MCP 不受影响）。
_webui_dir = settings.base_dir / "webui"
if _webui_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_webui_dir), html=True), name="webui")
# 允许桌面壳的内置 fallback 页跨源探测 /health（网关本身依赖 Tailscale 内网隔离，未暴露公网）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
