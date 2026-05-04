<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({ master: Object });

const form = reactive({ rounds: 1, per_round: 1, mail_provider: "", register_only: false });
const error = ref("");
const success = ref("");

const memberCount = ref(null);
const apState = ref(null);
const probing = ref(false);

const runs = ref([]);
const focused = ref(null);
const logRef = ref(null);
let pollTimer = null;

const cap = 10;
const totalNew = computed(() => Number(form.rounds) * Number(form.per_round));
const perRoundOK = computed(() =>
  memberCount.value === null ? null : Number(form.per_round) + Number(memberCount.value) <= cap
);

async function probe() {
  probing.value = true;
  error.value = "";
  try {
    const ident = await api.masterIdentity();
    apState.value = ident.auto_provision;
    const members = await api.masterMembers();
    memberCount.value = members.count;
  } catch (e) {
    error.value = e.message;
    apState.value = null;
    memberCount.value = null;
  } finally {
    probing.value = false;
  }
}

async function refreshRuns() {
  try {
    const list = await api.runsList();
    runs.value = list.runs || [];
  } catch (e) { /* swallow */ }
}

async function start() {
  error.value = "";
  success.value = "";
  if (perRoundOK.value === false) {
    error.value = `本轮新增 ${form.per_round} + 已有 ${memberCount.value} > ${cap}`;
    return;
  }
  try {
    const payload = { rounds: Number(form.rounds), per_round: Number(form.per_round), register_only: form.register_only };
    if (form.mail_provider) payload.mail_provider = form.mail_provider;
    const rec = await api.runsStart(payload);
    success.value = `任务已启动 ${rec.id}`;
    setTimeout(() => (success.value = ""), 2500);
    focused.value = rec;
    await refreshRuns();
  } catch (e) {
    error.value = e.message;
  }
}

async function inspect(id) {
  try {
    focused.value = await api.runsGet(id);
  } catch (e) {
    error.value = e.message;
  }
}

async function cancel(id) {
  if (!confirm(`取消 run ${id}?`)) return;
  try {
    await api.runsCancel(id);
    await inspect(id);
    await refreshRuns();
  } catch (e) {
    error.value = e.message;
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (focused.value && focused.value.status && !["done", "failed", "cancelled", "done_with_errors"].includes(focused.value.status)) {
      try { focused.value = await api.runsGet(focused.value.id); } catch (e) { /* ignore */ }
    }
    refreshRuns();
  }, 2500);
}

const stages = ["init", "auto_provision_off", "register", "auto_provision_on", "oauth", "kick", "done", "register_only_done"];
function stageIdx(label) {
  return stages.indexOf(label);
}

const masterReady = computed(() => props.master?.has_session_token && props.master?.account_id);

watch(
  () => focused.value?.logs?.length,
  async () => {
    await nextTick();
    if (logRef.value) {
      logRef.value.scrollTop = logRef.value.scrollHeight;
    }
  }
);

