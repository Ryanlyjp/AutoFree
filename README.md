<div align="center">

# AutoFree

批量生产 ChatGPT Personal `plan_type=free` 账号 OAuth `auth.json` 的本地工具，支持 Web 面板、CLI、Docker 部署，以及推送到 CLIProxyAPI。

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue_3-Web-4FC08D?style=flat-square&logo=vue.js&logoColor=white)](https://vuejs.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## 项目概览

核心目标：

1. 使用母号完成 Team 侧的注册与编排。
2. 为新号执行 OAuth 流程并固定选择 personal workspace。
3. 获取 `auth.json`。
4. 按需将 `auth.json` 推送到 CLIProxyAPI。

产物默认保存在 `data/auths/`，运行过程和日志保存在 `data/runs/`。
/docs文件夹下有更详细项目说明
---

## 当前功能

- 批量运行：支持 `rounds × per_round` 多轮串行任务。
- 注册流程：自动完成注册、邮箱 OTP、资料补全。
- OAuth 流程：自动执行 PKCE、二次 OTP、workspace 选择和 token 交换。
- Personal 强制选择：优先选择 personal workspace，避免拿到 team token。
- 邮箱后端：支持 `tempmail`、`cf_temp_email`、`maillab`。
- 代理支持：支持 HTTP/HTTPS/SOCKS5，Web UI 可直接修改。
- 母号管理：支持导入 `session_token`、`access_token`、`account_id`。
- 任务控制：支持运行、查看日志、查看历史、取消当前批次。
- 仅注册模式：支持跳过 OAuth/kick，仅完成注册阶段。
- Auth 管理：支持查看、删除、单个推送、批量推送到 CPA。
- Docker 部署：内置 Chromium、xvfb、前端构建流程。
- CLI 入口：支持本地命令行启动 API、运行任务和推送产物。

---

## 工作流

```text
关闭 auto_provision
  -> 批量注册新号
  -> 打开 auto_provision
  -> 等待设置生效
  -> 批量执行 OAuth
  -> 强制选择 personal workspace
  -> 生成并保存 auth.json
  -> 可选推送到 CLIProxyAPI
  -> 可选把本轮账号从 Team 踢出
```

对应的核心模块：

- `src/autofree/flow.py`：注册与 OAuth 自动化。
- `src/autofree/master.py`：母号的 Team/identity/backend-api 操作。
- `src/autofree/runner.py`：批量编排、状态推进、取消逻辑。
- `src/autofree/api.py`：Web/API 入口。
- `src/autofree/cpa_push.py`：`auth.json` 生成与 CPA 推送。

---

## 运行前准备

开始部署前，请先确认以下条件：

1. 具备 Python 3.10+ 环境。
2. 如需本地源码运行，具备 Node.js 18+ 环境。
3. 机器能够通过代理访问 OpenAI 相关站点。
4. 具备一个可用的 ChatGPT Team 母号。
5. 母号侧已配置 Verified Domain，且能使用 Identity & Access 的 `auto_provision`。
6. 至少准备一个可用邮箱后端。

---

## 部署方式

### 方式一：Docker

适合直接起服务，本机只保留源码和 `data/` 持久化目录。

```bash
git clone <your-fork-or-repo-url> AutoFree
cd AutoFree
docker compose up -d --build
```

启动后：

1. 服务默认监听 `http://127.0.0.1:8788`。
2. API Key 会自动写入 `data/.env`。
3. 首次构建会安装 Python 依赖、Playwright Chromium、前端依赖，耗时会明显更长。

查看 API Key：

```bash
cat data/.env
```

查看容器状态和日志：

```bash
docker compose ps
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

### 方式二：本地源码运行

适合调试、改代码和直接观察运行日志。

```bash
git clone <your-fork-or-repo-url> AutoFree
cd AutoFree
uv sync
uv run playwright install chromium
uv run playwright install-deps chromium
cd web && npm install && npm run build && cd ..
```

初始化运行目录并启动 API：

```bash
mkdir -p data
echo "AUTOFREE_API_KEY=change-me" > data/.env
uv run autofree api
```

Linux/macOS 也可以直接使用：

```bash
bash setup.sh
uv run autofree api
```

---

## 首次配置

服务启动后，浏览器打开 `http://127.0.0.1:8788`，按以下顺序配置。

### 1. 登录 Web 面板

1. 打开页面。
2. 输入 `data/.env` 里的 `AUTOFREE_API_KEY`。
3. 进入 `Setup` 页面。

### 2. 配置代理

在 `Setup` 页面填写 `proxy`。

常见格式：

```text
http://127.0.0.1:7890
https://127.0.0.1:7890
socks5://127.0.0.1:1080
```

如果 AutoFree 运行在 Docker 容器中，容器内访问宿主机代理时应填写：

```text
http://host.docker.internal:7890
```

不要在 Docker 容器里填 `127.0.0.1:7890`，那会指向容器自身。

### 3. 配置邮箱后端

支持三种后端：

1. `tempmail`
2. `cf_temp_email`
3. `maillab`

按后端类型填写对应的 `base_url/api_url`、认证信息和域名，然后使用页面上的测试功能确认后端可用。

### 4. 配置母号

推荐填写：

1. `session_token`
2. `access_token`
3. `account_id`
4. 可选 `email`

建议从浏览器抓母号真实登录态(F12)：

1. 在 `chatgpt.com` 登录母号。
2. 从Application - Cookie 中提取 `__Secure-next-auth.session-token`以及`account_id`。
3. 从Network - 任意请求中的Request headers下的Authorization中提取 `accessToken` (Bearer之后的内容，ey开头)。

未加入原项目的playwright登陆，因为懒。

### 5. 可选配置 CLIProxyAPI

如果你需要自动推送 `auth.json`，再填写：

1. `CPA base_url`
2. `CPA key`

未配置时不影响本地产物落盘。

---

## 使用步骤

### 1. 启动批次任务

打开 `Run` 页面，设置：

1. `rounds`
2. `per_round`
3. 是否启用 `register_only`

然后点击开始。

### 2. 观察任务状态

运行期间可以在 `Run` 页面查看：

1. 当前阶段
2. 每个账号的状态
3. 实时日志
4. 历史任务

如果需要停止，可以使用页面上的 `cancel`。

### 3. 查看产物

成功后到 `Auths` 页面查看生成结果，或直接检查本地目录：

```bash
ls data/auths
```

每个账号会生成一个对应的 `*.json`。

### 4. 推送到 CPA

在 `Auths` 页面可以：

1. 推送单个 auth
2. 批量推送 auth
3. 删除本地 auth

---

## CLI 用法

```text
autofree api
autofree run -R 3 -n 2
autofree status
autofree push <email>
autofree push-all
autofree import-token
autofree set-account-id <id>
```

常用示例：

```bash
uv run autofree api
uv run autofree run -R 1 -n 1
uv run autofree status
```

---

## Web 页面说明

### Setup

用于配置：

1. 代理
2. 邮箱后端
3. CLIProxyAPI
4. 母号登录态

### Run

用于：

1. 启动批次
2. 查看实时进度
3. 查看运行日志
4. 取消当前任务

### Auths

用于：

1. 查看落盘的 `auth.json`
2. 删除本地 auth
3. 推送到 CPA

---

## 数据目录

```text
data/
├── .env
├── settings.json
├── admin_state.json
├── auths/
├── runs/
└── logs/
```

说明：

- `data/.env`：API Key 等少量启动配置。
- `data/settings.json`：代理、邮箱、CPA 等运行配置。
- `data/admin_state.json`：母号凭据与状态。
- `data/auths/`：生成的 `auth.json`。
- `data/runs/`：批次记录和完整日志。

---

## 配置优先级

1. `data/settings.json`
2. `data/.env`
3. 代码默认值

Web UI 修改的大部分配置都会写入 `data/settings.json`，后续任务直接读取该文件。

---

## 已知限制

1. 必须有 Verified Domain 和可用的 `auto_provision`。
2. 数据中心 IP 更容易触发风控，建议使用质量更好的代理和域名邮箱。
3. 母号 `session_token` 需要来自已通过 Cloudflare 的真实会话。
4. `OAuth add-phone` 分支目前没有自动处理逻辑。
5. 取消任务是协作式取消，不会暴力终止当前账号执行到一半的浏览器流程。

---

## 故障排查

常见排查入口：

1. `docs/getting-started.md`
2. `docs/configuration.md`
3. `docs/api.md`
4. `docs/architecture.md`
5. `docs/troubleshooting.md`

常用命令：

```bash
docker compose logs -f
uv run autofree status
```

---

## 致谢

本项目在设计和实现过程中参考了以下项目与社区：

1. [ZRainbow1275/AutoTeam-F](https://github.com/ZRainbow1275/AutoTeam-F)  
   本项目的二开参考，特别是在整体流程拆解方面提供了直接启发。
2. [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam)  
   上游自动化流程和整体思路的重要来源。
3. [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)  
   为 `auth.json` 推送与后续消费提供了目标接口与使用场景。
4. [LinuxDo](https://linux.do/)  
  

---

## 友链

1. [AutoTeam-F](https://github.com/ZRainbow1275/AutoTeam-F)
2. [AutoTeam](https://github.com/cnitlrt/AutoTeam)
3. [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
4. [LinuxDo](https://linux.do/)

---

## License

MIT
