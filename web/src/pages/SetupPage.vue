<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api.js";

const emit = defineEmits(["master-changed"]);
const MASK_RE = /^<set:\d+>$/;

// ---- settings ----------------------------------------------------------------

const settings = ref(null);
const loading = ref(false);
const error = ref("");
const success = ref("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    settings.value = await api.settingsGet();
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}

async function patch(payload) {
  error.value = "";
  success.value = "";
  try {
    settings.value = await api.settingsPatch(payload);
    success.value = "已保存";
    setTimeout(() => (success.value = ""), 2000);
  } catch (e) {
    error.value = e.message;
  }
}

// ---- proxy ----

const proxyForm = reactive({ value: "", master_mode: "follow_proxy" });
const proxyProbe = ref(null);
const easyproxyForm = reactive({
  enabled: false,
  management_url: "http://127.0.0.1:9888",
  password: "",
  proxy_host: "127.0.0.1",
  pool_port: 2323,
  port_min: 24000,
  port_max: 24100,
  cooldown_minutes: 60,
  master_mode: "direct",
});
const easyproxyStatus = ref(null);
const easyproxyReleasing = ref(false);
const standardProxyDisabled = computed(() => !!easyproxyForm.enabled);
const easyproxyPorts = computed(() => easyproxyStatus.value?.ports || []);
const easyproxySummary = computed(() => easyproxyStatus.value?.summary || null);

function loadProxyFromSettings() {
  proxyForm.value = settings.value?.proxy || "";
  proxyForm.master_mode = settings.value?.proxy_master_mode || "follow_proxy";
}
async function saveProxy() {
  await patch({ proxy: proxyForm.value, proxy_master_mode: proxyForm.master_mode });
  loadProxyFromSettings();
}
async function probeProxy() {
  proxyProbe.value = { running: true };
  try {
    proxyProbe.value = await api.proxyProbe();
  } catch (e) {
    proxyProbe.value = { ok: false, error: e.message };
  }
}

function loadEasyProxyFromSettings() {
  const s = settings.value?.easyproxy || {};
  easyproxyForm.enabled = !!s.enabled;
  easyproxyForm.management_url = s.management_url || "http://127.0.0.1:9888";
  easyproxyForm.password = s.password || "";
  easyproxyForm.proxy_host = s.proxy_host || "127.0.0.1";
  easyproxyForm.pool_port = Number(s.pool_port || 2323);
  easyproxyForm.port_min = Number(s.port_min || 24000);
  easyproxyForm.port_max = Number(s.port_max || 24100);
  easyproxyForm.cooldown_minutes = Number(s.cooldown_minutes || 60);
  easyproxyForm.master_mode = s.master_mode || "direct";
}

async function saveEasyProxy() {
  const payload = {
    enabled: easyproxyForm.enabled,
    management_url: easyproxyForm.management_url,
    proxy_host: easyproxyForm.proxy_host,
    pool_port: Number(easyproxyForm.pool_port),
    port_min: Number(easyproxyForm.port_min),
    port_max: Number(easyproxyForm.port_max),
    cooldown_minutes: Number(easyproxyForm.cooldown_minutes),
    master_mode: easyproxyForm.master_mode,
  };
  if (easyproxyForm.password && !MASK_RE.test(easyproxyForm.password)) {
    payload.password = easyproxyForm.password;
  }
  await patch({ easyproxy: payload });
  loadEasyProxyFromSettings();
}

async function probeEasyProxy() {
  easyproxyStatus.value = { running: true };
  try {
    easyproxyStatus.value = await api.easyproxyStatus();
  } catch (e) {
    easyproxyStatus.value = { ok: false, error: e.message };
  }
}

async function releaseEasyProxy(ports = null) {
  easyproxyReleasing.value = true;
  try {
    easyproxyStatus.value = await api.easyproxyRelease({ ports, remote: true });
  } catch (e) {
    error.value = e.message;
  } finally {
    easyproxyReleasing.value = false;
  }
}

