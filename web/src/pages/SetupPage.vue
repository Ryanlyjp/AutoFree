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
function loadProxyFromSettings() {
  proxyForm.value = settings.value?.proxy || "";
}
async function saveProxy() {
  await patch({ proxy: proxyForm.value });
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
const tokenForm = reactive({ session_token: "", account_id: "", email: "" });
const accountIdForm = reactive({ account_id: "" });
const masterProbe = ref(null);

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
    if (tokenForm.account_id) payload.account_id = tokenForm.account_id;
    if (tokenForm.email) payload.email = tokenForm.email;
    await api.masterImportToken(payload);
    tokenForm.session_token = "";
    success.value = "session_token 已导入";
    setTimeout(() => (success.value = ""), 2500);
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
        <span class="text-xs text-slate-500">所有 Playwright 流量走此代理。留空 = 不使用</span>
      </header>
      <div class="grid grid-cols-3 gap-3">
        <div class="col-span-2">
          <label class="label">HTTP/HTTPS Proxy URL</label>
          <input v-model="proxyForm.value" class="input" placeholder="http://127.0.0.1:7890" />
        </div>
        <div class="self-end">
          <button class="btn-primary w-full" @click="saveProxy">保存</button>
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
        <summary class="cursor-pointer">如何取 session_token?</summary>
        <ol class="mt-1 ml-4 list-decimal space-y-1">
          <li>浏览器登录母号 ChatGPT,DevTools → Application → Cookies → chatgpt.com</li>
          <li>找 <code>__Secure-next-auth.session-token</code>(或 .0/.1 两段),复制 value</li>
          <li>account_id 在 ChatGPT URL <code>/admin/...</code> 或 <code>/api/auth/session</code> 响应里</li>
        </ol>
      </details>

      <div class="grid grid-cols-3 gap-3">
        <div class="col-span-3">
          <label class="label">session_token</label>
          <textarea v-model="tokenForm.session_token" class="textarea" rows="3" placeholder="__Secure-next-auth.session-token 的 value"></textarea>
        </div>
        <div><label class="label">account_id (可选)</label><input v-model="tokenForm.account_id" class="input" /></div>
        <div><label class="label">email (可选)</label><input v-model="tokenForm.email" class="input" /></div>
        <div class="self-end"><button class="btn-primary w-full" @click="importToken">导入并验证</button></div>
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

      <div class="flex items-center gap-3 border-t pt-3">
        <button class="btn-secondary" @click="probeMaster">probe identity + members</button>
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
