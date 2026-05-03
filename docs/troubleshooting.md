# 常见问题

按 **现象** 索引。如果你不知道现在卡哪了，先去看 `data/runs/{run_id}.json` 的 `current_stage` + `logs` —— 90% 的问题能定位。

---

## 安装阶段

### `playwright install chromium` 慢 / 卡

国内网络问题。换源：

```bash
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    uv run playwright install chromium
```

或直接用 host 上已有的 chrome（不推荐，UA 指纹会和 Playwright 默认不同）。

### `npm run build` 报 `tailwindcss` 找不到

```bash
cd web
rm -rf node_modules package-lock.json
npm install
npm run build
```

某些 npm 版本对 type:module + tailwind 兼容差，删了重装一般好。

### `uv run autofree api` 报 `module 'autofree.api' not found`

`pyproject.toml` 没生效。看 `uv sync` 有没有报错；或者 `pip install -e .` 看一眼 `pip show autofree`。

---

## Setup 阶段

### 测试连通：邮箱后端 FAIL

| error 字符串 | 病因 | 修法 |
|---|---|---|
| `cf_temp_email 响应不像 dreamhunter2333 后端` | base_url 指向了 maillab 服务器 | 切 provider 到 maillab 或换 base_url |
| `maillab {path} 404` | base_url 指向 cf_temp_email | 反过来切 |
| `tempmail api_key 无效 (HTTP 401)` | api_key 错或 tempmail 没启动 | 进 tempmail 后台重新生成；`curl http://tempmail/health` 看活没活 |
| `Connection refused` / `Failed to establish a new connection` | base_url 写错（端口、scheme、有没有 `/api` 后缀） | 检查 — tempmail 不要带 `/api` 后缀，cf_temp_email 要带 `/api` |
| `ssl: certificate verify failed` | 自签证书 | 用 nginx 套个 LE 证书；或者拿 IP+http |

### 测试连通：CPA FAIL

```
list_remote HTTP 401: Unauthorized
```

→ Bearer key 写错。CPA 的 key 是它自己的 admin key，不是 AutoFree 的 key。

```
Connection refused
```

→ CPA 没起，或不在你想的端口。CPA 默认 8317。

### 母号导入：`/api/auth/session HTTP 401`

session_token 复制时漏了一段。`__Secure-next-auth.session-token` 在 cookie 表里可能被拆成 `.0` `.1` 两段，按数字顺序拼起来再粘。粘完总长一般 6000-8000 字符。

### 母号导入后操作：`Unauthorized - Access token is missing` (HTTP 401)

完整报文长这样：

```
GET /backend-api/accounts/<uuid>/settings: HTTP 401
{"detail":{"message":"Unauthorized - Access token is missing"}}
```

**根因**：chatgpt 的 `/backend-api/*` 端点 **要求 Bearer access_token**，光有 session cookie 不够。AutoFree 默认会用 session cookie 去 `/api/auth/session` 自动换 access_token；但有些情况下（cookie 拼接错位、Cloudflare 干扰、chatgpt 后端 NextAuth 版本问题）这一步换不出来，于是 backend-api 调用就 401。

**修复**：手动从浏览器把 access_token 也粘进来。两步：

1. 浏览器 F12 → **Network** → 刷新当前 chatgpt 页 → 找 `/api/auth/session` 那条请求
2. 切到 **Response** 面板 → 复制 `accessToken` 字段的 value（很长，`eyJ...` 开头）
3. 在 AutoFree Setup 页：
   - **首次导入** 同时填 session_token + access_token,点 **导入并验证**
   - **已导入但报 401** 用 **单独更新 access_token** 那一栏粘贴,点 **更新 access_token**

之后所有 `/backend-api/*` 调用都会带这个 Bearer 头，401 消失。

**诊断**：Setup 页的 **诊断** 按钮（或 `GET /api/master/diagnose`）会打印：

```
session_token_set: true
access_token_set: false        ← 这里 false 就是问题
account_id_set: true
/api/auth/session: 200 has_user=false has_access_token=false  ← 空 session,cookie 不被认
/backend-api/.../settings: 401 preview: '...Access token is missing...'
```

