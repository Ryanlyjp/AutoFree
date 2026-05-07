<script setup>
import { computed, onMounted, ref } from "vue";
import { api } from "../api.js";

const auths = ref([]);
const error = ref("");
const success = ref("");
const selected = ref(new Set());
const detail = ref(null);
const filter = ref("all"); // all | unpushed | pushed

async function load() {
  error.value = "";
  try {
    const data = await api.authsList();
    auths.value = data.auths || [];
    // Drop selections that no longer exist
    const present = new Set(auths.value.map(a => a.email));
    selected.value = new Set([...selected.value].filter(e => present.has(e)));
  } catch (e) {
    error.value = e.message;
  }
}

const filtered = computed(() => {
  if (filter.value === "unpushed") return auths.value.filter(a => !a.pushed_to_cpa_at);
  if (filter.value === "pushed") return auths.value.filter(a => a.pushed_to_cpa_at);
  return auths.value;
});

function toggle(email) {
  const next = new Set(selected.value);
  if (next.has(email)) next.delete(email); else next.add(email);
  selected.value = next;
}

function toggleAll() {
  if (selected.value.size === filtered.value.length) {
    selected.value = new Set();
  } else {
    selected.value = new Set(filtered.value.map(a => a.email));
  }
}

async function inspect(email) {
  try {
    detail.value = await api.authsGet(email);
  } catch (e) {
    error.value = e.message;
  }
}