// ---- mail ----

const mailForm = reactive({
  provider: "tempmail",
  cf_temp_email: { base_url: "", password: "", domain: "" },
  maillab: { api_url: "", username: "", password: "", domain: "" },
  tempmail: { base_url: "", api_key: "", domain: "" },
});

function loadMailFromSettings() {
  const s = settings.value?.mail || {};
  mailForm.provider = s.provider || "tempmail";
  for (const b of ["cf_temp_email", "maillab", "tempmail"]) {
    if (s[b]) Object.assign(mailForm[b], s[b]);
  }
}

async function saveMail() {
  // Don't echo masked secrets back. If a field equals "<set:N>", drop it.
  const sanitized = JSON.parse(JSON.stringify(mailForm));
  for (const b of ["cf_temp_email", "maillab", "tempmail"]) {
    for (const k of Object.keys(sanitized[b] || {})) {
      const v = sanitized[b][k];
      if (typeof v === "string" && /^<set:\d+>$/.test(v)) delete sanitized[b][k];
    }
  }
  await patch({ mail: sanitized });
}

const mailProbe = ref(null);
async function probeMail() {
  mailProbe.value = { running: true };
  try {
    mailProbe.value = await api.mailProbe(mailForm.provider);
  } catch (e) {
    mailProbe.value = { ok: false, error: e.message };
  }
}

// ---- cpa ----

const cpaForm = reactive({ base_url: "", key: "" });
function loadCpaFromSettings() {
  const s = settings.value?.cpa || {};
  cpaForm.base_url = s.base_url || "";
  cpaForm.key = "";  // never round-trip masked
}

async function saveCpa() {
  const payload = { base_url: cpaForm.base_url };
  if (cpaForm.key && !/^<set:\d+>$/.test(cpaForm.key)) payload.key = cpaForm.key;
  await patch({ cpa: payload });
}

const cpaProbe = ref(null);
async function probeCpa() {
  cpaProbe.value = { running: true };
  try {
    cpaProbe.value = await api.cpaProbe();
  } catch (e) {
    cpaProbe.value = { ok: false, error: e.message };
  }
}

// ---- master ----

const masterState = ref(null);
const tokenForm = reactive({ session_token: "", access_token: "", account_id: "", email: "" });
const accountIdForm = reactive({ account_id: "" });
const accessTokenForm = reactive({ access_token: "" });
const masterProbe = ref(null);
const masterDiag = ref(null);

async function refreshMaster() {
  try {
    masterState.value = await api.masterState();
    accountIdForm.account_id = masterState.value?.account_id || "";
  } catch (e) {
    error.value = e.message;
  }
}

async function importToken() {
  error.value = "";
  if (!tokenForm.session_token) {
    error.value = "session_token 不能为空";
    return;
  }
  try {
    const payload = { session_token: tokenForm.session_token };
    if (tokenForm.access_token) payload.access_token = tokenForm.access_token;
    if (tokenForm.account_id) payload.account_id = tokenForm.account_id;
    if (tokenForm.email) payload.email = tokenForm.email;
    const res = await api.masterImportToken(payload);
    tokenForm.session_token = "";
    tokenForm.access_token = "";
    success.value = `session_token 已导入 (access_token: ${res.access_token_source || '?'})`;
    setTimeout(() => (success.value = ""), 3500);
    await refreshMaster();
    emit("master-changed");
  } catch (e) {
    error.value = e.message;
  }
}

async function setAccountId() {
  error.value = "";
  try {
    await api.masterSetAccountId(accountIdForm.account_id);
    success.value = "account_id 已更新";
    await refreshMaster();
    emit("master-changed");
  } catch (e) {
    error.value = e.message;
  }
}

async function setAccessTokenOnly() {
  error.value = "";
  try {
    await api.masterSetAccessToken(accessTokenForm.access_token);
    accessTokenForm.access_token = "";
    success.value = "access_token 已更新";
    setTimeout(() => (success.value = ""), 2500);
    await refreshMaster();
    emit("master-changed");
  } catch (e) {
    error.value = e.message;
  }
}