**注意**：access_token 是个 JWT，**有效期一般 30 天**（看 payload 的 `exp`），过期后再补一次新值即可。

### 母号导入：`MasterCloudflareError`

Cloudflare 把直连 chatgpt 的请求拦截了。两种可能：

1. **没用代理** — Setup 页填代理；或 `data/settings.json` 把 `proxy` 写好。
2. **用了机房 IP** — 即使有代理 Cloudflare 也可能拦。换住宅代理。

紧急绕过：用 Playwright headed 模式手登一次，把整段 cookie jar 导出来 (DevTools → Application → Cookies → Right click → Copy all)，然后只挑 `__Secure-next-auth.session-token` 那段粘进 AutoFree。

### probe identity + members：401

session_token 过期了 (一般 30 天) 或被 OpenAI 主动注销 (吊销了原 cookie)。重新登录母号 → 复制新的 session_token 进来。

### probe identity + members：404 / 找不到 auto_provision

母号不是 Team workspace 而是 Personal/Plus 账号。Personal 账号没有 `/admin/identity` 这一套接口。**换一个 Team 母号**。

---

## Run 阶段

### 启动 → 立即失败 `母号 account_id 未设置`

session_token 导入了但没拿到 account_id。多半是因为 `/api/auth/session` 没返回 `default_workspace_id`。手动在 Setup 页 **手动覆盖 account_id** 那一栏填上（从浏览器 DevTools Network 面板看任意 chatgpt admin 请求的 `chatgpt-account-id` header）。

### `N + 已有成员=11 > 10`

OpenAI Team 上限 10 人。你的 master workspace 里现在 K 个人，本轮想新增 N，K+N>10 就拒。要么让 N 更小，要么先在 ChatGPT admin 页手动踢几个旧号。

### Round 1 step 3/6 卡在注册

最常见三种：

1. **OTP 一直没收到** — Run 详情日志里有 `等待邮箱 OTP for ...`，超过 120 秒就 TimeoutError。
   - 邮箱后端的 MX 记录没生效 → `dig MX yourdomain.com` 看一下
   - OpenAI 把这个域名标黑 → 换域名
   - 后端服务挂了 → 直接 `curl tempmail/api/me` 看

