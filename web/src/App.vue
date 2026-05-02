<script setup>
import { computed, onMounted, ref } from "vue";
import { api, getApiKey, setApiKey } from "./api.js";
import SetupPage from "./pages/SetupPage.vue";
import RunPage from "./pages/RunPage.vue";
import AuthsPage from "./pages/AuthsPage.vue";

const tabs = [
  { id: "setup", label: "Setup" },
  { id: "run", label: "Run" },
  { id: "auths", label: "Auths" },
];

const tab = ref("setup");
const ready = ref(false);
const apiKeyInput = ref(getApiKey());
const apiError = ref("");
const masterState = ref(null);

async function handshake() {
  apiError.value = "";
  try {
    setApiKey(apiKeyInput.value.trim());
    await api.health();
    masterState.value = await api.masterState();
    ready.value = true;
  } catch (e) {
    apiError.value = e.message || String(e);
    ready.value = false;
  }
}

async function refreshMaster() {
  try {
    masterState.value = await api.masterState();
  } catch (e) {
    apiError.value = e.message;
  }
}

const masterSummary = computed(() => {
  if (!masterState.value) return "(unknown)";
  const s = masterState.value;
  if (!s.has_session_token) return "未导入 session_token";
  return `${s.email || "?"} / ${s.workspace_name || "?"} / acct ${s.account_id?.slice(0, 8) || "?"}`;
});

onMounted(handshake);
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <header class="border-b border-slate-200 bg-white">
      <div class="max-w-6xl mx-auto px-6 py-3 flex items-center gap-4">
        <h1 class="text-lg font-semibold tracking-tight">AutoFree</h1>
        <nav class="flex gap-1 ml-2">
          <button
            v-for="t in tabs"
            :key="t.id"
            class="px-3 py-1.5 rounded-md text-sm font-medium transition-colors"
            :class="tab === t.id ? 'bg-indigo-600 text-white' : 'text-slate-700 hover:bg-slate-100'"
            @click="tab = t.id"
          >
            {{ t.label }}
          </button>
        </nav>
        <div class="ml-auto flex items-center gap-3 text-xs text-slate-600">
          <span class="tag-neutral" :title="masterSummary">{{ masterSummary }}</span>
          <button class="btn-secondary" @click="refreshMaster">刷新</button>
        </div>
      </div>
    </header>

    <main class="flex-1 max-w-6xl mx-auto w-full px-6 py-6 space-y-6">
      <section v-if="!ready" class="card max-w-xl mx-auto space-y-4">
        <h2 class="text-base font-semibold">输入 API Key</h2>
        <p class="text-xs text-slate-600">
          后端要求 Bearer 鉴权。Key 来自 <code>.env</code> 中的
          <code>AUTOFREE_API_KEY</code>。仅保存在浏览器 localStorage,不上传。
        </p>
        <div>
          <label class="label">API Key</label>
          <input v-model="apiKeyInput" class="input" type="password" autocomplete="off" />
        </div>
        <div v-if="apiError" class="text-rose-600 text-xs">{{ apiError }}</div>
        <button class="btn-primary" @click="handshake">连接</button>
      </section>

      <template v-else>
        <SetupPage v-if="tab === 'setup'" @master-changed="refreshMaster" />
        <RunPage v-else-if="tab === 'run'" :master="masterState" @master-changed="refreshMaster" />
        <AuthsPage v-else-if="tab === 'auths'" />
      </template>
    </main>

    <footer class="text-center text-xs text-slate-400 py-4 border-t border-slate-100">
      AutoFree · CTF build · single-tab serial pipeline
    </footer>
  </div>
</template>