async function pushOne(email, force = false) {
  error.value = "";
  try {
    const res = await api.authPushOne(email, force);
    if (res.skipped) {
      success.value = `${email} 已存在于 CPA(reason=${res.reason}),如需覆盖请勾 force`;
    } else if (res.ok) {
      success.value = `${email} 已推送`;
    }
    setTimeout(() => (success.value = ""), 3000);
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

async function pushSelected(force = false) {
  if (!selected.value.size) {
    error.value = "未选择";
    return;
  }
  if (!confirm(`推送 ${selected.value.size} 个 auth.json 到 CPA?`)) return;
  try {
    const res = await api.authPushAll({ emails: [...selected.value], force });
    success.value = `pushed=${res.pushed} skipped=${res.skipped} failed=${res.failed}`;
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

async function pushAllUnpushed() {
  if (!confirm("推送所有尚未推送的 auth.json 到 CPA?")) return;
  try {
    const res = await api.authPushAll({});
    success.value = `pushed=${res.pushed} skipped=${res.skipped} failed=${res.failed}`;
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

async function del(email) {
  if (!confirm(`删除本地 auth.json: ${email}? CPA 上的不会变。`)) return;
  try {
    await api.authsDelete(email);
    if (detail.value?.email === email) detail.value = null;
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

async function deleteSelected() {
  if (!selected.value.size) { error.value = "未选择"; return; }
  if (!confirm(`删除本地 ${selected.value.size} 条 auth.json? CPA 上的不会变。`)) return;
  try {
    const res = await api.authsDeleteBatch({ emails: [...selected.value] });
    success.value = `已删除 ${res.deleted} 条`;
    setTimeout(() => (success.value = ""), 3000);
    selected.value = new Set();
    if (detail.value && [...selected.value].includes(detail.value.email)) detail.value = null;
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

async function deletePushed() {
  const pushed = auths.value.filter(a => a.pushed_to_cpa_at);
  if (!pushed.length) { error.value = "没有已推送的记录"; return; }
  if (!confirm(`删除 ${pushed.length} 条已推送到 CPA 的本地 auth.json? CPA 上的不会变。`)) return;
  try {
    const res = await api.authsDeleteBatch({ pushed_only: true });
    success.value = `已删除 ${res.deleted} 条`;
    setTimeout(() => (success.value = ""), 3000);
    selected.value = new Set();
    if (detail.value?.pushed_to_cpa_at) detail.value = null;
    await load();
  } catch (e) {
    error.value = e.message;
  }
}

onMounted(load);
</script>

<template>
  <div class="grid grid-cols-3 gap-5">
    <section class="card col-span-2 space-y-3">
      <header class="flex items-center gap-3 flex-wrap">
        <h2 class="text-base font-semibold">已生产 free-auth</h2>
        <span class="tag-neutral">{{ auths.length }}</span>
        <span class="ml-auto flex gap-2">
          <select v-model="filter" class="select max-w-[10rem]">
            <option value="all">全部</option>
            <option value="unpushed">未推送</option>
            <option value="pushed">已推送</option>
          </select>
          <button class="btn-secondary" @click="load">刷新</button>
        </span>
      </header>

      <div v-if="error" class="text-rose-600 text-xs">{{ error }}</div>
      <div v-if="success" class="text-emerald-600 text-xs">{{ success }}</div>

      <div class="flex items-center gap-2 text-xs flex-wrap">
        <button class="btn-secondary" @click="toggleAll">
          {{ selected.size === filtered.length && filtered.length ? '取消全选' : '全选' }}
        </button>
        <span class="tag-neutral">已选 {{ selected.size }}</span>
        <button class="btn-primary" :disabled="!selected.size" @click="pushSelected(false)">推送选中 → CPA</button>
        <button class="btn-secondary" :disabled="!selected.size" @click="pushSelected(true)" title="覆盖 CPA 上的同名文件">推送 (force)</button>
        <button class="btn-danger" :disabled="!selected.size" @click="deleteSelected">删除选中</button>
        <span class="flex-1"></span>
        <button class="btn-secondary" @click="pushAllUnpushed">一键推送未推送</button>
        <button class="btn-danger" @click="deletePushed" title="删除所有已推送到 CPA 的本地文件">删除已推送</button>
      </div>

      <div class="border rounded text-xs overflow-x-auto">
        <table class="min-w-full">
          <thead class="bg-slate-50">
            <tr>
              <th class="px-2 py-1 w-6"></th>
              <th class="text-left px-2 py-1">email</th>
              <th class="text-left px-2 py-1">account_id</th>
              <th class="text-left px-2 py-1">expired</th>
              <th class="text-left px-2 py-1">CPA 推送</th>
              <th class="text-left px-2 py-1 w-28"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in filtered" :key="a.email" class="border-t hover:bg-slate-50"
                :class="detail?.email === a.email ? 'bg-indigo-50' : ''">
              <td class="px-2 py-1">
                <input type="checkbox"
                       :checked="selected.has(a.email)"
                       @change="toggle(a.email)" />
              </td>
              <td class="px-2 py-1 font-mono cursor-pointer" @click="inspect(a.email)">{{ a.email }}</td>
              <td class="px-2 py-1 font-mono text-slate-500">{{ a.account_id?.slice(0, 8) || '' }}</td>
              <td class="px-2 py-1 text-slate-500">{{ (a.expired || '').slice(0, 19) }}</td>
              <td class="px-2 py-1">
                <span v-if="a.pushed_to_cpa_at" class="tag-ok" :title="a.pushed_to_cpa_at">已推送</span>
                <span v-else class="tag-warn">未推送</span>
              </td>
              <td class="px-2 py-1">
                <div class="flex items-center gap-1">
                  <button class="btn-secondary text-xs px-2 py-0.5" @click="pushOne(a.email, false)">push</button>
                  <button class="btn-danger text-xs px-1.5 py-0.5 leading-none" title="删除" @click="del(a.email)">
                    <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
                      <path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="!filtered.length"><td colspan="6" class="px-2 py-3 text-center text-slate-400">无</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card col-span-1 space-y-2">
      <h3 class="text-sm font-semibold">auth.json 详情</h3>
      <div v-if="!detail" class="text-xs text-slate-500">点击左侧 email 查看(token 已脱敏)</div>
      <div v-else class="text-xs space-y-1">
        <div class="font-mono"><span class="text-slate-500">email:</span> {{ detail.email }}</div>
        <div><span class="text-slate-500">account_id:</span> <span class="font-mono">{{ detail.account_id }}</span></div>
        <div><span class="text-slate-500">type:</span> {{ detail.type }}</div>
        <div><span class="text-slate-500">expired:</span> {{ detail.expired }}</div>
        <div><span class="text-slate-500">last_refresh:</span> {{ detail.last_refresh }}</div>
        <div><span class="text-slate-500">access_token:</span> <span class="font-mono">{{ detail.access_token }}</span></div>
        <div><span class="text-slate-500">refresh_token:</span> <span class="font-mono">{{ detail.refresh_token }}</span></div>
        <div><span class="text-slate-500">id_token:</span> <span class="font-mono">{{ detail.id_token }}</span></div>
        <div v-if="detail.run_id"><span class="text-slate-500">run:</span> {{ detail.run_id }} round {{ detail.round }}</div>
        <div v-if="detail.pushed_to_cpa_at" class="text-emerald-600">CPA pushed @ {{ detail.pushed_to_cpa_at }}</div>
      </div>
    </section>
  </div>
</template>
