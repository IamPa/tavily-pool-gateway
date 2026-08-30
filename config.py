"""配置加载：优先读环境变量，其次解析同目录 .env 文件（无需 python-dotenv 依赖）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
API_TXT = BASE_DIR / "api.txt"


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """极简 .env 解析：支持 KEY=VALUE、# 注释、export 前缀、可选引号。已存在的环境变量不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class Settings:
    def __init__(self) -> None:
        self.base_dir = BASE_DIR
        _load_dotenv()
        # Tavily key 列表 = 环境变量 TAVILY_KEYS（逗号分隔）+ api.txt（每行一个），合并去重
        raw_keys = os.environ.get("TAVILY_KEYS", "")
        env_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        file_keys: list[str] = []
        if API_TXT.exists():
            file_keys = [
                line.strip()
                for line in API_TXT.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        seen: set[str] = set()
        self.tavily_keys: list[str] = []
        for k in env_keys + file_keys:
            if k not in seen:
                seen.add(k)
                self.tavily_keys.append(k)
        self.port: int = int(os.environ.get("PORT", "8000"))
        # 留空 = 不鉴权（Tailscale 内网自用）；配置后 /v1/* 需带 Authorization: Bearer <token>
        self.gateway_token: str | None = os.environ.get("GATEWAY_TOKEN", "").strip() or None
        # /usage 配额校准周期（秒），该接口限流 10 次 / 10 分钟
        self.usage_sync_interval: int = int(os.environ.get("USAGE_SYNC_INTERVAL", "3600"))
        # 上游地址（测试时可指向 mock）
        self.tavily_base_url: str = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")


def persist_keys_to_file(keys: list[str]) -> None:
    """把新增 key 追加写入 api.txt（去重，保留已有 key 行）。

    注意：会重写 api.txt，丢弃注释行/空行；api.txt 本质是纯 key 列表，可接受。
    """
    existing: list[str] = []
    if API_TXT.exists():
        existing = [
            line.strip()
            for line in API_TXT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    merged = existing + [k for k in keys if k not in existing]
    API_TXT.write_text("\n".join(merged) + "\n", encoding="utf-8")


def remove_keys_from_file(keys: list[str]) -> None:
    """从 api.txt 移除指定 key 行（不存在的忽略）。"""
    if not API_TXT.exists() or not keys:
        return
    removed = set(keys)
    remaining = [
        line
        for line in API_TXT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and line.strip() not in removed
    ]
    API_TXT.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


settings = Settings()
