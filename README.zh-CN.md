<!--
关于
Codex / ChatGPT 账户负载均衡与代理，提供用量追踪、仪表盘和 OpenCode 兼容端点。

主题
python oauth sqlalchemy dashboard load-balancer openai rate-limit api-proxy codex fastapi usage-tracking chatgpt opencode
-->

# codex-lb

![codex-lb](docs/screenshots/banner.jpg)

> **这是 [Soju06/codex-lb](https://github.com/Soju06/codex-lb) 的维护分支（fork），由
> [@insciqq](https://github.com/insciqq) 维护。** codex-lb 由 Soju06 创建，并由下方英文 README 中列出的
> 贡献者共同完成，功劳归于他们。上游自 2026-10-16 起没有新的提交；本 fork 让 codex-lb 继续兼容当前的
> Codex 客户端和 OpenAI 后端。
>
> - **范围：** 缺陷修复、安全、对当前 Codex 客户端与 OpenAI 后端的兼容性，以及依赖更新。在 fork 经过几个稳定
>   版本之前不添加新功能。
> - **版本：** 标签为 `v<上游基线>-fork.<n>`，镜像为 `ghcr.io/insciqq/codex-lb`（没有 `latest`）。发布说明见
>   [Releases](https://github.com/insciqq/codex-lb/releases)；`CHANGELOG.md` 是上游的。
> - **从上游迁移：** 见英文 README 的 [Migrating from upstream](./README.md#migrating-from-upstream)。
> - **自 `fork.2` 版本起默认关闭遥测**（[说明](docs/telemetry.md)）。
> - **问题反馈：** 请在[本仓库提交 issue](https://github.com/insciqq/codex-lb/issues/new/choose)，不要提交到上游。
>   安全问题请通过[私密安全通告](https://github.com/insciqq/codex-lb/security/advisories/new)报告。

[English](./README.md) | **简体中文**

> **文档（英文，权威版本）**: [docs/](docs/index.md) — 本页内容可能滞后，最新使用说明以英文文档为准。站点发布于 <https://insciqq.github.io/codex-lb/>；上游自己的站点 <https://soju06.github.io/codex-lb/> 对应上游的版本、镜像和 Helm chart。

ChatGPT 账户负载均衡器。聚合多个账户、追踪用量、管理 API Key，所有内容在仪表盘中查看。

**文档: [docs/](docs/index.md)** — 快速上手、客户端配置、配置项、部署、故障排查以及更多截图。

## 功能特性

<table>
<tr>
<td><b>账户池化</b><br>在多个 ChatGPT 账户之间负载均衡</td>
<td><b>用量追踪</b><br>按账户记录 token、成本及 28 天趋势</td>
<td><b>API Key</b><br>按 token、成本、时间窗口、模型限流</td>
</tr>
<tr>
<td><b>仪表盘鉴权</b><br>密码 + 可选 TOTP</td>
<td><b>OpenAI 兼容</b><br>支持 Codex CLI、OpenCode 及任意 OpenAI 客户端</td>
<td><b>模型自动同步</b><br>从上游拉取可用模型列表</td>
</tr>
</table>

| ![dashboard](docs/screenshots/dashboard.jpg) | ![accounts](docs/screenshots/accounts.jpg) |
|:---:|:---:|

## 快速开始

```bash
# Docker（推荐）
docker volume create codex-lb-data
docker network inspect codex-lb-net >/dev/null 2>&1 || docker network create codex-lb-net
docker run -d --name codex-lb \
  --stop-timeout 65 \
  --network codex-lb-net \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.2

# 或者使用 nix
nix run github:insciqq/codex-lb/v1.25.0-fork.2
```

`uvx codex-lb` 安装的是上游的 PyPI 版本，而不是本 fork；本 fork 只发布容器镜像。

本 fork 发布 `ghcr.io/insciqq/codex-lb:<version>` 和
`ghcr.io/insciqq/codex-lb:sha-<完整 commit SHA>`，没有 `latest` 标签。
生产环境请固定 `sha-` 标签或镜像 digest（见 [FORK.md](FORK.md#releases-and-images)）。

打开 [localhost:2455](http://localhost:2455) → 添加账户 → 完成。

首次远程访问仪表盘？需要一次性的 bootstrap token ——
参见 [快速上手](docs/getting-started.md)。

## 客户端配置

将任意 OpenAI 兼容客户端指向 codex-lb 即可。以 Codex CLI 为例，`~/.codex/config.toml`：

```toml
model = "gpt-6-astra"
model_reasoning_effort = "xhigh"
model_provider = "codex-lb"

[model_providers.codex-lb]
name = "openai"  # 必填 —— 启用远程 /responses/compact。自 Codex 2026-05-23 起须为小写；旧写法 "OpenAI" 无法解析 gpt-5.5
base_url = "http://127.0.0.1:2455/backend-api/codex"
wire_api = "responses"
supports_websockets = true
supports_standalone_web_search = true # 需要 codex-lb >= 1.22.0
requires_openai_auth = true # codex 应用需要
```

| Logo | 客户端 | 端点 | 指南 |
|---|--------|----------|-------|
| <img src="https://avatars.githubusercontent.com/u/14957082?s=200" width="32" alt="OpenAI"> | **Codex CLI / IDE** | `http://127.0.0.1:2455/backend-api/codex` | [客户端配置 → Codex CLI](docs/client-setup.md#codex-cli--ide-extension) |
| <img src="https://avatars.githubusercontent.com/u/66570915?s=200" width="32" alt="OpenCode (Anomaly)"> | **OpenCode** | `http://127.0.0.1:2455/v1` | [客户端配置 → OpenCode](docs/client-setup.md#opencode) |
| <img src="https://avatars.githubusercontent.com/u/252820863?s=200" width="32" alt="OpenClaw"> | **OpenClaw** | `http://127.0.0.1:2455/v1` | [客户端配置 → OpenClaw](docs/client-setup.md#openclaw) |
| <img src="https://avatars.githubusercontent.com/u/134168893?s=200" width="32" alt="Hermes Agent (Nous Research)"> | **Hermes Agent** | `http://127.0.0.1:2455/v1` | [客户端配置 → Hermes Agent](docs/client-setup.md#hermes-agent) |
| <img src="https://cdn.jsdelivr.net/gh/devicons/devicon/icons/python/python-original.svg" width="32" alt="Python"> | **OpenAI Python SDK** | `http://127.0.0.1:2455/v1` | [客户端配置 → Python SDK](docs/client-setup.md#openai-python-sdk) |

远程客户端需要在仪表盘中创建的 [API Key](docs/api-keys.md)。

## 配置

通过 `CODEX_LB_` 前缀的环境变量或 `.env.local` 配置 —— 详见 [`.env.example`](.env.example) 与
[配置指南](docs/configuration.md)。默认数据库后端是 SQLite；
可选通过 `CODEX_LB_DATABASE_URL` 切换到 PostgreSQL。

## 数据

| 环境 | 路径 |
|-------------|------|
| 本地 / uvx | `~/.codex-lb/` |
| Docker | `/var/lib/codex-lb/` |

请备份此目录以保留你的数据。

## 文档

完整文档位于 **[docs/](docs/index.md)**：

- [快速上手](docs/getting-started.md) —— 快速开始、远程访问 bootstrap token
- [客户端配置](docs/client-setup.md) —— Codex CLI、OpenCode、OpenClaw、Python SDK
- [配置](docs/configuration.md) —— 真正重要的少数设置项
- [鉴权](docs/authentication.md) —— 仪表盘鉴权模式
- [API Key](docs/api-keys.md) —— 保护代理路由
- [路由](docs/routing.md) —— 策略指南
- [数据库](docs/database.md) —— SQLite / PostgreSQL、Postgres 16 → 18 升级
- [部署](docs/deployment/docker.md) —— [Docker](docs/deployment/docker.md)、[Kubernetes](docs/deployment/kubernetes.md)、[远程访问](docs/deployment/remote.md)
- [故障排查](docs/troubleshooting.md)

### 社区伴生项目

由社区独立维护、消费仪表盘 API 的项目，不属于 codex-lb 本体
（访问指引见 [文档中的列表](docs/index.md#community-companions)）：

- [Codex LB Status Bar](https://github.com/sm1ee/codex-lb-statusbar) —— 原生 macOS 应用：账户状态、配额详情、账户控制
- [codex-lb SwiftBar](https://github.com/joschi655/codex-lb-swiftbar) —— 只读的 SwiftBar/Bun 监控器，显示账户池状态与配额余量

## 开发

```bash
# Docker
docker compose watch

# 本地
uv sync && cd frontend && bun install && cd ..
uv run codex-lb                              # 后端 :2455
cd frontend && bun run dev                   # 前端 :5173

# Nix
nix run .
nix develop # 进入开发环境
```

## 贡献者 ✨

完整的贡献者名单请参见英文 [README](./README.md#contributors-) 中由 [all-contributors](https://github.com/all-contributors/all-contributors) 自动生成的列表（该列表由机器人维护，仅写入 `README.md`）。该项目欢迎任何形式的贡献！
