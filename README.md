<div align="center">

# AutoFree

**ChatGPT Personal `plan_type=free` 批量生产工具**

基于 [AutoTeam-Free](https://github.com/ZRainbow1275/AutoTeam-F) 的 free 链路 + [daily-playwright](daily-playwright.py) 注册脚本，重写为单一职责的精简版本。CTF 专用。

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue_3-Web-4FC08D?style=flat-square&logo=vue.js&logoColor=white)](https://vuejs.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## 它是什么 / 它不是什么

**是**：把母号 (ChatGPT Team) 当跳板，**批量** 生产 Personal 免费号的 Codex OAuth `auth.json`，再把 `auth.json` 推到 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 当作免费的 `plan_type=free` 后端用。

**不是**：通用账号管理面板。**没有** 轮转、巡检、Team 子号补位、CPA 反向同步等能力 — 那些在 [AutoTeam-Free](https://github.com/ZRainbow1275/AutoTeam-F) 里。AutoFree 砍到只剩一条主线。

---

## 核心流程（这是我们要做的事情）

每一轮 `N` 个号、共 `R` 轮、全部串行：

```text
关 auto_provision  ←─  /backend-api/accounts/{id}/settings/auto_provision {"value":false}
        ↓
[串行 ×N] gpt.har 流程注册 free 号
        ↓                  ├─ /api/auth/csrf
        ↓                  ├─ /api/auth/signin/openai
        ↓                  ├─ /api/accounts/user/register
        ↓                  ├─ /api/accounts/email-otp/send  →  邮箱后端 wait_for_otp
        ↓                  ├─ /api/accounts/email-otp/validate
        ↓                  └─ /api/accounts/create_account  (name + birthdate)
        ↓
开 auto_provision  ←─  让 verified-domain 的号在下一步 OAuth 时自动归到 Personal workspace
        ↓
[串行 ×N] auth.har 流程做 Codex OAuth，强制选 personal workspace
        ↓                  ├─ /oauth/authorize  (PKCE)
        ↓                  ├─ /api/accounts/authorize/continue
        ↓                  ├─ /api/accounts/password/verify
        ↓                  ├─ (二次) /api/accounts/email-otp/validate
        ↓                  ├─ /api/accounts/workspace/select  ← 关键，挑 personal
        ↓                  ├─ consent 自动点击
        ↓                  └─ /oauth/token  →  access_token / refresh_token / id_token
        ↓
落盘 data/auths/{email}.json  (codex CLI / CPA 兼容格式)
        ↓
[串行 ×N] 母号 kick 这一批 free 号
                                   └─ DELETE /backend-api/accounts/{id}/users/{user_id}
        ↓
等用户在 Web 上手动点 "推送 → CPA"   (不会动 CPA 上已存在的文件)
```

> 容量约束：单轮 `N + 当前 Team 成员数 ≤ 10`，前端会提前校验。

---

## 特性

| | 功能 | 说明 |
|---|---|---|
| 🆓 | **批量生产 free 号** | 单轮 1-10 个，多轮串行；Team 容量校验前置 |
| 🔁 | **auto_provision 程序化** | 关→注册→开→OAuth 的精确编排，一键完成 |
| 📧 | **三种邮箱后端** | `tempmail`(自托管) / `cf_temp_email`(dreamhunter2333) / `maillab` |
| 🌐 | **网页配置代理** | proxy 在 web UI 改，不需要改 .env 重启 |
| 🔐 | **母号双链路登录** | session_token 直接导入 (推荐) + Playwright 邮箱密码登录 |
| 🛑 | **协作式取消** | Web "cancel" 按钮 / `cancel_run()` API,跑到当前安全点退出 |
| 📊 | **任务进度可视化** | stepper + cohort 表 + 实时日志，浏览器端 2.5s 轮询 |
| 📤 | **CPA 推送只入库** | 自动落盘但不主动推；前端多选→push;有同名前缀防误覆盖 |
| 🐍 | **CLI + Web 双入口** | `autofree run -R 3 -n 2` 或 `autofree api` |

---

## 30 秒上手

### 方式 A：一键脚本（推荐）

```bash
cd AutoFree
bash setup.sh                  # uv sync + playwright install chromium + frontend build + 生成 .env
uv run autofree api            # http://0.0.0.0:8788
```

### 方式 B：Docker

```bash
cd AutoFree
docker compose up -d --build   # 自带 chromium + xvfb,首次 build 约 5 分钟
# API key 在 data/.env(自动生成),用 `docker logs autofree | grep API_KEY` 看
```

### 方式 C：手动

```bash
cd AutoFree
uv sync                                       # 或 pip install -e .
uv run playwright install chromium            # ⚠️  必跑,否则 Flow.start() 会崩
uv run playwright install-deps chromium       # Linux 装 chromium 系统依赖

cd web && npm install && npm run build && cd ..

mkdir -p data && echo 'AUTOFREE_API_KEY=change-me' > data/.env
uv run autofree api
```

> ⚠️ **Playwright Chromium 不会自动装**。漏了这一步,启动 Run 任务时会报
> `BrowserType.launch: Executable doesn't exist at /root/.cache/ms-playwright/...`
> setup.sh 和 Dockerfile 都帮你做了;手动安装务必跑 `playwright install chromium`。

启动后浏览器访问 `http://<host>:8788` → 输入 API Key → Setup 配齐 → Run 启动。

**完整教程**：[docs/getting-started.md](docs/getting-started.md)

---

## CLI 命令

```text
autofree api                          启动 Web + API （默认 8788)
autofree run -R 3 -n 2                跑 3 轮 × 2 个号 （阻塞，CLI 看日志）
autofree status                       看母号状态 + 已生产 auth + 历史任务
autofree push <email>                 推一个 auth.json 到 CPA
autofree push-all                     推所有未推送的
autofree import-token                 stdin 粘贴 master session_token
autofree set-account-id <id>          手动覆盖 master 的 workspace account_id
```

跑 `autofree <command> -h` 看每个子命令的参数。

---

## Web 面板

| 页 | 功能 |
|---|---|
| **Setup** | 改 proxy / 选邮箱后端并填配置 / 填 CPA / 导入 master session_token / probe 测连通 |
| **Run** | 启动批次 (rounds × per_round) / 实时进度 stepper / cohort 表 / 日志 / cancel |
| **Auths** | 已落盘 auth 列表 / 多选推送 CPA / 详情查看 (token 脱敏) / 删除本地 |

启动 `autofree api` 后访问 `http://localhost:8788`。

---

## 设计原则

| 决策 | 取舍 |
|---|---|
| 砍掉 Team 轮转、巡检、CPA 反向同步 | 单一职责，CTF 用不上 |
| 全流程串行 | 浏览器并发会触发 IP 风控；串行慢但稳 |
| OAuth 强制选 personal workspace | 这是拿到 `plan_type=free` 的关键，不留 fallback 给 team |
| CPA push 只入库不自动推 | 出错可以 review，CPA 不会被半成品污染 |
| 文件名前缀 `codex-free-` | 跟 AutoTeam Team 模式的 CPA 文件不撞 |
| 配置在 `data/settings.json` 而非 .env | 网页能改，重启即生效；.env 只剩 `AUTOFREE_API_KEY` |
| 母号优先 session_token 导入 | Playwright 登录依赖 chatgpt 登录页结构稳定，靶场环境 cookie 一句话 |

---

## 项目结构

```text
AutoFree/
├── pyproject.toml
├── setup.sh                        一键安装脚本(uv + playwright + npm + .env)
├── Dockerfile                      容器镜像(自带 chromium + xvfb + 前端 build)
├── docker-compose.yml              单服务编排,挂载 ./data
├── docker-entrypoint.sh            启动时跑 xvfb + 自检关键 import
├── .env.example                    复制成 data/.env,只需要 AUTOFREE_API_KEY
├── README.md                       本文档
├── docs/                           详细文档
│   ├── getting-started.md          首次部署 + 跑通单号
│   ├── configuration.md            settings.json + 邮箱后端 + 母号
│   ├── api.md                      HTTP 端点参考
│   ├── architecture.md             模块图 + 数据流
│   └── troubleshooting.md          常见问题
├── src/autofree/
│   ├── config.py                   path 常量 + .env loader
│   ├── settings.py                 网页可改的运行时配置
│   ├── admin_state.py              母号 session_token 持久化
│   ├── storage.py                  data/auths/ + data/runs/ 读写
│   ├── master.py                   chatgpt.com /backend-api 客户端
│   │                                 ├ auto_provision toggle
│   │                                 ├ list_members / kick_user_by_email
│   │                                 └ verify_session / import_session_token
│   ├── mail/                       邮箱后端
│   │   ├── base.py                 MailProvider 抽象 + OTP 提取
│   │   ├── cf_temp_email.py        dreamhunter2333/cloudflare_temp_email
│   │   ├── maillab.py              maillab/cloud-mail
│   │   └── tempmail.py             /opt/code-server/project/tempmail (新增)
│   ├── flow.py                     Playwright register + OAuth-personal-forced
│   ├── runner.py                   多轮编排,后台线程 + cancel signal
│   ├── cpa_push.py                 CLIProxyAPI 推送 (add-only)
│   ├── api.py                      FastAPI 22 个端点 + 静态前端
│   ├── cli.py                      argparse 子命令
│   └── web/dist/                   前端 npm build 输出 (不入库)
├── web/                            Vue 3 + Vite + Tailwind
│   ├── src/api.js                  fetch 包装
│   ├── src/App.vue                 顶层导航 + key handshake
│   └── src/pages/
│       ├── SetupPage.vue
│       ├── RunPage.vue
│       └── AuthsPage.vue
└── data/                           运行时数据 (gitignore)
    ├── .env                        本地配置 (只放 AUTOFREE_API_KEY)
    ├── settings.json               proxy / mail / cpa,网页可改
    ├── admin_state.json            母号 session_token + account_id (0600)
    ├── auths/{email}.json          每个 free 号的 auth (0600)
    ├── runs/{run_id}.json          任务历史 + 完整日志
    └── logs/                       预留
```

---

## 文档导航

| 想做的事 | 看哪一份 |
|---|---|
| 第一次部署，跑通一个 free 号 | [getting-started.md](docs/getting-started.md) |
| 配某个邮箱后端 / 改 proxy / 导入母号 | [configuration.md](docs/configuration.md) |
| 用脚本调 HTTP API | [api.md](docs/api.md) |
| 想看每一步在哪个文件 / 哪个 commit | [architecture.md](docs/architecture.md) |
| 报错了 / 卡死了 / OAuth 拿不到 personal | [troubleshooting.md](docs/troubleshooting.md) |

---

## 已知限制

- **必须有 verified domain** + 母号 Identity & Access 里能配 auto_provision；这是项目能跑的前提。如果母号没有 verified domain，跑不通 — 因为 free 号注册后需要靠 verified-domain 自动加入 Team workspace 才能在 OAuth 时拿到 personal 选项。
- **IP 风控**：VPS / 数据中心 IP 容易被 OpenAI 标记为 abuse；建议给 proxy 配住宅。
- **Cloudflare 5 秒挑战**：母号 session_token 必须从已通过 Cloudflare 的会话里复制，不然 master client 直接 403。
- **OAuth `add-phone` gate**：理论上 OpenAI 可能要求新号绑手机号；本项目 **没有** 处理这个分支（CTF 母号一般不会触发）。如果遇到，参考 daily-playwright.py 的 `_report_add_phone`。
- **soft-cancel 不打断当前账号**：取消后会跑完当前正在注册/OAuth 的那一个号才退出；不暴力关浏览器。

---

## 免责声明

本项目仅供学习研究、CTF 比赛使用。使用本工具可能违反 OpenAI 服务条款。账号封禁、IP 限制、CLIProxyAPI 数据污染等后果由使用者自担。

## 致谢

- [cnitlrt/AutoTeam](https://github.com/cnitlrt/AutoTeam) — 上游骨架
- [ZRainbow1275/AutoTeam-F](https://github.com/ZRainbow1275/AutoTeam-F) — free 链路 + workspace 选择修复
- [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — 推送目标
- 本机 `daily-playwright.py` — 提供注册 + OAuth 网络层验证过的实现

License: MIT
