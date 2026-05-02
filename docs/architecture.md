# Architecture

## 顶层视图

```text
┌─────────────────────────────────────────────────────────┐
│                     Web Browser                          │
│   Vue 3 SPA  (web/src/{App,pages/{Setup,Run,Auths}}.vue)│
└──────────────────────────┬──────────────────────────────┘
                           │ Bearer key in localStorage
                           │ JSON over /api/*
                           ▼
┌─────────────────────────────────────────────────────────┐
│  FastAPI (api.py)         22 endpoints + StaticFiles    │
│  - bearer auth dependency                               │
│  - delegates to settings / admin_state / master /       │
│    runner / cpa_push / mail / storage                   │
└──────────────────────────┬──────────────────────────────┘
                           │
       ┌───────────────────┼─────────────────────────┐
       │                   │                         │
       ▼                   ▼                         ▼
┌───────────────┐    ┌──────────────┐         ┌──────────────┐
│ runner.py     │    │ master.py    │         │ cpa_push.py  │
│ (orchestrator)│    │ (chatgpt.com │         │ (CLIProxyAPI)│
│ thread + cancel  /backend-api)   │         │              │
└──────┬────────┘    └──────────────┘         └──────────────┘
       │
       │ uses
       ▼
┌───────────────┐    ┌──────────────┐
│ flow.py       │    │ mail/        │
│ (Playwright)  │◄───┤ MailProvider │
└──────┬────────┘    └──────────────┘
       │
       ▼
┌───────────────────────────────────────────────────────┐
│ chatgpt.com / auth.openai.com (real OpenAI traffic)   │
└───────────────────────────────────────────────────────┘
```

---

## 数据流（单轮 1 个号）

```text
RunPage.vue           POST /api/runs {rounds:1,per_round:1}
    │
    ▼
api.py runs_start            → runner.start_run(1, 1)
    │                              │
    │  202 + run_record            │ 起 thread,马上返回
    │                              │
    ▼                              ▼
RunPage 开始 poll          runner._run_multi_inner(run_id)
GET /api/runs/{id}/2.5s        ↓
    │                          1. master.set_auto_provision(False)
    │                          2. master.list_members() (校验容量)
    │                          3. for i in range(N):
    │                                mail.create_temp_email() → mailbox_id, email
    │                                Flow().start()
    │                                Flow.run_register()
    │                                  └─ csrf, signin, register, send_otp
    │                                  └─ mail.wait_for_otp(email, mailbox_id)
    │                                  └─ validate_otp, create_account
    │                                Flow.close()
    │                          4. master.set_auto_provision(True)
    │                          5. sleep AP_PROPAGATION_DELAY
    │                          6. for member in successes:
    │                                Flow().start()
    │                                Flow.oauth_personal()
    │                                  └─ /oauth/authorize (PKCE)
    │                                  └─ authorize/continue
    │                                  └─ password/verify
    │                                  └─ email-otp/validate (mail.wait_for_otp 二次)
    │                                  └─ workspace/select (force personal)
    │                                  └─ consent click
    │                                  └─ /oauth/token
    │                                cpa_push.save_and_register(email, tokens)
    │                                Flow.close()
    │                          7. for member in cohort:
    │                                master.kick_user_by_email(email)
    │                          (each step writes back to runs/{run_id}.json)
    ▼
看到 status:done             ↑
prompts user to click          所有日志 / cohort / summary 都在这个文件
"推送 → CPA"                  里,前端 poll 来回显
    │
    ▼
POST /api/auths/{email}/push
    │
    ▼
cpa_push.push_one()
    └─ list_remote() → 查重
    └─ POST /v0/management/auth-files (multipart)
    └─ storage.mark_pushed(email)
```

---

## 模块责任

| 模块 | 行数 | 责任 | 依赖 |
|---|---:|---|---|
| `config.py` | 63 | 路径常量 + .env loader + 时间常量 | — |
| `settings.py` | 114 | `data/settings.json` 读写 + 深合并 | `config` |
| `admin_state.py` | 87 | 母号 token 持久化（mode 0600） | `config` |
| `storage.py` | 206 | `auths/` + `runs/` 增删改查 | `config` |
| `mail/base.py` | 273 | `MailProvider` ABC + OTP 提取 + 轮询 | `config` |
| `mail/cf_temp_email.py` | 234 | dreamhunter2333 后端 | `mail.base`, `settings` |
| `mail/maillab.py` | 287 | maillab 后端（带 401 自愈） | `mail.base`, `settings` |
| `mail/tempmail.py` | 247 | tempmail 后端（服务端 OTP fast path） | `mail.base`, `settings` |
| `master.py` | 398 | chatgpt.com /backend-api 客户端 | `settings`, `admin_state` |
| `flow.py` | 1399 | Playwright 注册 + OAuth-personal | `mail.base`, `config` |
| `cpa_push.py` | 184 | CLIProxyAPI 推送 + auth.json builder | `settings`, `storage` |
| `runner.py` | 343 | 多轮编排 + thread + cancel signal | `master`, `mail`, `flow`, `cpa_push`, `storage` |
| `api.py` | 407 | FastAPI 路由层 | 几乎所有 |
| `cli.py` | 192 | argparse 子命令 | `runner`, `master`, `cpa_push`, ... |

