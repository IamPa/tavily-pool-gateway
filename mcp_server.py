"""FastMCP 工具集：把 key 池封装成 MCP 工具，挂载路径 /mcp（streamable-http）。

工具:
    tavily_search         网页搜索
    tavily_extract        提取指定 URL 内容
    tavily_crawl          从根 URL 爬取多页
    tavily_map            枚举站点 URL 结构
    tavily_research       深度研究（内部轮询至完成，可配超时）
    tavily_get_research   查询 research 任务状态
    tavily_pool_status    查看 key 池额度与状态
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastmcp import FastMCP

from keypool import get_pool, ProxyResult

mcp = FastMCP("tavily-juhe")

RESEARCH_POLL_INTERVAL = 5
DEFAULT_RESEARCH_TIMEOUT = 300


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
@mcp.tool
async def tavily_search(
    query: str,
    topic: str = "general",
    search_depth: str = "basic",
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: bool = False,
    include_images: bool = False,
    include_domains: Optional[list[str]] = None,
    exclude_domains: Optional[list[str]] = None,
    time_range: Optional[str] = None,
    days: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """使用 key 池执行 Tavily 网页搜索。

    Args:
        query: 搜索关键词
        topic: 主题，可选 general / news / finance
        search_depth: basic(1 credit) / advanced(2 credits)
        max_results: 返回结果数量
        include_answer: 是否附带 AI 摘要回答
        include_raw_content: 是否附带结果的原始网页内容
        include_images: 是否附带相关图片
        include_domains: 仅在这些域名内搜索
        exclude_domains: 排除这些域名
        time_range: 时间范围，如 day / week / month / year
        days: 限定最近 N 天（部分计划支持）
        max_tokens: 摘要回答的最大 token 数
    """
    body: dict[str, Any] = {
        "query": query,
        "topic": topic,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
        "include_images": include_images,
    }
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    if time_range:
        body["time_range"] = time_range
    if days is not None:
        body["days"] = days
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return _unpack(await get_pool().forward("search", body))


@mcp.tool
async def tavily_extract(
    urls: list[str],
    extract_depth: str = "basic",
    include_images: bool = False,
    format: str = "markdown",
) -> dict:
    """使用 key 池提取指定 URL 的正文内容。

    Args:
        urls: 要提取的 URL 列表（basic 深度 1 credit / 5 个 URL）
        extract_depth: basic / advanced
        include_images: 是否包含图片
        format: 输出格式 markdown / text
    """
    body: dict[str, Any] = {
        "urls": urls,
        "extract_depth": extract_depth,
        "include_images": include_images,
        "format": format,
    }
    return _unpack(await get_pool().forward("extract", body))


@mcp.tool
async def tavily_crawl(
    url: str,
    max_depth: int = 2,
    max_breadth: int = 10,
    limit: int = 20,
    include_raw_content: bool = True,
) -> dict:
    """使用 key 池从根 URL 爬取多页内容。

    Args:
        url: 爬取起点
        max_depth: 最大链接深度
        max_breadth: 每层最大链接数
        limit: 最多处理页数
        include_raw_content: 是否包含原始网页内容
    """
    body: dict[str, Any] = {
        "url": url,
        "max_depth": max_depth,
        "max_breadth": max_breadth,
        "limit": limit,
        "include_raw_content": include_raw_content,
    }
    return _unpack(await get_pool().forward("crawl", body))


@mcp.tool
async def tavily_map(
    url: str,
    include_subdomains: bool = True,
    limit: int = 10,
) -> dict:
    """使用 key 池枚举站点 URL 结构。

    Args:
        url: 目标站点根 URL
        include_subdomains: 是否包含子域名页面
        limit: 最多返回的 URL 数量
    """
    body: dict[str, Any] = {
        "url": url,
        "include_subdomains": include_subdomains,
        "limit": limit,
    }
    return _unpack(await get_pool().forward("map", body))


@mcp.tool
async def tavily_research(
    query: str,
    model: str = "auto",
    timeout_seconds: int = DEFAULT_RESEARCH_TIMEOUT,
) -> dict:
    """执行 Tavily 深度研究（异步任务，内部轮询至完成或超时）。

    Args:
        query: 研究问题/主题
        model: mini（轻量快速）/ pro（深度综合）/ auto
        timeout_seconds: 等待完成的最长时间；超时后返回 request_id，
            可用 tavily_get_research 继续查询
    """
    body: dict[str, Any] = {"input": query, "model": model}
    created = await get_pool().create_research(body)
    if created.status_code not in (200, 201):
        return _unpack(created)

    try:
        request_id = json.loads(created.body).get("request_id")
    except json.JSONDecodeError:
        request_id = None
    if not request_id:
        return _unpack(created)

    key_name = created.key_name
    deadline = asyncio.get_event_loop().time() + max(1, timeout_seconds)
    while True:
        result = await get_pool().forward_get_research(request_id)
        if result.status_code == 200:
            return _unpack(result)
        if result.status_code != 202:
            return _unpack(result)
        if asyncio.get_event_loop().time() > deadline:
            return {
                "status": "pending",
                "request_id": request_id,
                "message": f"任务在 {timeout_seconds}s 内未完成，可调用 tavily_get_research(request_id={request_id!r}) 继续查询",
            }
        await asyncio.sleep(RESEARCH_POLL_INTERVAL)


@mcp.tool
async def tavily_get_research(request_id: str) -> dict:
    """查询 Tavily research 任务状态与结果。

    Args:
        request_id: 创建任务时返回的 request_id
    """
    return _unpack(await get_pool().forward_get_research(request_id))


@mcp.tool
async def tavily_pool_status() -> dict:
    """查看 key 池中每个 key 的额度、状态、冷却信息（不包含真实 key 值）。"""
    pool = get_pool()
    keys = pool.snapshot()
    return {
        "total_keys": len(keys),
        "total_credits_remaining": sum(
            k["credits_remaining"] for k in keys if k["status"] == "healthy"
        ),
        "keys": keys,
    }


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _unpack(result: ProxyResult) -> dict:
    """把 ProxyResult 转成 dict：成功返回上游 JSON，失败附上 HTTP 状态。"""
    try:
        data = json.loads(result.body)
    except json.JSONDecodeError:
        data = {"detail": {"error": result.body.decode("utf-8", "ignore")[:500]}}
    if result.status_code >= 400:
        return {"error": f"HTTP {result.status_code}", **data}
    return data
