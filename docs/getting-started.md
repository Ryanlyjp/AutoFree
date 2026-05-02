# 从零开始：跑通一个 free 号

这份文档假设你刚拿到 AutoFree 仓库，还没有任何配置。跟着走完一遍，你会得到 **一个落盘的 `data/auths/xxx@yourdomain.com.json`**，里面是合法的 `plan_type=free` Codex token。

整个过程分 7 步：

1. [前置条件](#1-前置条件)
2. [安装](#2-安装)
3. [建临时邮箱后端](#3-建临时邮箱后端)
4. [设置 OpenAI Verified Domain + Identity & Access](#4-设置-openai-verified-domain--identity--access)
5. [搭一个 CLIProxyAPI（可选，仅做推送）](#5-搭一个-cliproxyapi-可选仅做推送)
6. [启动 AutoFree + 在 Web 配齐](#6-启动-autofree--在-web-配齐)
7. [跑第一个号](#7-跑第一个号)

---

## 1. 前置条件

| 必备 | 说明 |
|---|---|
| Python ≥ 3.10 | Linux 推荐 3.11；macOS / Windows 也可 |
| Node.js ≥ 18 | 用来 build 前端，build 完之后可以卸 |
| 一台能上网的机器 | OpenAI 不能直连国内 IP；准备好 HTTP 代理 (推荐住宅) |
| 一个 OpenAI ChatGPT Team 母号 | 是项目能跑的前提；个人版不行 |
| 一个你拥有 DNS 的域名 | 要做 OpenAI 的 verified domain |

---

## 2. 安装

### 方式 A：uv（推荐）

```bash
git clone <your-fork-or-mirror-url> AutoFree
cd AutoFree
uv sync
uv run playwright install chromium
```

`uv` 没装的话：`curl -LsSf https://astral.sh/uv/install.sh | sh`。

### 方式 B：纯 pip

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

### 验证

```bash
uv run autofree status
# 应该看到 "[settings]" "[admin]" "[auths] total: 0" "[runs] (latest 10)"
```

---

## 3. 建临时邮箱后端

AutoFree 支持三种后端，**任选其一**，对接复杂度差不多。

### 3.A 选 `tempmail` （自托管，推荐 CTF 用）

如果你已经有 `/opt/code-server/project/tempmail` 在跑，直接拿它的 API key + 域名：

```bash
# 在 tempmail 项目里管理员创建一个 API account，得到 api_key
# 然后在 tempmail 后台导入一个 verified domain (例如 mail.example.com)
```

记下：

- `TEMPMAIL_BASE_URL` ← 类似 `http://127.0.0.1:8080`（**不要带 `/api` 后缀**，AutoFree 自己拼）
- `TEMPMAIL_API_KEY` ← tempmail 给的 api_key
- `TEMPMAIL_DOMAIN` ← 例如 `@mail.example.com`（带不带 `@` 都可，AutoFree 会归一化）

### 3.B 选 `cf_temp_email`（dreamhunter2333）

部署：参考 [dreamhunter2333/cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)。是 Cloudflare Workers 实现，搭起来最快。

记下：

- `CLOUDMAIL_BASE_URL` ← `https://your-worker.example.com/api`
- `CLOUDMAIL_PASSWORD` ← admin password (作为 `x-admin-auth` header)
- `CLOUDMAIL_DOMAIN` ← `@your-domain.com`

### 3.C 选 `maillab`（cloud-mail）

部署：参考 [maillab/cloud-mail](https://github.com/maillab/cloud-mail)。

记下：

- `MAILLAB_API_URL` ← `https://your-domain.com/api`
- `MAILLAB_USERNAME` / `MAILLAB_PASSWORD` ← 主账号用密码登录 (拿 JWT)
- `MAILLAB_DOMAIN` ← `@your-domain.com`

> 这三个域名需要做 MX 记录指向你的邮件服务，注册流程才能收到 OpenAI 的 OTP。

---

## 4. 设置 OpenAI Verified Domain + Identity & Access

这一步是 **AutoFree 能成立的物理前提**。AutoFree 不能帮你自动做 — 这是 OpenAI 后台的一次性操作。

### 4.1 验证域名

1. 浏览器登录母号 ChatGPT（`@chatgpt.com`）
2. 右上角头像 → **Settings → Account**
3. 找到 **Verified Domains**，点 **Verify new domain**
4. 输入你 *邮箱后端* 的域名（步骤 3 里的 `*_DOMAIN`）
5. OpenAI 会给一段 `_openai-domain-verification` 的 TXT 记录
6. 在你的 DNS 里加这条 TXT，等几分钟，回到 OpenAI 点 **Check**
7. 状态变 **Verified**

### 4.2 检查 Identity & Access

1. 在 ChatGPT 左下角切到 Workspace（不是 Personal）
2. 顶部菜单 → **Admin** → **Identity & Access**
3. 找到 **Automatic account creation** 开关 — **现在不用打开**，AutoFree 会程序化控制它，但你要确认这个开关 **能用**（有时新建 workspace 这一项不显示，说明你的母号不是 Owner / 没开放该功能）

> 如果这个开关你看不到 / 灰着 / 提示无权限，AutoFree 的 `set_auto_provision` API 会返回 401 / 403，整套流程跑不下去。换一个 owner 母号，或者联系 OpenAI 客服开通。

### 4.3 拿到母号关键信息

后面要导入 AutoFree，**记一下**：

- **母号邮箱**
- **session_token** ← 浏览器 DevTools → Application → Cookies → `https://chatgpt.com` → 找 `__Secure-next-auth.session-token`，复制 value（很长，可能分 `.0` `.1` 两段，复制时按字母顺序拼起来）
- **account_id** ← URL 里 `chatgpt.com/admin/...` 那一段不显式有，但 `chatgpt.com/api/auth/session` 响应里有；或者打开 DevTools Network 面板，随便点一下 admin 页面，看请求头 `chatgpt-account-id` 的值（一个 UUID）

---

## 5. 搭一个 CLIProxyAPI（可选，仅做推送）

如果你只想生产 auth.json，不推 CPA，**这一步可以跳过**。AutoFree 会把 auth 落到 `data/auths/`，你随时手动拷走。

要推 CPA 的话，参考 [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 部署。记下：

- `CPA_BASE_URL` ← 例如 `http://127.0.0.1:8317`
- `CPA_KEY` ← Bearer key

---

## 6. 启动 AutoFree + 在 Web 配齐

### 6.1 写 .env（只需 API Key）

```bash
mkdir -p data
cat > data/.env <<EOF
AUTOFREE_API_KEY=$(openssl rand -hex 16)
EOF
cat data/.env
# 记下这个 key，待会 web 要用
```

### 6.2 build 前端

```bash
cd web
npm install
npm run build
cd ..
# 输出到 src/autofree/web/dist/
```

> 如果你不打算改前端，build 一次就够。后端会从 `src/autofree/web/dist/` 静态发出。

### 6.3 启动后端

```bash
uv run autofree api
# 听到: "AutoFree API listening on http://0.0.0.0:8788"
```

或后台跑：`nohup uv run autofree api > data/logs/api.log 2>&1 &`

### 6.4 浏览器打开 + 填配置

1. 访问 `http://<host>:8788`
2. 输入 6.1 那个 API Key，点 **连接**
3. 进 **Setup** 页：
   - **代理**：填 `http://127.0.0.1:7890`（你的 HTTP 代理），点保存
   - **邮箱后端**：选 provider，填字段，点保存，再点 **测试连通**，应得 ✅
   - **CLIProxyAPI**：填 `base_url` + `key`，点保存，点 **probe**，应得 ✅
   - **母号 session_token**：粘贴 session_token，account_id，email，点 **导入并验证**
   - 点最下面的 **probe identity + members**，应该看到 `auto_provision: false / true` 和成员数

> 任何一个 probe 红色 `FAIL` 都不要往下走，先翻 [troubleshooting.md](troubleshooting.md)。

---

## 7. 跑第一个号

### 7.1 切到 Run 页

- 右上角 master 状态条应该显示 `<email> / <workspace> / acct <8 位前缀>`
- 点 **刷新母号容量** → 显示 `已有成员: X` `auto_provision: false`

### 7.2 启动 1 × 1

- **轮数 R** = `1`
- **每轮 N** = `1`
- **邮箱后端** = 留空（用 Setup 里配的）
- 点 **启动**

### 7.3 实时观察 stepper

```text
init  →  auto_provision_off  →  register  →  auto_provision_on  →  oauth  →  kick  →  done
```

正常情况整个流程 90~180 秒（大头是 OAuth 阶段的 OTP 等待 + Cloudflare 等待）。

### 7.4 检查产物

```bash
ls data/auths/
# 应该看到 xxx@yourdomain.com.json

cat data/auths/xxx@yourdomain.com.json
# {
#   "type": "codex",
#   "email": "xxx@yourdomain.com",
#   "expired": "2026-XX-XX...",
#   "id_token": "eyJ...",
#   "account_id": "<personal workspace id>",
#   "access_token": "eyJ...",
#   "last_refresh": "2026-XX-XX...",
#   "refresh_token": "rt_..."
# }
```

**关键校验**：把 `access_token` 复制到 [jwt.io](https://jwt.io) 看 payload，`https://api.openai.com/auth.chatgpt_plan_type` 应该是 `free`。如果是 `team`，说明 workspace 选择没生效，看 [troubleshooting.md#oauth-拿到-team-而不是-free](troubleshooting.md#oauth-拿到-team-而不是-free)。

### 7.5 切到 Auths 页推送 CPA

- 列表里应该有刚才那行
- 勾选它，点 **推送选中 → CPA**
- 状态条变 `已推送`，CPA `/v0/management/auth-files` 里能看到 `codex-free-xxx@yourdomain.com.json`

### 7.6 跑多号

回 Run 页，把 R/N 调成 `1 × 3` 或 `2 × 2`，启动。流程一样，只是 register / oauth / kick 三段都会重复 N 次。容量上限是 N + 已有成员 ≤ 10。

---

## 跑通后下一步

- 看 [configuration.md](configuration.md) 学怎么改 settings.json / 切换 provider
- 看 [api.md](api.md) 用脚本批量调 API
- 跑得好不好看 [data/runs/](../data/runs/) 里那些 JSON，每个 run 一份完整日志
