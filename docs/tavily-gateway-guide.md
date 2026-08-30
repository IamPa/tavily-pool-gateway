# Tavily 聚合网关接入指南

> 本文档指导各类 agent / 客户端把 Tavily 从官方 API 切换到自建聚合网关。
> 网关把多个 Tavily 账号的额度池化，自动轮询、熔断、记账，用完一个自动切下一个，无需手动换 key。

## 一、网关信息

| 项目 | 地址 |
|---|---|
| HTTP 网关（SDK / 代码直连） | `http://YOUR_GATEWAY_HOST:8000/v1` |
| MCP Server（AI 客户端） | `http://YOUR_GATEWAY_HOST:8000/mcp/` |
| 存活检查 | `http://YOUR_GATEWAY_HOST:8000/health` |
| key 池状态 | `http://YOUR_GATEWAY_HOST:8000/admin/pool` |

> 地址仅 Tailscale 组网内可达（开发机 `YOUR_DEV_HOST`，服务器 `YOUR_GATEWAY_HOST`）。
> 若部署位置或端口改变，替换对应的 host 和 port 即可。

## 二、两种接入方式

| 方式 | 适合谁 | 地址 |
|---|---|---|
| **MCP** | 支持 MCP 的 AI 客户端（Hermes / Codex / Cursor / ZCode / 各类 agent 框架） | `http://YOUR_GATEWAY_HOST:8000/mcp/` |
| **HTTP** | 自己写代码、用 Tavily SDK 的程序 | `http://YOUR_GATEWAY_HOST:8000/v1` |

两者底层是同一个 key 池，**agent 侧完全不需要再配置任何 Tavily API key** —— key 统一由网关管理。

## 三、按客户端对号入座

### 1. Hermes

配置文件：`/home/ubuntu/.hermes/config.yaml`（**Hermes 部署在服务器 YOUR_GATEWAY_HOST 上**，这是服务器上的路径）

Hermes 的 MCP 配置在 `mcp_servers` 键下，支持 HTTP 传输（`url`）。在配置文件中添加：

```yaml
mcp_servers:
  tavily:
    url: "http://YOUR_GATEWAY_HOST:8000/mcp/"
```

> 说明：
> - ⚠️ 网关绑定的是 `YOUR_GATEWAY_HOST`，**不监听 `127.0.0.1`**（Linux 绑定特定 IP 时回环不可达）。即使 Hermes 与网关同机，也必须用 `YOUR_GATEWAY_HOST:8000`，不要用 `127.0.0.1`。
> - 前置：需安装 MCP SDK（`pip install mcp`），服务器 Hermes 的 venv 已装。
> - 改完重启 Hermes Agent，工具自动注册为 `mcp_tavily_search`、`mcp_tavily_pool_status` 等。

### 2. Codex（OpenAI Codex）

配置文件：`C:\Users\Pa\.codex\config.toml`

找到 `[mcp_servers.tavily]` 段，把 `url` 换成网关地址：

```toml
[mcp_servers.tavily]
url = "http://YOUR_GATEWAY_HOST:8000/mcp/"
```

> 原配置里如果带 `tavilyApiKey=...` 参数，**整个 url 替换掉即可**，不再需要该参数。
> 改完重启 Codex（或新开会话）生效。

### 3. ZCode

配置文件：`C:\Users\Pa\.zcode\cli\config.json`

```json
{
  "mcp": {
    "servers": {
      "tavily": {
        "url": "http://YOUR_GATEWAY_HOST:8000/mcp/"
      }
    }
  }
}
```

（ZCode 已切换完成；其他 CLI 类 agent 结构类似，把 Tavily 的 `url` 换成网关地址即可。）

### 4. 其他 MCP 框架（Cursor / Cline / Roo / OpenClaw / 通用）

无论哪种框架，找到它的 MCP server 配置文件，把 Tavily 服务器的地址替换为：

```
http://YOUR_GATEWAY_HOST:8000/mcp/
```