async function runDiagnose() {
  masterDiag.value = { running: true };
  try {
    masterDiag.value = await api.masterDiagnose();
  } catch (e) {
    masterDiag.value = { error: e.message };
  }
}

async function clearMaster() {
  if (!confirm("确认清除母号 session_token?")) return;
  try {
    await api.masterClear();
    await refreshMaster();
    emit("master-changed");
  } catch (e) {
    error.value = e.message;
  }
}

async function probeMaster() {
  masterProbe.value = { running: true };
  try {
    const ident = await api.masterIdentity();
    const members = await api.masterMembers();
    masterProbe.value = {
      ok: true,
      auto_provision: ident.auto_provision,
      members_count: members.count,
    };
  } catch (e) {
    masterProbe.value = { ok: false, error: e.message };
  }
}

const masterSummary = computed(() => {
  const s = masterState.value;
  if (!s) return "未知";
  if (!s.has_session_token) return "未导入";
  return `${s.email || "?"} · workspace ${s.workspace_name || "?"} · acct ${s.account_id || "?"}`;
});

onMounted(async () => {
  await load();
  loadProxyFromSettings();
  loadEasyProxyFromSettings();
  loadMailFromSettings();
  loadCpaFromSettings();
  await refreshMaster();
});
</script>

<template>
  <div class="space-y-5">
    <div v-if="error" class="card border-rose-300 bg-rose-50 text-rose-700 text-sm">{{ error }}</div>
    <div v-if="success" class="card border-emerald-300 bg-emerald-50 text-emerald-700 text-sm">{{ success }}</div>

    <!-- proxy -->
    <section class="card space-y-3">
      <header class="flex items-center justify-between">
        <h2 class="text-base font-semibold">代理</h2>
        <span class="text-xs text-slate-500">普通代理模块。easyproxy 启用后此区停用</span>
      </header>
      <div v-if="standardProxyDisabled" class="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
        easyproxy 已启用。当前普通 proxy 仅保留配置，不参与运行，避免和 easyproxy 冲突。
      </div>
      <div>
        <label class="label">Proxy URL</label>
        <input
          v-model="proxyForm.value"
          class="input"
          placeholder="socks5://127.0.0.1:1080"
          :disabled="standardProxyDisabled"
        />
        <p class="mt-2 text-xs text-slate-500">
          格式说明：填写一个完整 URL，不是 <code>IP,PORT,USER,PWD</code>。
          例如 <code>http://127.0.0.1:7890</code>、
          <code>socks5://127.0.0.1:1080</code>、
          <code>socks5://user:pass@127.0.0.1:1080</code>。
        </p>
      </div>
      <div class="max-w-xs">
        <label class="label">Master 请求</label>
        <select v-model="proxyForm.master_mode" class="select" :disabled="standardProxyDisabled">
          <option value="follow_proxy">跟随普通 proxy</option>
          <option value="direct">Master 直连</option>
        </select>
      </div>
      <div class="flex items-center gap-3">
        <button class="btn-primary" :disabled="standardProxyDisabled" @click="saveProxy">保存</button>
        <button class="btn-secondary" :disabled="standardProxyDisabled" @click="probeProxy">测试连通</button>
        <span v-if="proxyProbe?.running" class="text-xs text-slate-500">probing...</span>
        <span v-else-if="proxyProbe?.ok" class="tag-ok">OK · {{ proxyProbe.latency_ms }}ms (HTTP {{ proxyProbe.status }})</span>
        <span v-else-if="proxyProbe" class="tag-err" :title="proxyProbe.error">FAIL</span>
      </div>
      <p v-if="proxyProbe && !proxyProbe.ok && proxyProbe.error" class="text-xs text-rose-600">{{ proxyProbe.error }}</p>
    </section>

    <section class="card space-y-3">
      <header class="flex items-center justify-between">
        <h2 class="text-base font-semibold">easyproxy</h2>
        <span class="text-xs text-slate-500">hybrid 模式下按账号固定一个端口，注册和 OAuth 复用</span>
      </header>
      <div class="flex items-center gap-2 text-sm">
        <input id="easyproxy-enabled" v-model="easyproxyForm.enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300" />
        <label for="easyproxy-enabled" class="cursor-pointer select-none">
          启用 easyproxy 账号端口池
          <span class="text-slate-500 text-xs">(启用后普通 proxy 模块停用)</span>
        </label>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="label">management_url</label>
          <input v-model="easyproxyForm.management_url" class="input" placeholder="http://127.0.0.1:9888" />
        </div>
        <div>
          <label class="label">管理密码</label>
          <input v-model="easyproxyForm.password" class="input" type="password" placeholder="留空表示保持当前值" />
        </div>
        <div>
          <label class="label">本机代理 Host</label>
          <input v-model="easyproxyForm.proxy_host" class="input" placeholder="127.0.0.1" />
        </div>
        <div>
          <label class="label">2323 池端口</label>
          <input v-model.number="easyproxyForm.pool_port" type="number" min="1" max="65535" class="input" />
        </div>
        <div>
          <label class="label">开始端口</label>
          <input v-model.number="easyproxyForm.port_min" type="number" min="1" max="65535" class="input" />
        </div>
        <div>
          <label class="label">结束端口</label>
          <input v-model.number="easyproxyForm.port_max" type="number" min="1" max="65535" class="input" />
        </div>
        <div>
          <label class="label">本地拉黑冷却 (分钟)</label>
          <input v-model.number="easyproxyForm.cooldown_minutes" type="number" min="1" max="1440" class="input" />
        </div>
        <div>
          <label class="label">Master 请求</label>
          <select v-model="easyproxyForm.master_mode" class="select">
            <option value="direct">Master 直连</option>
            <option value="follow_pool">Master 走 2323 池</option>
          </select>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <button class="btn-primary" @click="saveEasyProxy">保存</button>
        <button class="btn-secondary" @click="probeEasyProxy">刷新状态</button>
        <button
          class="btn-secondary"
          :disabled="easyproxyReleasing || !(easyproxySummary?.local_blacklisted > 0)"
          @click="releaseEasyProxy()"
        >{{ easyproxyReleasing ? '释放中…' : '释放全部本地黑名单' }}</button>
        <span v-if="easyproxyStatus?.running" class="text-xs text-slate-500">probing...</span>
        <span v-else-if="easyproxyStatus?.ok" class="tag-ok">
          可用 {{ easyproxySummary?.selectable ?? 0 }} / {{ easyproxySummary?.total ?? 0 }}
        </span>
        <span v-else-if="easyproxyStatus" class="tag-err" :title="easyproxyStatus.error">FAIL</span>
      </div>
      <p class="text-xs text-slate-500">
        说明：这里按管理 API 的真实端口列表筛选 <code>开始端口 ~ 结束端口</code>，不会假设一定从 24000 连续排到末尾。
      </p>
      <p v-if="easyproxyStatus && !easyproxyStatus.ok && easyproxyStatus.error" class="text-xs text-rose-600">
        {{ easyproxyStatus.error }}
      </p>
      <div v-if="easyproxyStatus?.ok" class="space-y-2">
        <div class="flex flex-wrap gap-2 text-xs">
          <span class="tag-neutral">远端可用 {{ easyproxySummary?.remote_available ?? 0 }}</span>
          <span class="tag-neutral">本地黑名单 {{ easyproxySummary?.local_blacklisted ?? 0 }}</span>
          <span class="tag-neutral">Master {{ easyproxyForm.master_mode === 'direct' ? '直连' : '2323 池' }}</span>
        </div>
        <div class="border rounded text-xs max-h-72 overflow-y-auto">
          <table class="min-w-full">
            <thead class="bg-slate-50">
              <tr>
                <th class="text-left px-2 py-1">port</th>
                <th class="text-left px-2 py-1">tag</th>
                <th class="text-left px-2 py-1">状态</th>
                <th class="text-left px-2 py-1">说明</th>
                <th class="text-left px-2 py-1"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="entry in easyproxyPorts" :key="entry.port" class="border-t">
                <td class="px-2 py-1 font-mono">{{ entry.port }}</td>
                <td class="px-2 py-1">{{ entry.tag || entry.name || '-' }}</td>
                <td class="px-2 py-1">
                  <span v-if="entry.selectable" class="tag-ok">可选</span>
                  <span v-else-if="entry.local_blacklisted" class="tag-warn">本地拉黑</span>
                  <span v-else-if="entry.remote_blacklisted" class="tag-warn">远端拉黑</span>
                  <span v-else class="tag-neutral">{{ entry.available ? '占用/不可选' : '不可用' }}</span>
                </td>
                <td class="px-2 py-1 text-slate-500 max-w-sm truncate" :title="entry.local_blacklist_reason || entry.last_error">
                  {{ entry.local_blacklist_reason || entry.last_error || '-' }}
                </td>
                <td class="px-2 py-1">
                  <button
                    v-if="entry.local_blacklisted || entry.remote_blacklisted"
                    class="text-indigo-600 hover:text-indigo-800 text-xs underline"
                    :disabled="easyproxyReleasing"
                    @click="releaseEasyProxy([entry.port])"
                  >释放</button>
                </td>
              </tr>
              <tr v-if="!easyproxyPorts.length">
                <td colspan="5" class="px-2 py-2 text-slate-400">当前范围内没有可展示的 hybrid 端口</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- mail -->
    <section class="card space-y-3">
      <header class="flex items-center justify-between">
        <h2 class="text-base font-semibold">邮箱后端</h2>
      </header>
      <div>
        <label class="label">Provider</label>
        <select v-model="mailForm.provider" class="select max-w-xs">
          <option value="tempmail">tempmail (self-hosted)</option>
          <option value="cf_temp_email">cf_temp_email (dreamhunter2333)</option>
          <option value="maillab">maillab</option>
        </select>
      </div>

      <div v-if="mailForm.provider === 'tempmail'" class="grid grid-cols-3 gap-3">
        <div><label class="label">base_url</label><input v-model="mailForm.tempmail.base_url" class="input" /></div>
        <div><label class="label">api_key</label><input v-model="mailForm.tempmail.api_key" class="input" type="password" /></div>
        <div><label class="label">默认域名</label><input v-model="mailForm.tempmail.domain" class="input" placeholder="@example.com" /></div>
      </div>
      <div v-if="mailForm.provider === 'cf_temp_email'" class="grid grid-cols-3 gap-3">
        <div><label class="label">base_url</label><input v-model="mailForm.cf_temp_email.base_url" class="input" /></div>
        <div><label class="label">x-admin-auth</label><input v-model="mailForm.cf_temp_email.password" class="input" type="password" /></div>
        <div><label class="label">默认域名</label><input v-model="mailForm.cf_temp_email.domain" class="input" /></div>
      </div>
      <div v-if="mailForm.provider === 'maillab'" class="grid grid-cols-2 gap-3">
        <div><label class="label">api_url</label><input v-model="mailForm.maillab.api_url" class="input" /></div>
        <div><label class="label">默认域名</label><input v-model="mailForm.maillab.domain" class="input" /></div>
        <div><label class="label">username</label><input v-model="mailForm.maillab.username" class="input" /></div>
        <div><label class="label">password</label><input v-model="mailForm.maillab.password" class="input" type="password" /></div>
      </div>

      <div class="flex items-center gap-3">
        <button class="btn-primary" @click="saveMail">保存</button>
        <button class="btn-secondary" @click="probeMail">测试连通</button>
        <span v-if="mailProbe?.running" class="text-xs text-slate-500">probing...</span>
        <span v-else-if="mailProbe?.ok" class="tag-ok">OK · {{ mailProbe.provider }}</span>
        <span v-else-if="mailProbe" class="tag-err" :title="mailProbe.error">FAIL</span>
      </div>
      <p v-if="mailProbe && !mailProbe.ok && mailProbe.error" class="text-xs text-rose-600">
        {{ mailProbe.error }}
      </p>
    </section>

    <!-- cpa -->
    <section class="card space-y-3">
      <header><h2 class="text-base font-semibold">CLIProxyAPI</h2></header>
      <div class="grid grid-cols-2 gap-3">
        <div><label class="label">base_url</label><input v-model="cpaForm.base_url" class="input" placeholder="http://127.0.0.1:8317" /></div>
        <div><label class="label">key (Bearer)</label><input v-model="cpaForm.key" class="input" type="password" placeholder="留空表示不更新" /></div>
      </div>
      <div class="flex items-center gap-3">
        <button class="btn-primary" @click="saveCpa">保存</button>
        <button class="btn-secondary" @click="probeCpa">probe</button>
        <span v-if="cpaProbe?.running" class="text-xs text-slate-500">probing...</span>
        <span v-else-if="cpaProbe?.ok" class="tag-ok">OK · 远端 {{ cpaProbe.total_files }} 个文件 (我们: {{ cpaProbe.our_files }})</span>
        <span v-else-if="cpaProbe" class="tag-err" :title="cpaProbe.error">FAIL</span>
      </div>
    </section>

    <!-- master -->
    <section class="card space-y-3">
      <header class="flex items-center justify-between">
        <h2 class="text-base font-semibold">母号 session_token</h2>
        <span class="text-xs text-slate-500">{{ masterSummary }}</span>
      </header>

      <details class="text-xs text-slate-600">
        <summary class="cursor-pointer">如何取 session_token + access_token?</summary>
        <ol class="mt-1 ml-4 list-decimal space-y-1">
          <li>浏览器登录母号 ChatGPT,F12 打开 DevTools</li>
          <li><strong>session_token</strong>: Application → Cookies → chatgpt.com →
            找 <code>__Secure-next-auth.session-token</code>(可能被切成 <code>.0</code> + <code>.1</code> 两段,
            <strong>按数字顺序拼接</strong>),复制完整 value 粘到下面 session_token 框</li>
          <li><strong>access_token</strong>(强烈推荐): Network → 刷新页面 → 找
            <code>/api/auth/session</code> 请求 → Response → 复制 <code>accessToken</code> 字段的值
            (<code>eyJ...</code> 开头),粘到 access_token 框</li>
          <li><strong>account_id</strong>: <code>/api/auth/session</code> 同一响应里的
            <code>user.default_workspace_id</code>; 或 Network 任何 admin 页面请求头
            <code>chatgpt-account-id</code> 的值 (UUID)</li>
        </ol>
        <p class="mt-1 text-amber-700">如果不填 access_token, 程序会用 session_token 去
            <code>/api/auth/session</code> 自己换;若 chatgpt 返回空 session(出现 "Access token is missing" 401)
            就需要补填 access_token。</p>
      </details>

      <div class="grid grid-cols-3 gap-3">
        <div class="col-span-3">
          <label class="label">session_token <span class="text-rose-500">*</span></label>
          <textarea v-model="tokenForm.session_token" class="textarea" rows="3" placeholder="__Secure-next-auth.session-token 的 value (chunked 时按 .0+.1 顺序拼接)"></textarea>
        </div>
        <div class="col-span-3">
          <label class="label">access_token (推荐)</label>
          <textarea v-model="tokenForm.access_token" class="textarea" rows="2" placeholder="从 /api/auth/session 响应里复制的 accessToken (eyJ... 开头)"></textarea>
        </div>
        <div><label class="label">account_id (可选)</label><input v-model="tokenForm.account_id" class="input" /></div>
        <div><label class="label">email (可选)</label><input v-model="tokenForm.email" class="input" /></div>
        <div class="self-end"><button class="btn-primary w-full" @click="importToken">导入并验证</button></div>
      </div>

      <div class="grid grid-cols-3 gap-3 border-t pt-3">
        <div class="col-span-2">
          <label class="label">单独更新 access_token</label>
          <input v-model="accessTokenForm.access_token" class="input" type="password" placeholder="用于已导入 session_token 但 401 时补 Bearer (留空 = 清除)" />
        </div>
        <div class="self-end">
          <button class="btn-primary w-full" @click="setAccessTokenOnly">更新 access_token</button>
        </div>
      </div>

      <div class="grid grid-cols-3 gap-3 border-t pt-3">
        <div class="col-span-2">
          <label class="label">手动覆盖 account_id</label>
          <input v-model="accountIdForm.account_id" class="input" placeholder="覆盖当前 workspace account_id" />
        </div>
        <div class="self-end flex gap-2">
          <button class="btn-secondary flex-1" @click="setAccountId">更新</button>
          <button class="btn-danger flex-1" @click="clearMaster">清除</button>
        </div>
      </div>

      <div class="border-t pt-3 space-y-2">
        <div class="flex items-center gap-3">
          <button class="btn-secondary" @click="runDiagnose">诊断 (检查 session + backend)</button>
          <button class="btn-secondary" @click="probeMaster">probe identity + members</button>
        </div>
        <div v-if="masterDiag" class="text-xs bg-slate-50 rounded p-2 space-y-1 font-mono">
          <div v-if="masterDiag.running">running...</div>
          <template v-else>
            <div>session_token_set: <span :class="masterDiag.session_token_set ? 'text-emerald-600' : 'text-rose-600'">{{ masterDiag.session_token_set }}</span></div>
            <div>access_token_set: <span :class="masterDiag.access_token_set ? 'text-emerald-600' : 'text-amber-600'">{{ masterDiag.access_token_set }}</span></div>
            <div>account_id_set: <span :class="masterDiag.account_id_set ? 'text-emerald-600' : 'text-amber-600'">{{ masterDiag.account_id_set }}</span></div>
            <div v-if="masterDiag.session">
              /api/auth/session: <span :class="masterDiag.session.ok ? 'text-emerald-600' : 'text-rose-600'">{{ masterDiag.session.status || '-' }}</span>
              has_user={{ masterDiag.session.has_user }} has_access_token={{ masterDiag.session.has_access_token }}
              <div v-if="masterDiag.session.preview" class="text-rose-600 break-all">preview: {{ masterDiag.session.preview }}</div>
              <div v-if="masterDiag.session.error" class="text-rose-600">error: {{ masterDiag.session.error }}</div>
            </div>
            <div v-if="masterDiag.backend_settings">
              /backend-api/.../settings: <span :class="masterDiag.backend_settings.ok ? 'text-emerald-600' : 'text-rose-600'">{{ masterDiag.backend_settings.status || '-' }}</span>
              <div v-if="masterDiag.backend_settings.preview" class="text-rose-600 break-all">preview: {{ masterDiag.backend_settings.preview }}</div>
              <div v-if="masterDiag.backend_settings.error" class="text-rose-600">error: {{ masterDiag.backend_settings.error }}</div>
            </div>
          </template>
        </div>
        <span v-if="masterProbe?.running" class="text-xs text-slate-500">probing...</span>
        <template v-else-if="masterProbe?.ok">
          <span class="tag-ok">auto_provision: {{ masterProbe.auto_provision }}</span>
          <span class="tag-neutral">已有成员: {{ masterProbe.members_count }}</span>
        </template>
        <span v-else-if="masterProbe" class="tag-err" :title="masterProbe.error">FAIL: {{ masterProbe.error }}</span>
      </div>
    </section>
  </div>
</template>
