# HTTP API 参考

所有 `/api/*` 端点都需要 Bearer 鉴权（除了 `/api/health`）。

```
Authorization: Bearer <AUTOFREE_API_KEY>
```

或 query 形式：`?api_key=<AUTOFREE_API_KEY>`。

`Content-Type: application/json` 用于带 body 的请求。

> Swagger UI: `http://<host>:8788/api/docs`（不需要鉴权也能看 schema，但调用要带 key）.

---

## 公共端点

### `GET /api/health`

```bash
curl http://localhost:8788/api/health
```

```json
{ "ok": true, "version": "0.1.0" }
```

---

## Settings

### `GET /api/settings`

返回当前配置。**敏感字段会被脱敏成 `<set:N>`**（N = 字符长度）。

```bash
curl -H "Authorization: Bearer $KEY" http://localhost:8788/api/settings
```

### `PATCH /api/settings`

深合并补丁。只发要改的字段；脱敏值（`<set:N>`）不要回传 — 服务器会原样存。

```bash
curl -X PATCH -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"proxy":"http://127.0.0.1:7890"}' \
     http://localhost:8788/api/settings
```

切邮箱 provider 并填 tempmail 配置：

```bash
curl -X PATCH -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "mail": {
         "provider": "tempmail",
         "tempmail": {
           "base_url": "http://127.0.0.1:8080",
           "api_key": "tm_...",
           "domain": "@mail.example.com"
         }
       }
     }' \
     http://localhost:8788/api/settings
```

---

## Probes

### `POST /api/mail/probe`

测试当前邮箱后端连通。可以传 `provider` 临时切换不动 settings：

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"provider":"tempmail"}' \
     http://localhost:8788/api/mail/probe
# {"ok":true,"provider":"tempmail","token_hint":"key-abc123"}
```

`ok=false` 时 `error` 字段含原因。

### `POST /api/cpa/probe`

```bash
curl -X POST -H "Authorization: Bearer $KEY" http://localhost:8788/api/cpa/probe
# {"ok":true,"total_files":42,"our_files":3}
```

---

## 母号 Master

### `GET /api/master/state`

```bash
curl -H "Authorization: Bearer $KEY" http://localhost:8788/api/master/state
```

```json
{
  "email": "master@example.com",
  "account_id": "abc-...-...",
  "workspace_name": "My Team",
  "has_session_token": true,
  "session_token_len": 7642,
  "updated_at": "2026-05-02T10:00:00+08:00",
  "proxy": "http://127.0.0.1:7890"
}
```

### `POST /api/master/import-token`

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "session_token": "eyJhb...全文",
       "access_token":  "eyJhb...来自 /api/auth/session 响应的 accessToken (推荐)",
       "account_id":    "abc-...",
       "email":         "master@example.com"
     }' http://localhost:8788/api/master/import-token
```

`account_id` / `email` 可省 — 服务端会从 `/api/auth/session` 推断。

`access_token` **强烈推荐填**：chatgpt 的 `/backend-api/*` 端点必须带 Bearer。不填则程序会试着用 session cookie 自动换；换不出来就会报 "Access token is missing" 401。详见 [troubleshooting.md](troubleshooting.md#母号导入后操作unauthorized---access-token-is-missing-http-401)。

返回字段 `access_token_source` 会标 `user-provided` 或 `from-session`。

### `POST /api/master/set-account-id`

session_token 已导入但 account_id 错的情况下用。

### `POST /api/master/set-access-token`

单独更新 / 清除 access_token,不动 session_token：

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"access_token":"eyJ..."}' \
     http://localhost:8788/api/master/set-access-token
# 清除:
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"access_token":""}' \
     http://localhost:8788/api/master/set-access-token
```

### `GET /api/master/diagnose`

非破坏性自检,打印 session + backend-api 各自的状态。看不懂日志时先调这个：

```json
{
  "session_token_set": true,
  "access_token_set": false,
  "account_id_set": true,
  "session": {
    "ok": true, "status": 200,
    "has_user": false, "has_access_token": false
  },
  "backend_settings": {
    "ok": false, "status": 401,
    "preview": "{\"detail\":{\"message\":\"Unauthorized - Access token is missing\"}}"
  }
}
```

### `DELETE /api/master/state`

清掉所有母号信息。

### `GET /api/master/identity`

读取 master workspace 的 Identity & Access 配置块 + 当前 auto_provision。

```json
{ "ok": true, "auto_provision": false, "identity": { ... } }
```

### `POST /api/master/auto-provision`

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"value":true}' \
     http://localhost:8788/api/master/auto-provision
# {"ok":true,"auto_provision":true}
```

> 调用底层 `POST /backend-api/accounts/{account_id}/settings/auto_provision`。

### `GET /api/master/members`

```json
{
  "ok": true,
  "count": 3,
  "members": [
    { "user_id": "user-AAAA", "email": "master@example.com", "role": "account-owner", "status": "active" }
  ]
}
```

### `POST /api/master/kick`

按 email 或 user_id 之一踢人。

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"email":"foo@bar.com"}' http://localhost:8788/api/master/kick
```

底层 `DELETE /backend-api/accounts/{account_id}/users/{user_id}`。

---

## Runs（核心）

### `POST /api/runs`

启动 R 轮 × N 个号。后台 thread 跑，立刻返回 run record。

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"rounds":2,"per_round":3,"auto_push_cpa":true}' \
     http://localhost:8788/api/runs
```

```json
{
  "id": "abc123def456",
  "status": "pending",
  "created_at": "2026-05-02T10:00:00+08:00",
  "rounds": 2,
  "per_round": 3,
  "params": {
    "mail_provider": "",
    "proxy": "http://127.0.0.1:7890",
    "register_only": false,
    "auto_push_cpa": true
  },
  "current_round": 0,
  "current_stage": "",
  "logs": [],
  "cohort": [],
  "summary": { "ok": 0, "failed": 0 },
  "error": ""
}
```

