<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({ master: Object });

const form = reactive({
  rounds: 1,
  per_round: 1,
  mail_provider: "",
  register_only: false,
  auto_push_cpa: true,
  kick_mode: "round_end",
});
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
const immediateKickMode = computed(() => form.kick_mode === "after_each_auth");
const autoPushTitle = computed(() =>
  immediateKickMode.value ? "每个账号 kick 后自动推送到 CPA" : "结束后自动推送到 CPA"
);
const autoPushHint = computed(() =>
  immediateKickMode.value
    ? "(只推本次 run 成功 OAuth 的账号；每个账号 kick 后立刻推送，已存在同名文件会跳过，不覆盖)"
    : "(只推本次 run 成功 OAuth 的账号；run 结束后统一推送，已存在同名文件会跳过，不覆盖)"
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
    const payload = {
      rounds: Number(form.rounds),
      per_round: Number(form.per_round),
      register_only: form.register_only,
      auto_push_cpa: form.auto_push_cpa,
      kick_mode: form.kick_mode,
    };
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

async function deleteRun(id) {
  try {
    await api.runsDelete(id);
    if (focused.value?.id === id) focused.value = null;
    await refreshRuns();
  } catch (e) {
    error.value = e.message;
  }
}

async function clearFinished() {
  const done = runs.value.filter(r => ["done", "done_with_errors", "failed", "cancelled"].includes(r.status));
  if (!done.length) { error.value = "没有已完成的记录"; return; }
  if (!confirm(`删除 ${done.length} 条已完成的运行记录?`)) return;
  for (const r of done) await api.runsDelete(r.id).catch(() => {});
  if (focused.value && done.some(r => r.id === focused.value?.id)) focused.value = null;
  await refreshRuns();
}

// ---- kick cohort ----
const kicking = ref(false);
const kickingEmail = ref("");  // email being kicked individually
const kickResult = ref(null);

async function kickAll(id) {
  if (!confirm(`把 run ${id} 里所有未踢账号都踢出 Team?`)) return;
  kicking.value = true;
  kickResult.value = null;
  try {
    const res = await api.runsKickCohort(id, null);
    kickResult.value = res;
    await inspect(id);
  } catch (e) {
    error.value = e.message;
  } finally {
    kicking.value = false;
  }
}

async function kickOne(id, email) {
  kickingEmail.value = email;
  kickResult.value = null;
  try {
    const res = await api.runsKickCohort(id, [email]);
    kickResult.value = res;
    await inspect(id);
  } catch (e) {
    error.value = e.message;
  } finally {
    kickingEmail.value = "";
  }
}

const hasUnkicked = computed(() =>
  (focused.value?.cohort || []).some(m => !m.kicked)
);

const focusedStages = computed(() => {
  const base = ["init", "auto_provision_off", "register", "auto_provision_on"];
  if (focused.value?.params?.register_only) return [...base, "register_only_done", "done"];
  const tail = ["oauth", "kick"];
  if (focused.value?.params?.auto_push_cpa) tail.push("cpa_push");
  tail.push("done");
  return [...base, ...tail];
});

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (focused.value && focused.value.status && !["done", "failed", "cancelled", "done_with_errors"].includes(focused.value.status)) {
      try { focused.value = await api.runsGet(focused.value.id); } catch (e) { /* ignore */ }
    }
    refreshRuns();
  }, 2500);
}

function stageIdx(label) {
  return focusedStages.value.indexOf(label);
}

function runLabel(r) {
  const total = (r.rounds || 1) * (r.per_round || 1);
  if (r.params?.register_only && (r.status === "done" || r.status === "done_with_errors")) {
    const reg = r.summary?.registered ?? 0;
    return `${reg}/${total} 注册`;
  }
  if (r.status === "done" || r.status === "done_with_errors") {
    const ok = r.summary?.ok ?? 0;
    return `${ok}/${total} 成功`;
  }
  return r.status;
}

function runTagClass(r) {
  if (r.status === "done") return "tag-ok";
  if (r.status === "done_with_errors") return "tag-warn";
  if (r.status === "failed") return "tag-err";
  return "tag-neutral";
}