---

## 关键决策

### 1. 为什么 settings.json 而不是纯 .env

CTF 比赛环境经常要换代理 / 换邮箱后端 / 换母号；如果每次改 .env 都要重启 + 丢任务进度，不可接受。`settings.json` + Web UI 可以热改，下一次任务读到新值。

### 2. 为什么 CPA push 不自动

OAuth 出错的概率不低 (Cloudflare、OTP 慢、workspace 漂移)。如果失败的 auth 也被自动推到 CPA，会污染 CPA 的可用文件池。改成「落盘 + 用户手动推」让人能 review 再决定。

文件名前缀 `codex-free-` 也是同理 — 让 CPA 上 AutoFree 产物和 AutoTeam Team 产物视觉可分。

### 3. 为什么 OAuth 强制选 personal

抓包 (`auth.har`) 显示 OpenAI 在 OAuth 流程的 `oai-client-auth-session` cookie 里下发 `workspaces[]`，前端用 `POST /api/accounts/workspace/select` 告诉服务器选哪个。如果不挑，server 按 `default_workspace_id` 颁，对 master 母号下注册的号来说 `default_workspace_id` 会被设成 Team workspace —— 拿到的 token 的 `chatgpt_plan_type=team`，不是我们想要的 free。

`flow._select_personal_workspace` 用三条件 OR 识别 personal:

```python
structure ∈ {personal, personal_v2, personal_account}
plan_type == "free"
is_personal == True
```

任一命中即选。

### 4. 为什么用 thread 不是 async

Playwright 的 sync API 比 async 好调试，daily-playwright.py 也用的 sync。runner 起一个 thread 里跑 sync Playwright + sync HTTP，FastAPI 主线程依然异步响应 poll 请求。简单可靠。

cancel signal 是 `threading.Event()`，在每个安全边界 (round 之间、每个号注册前) check 一次。不暴力中断浏览器进程。

### 5. 为什么 master.py 用纯 requests 不是 Playwright

master 的所有操作 (auto_provision toggle / kick / list members / identity) 都是简单的 `/backend-api/*` 调用，session_token cookie + headers 就够了，不需要浏览器渲染。

唯一的风险是 Cloudflare 5s 挑战 — 这种情况下 `session_token` 已经在浏览器里通过过一次了，cookie 已经包含 `cf_clearance`。如果用户从浏览器复制 session_token 时没复制 `cf_clearance`，第一次调用会 403。`master.MasterCloudflareError` 会专门捕获这个并提示。

> 如果靶场环境真的过不去 Cloudflare，最快的办法是给 master 加一段 Playwright headed 模式让人手过一次，复制完整 cookie jar 进来。当前没实现，因为大部分 CTF 环境用代理就能直连。

---

## 数据落盘

```text
data/
├── settings.json            非敏感配置 (proxy / mail / cpa)
├── admin_state.json    0600 母号 session_token + account_id
├── auths/
│   └── *.json          0600 auth payload (含 access/refresh/id_token + 推送时间戳)
└── runs/
    └── *.json               日志 cap 5000 行,超过自动 ring-buffer
```

`settings.json` 解析失败时会 rename 成 `.bak` 然后从 .env 重建 (避免一次坏写丢配置)。

`admin_state.json` 和 `auths/*.json` 写完后会 chmod 600（仅 owner 可读）。

`runs/*.json` 单进程串行写，没加锁但有 RLock 保护读改写循环。多进程读会并发安全（不会写），写的话会冲突 — 不要并发跑两个 autofree api 共享同一份 data/。

---

## 上行 / 下行兼容性

| 出口 | 数据格式 | 兼容目标 |
|---|---|---|
| `data/auths/*.json` | `{type, email, expired, id_token, account_id, access_token, last_refresh, refresh_token, ...}` | Codex CLI / [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) / AutoTeam-Free |
| CPA upload | multipart `file=` 字段，名字 `codex-free-{email}.json` | CLIProxyAPI 任意版本 |

如果 CPA 升级了字段，改 [`cpa_push.build_auth_payload`](../src/autofree/cpa_push.py#L122) 一处即可，不会影响落盘格式。
