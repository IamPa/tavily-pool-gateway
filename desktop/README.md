# 桌面应用（Tauri 壳）

桌面壳是一个轻量的原生日历窗口：启动时探测网关 `/health`，可达则直接加载网关托管的 Web 控制台（`/ui/`）；不可达则显示内置的连接指导页（可填写网关地址重试）。

> UI 本体由网关托管（仓库根目录 `webui/`），桌面壳不包含 UI 代码，二者始终一致。

## 前置要求

- Rust 1.77+（含 cargo；Windows 需 MSVC 工具链）
- Node.js（用于 npx 运行 tauri-cli）
- Windows 10/11 自带 WebView2；Linux 需 webkit2gtk-4.1；macOS 自带 WebKit

## 构建

```bash
cd desktop/src-tauri
npx -y @tauri-apps/cli build
```

产物位于 `target/release/bundle/`：

| 平台 | 产物 |
|---|---|
| Windows | `msi/*.msi`、`nsis/*.exe` |
| macOS | `dmg/*.dmg` |
| Linux | `appimage/*.AppImage` |

本地快速验证（不打包，直接开窗口）：

```bash
npx -y @tauri-apps/cli dev
```

## 运行配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `TAVILY_GATEWAY_URL` | 网关地址 | `http://127.0.0.1:8000` |

指向远程网关示例（Tailscale 内网服务器）：

```powershell
$env:TAVILY_GATEWAY_URL="http://<your-gateway-host>:8000"; .\tavily-pool-gateway.exe
```

在 fallback 页手动填写的地址会存入 `localStorage`，下次浏览器跳转优先使用。

## CI 构建

推送 `v*` tag（如 `git tag v0.1.0 && git push --tags`）会触发 `.github/workflows/tauri-build.yml`，在 GitHub Actions 上构建三平台安装包。
