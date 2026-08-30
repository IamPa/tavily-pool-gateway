"""HTTP 网关：暴露与 Tavily 官方 API 兼容的端点（/v1/*），调用方只需换 base URL。

端点:
    POST /v1/search|extract|crawl|map   透传（网关注入上游 key，响应原样返回）
    POST /v1/research                   创建 research 任务（记录 request_id->key 映射）
    GET  /v1/research/{request_id}      查询 research 任务状态
    GET  /v1/usage                      各 key 实时剩余额度汇总（本地账本，不触发上游调用）
    GET  /health                        存活检查（不鉴权）
    GET  /admin/pool                    key 池明细
    POST /admin/keys                    热添加 key（body: {"keys": ["tvly-...", ...]} 或 {"key": "tvly-..."}）

鉴权: 配置 GATEWAY_TOKEN 后，/v1/* 与 /admin/* 需带 Authorization: Bearer <token>
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import settings
from keypool import get_pool

logger = logging.getLogger("gateway")

router = APIRouter()

SYNC_ENDPOINTS = {"search", "extract", "crawl", "map"}


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------
def _check_auth(request: Request) -> bool:
    if not settings.gateway_token:
        return True
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {settings.gateway_token}"


def _unauthorized() -> JSONResponse:
    return JSONResponse({"detail": {"error": "未授权：需要有效的 GATEWAY_TOKEN"}}, status_code=401)


# ----------------------------------------------------------------------
# 业务端点
# ----------------------------------------------------------------------
@router.post("/v1/{endpoint}")
async def proxy_sync(request: Request, endpoint: str):
    if not _check_auth(request):
        return _unauthorized()
    if endpoint not in SYNC_ENDPOINTS and endpoint != "research":
        return JSONResponse({"detail": {"error": f"未知端点: {endpoint}"}}, status_code=404)

    pool = get_pool()
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"detail": {"error": str(exc)}}, status_code=400)

    if endpoint == "research" and (body is None or not str(body.get("input") or "").strip()):
        return JSONResponse(
            {"detail": {"error": "research 请求缺少必填字段 input"}},
            status_code=400,
        )

    if endpoint == "research":
        result = await pool.create_research(body)
    else:
        result = await pool.forward(endpoint, body)

    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type or "application/json",
    )


@router.get("/v1/research/{request_id}")
async def get_research(request: Request, request_id: str):
    if not _check_auth(request):
        return _unauthorized()
    pool = get_pool()
    result = await pool.forward_get_research(request_id)
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type or "application/json",
    )


@router.get("/v1/usage")
async def usage(request: Request):
    if not _check_auth(request):
        return _unauthorized()
    pool = get_pool()
    keys = pool.snapshot()
    return JSONResponse({
        "keys": keys,
        "total_credits_remaining": sum(
            k["credits_remaining"] for k in keys if k["status"] == "healthy"
        ),
    })


# ----------------------------------------------------------------------
# 管理端点
# ----------------------------------------------------------------------
@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/admin/pool")
async def admin_pool(request: Request):
    if not _check_auth(request):
        return _unauthorized()
    return JSONResponse({"keys": get_pool().snapshot()})


@router.post("/admin/keys")
async def admin_add_keys(request: Request):
    if not _check_auth(request):
        return _unauthorized()
    try:
        data = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"detail": {"error": str(exc)}}, status_code=400)
    raw_keys: list[str] = []
    if isinstance(data, dict):
        if isinstance(data.get("keys"), list):
            raw_keys = [str(k) for k in data["keys"]]
        elif data.get("key"):
            raw_keys = [str(data["key"])]
    if not raw_keys:
        return JSONResponse(
            {"detail": {"error": "请提供 {\"keys\": [\"tvly-...\"]} 或 {\"key\": \"tvly-...\"}"}},
            status_code=400,
        )
    pool = get_pool()
    added = pool.add_keys(raw_keys)
    await pool.sync_added_keys(added)
    return JSONResponse({"added": len(added), "total_keys": len(pool.snapshot())})


@router.delete("/admin/keys/{name}")
async def admin_remove_key(request: Request, name: str):
    if not _check_auth(request):
        return _unauthorized()
    ok, err = await get_pool().remove_key(name)
    if not ok:
        return JSONResponse({"detail": {"error": err}}, status_code=404)
    pool = get_pool()
    return JSONResponse({"removed": name, "total_keys": len(pool.snapshot())})


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
async def _read_json_body(request: Request) -> dict[str, Any] | None:
    """读取 JSON body。

    - 非 application/json 或空 body: 返回 None
    - JSON 解析失败（编码或语法错误）: 抛 ValueError，由路由层转 400
      （避免静默把空请求透传给上游白扣 credit）
    """
    ctype = request.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"请求体不是合法的 UTF-8 JSON: {exc}") from exc
    return data if isinstance(data, dict) else None
