# 配置说明

AutoFree 把配置分成两层：

| 文件 | 谁改 | 重启? |
|---|---|---|
| `data/.env` | 部署者 | 是。改完重启 `autofree api`. |
| `data/settings.json` | Web UI（或 PATCH API） | **否**，下一次任务读到新值即生效 |

`.env` 只放真正不能放 JSON 的东西（API Key 之类）。其余 (proxy / 邮箱后端 / CPA) 全部 `settings.json`。

---

## .env 字段

```dotenv
# 必填 — Web Bearer 鉴权用
AUTOFREE_API_KEY=随机串

# 可选 — 首次启动时这些会被复制成 settings.json 的初始值。
# settings.json 创建后 .env 这些值就不再生效（settings.json 优先）。
HTTP_PROXY=
MAIL_PROVIDER=tempmail
TEMPMAIL_BASE_URL=
TEMPMAIL_API_KEY=
TEMPMAIL_DOMAIN=
CLOUDMAIL_BASE_URL=
CLOUDMAIL_PASSWORD=
CLOUDMAIL_DOMAIN=
MAILLAB_API_URL=
MAILLAB_USERNAME=
MAILLAB_PASSWORD=
MAILLAB_DOMAIN=
CPA_BASE_URL=
CPA_KEY=

# 高级 — 一般不改
EMAIL_POLL_INTERVAL=3                 # OTP 轮询周期 (秒)
EMAIL_POLL_TIMEOUT=120                # OTP 单封等待上限 (秒)
AP_PROPAGATION_DELAY=5                # auto_provision 开启后的等待 (秒,留时间让 OpenAI 后端同步)
```

---

## settings.json 结构

```json
{
  "proxy": "http://127.0.0.1:7890",
  "mail": {
    "provider": "tempmail",
    "tempmail": {
      "base_url": "http://tempmail.local",
      "api_key": "...",
      "domain": "@mail.example.com"
    },
    "cf_temp_email": {
      "base_url": "https://worker.example.com/api",
      "password": "...",
      "domain": "@example.com"
    },
    "maillab": {
      "api_url": "https://maillab.example.com/api",
      "username": "admin@example.com",
      "password": "...",
      "domain": "@example.com"
    }
  },
  "cpa": {
    "base_url": "http://127.0.0.1:8317",
    "key": "..."
  }
}
```

直接编辑这个文件也行，但 **改完 web 也要刷新页面** 才能看到（前端有缓存）。改运行中的任务无效 — 任务读的是启动那一刻的 settings 快照。

---

## 在网页改

| 区域 | 字段 | probe |
|---|---|---|
| 代理 | `proxy` | — |
| 邮箱后端 | provider 切换 + 各自 base_url / 密码 / 域名 | **测试连通** 调 `MailProvider.login()` |
| CLIProxyAPI | `base_url` + `key` | **probe** 调 `GET /v0/management/auth-files` 看通不通 |
| 母号 session_token | `session_token` (+ 可选 `account_id` / `email`) | **导入并验证** 自动调 `/api/auth/session` |
| 母号 account_id | 单独覆盖 (适合 session_token 已导入但 workspace 拿错的情况) | — |
| 母号 probe | — | **probe identity + members** 调 `/identity` + `/users` |

> 表单提交后,响应里会把 password / api_key / cpa.key 这些字段返回为 `<set:N>` (N = 长度)，**不要把这个伪字符串再次提交**，前端代码会自动剥掉，但如果你直接调 PATCH API，记得别带它。

---

## 三个邮箱后端的差别

| 项 | tempmail | cf_temp_email | maillab |
|---|---|---|---|
| 部署难度 | 中（要 Postgres + Postfix） | 低（Cloudflare Workers） | 中（要 D1 + 自部署） |
| 鉴权 | Bearer token | `x-admin-auth` header | `Authorization: <jwt>` (无 Bearer 前缀) |
| 创建邮箱接口 | `POST /api/mailboxes` | `POST /admin/new_address` | `POST /account/add` |
| OTP 提取 | **服务端** `/otp/latest` 内置正则 | 客户端正则 | 客户端正则 |
| 自动建箱 (catch-all) | ✅ | ❌ | ❌ |
| 多域名池 | ✅ | ✅ | ✅ |

**推荐 CTF 用 tempmail** — `/otp/latest` 服务端正则比客户端的更鲁棒，能省一次邮件解析的来回。

---

## 母号导入

session_token 是关键凭据，三种来源：

### A. 浏览器手抄（最快）

需要 **两段** 数据：session_token (来自 cookie) + access_token (来自 JSON 响应)。

#### A.1 session_token

1. 母号已登录的浏览器 → DevTools (F12) → Application → Storage → Cookies → `https://chatgpt.com`
2. 找到 `__Secure-next-auth.session-token`
3. 直接复制 value 列。**注意**：cookie 太长会被自动拆成 `.0` 和 `.1` 两行，**按数字顺序拼起来，不要漏字符**：

```
[  浏览器 cookie 表  ]
    name                                    value
    __Secure-next-auth.session-token.0      eyJhbGciOi...AAA
    __Secure-next-auth.session-token.1      BBB...zzzZZZ

session_token = "eyJhbGciOi...AAABBB...zzzZZZ"   # 顺序拼接
```

#### A.2 access_token （**强烈推荐**）

chatgpt 的 `/backend-api/*` 端点必须带 Bearer access_token。AutoFree 会试着用 session cookie 自动换；但有时换不出来（NextAuth 版本差异、Cloudflare），就报 `401 Access token is missing`。直接粘 access_token 一劳永逸：