2. **OTP 来了但 `validate_otp` 502** — sentinel token 失效，OpenAI 改 sdk 版本了。
   - 看 `data/runs/{id}.json` 里有没有 `Sentinel ... failed`，有就改 [flow.py SENTINEL_SDK_VERSION](../src/autofree/flow.py#L52) 为最新
   - 临时 workaround：跑 headed 模式 (`Flow(..., headless=False)`)，让 sentinel SDK 在浏览器里正常初始化

3. **`register failed: {detail: 'risk_assessment'}`** — IP 被 OpenAI 风控。换代理出口 IP；同一个 IP 不要注册超过 5 个号 / 小时。

### Round 1 step 5/6 OAuth 卡死 → 拿到 team 而不是 free

```
OAuth workspaces[] 中无 personal 选项: [...]
```

最常见原因：**auto_provision 还没开起来**。

- 检查 `master.set_auto_provision(True)` 是否真的成功 (curl `/api/master/identity` 看 auto_provision 字段)
- `AP_PROPAGATION_DELAY=5` 太短，OpenAI 后端没同步过去 → 把 .env 里这个值改大到 15-30 重启
- 域名 verified 状态丢了 (DNS TXT 被删了之类) → 回 ChatGPT Account → Verified Domains 检查

如果 auto_provision 是 `True` 还是没 personal —— 那就是 OpenAI 改了 workspace 选择逻辑（README 顶部那条 fork 警告说的 "round 11 阻断"）。需要：
1. 去 [AutoTeam-F README](https://github.com/ZRainbow1275/AutoTeam-F#readme) 看最新动态
2. 抓自己的包看 `oai-client-auth-session` cookie 解出来什么样
3. 改 [`flow._is_personal_workspace`](../src/autofree/flow.py#L83) 的判定规则

### Run 状态卡在 `running` 永远不动

后台 thread 死锁了。看 `ps aux | grep autofree`，看到 thread 还在但 stage 不变就是 hang 了。

紧急处理：

```bash
# 取消当前 run
curl -X POST -H "Authorization: Bearer $KEY" \
     http://localhost:8788/api/runs/<run_id>/cancel
# 再不行就 kill autofree 然后重启,run 会被标 cancelled
```

### Run done_with_errors 但都失败了

看 `cohort` 列表的 `error` 字段。常见：

- `register OTP validate failed` → OTP 收到了但提交失败,sentinel token 问题 (见上)
- `oauth_personal 返回空 token` → consent 阶段没拿到 code,看 logs 里有没有 `add-phone`
- `kick: ... HTTP 404` → 这个 email 在母号 list_members 里查不到 user_id (可能因为 free 号没加入 Team workspace,正常)

---

## CPA 推送阶段

### `already_exists`

CPA 上已经有 `codex-free-{email}.json`。两种情况：

1. 你之前推过同一个 email 的号 — 用 `force=true` 重推
2. AutoTeam-Free 也在推这个 email — 不太可能，因为 prefix 不同 (`codex-` vs `codex-free-`),撞名只可能因为你重复跑同一个 email

### `upload HTTP 413`

CPA 的 nginx 设了 body size 限制。改 nginx `client_max_body_size 1m;`。auth.json 一般 < 10KB，不该 413。

### push 完 CPA 看不到文件

`curl -H Authorization:Bearer\ $CPA_KEY http://cpa/v0/management/auth-files`，确认。如果 CPA 自己有缓存或 worker pool 没刷新，重启 CPA。

---

## 邮箱后端特定问题

### tempmail：创建邮箱 503 `no active domains available`

tempmail 后台没有激活的 domain。进 tempmail 管理界面 → 域名 → 状态 `pending` 的等 MX 自动验证；或手动 toggle。

### tempmail：OTP 收到了但 fast path 没命中

tempmail 的 `otp/latest` 内置正则可能漏 — 改用通用正则 fallback，看 base.py `extract_otp_from_email` 有没有命中。如果都没命中，把那封原始邮件的 body 贴出来发 issue（去掉 token 部分）。

### cf_temp_email：`/admin/mails 返回 304`

cf_temp_email 的某些版本会缓存。AutoFree 不带 cache-bust query；现象是邮件来了但本地查不到。临时 workaround：把后端 `?_cb=$random` 拼上去 (改 [`cf_temp_email._get`](../src/autofree/mail/cf_temp_email.py#L42))。

### maillab：`maillab /login HTTP 200 code:401`

字面意思 — username/password 错。注意 maillab 的 username 是 **邮箱地址**，不是 username。

---

## Web UI 问题

### "未连接" / handshake 失败

- API key 错 → 看 `data/.env` 里的 `AUTOFREE_API_KEY`
- 跨域 → 当前 CORS 全开 `*`，正常不应跨域问题；如果你套了 nginx，在 nginx 里加 `Access-Control-Allow-Origin: *`

### 状态栏 master 显示 "未导入" 但 setup 页明明导入了

刷新页面 (Ctrl+R)。master state 在内存里只在导入时刷新，如果中间手动改了 admin_state.json，需要触发一次刷新。

### Run 详情 stepper 不动

前端 poll 间隔 2.5s，所以最长有 2.5 秒延迟。如果超过 5s 不动，可能是后端 task 真的卡了 (见上)。

---

## 还是搞不定

把下面打包发你能联系的人:

```bash
cd AutoFree
tar czf autofree-debug.tgz \
    --exclude='data/auths/*.json' \
    --exclude='data/admin_state.json' \
    --exclude='data/.env' \
    data/runs/ data/settings.json \
    src/autofree/*.py src/autofree/mail/*.py
```

不要把 `data/auths/` 和 `admin_state.json` 发出去 —— 里面有 token。