function kickModeLabel(mode) {
  return mode === "after_each_auth" ? "逐账号即时 kick" : "统一收尾 kick";
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

watch(
  () => form.register_only,
  (value) => {
    form.auto_push_cpa = !value;
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

      <div class="grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
        <div class="md:col-span-2 lg:col-span-1 max-w-[7rem]">
          <label class="label">轮数 R</label>
          <input v-model.number="form.rounds" type="number" min="1" max="20" class="input" />
        </div>
        <div class="md:col-span-2 lg:col-span-1 max-w-[7rem]">
          <label class="label">每轮 N</label>
          <input v-model.number="form.per_round" type="number" min="1" :max="cap" class="input" />
        </div>
        <div class="md:col-span-4 lg:col-span-4">
          <label class="label">邮箱后端 (可选覆盖)</label>
          <select v-model="form.mail_provider" class="select">
            <option value="">使用配置</option>
            <option value="tempmail">tempmail</option>
            <option value="cf_temp_email">cf_temp_email</option>
            <option value="maillab">maillab</option>
          </select>
        </div>
        <div class="md:col-span-4 lg:col-span-4">
          <label class="label">踢出方式</label>
          <select v-model="form.kick_mode" class="select">
            <option value="round_end">统一踢出 (现有模式)</option>
            <option value="after_each_auth">逐账号 auth 完成立刻 kick</option>
          </select>
        </div>
        <div class="md:col-span-12 lg:col-span-2">
          <button class="btn-primary w-full" :disabled="!masterReady" @click="start">启动</button>
        </div>
      </div>

      <div class="flex items-center gap-2 text-sm">
        <input id="register-only" v-model="form.register_only" type="checkbox" class="h-4 w-4 rounded border-slate-300" />
        <label for="register-only" class="cursor-pointer select-none">
          仅注册模式 <span class="text-slate-500 text-xs">(只走注册 + AP-on, 跳过 OAuth 和 kick, 可手动 auth)</span>
        </label>
      </div>

      <div class="flex items-center gap-2 text-sm">
        <input
          id="auto-push-cpa"
          v-model="form.auto_push_cpa"
          type="checkbox"
          class="h-4 w-4 rounded border-slate-300"
          :disabled="form.register_only"
        />
        <label for="auto-push-cpa" class="cursor-pointer select-none" :class="form.register_only ? 'text-slate-400' : ''">
          {{ autoPushTitle }}
          <span class="text-slate-500 text-xs">{{ autoPushHint }}</span>
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
        <header class="flex items-center justify-between gap-2">
          <h3 class="text-sm font-semibold">历史</h3>
          <div class="flex gap-1.5 ml-auto">
            <button class="btn-secondary text-xs px-2 py-1" @click="clearFinished" title="删除所有已完成/失败/取消的记录">清除已完成</button>
            <button class="btn-secondary text-xs px-2 py-1" @click="refreshRuns">刷新</button>
          </div>
        </header>
        <ul class="space-y-1 max-h-[60vh] overflow-y-auto pr-1">
          <li
            v-for="r in runs"
            :key="r.id"
            class="text-xs px-2 py-1.5 rounded border group relative"
            :class="focused?.id === r.id ? 'border-indigo-500 bg-indigo-50' : 'border-slate-200 hover:bg-slate-50'"
          >
            <div class="flex justify-between items-center cursor-pointer" @click="inspect(r.id)">
              <span class="font-mono">{{ r.id }}</span>
              <div class="flex items-center gap-1">
                <span :class="runTagClass(r)">{{ runLabel(r) }}</span>
                <button
                  v-if="!['running','pending'].includes(r.status)"
                  class="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-rose-500 leading-none px-0.5 transition-opacity"
                  title="删除此记录"
                  @click.stop="deleteRun(r.id)"
                >×</button>
              </div>
            </div>
            <div class="text-slate-500 mt-0.5 cursor-pointer" @click="inspect(r.id)">
              R{{ r.current_round }}/{{ r.rounds }} · per={{ r.per_round }} · {{ (r.created_at||'').slice(0,19) }}
            </div>
          </li>
          <li v-if="!runs.length" class="text-xs text-slate-400">暂无任务</li>
        </ul>
      </section>

      <!-- focused detail -->
      <section class="card col-span-2 space-y-3">
        <header class="flex items-center gap-3 flex-wrap">
          <h3 class="text-sm font-semibold">详情</h3>
          <span v-if="focused" class="text-xs text-slate-500 font-mono">{{ focused.id }}</span>
          <span v-if="focused" :class="runTagClass(focused)">{{ runLabel(focused) }}</span>
          <div class="ml-auto flex items-center gap-2">
            <button
              v-if="focused && ['running','pending'].includes(focused.status)"
              class="btn-danger text-xs px-2 py-1" @click="cancel(focused.id)"
            >cancel</button>
            <button
              v-if="focused && hasUnkicked && !['running','pending'].includes(focused.status)"
              class="btn-primary text-xs px-2 py-1"
              :disabled="kicking"
              @click="kickAll(focused.id)"
            >{{ kicking ? '踢出中…' : '踢出全部未踢' }}</button>
          </div>
        </header>

        <!-- kick result banner -->
        <div v-if="kickResult" class="text-xs rounded px-3 py-2"
          :class="kickResult.kicked === kickResult.total ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'">
          踢出 {{ kickResult.kicked }}/{{ kickResult.total }}
          <span v-for="r in kickResult.results" :key="r.email" class="ml-2">
            {{ r.email.split('@')[0] }}:{{ r.ok ? '✓' : '✗' + r.reason }}
          </span>
        </div>

        <div v-if="!focused" class="text-xs text-slate-500">点左侧选一条任务查看进度</div>

        <template v-else>
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <span v-if="focused.params?.register_only" class="tag-neutral">模式: 仅注册</span>
            <span v-else class="tag-neutral">模式: 全流程</span>
            <span class="tag-neutral">踢出策略: {{ kickModeLabel(focused.params?.kick_mode) }}</span>
            <span v-if="focused.params?.auto_push_cpa && focused.params?.kick_mode === 'after_each_auth'" class="tag-ok">CPA 自动推送: 逐账号即时</span>
            <span v-else-if="focused.params?.auto_push_cpa" class="tag-ok">CPA 自动推送: run 结束后</span>
            <span v-else class="tag-neutral">CPA 自动推送: 关闭</span>
          </div>

          <!-- stepper -->
          <div class="flex flex-wrap items-center gap-1 text-xs">
            <span
              v-for="(s, i) in focusedStages"
              :key="s"
              class="px-2 py-1 rounded"
              :class="stageIdx(focused.current_stage) >= i ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-500'"
            >{{ s }}</span>
          </div>

          <div class="grid grid-cols-4 gap-3 text-xs">
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
              <div class="text-lg font-bold">{{ focused.summary?.kicked ?? (focused.cohort||[]).filter(m=>m.kicked).length }}</div>
            </div>
            <div class="card p-3 bg-slate-50">
              <div class="text-slate-500">CPA 推送</div>
              <div class="text-lg font-bold">
                <template v-if="focused.params?.auto_push_cpa">
                  {{ focused.summary?.cpa?.pushed ?? 0 }}/{{ focused.summary?.cpa?.attempted ?? 0 }}
                </template>
                <template v-else>关闭</template>
              </div>
              <div v-if="focused.params?.auto_push_cpa" class="mt-1 text-[11px] text-slate-500">
                skip {{ focused.summary?.cpa?.skipped ?? 0 }} · fail {{ focused.summary?.cpa?.failed ?? 0 }}
              </div>
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
                    <th class="text-left px-2 py-1"></th>
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
                    <td class="px-2 py-1">
                      <button
                        v-if="!m.kicked"
                        class="text-indigo-600 hover:text-indigo-800 disabled:opacity-40 text-xs underline"
                        :disabled="kickingEmail === m.email"
                        @click="kickOne(focused.id, m.email)"
                      >{{ kickingEmail === m.email ? '…' : '踢' }}</button>
                    </td>
                  </tr>
                  <tr v-if="!focused.cohort?.length"><td colspan="6" class="px-2 py-2 text-slate-400">尚无成员</td></tr>
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