1. DevTools → **Network** 面板
2. 刷新当前 chatgpt 页面（Ctrl+R）
3. 在 Network 列表里找 **`session`** 这条请求（路径是 `/api/auth/session`）
4. 点开 → **Response** 标签 → 复制 `accessToken` 字段的整个 value（`eyJ...` 开头，~1500 字符）

#### A.3 粘贴

打开 AutoFree Web → Setup 页 → 母号区：

- **session_token** 框：粘 A.1 的拼接值
- **access_token** 框：粘 A.2 的值
- **account_id** 框：粘 `default_workspace_id` 或任意 admin 请求头里 `chatgpt-account-id` 的 UUID
- 点 **导入并验证**

如果以后 access_token 过期（一般 30 天），用 **单独更新 access_token** 那一栏补一次新值即可，不用重导 session_token。

### B. CLI

```bash
uv run autofree import-token
# session_token (从浏览器 cookie 复制): <粘贴>
```

stdin 模式不显示输入。

> CLI 当前版本不支持 access_token；如要补 access_token,直接调 API:
>
> ```bash
> KEY=$(grep ^AUTOFREE_API_KEY data/.env | cut -d= -f2)
> curl -X POST -H "Authorization: Bearer $KEY" \
>      -H "Content-Type: application/json" \
>      -d '{"access_token":"eyJ..."}' \
>      http://localhost:8788/api/master/set-access-token
> ```

### C. Playwright 邮箱密码登录（实验）

只在你有母号密码并且能解决 Cloudflare 时用：

```python
from autofree.flow import master_playwright_login
master_playwright_login(
    email="master@example.com",
    password="...",
    proxy="http://127.0.0.1:7890",
    headless=False,    # 第一次让浏览器开着,你能看进度
)
```

不推荐：Cloudflare 5s 挑战 + 邮件 OTP 几乎一定要人盯着，CTF 时间紧不如直接抄 cookie。

---

## 输出目录

```
data/
├── .env                            部署时人工写
├── settings.json                   web 改 / PATCH /api/settings 改
├── settings.json.bak               settings.json 解析失败时自动备份
├── admin_state.json                母号信息 (mode 0600)
├── auths/
│   └── <email>.json                每个 free 号一份 (mode 0600)
├── runs/
│   └── <run_id>.json               每个任务一份,含完整 logs / cohort
└── logs/                           预留,目前没用
```

如果想转移配置到另一台机器：拷贝整个 `data/` 即可（除了 `.env` 和 `admin_state.json` 因为含密）。

---

## 高级

### 跨 host 部署

`autofree api --host 0.0.0.0 --port 8788` 后用 nginx 反代:

```nginx
location / {
    proxy_pass http://127.0.0.1:8788;
    proxy_set_header Host $host;
    proxy_http_version 1.1;
}
```

记得 nginx 不要加默认 timeout,任务可能跑很久。

### Docker 部署

```bash
docker compose up -d --build
docker compose logs -f autofree
```

容器把 `./data` 挂进 `/app/data`,所有运行时数据(.env / settings.json / admin_state.json / auths/ / runs/) 都在宿主机可见。

**镜像里都有什么**:

| 内容 | 由谁装 |
|---|---|
| Python 3.12 + uv | base image + curl install |
| autofree 包 + 依赖 (FastAPI / Playwright / requests) | `uv sync` |
| Chromium (~/.cache/ms-playwright/) | `uv run playwright install chromium` ← **不会复用宿主机的!** |
| Chromium 系统 .so | `uv run playwright install-deps chromium` |
| Node.js + npm | apt-get install |
| 前端 build 产物 (web/dist/) | `npm install && npm run build` |
| xvfb (虚拟 X server) | apt-get install,entrypoint 启动时跑 `Xvfb :99` |

容器启动时 `docker-entrypoint.sh` 会自检 5 个关键 module 能否 import,失败就 crash-loop —— 你 `docker compose logs` 能看到原因。

**重 build 时机**:
- 改了 src/ Python 代码 → 重 build (COPY src 在 sync 之后)
- 改了 web/ 前端 → 重 build
- 改了 pyproject.toml → 重 build
- 只改 settings.json / .env → **不用** 重 build,docker compose restart 即可

```bash
# 改完代码后
docker compose down
docker compose build
docker compose up -d
```

**生产建议**:
- shm_size 留 1g 不要改(Chromium 需要)
- 重启策略 `unless-stopped`,母号 token 30 天过期前主动刷新
- 不要把 `data/` chmod 全开,里面有 session_token + access_token

### 直接修 settings.json

不开 web 也能改:

```bash
# 编辑
vim data/settings.json
# 让正在运行的任务读到新值? 不会 —— 任务读的是 *启动那一刻* 的快照。
# 下一次 POST /api/runs 时才生效。
```

settings.json 解析失败时会 rename 成 `.json.bak` 自动重建,不会丢一切。

### 多实例隔离

把环境变量改一下让 `data/` 路径分开:

```bash
# 实例 A
PYTHONPATH=src python3 -m autofree api &
# 实例 B
cd /path/to/another/AutoFree
PYTHONPATH=src python3 -m autofree api --port 8789 &
```

或者 docker-compose 跑两个 stack（自己加 Dockerfile;项目自带的占位）.

### 并发 (实验)

当前 runner.py 是 **完全串行**。如果想并发跑多个浏览器，在不同 `data/` 目录起多个 autofree 实例，每个用不同的母号 + 不同的代理出口 IP，效果≈手动多开。

不要在同一个母号 + 同一个 IP 上并发 — Cloudflare 会拉黑 5 分钟。
