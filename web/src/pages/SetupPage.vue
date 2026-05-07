<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api.js";

const emit = defineEmits(["master-changed"]);

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

const proxyForm = reactive({ value: "" });
const proxyProbe = ref(null);
function loadProxyFromSettings() {
  proxyForm.value = settings.value?.proxy || "";
}
async function saveProxy() {
  await patch({ proxy: proxyForm.value });
}
async function probeProxy() {
  proxyProbe.value = { running: true };
  try {
    proxyProbe.value = await api.proxyProbe();
  } catch (e) {
    proxyProbe.value = { ok: false, error: e.message };
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
        <span class="text-xs text-slate-500">浏览器和后端请求都会走此代理。留空 = 不使用</span>
      </header>
      <div>
        <label class="label">Proxy URL</label>
        <input v-model="proxyForm.value" class="input" placeholder="socks5://127.0.0.1:1080" />
        <p class="mt-2 text-xs text-slate-500">
          格式说明：填写一个完整 URL，不是 <code>IP,PORT,USER,PWD</code>。
          例如 <code>http://127.0.0.1:7890</code>、
          <code>socks5://127.0.0.1:1080</code>、
          <code>socks5://user:pass@127.0.0.1:1080</code>。
        </p>
      </div>
      <div class="flex items-center gap-3">
        <button class="btn-primary" @click="saveProxy">保存</button>
        <button class="btn-secondary" @click="probeProxy">测试连通</button>
        <span v-if="proxyProbe?.running" class="text-xs text-slate-500">probing...</span>
        <span v-else-if="proxyProbe?.ok" class="tag-ok">OK · {{ proxyProbe.latency_ms }}ms (HTTP {{ proxyProbe.status }})</span>
        <span v-else-if="proxyProbe" class="tag-err" :title="proxyProbe.error">FAIL</span>
      </div>
      <p v-if="proxyProbe && !proxyProbe.ok && proxyProbe.error" class="text-xs text-rose-600">{{ proxyProbe.error }}</p>
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
