// Tiny fetch wrapper. API key is held in localStorage and sent as Bearer.

const KEY_STORAGE = "autofree.apiKey";

export function getApiKey() {
  return localStorage.getItem(KEY_STORAGE) || "";
}

export function setApiKey(value) {
  if (value) localStorage.setItem(KEY_STORAGE, value);
  else localStorage.removeItem(KEY_STORAGE);
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  const init = { method, headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(path, init);
  let data = null;
  try {
    data = await resp.json();
  } catch (_) {
    data = null;
  }
  if (!resp.ok) {
    const detail = (data && (data.detail || data.error)) || resp.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  health: () => request("GET", "/api/health"),

  settingsGet: () => request("GET", "/api/settings"),
  settingsPatch: (patch) => request("PATCH", "/api/settings", patch),

  mailProbe: (provider) => request("POST", "/api/mail/probe", { provider }),
  cpaProbe: () => request("POST", "/api/cpa/probe"),

  masterState: () => request("GET", "/api/master/state"),
  masterImportToken: (payload) => request("POST", "/api/master/import-token", payload),
  masterSetAccountId: (account_id) => request("POST", "/api/master/set-account-id", { account_id }),
  masterSetAccessToken: (access_token) => request("POST", "/api/master/set-access-token", { access_token }),
  masterClear: () => request("DELETE", "/api/master/state"),
  masterIdentity: () => request("GET", "/api/master/identity"),
  masterDiagnose: () => request("GET", "/api/master/diagnose"),
  masterSetAP: (value) => request("POST", "/api/master/auto-provision", { value }),
  masterMembers: () => request("GET", "/api/master/members"),
  masterKick: (payload) => request("POST", "/api/master/kick", payload),

  runsList: () => request("GET", "/api/runs"),
  runsStart: (payload) => request("POST", "/api/runs", payload),
  runsGet: (id) => request("GET", `/api/runs/${id}`),
  runsCancel: (id) => request("POST", `/api/runs/${id}/cancel`),

  authsList: () => request("GET", "/api/auths"),
  authsGet: (email) => request("GET", `/api/auths/${encodeURIComponent(email)}`),
  authsDelete: (email) => request("DELETE", `/api/auths/${encodeURIComponent(email)}`),
  authPushOne: (email, force) => request("POST", `/api/auths/${encodeURIComponent(email)}/push`, { force: !!force }),
  authPushAll: (payload) => request("POST", "/api/auths/push-all", payload || {}),
};