- 若原配置是官方 remote MCP（形如 `https://mcp.tavily.com/mcp/?tavilyApiKey=xxx`），**整体替换**上面的网关地址，`tavilyApiKey` 参数不再需要。
- 若原配置是本地 stdio 启动的 Tavily MCP，改成 HTTP 类型并填网关地址。
- **Cursor / Windsurf**：设置 → MCP Servers → 添加 Server → 类型选 HTTP → URL 填 `http://YOUR_GATEWAY_HOST:8000/mcp/`。

### 5. 代码 / SDK 直连（HTTP 方式）

**Python：**

```python
from tavily import TavilyClient

client = TavilyClient(
    api_key="任意非空",                     # 网关注入真实 key，这里无需真实 key
    base_url="http://YOUR_GATEWAY_HOST:8000/v1",
)
result = client.search("hello")
```

**JavaScript / TypeScript：**

```js
import { tavily } from "@tavily/core";

const tvly = tavily({
  apiKey: "任意非空",
  apiBaseURL: "http://YOUR_GATEWAY_HOST:8000/v1",
});

const result = await tvly.search("hello");
```

**其他语言 / 裸 HTTP：**

把原来请求 `https://api.tavily.com/...` 的主机部分换成 `http://YOUR_GATEWAY_HOST:8000`，路径保持不变（`/search`、`/extract`、`/crawl`、`/map`、`/research`），`Authorization: Bearer <任意非空>` 即可。

## 四、验证切换成功

### 快速验证（浏览器 / curl）

```bash
# 存活
curl http://YOUR_GATEWAY_HOST:8000/health
# 应返回 {"status":"ok"}

# key 池状态（看有多少 key、剩余额度）
curl http://YOUR_GATEWAY_HOST:8000/admin/pool

# 真实搜索（HTTP 方式）
curl -X POST http://YOUR_GATEWAY_HOST:8000/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"hello world","max_results":2}'
```

### MCP 方式验证

在客户端里调一次 `tavily_pool_status`（或任意搜索工具），能正常返回即说明已连上网关。返回里的 `total_keys` 应等于当前池中的 key 数量。

## 五、后续加 key（无需改 agent 配置）

新增 Tavily 账号后，只需在**任意组网设备**上执行一条命令，所有 agent 立即共享新额度，无需逐个改配置：

```bash
curl -X POST http://YOUR_GATEWAY_HOST:8000/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"keys": ["tvly-新key-xxx"]}'
```

该命令自动完成：立即生效 + 持久化（重启不丢）+ 立即校准真实额度。

## 六、回滚（切回官方 Tavily）

如需切回官方，把上面的网关地址改回原官方地址即可：

- **MCP 方式**：改回 `https://mcp.tavily.com/mcp/?tavilyApiKey=<你的key>`
- **HTTP 方式**：把 `base_url` 改回 `https://api.tavily.com`，并填入真实 key

ZCode 的原配置备份在 `C:\Users\Pa\.zcode\cli\config.json.bak-*`。

## 七、常见问题

**Q：为什么 agent 里不用再填 Tavily key？**
网关统一持有所有 key，agent 只需连到网关地址，key 由网关注入并轮询。

**Q：MCP 地址要不要带尾斜杠 `/mcp/`？**
建议带。`/mcp` 会 307 到 `/mcp/`，带尾斜杠最稳。

**Q：切过去后额度还是 1000/月吗？**
不是。聚合后总可用额度 = 所有 key 额度之和（如 5 个 key ≈ 5000 credits/月），且网关会在 key 之间自动轮询，单个 key 用完后自动切换。

**Q：怎么知道当前还剩多少额度？**
看 `/admin/pool`（HTTP）或调 `tavily_pool_status` 工具（MCP）。

**Q：网关挂了怎么办？**
网关是单进程服务（systemd `tavily-juhe` 常驻 + 开机自启）。若异常会自动重启（`Restart=always`）。可在服务器执行 `systemctl status tavily-juhe` 排查。

## 八、当前网关部署信息（速查）

- 服务器：`YOUR_GATEWAY_HOST`（Tailscale 主机名 `vm-0-2-ubuntu`）
- systemd 服务名：`tavily-juhe`
- 部署目录：`/opt/juhe_api`
- 监听：`YOUR_GATEWAY_HOST:8000`（仅 Tailscale 内网，未暴露公网）
- 运行内存：约 60–100MB