可选字段：

- `register_only: true` → 只注册，不做 OAuth / kick / CPA 推送
- `auto_push_cpa: true` → run 结束后自动把本次 run 成功 OAuth 的 auth 推到 CPA

`auto_push_cpa=true` 只会推送本次 run 新产出的 auth，不会扫描历史 auth，也不会覆盖 CPA 上已有同名文件；若 `CPA base_url / key` 未配置，启动时直接返回 400。

校验失败 (rounds < 1, per_round > 10, 母号 token 缺失) 返回 400。

### `GET /api/runs`

```bash
curl -H "Authorization: Bearer $KEY" "http://localhost:8788/api/runs?limit=20"
```

返回精简版（不含 logs / cohort 详情）。

### `GET /api/runs/{run_id}`

完整记录。**前端每 2.5 秒 poll 这个**。

```json
{
  "id": "abc123",
  "status": "running",
  "current_round": 1,
  "current_stage": "register",
  "logs": [
    { "ts": "2026-05-02T10:00:01+08:00", "level": "info", "msg": "[Round 1] step 3/6 — 注册 1/3" }
  ],
  "cohort": [
    {
      "round": 1, "email": "abcd@mail.example.com", "password": "...",
      "mailbox_id": "uuid-...", "stage": "registered", "ok": true,
      "kicked": false, "error": ""
    }
  ],
  "summary": { "registered": 1, "oauthed": 0, "kicked": 0, "errors": [], "ok": 0, "failed": 0 }
}
```

`status` 取值：`pending` → `running` → `done` / `done_with_errors` / `failed` / `cancelled`。

`current_stage`：`init` / `auto_provision_off` / `register` / `auto_provision_on` / `oauth` / `kick` / `cpa_push` / `done`。

如果开启 `auto_push_cpa`，完成后 `summary.cpa` 里会额外返回：

```json
{
  "enabled": true,
  "attempted": 3,
  "pushed": 2,
  "skipped": 1,
  "failed": 0
}
```

### `POST /api/runs/{run_id}/cancel`

软取消。当前正在做的账号会跑完，下一步前退出。

```json
{ "ok": true, "running": false }
```

---

## Auths

### `GET /api/auths`

```json
{
  "auths": [
    {
      "email": "abc@mail.example.com",
      "account_id": "personal-...",
      "expired": "2026-05-30T...",
      "last_refresh": "2026-05-02T10:05:00+08:00",
      "type": "codex",
      "pushed_to_cpa_at": "",
      "file": "abc@mail.example.com.json"
    }
  ]
}
```

### `GET /api/auths/{email}`

完整 auth 详情。**access_token / refresh_token / id_token 字段会被脱敏成 `<set:N>`**。如果你需要原始 token，直接读盘 `data/auths/{email}.json`。

### `DELETE /api/auths/{email}`

删本地 auth.json。**不会** 同时从 CPA 删（add-only 设计）。

### `POST /api/auths/{email}/push`

推一个 auth 到 CPA。CPA 上同名文件存在时默认跳过：

```json
{ "ok": false, "skipped": true, "reason": "already_exists", "name": "codex-free-abc@mail.example.com.json" }
```

带 `force=true` 强行覆盖：

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"force":true}' \
     "http://localhost:8788/api/auths/abc@mail.example.com/push"
```

成功后会在本地 auth.json 上写 `pushed_to_cpa_at`，列表上显示 ✓。

### `POST /api/auths/push-all`

```bash
# 推所有未推送的
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" -d '{}' \
     http://localhost:8788/api/auths/push-all

# 指定一组 email
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"emails":["a@x.com","b@x.com"],"force":false}' \
     http://localhost:8788/api/auths/push-all
```

```json
{ "pushed": 2, "skipped": 0, "failed": 0, "total": 2, "results": [...] }
```

---

## 错误格式

```json
{ "detail": "session_token 未导入" }
```

HTTP 状态码：

| 码 | 含义 |
|---|---|
| 400 | 客户端入参不合法 / 母号未配置 |
| 401 | API key 错 |
| 404 | 资源不存在 (auth / run) |
| 502 | 上游 (chatgpt.com / mail / CPA) 报错 |
| 500 | AUTOFREE_API_KEY 没设 / 未捕获异常 |

---

## 简化的 e2e 例子

```bash
KEY="your-api-key"
H='-H Authorization:Bearer\ '$KEY
# 1. 配置
curl -X PATCH $H -H 'Content-Type:application/json' \
  -d '{"proxy":"http://127.0.0.1:7890",
       "mail":{"provider":"tempmail","tempmail":{"base_url":"...","api_key":"...","domain":"@x.com"}},
       "cpa":{"base_url":"http://...","key":"..."}}' \
  http://localhost:8788/api/settings

# 2. 导入母号
curl -X POST $H -H 'Content-Type:application/json' \
  -d "{\"session_token\":\"$(cat /tmp/master_token)\"}" \
  http://localhost:8788/api/master/import-token

# 3. 跑 1 个号
RUN=$(curl -X POST $H -H 'Content-Type:application/json' \
  -d '{"rounds":1,"per_round":1}' http://localhost:8788/api/runs | jq -r .id)

# 4. 等完成
while true; do
  status=$(curl -s $H http://localhost:8788/api/runs/$RUN | jq -r .status)
  echo "status=$status"
  [[ "$status" =~ done|failed|cancelled ]] && break
  sleep 5
done

# 5. 推送
curl -X POST $H -H 'Content-Type:application/json' -d '{}' \
  http://localhost:8788/api/auths/push-all
```