onMounted(async () => {
  await probe();
  await refreshRuns();
  startPolling();
});

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div class="space-y-5">
    <div v-if="!masterReady" class="card border-amber-300 bg-amber-50 text-amber-800 text-sm">
      请先在 Setup 页导入母号 session_token + account_id,再来跑 Run。
    </div>

    <section class="card space-y-3">
      <header class="flex items-center justify-between">
        <h2 class="text-base font-semibold">启动新批次</h2>
        <span class="text-xs text-slate-500">N + 已有成员 ≤ 10</span>
      </header>

      <div class="grid grid-cols-4 gap-3 items-end">
        <div>
          <label class="label">轮数 R</label>
          <input v-model.number="form.rounds" type="number" min="1" max="20" class="input" />
        </div>
        <div>
          <label class="label">每轮 N</label>
          <input v-model.number="form.per_round" type="number" min="1" :max="cap" class="input" />
        </div>
        <div>
          <label class="label">邮箱后端 (可选覆盖)</label>
          <select v-model="form.mail_provider" class="select">
            <option value="">使用配置</option>
            <option value="tempmail">tempmail</option>
            <option value="cf_temp_email">cf_temp_email</option>
            <option value="maillab">maillab</option>
          </select>
        </div>
        <div>
          <button class="btn-primary w-full" :disabled="!masterReady" @click="start">启动</button>
        </div>
      </div>

      <div class="flex items-center gap-2 text-sm">
        <input id="register-only" v-model="form.register_only" type="checkbox" class="h-4 w-4 rounded border-slate-300" />
        <label for="register-only" class="cursor-pointer select-none">
          仅注册模式 <span class="text-slate-500 text-xs">(只走注册 + AP-on, 跳过 OAuth 和 kick, 可手动 auth)</span>
        </label>
      </div>

      <div class="flex items-center gap-3 text-xs">
        <button class="btn-secondary" @click="probe">刷新母号容量</button>
        <span v-if="probing" class="text-slate-500">probing...</span>
        <template v-else>
          <span v-if="memberCount !== null" class="tag-neutral">已有成员: {{ memberCount }}</span>
          <span v-if="apState !== null" class="tag-neutral">auto_provision: {{ apState }}</span>
          <span v-if="totalNew" class="tag-neutral">本次合计新增: {{ totalNew }}</span>
          <span v-if="perRoundOK === false" class="tag-err">超出容量</span>
          <span v-else-if="perRoundOK === true" class="tag-ok">每轮容量 OK</span>
        </template>
      </div>

      <div v-if="error" class="text-rose-600 text-xs">{{ error }}</div>
      <div v-if="success" class="text-emerald-600 text-xs">{{ success }}</div>
    </section>

    <div class="grid grid-cols-3 gap-5">
      <!-- runs list -->
      <section class="card col-span-1 space-y-2">
        <header class="flex items-center justify-between">
          <h3 class="text-sm font-semibold">历史</h3>
          <button class="btn-secondary text-xs px-2 py-1" @click="refreshRuns">刷新</button>
        </header>
        <ul class="space-y-1 max-h-[60vh] overflow-y-auto pr-1">
          <li
            v-for="r in runs"
            :key="r.id"
            class="text-xs px-2 py-1.5 rounded cursor-pointer border"
            :class="focused?.id === r.id ? 'border-indigo-500 bg-indigo-50' : 'border-slate-200 hover:bg-slate-50'"
            @click="inspect(r.id)"
          >
            <div class="flex justify-between">
              <span class="font-mono">{{ r.id }}</span>
              <span class="tag-neutral">{{ r.status }}</span>
            </div>
            <div class="text-slate-500 mt-0.5">
              R{{ r.current_round }}/{{ r.rounds }} · per={{ r.per_round }} · {{ (r.created_at||'').slice(0,19) }}
            </div>
          </li>
          <li v-if="!runs.length" class="text-xs text-slate-400">暂无任务</li>
        </ul>
      </section>

      <!-- focused detail -->
      <section class="card col-span-2 space-y-3">
        <header class="flex items-center gap-3">
          <h3 class="text-sm font-semibold">详情</h3>
          <span v-if="focused" class="text-xs text-slate-500 font-mono">{{ focused.id }}</span>
          <span v-if="focused" class="tag-neutral">{{ focused.status }}</span>
          <button
            v-if="focused && ['running','pending'].includes(focused.status)"
            class="btn-danger ml-auto text-xs px-2 py-1" @click="cancel(focused.id)"
          >cancel</button>
        </header>

        <div v-if="!focused" class="text-xs text-slate-500">点左侧选一条任务查看进度</div>

        <template v-else>
          <!-- stepper -->
          <div class="flex items-center gap-1 text-xs">
            <span
              v-for="(s, i) in stages"
              :key="s"
              class="px-2 py-1 rounded"
              :class="stageIdx(focused.current_stage) >= i ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-500'"
            >{{ s }}</span>
          </div>

          <div class="grid grid-cols-3 gap-3 text-xs">
            <div class="card p-3 bg-slate-50">
              <div class="text-slate-500">注册成功</div>
              <div class="text-lg font-bold">{{ focused.summary?.registered ?? 0 }}</div>
            </div>
            <div class="card p-3 bg-slate-50">
              <div class="text-slate-500">拿 token</div>
              <div class="text-lg font-bold">{{ focused.summary?.oauthed ?? 0 }}</div>
            </div>
            <div class="card p-3 bg-slate-50">
              <div class="text-slate-500">已踢出</div>
              <div class="text-lg font-bold">{{ focused.summary?.kicked ?? 0 }}</div>
            </div>
          </div>

          <div>
            <h4 class="text-xs font-semibold text-slate-600 mb-1">cohort</h4>
            <div class="border rounded text-xs">
              <table class="min-w-full">
                <thead class="bg-slate-50">
                  <tr>
                    <th class="text-left px-2 py-1">round</th>
                    <th class="text-left px-2 py-1">email</th>
                    <th class="text-left px-2 py-1">stage</th>
                    <th class="text-left px-2 py-1">kicked</th>
                    <th class="text-left px-2 py-1">error</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="m in focused.cohort" :key="m.email" class="border-t">
                    <td class="px-2 py-1">{{ m.round }}</td>
                    <td class="px-2 py-1 font-mono">{{ m.email }}</td>
                    <td class="px-2 py-1">
                      <span class="tag-neutral">{{ m.stage || '-' }}</span>
                    </td>
                    <td class="px-2 py-1">{{ m.kicked ? '✓' : '' }}</td>
                    <td class="px-2 py-1 text-rose-600 max-w-xs truncate" :title="m.error">{{ m.error }}</td>
                  </tr>
                  <tr v-if="!focused.cohort?.length"><td colspan="5" class="px-2 py-2 text-slate-400">尚无成员</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <div>
            <h4 class="text-xs font-semibold text-slate-600 mb-1">日志</h4>
            <pre ref="logRef" class="bg-slate-900 text-slate-100 text-[11px] rounded p-2 max-h-64 overflow-y-auto whitespace-pre-wrap"
            >{{ (focused.logs || []).map(l => `[${l.ts.slice(11,19)}] ${l.level.padEnd(5)} ${l.msg}`).join('\n') }}</pre>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>
