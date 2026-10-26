// 统一 API 封装：候选人 / HR 双端 token + SSE 流式
const C_TOKEN = 'campus_c_token'
const A_TOKEN = 'campus_a_token'

export const cand = {
  get token() { return localStorage.getItem(C_TOKEN) || '' },
  set token(v) { v ? localStorage.setItem(C_TOKEN, v) : localStorage.removeItem(C_TOKEN) },
  get logged() { return !!this.token }
}
export const admin = {
  get token() { return sessionStorage.getItem(A_TOKEN) || '' },
  set token(v) { v ? sessionStorage.setItem(A_TOKEN, v) : sessionStorage.removeItem(A_TOKEN) },
  get logged() { return !!this.token }
}

async function request(path, { method = 'GET', body, auth } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (auth) headers.Authorization = 'Bearer ' + auth
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined })
  if (r.status === 401) {
    // 会话失效：清 token 回登录页，避免各页面静默显示空列表
    if (path.startsWith('/api/admin')) { admin.token = '' }
    else { localStorage.removeItem(C_TOKEN) }
    if (!location.pathname.endsWith('/expired')) location.reload()
    throw new Error('未登录或登录已过期')
  }
  let j = {}
  try { j = await r.json() } catch { /* ignore */ }
  if (!r.ok) throw new Error(j.detail || `请求失败(${r.status})`)
  return j
}

export const api = {
  // ---- 候选人端 ----
  requestCode: phone => request('/api/login/request_code', { method: 'POST', body: { phone } }),
  verify: (phone, code) => request('/api/login/verify', { method: 'POST', body: { phone, code } }),
  loginPassword: (phone, password) => request('/api/login/password', { method: 'POST', body: { phone, password } }),
  myPassword: () => request('/api/account/password', { auth: cand.token }),
  saveMyPassword: payload => request('/api/account/password', { method: 'POST', body: payload, auth: cand.token }),
  me: () => request('/api/me', { auth: cand.token }),
  chatHistory: () => request('/api/chat/history', { auth: cand.token }),
  myTickets: () => request('/api/my_tickets', { auth: cand.token }),
  // S5.3 主动推送：站内通知（候选人端）
  notices: (limit = 50) => request(`/api/notices?limit=${limit}`, { auth: cand.token }),
  readNotices: payload => request('/api/notices/read', { method: 'POST', body: payload, auth: cand.token }),
  agentStatus: () => request('/api/agent/status'),
  // SSE 流式问答，onEvent(event, data)，resolve 于 done/fallback 终止帧
  async chatStream(message, onEvent, headers = {}) {
    const r = await fetch('/api/chat/agent/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + cand.token, ...headers },
      body: JSON.stringify({ message })
    })
    if (!r.ok || !r.body) throw new Error('stream unavailable')
    const reader = r.body.getReader(), dec = new TextDecoder()
    let buf = ''
    outer: while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2)
        let ev = 'message', data = ''
        for (const line of frame.split('\n')) {
          if (line.startsWith('event: ')) ev = line.slice(7).trim()
          else if (line.startsWith('data: ')) data = line.slice(6)
        }
        if (!data) continue
        let d; try { d = JSON.parse(data) } catch { continue }
        onEvent(ev, d)
        if (ev === 'done') break outer
      }
    }
  },
  // 兜底：非流式固定管线
  chatFixed: message => request('/api/chat', { method: 'POST', body: { message }, auth: cand.token }),

  // ---- HR 端 ----
  adminLogin: (username, password) => request('/api/admin/login', { method: 'POST', body: { username, password } }),
  adminAccount: () => request('/api/admin/account', { auth: admin.token }),
  updateAdminAccount: payload => request('/api/admin/account', { method: 'POST', body: payload, auth: admin.token }),
  // 超管专属
  adminMe: () => request('/api/admin/me', { auth: admin.token }),
  adminAccounts: () => request('/api/admin/accounts', { auth: admin.token }),
  createAccount: payload => request('/api/admin/accounts', { method: 'POST', body: payload, auth: admin.token }),
  patchAccount: (id, payload) => request(`/api/admin/accounts/${id}`, { method: 'PATCH', body: payload, auth: admin.token }),
  deleteAccount: id => request(`/api/admin/accounts/${id}`, { method: 'DELETE', auth: admin.token }),
  adminCandidates: () => request('/api/admin/candidates', { auth: admin.token }),
  importCandidates: items => request('/api/admin/candidates/import', { method: 'POST', body: { items }, auth: admin.token }),
  updateStatus: (id, status, note) => request(`/api/admin/candidates/${id}/status`, { method: 'PATCH', body: { status, note }, auth: admin.token }),
  faqs: () => request('/api/admin/faqs', { auth: admin.token }),
  createFaq: payload => request('/api/admin/faqs', { method: 'POST', body: payload, auth: admin.token }),
  updateFaq: (id, payload) => request(`/api/admin/faqs/${id}`, { method: 'PUT', body: payload, auth: admin.token }),
  deleteFaq: id => request(`/api/admin/faqs/${id}`, { method: 'DELETE', auth: admin.token }),
  tickets: () => request('/api/admin/tickets', { auth: admin.token }),
  replyTicket: (id, payload) => request(`/api/admin/tickets/${id}/reply`, { method: 'POST', body: payload, auth: admin.token }),
  stats: () => request('/api/admin/stats', { auth: admin.token }),
  agentRuns: () => request('/api/admin/agent_runs', { auth: admin.token }),
  adminSettings: () => request('/api/admin/settings', { auth: admin.token }),
  saveSettings: payload => request('/api/admin/settings', { method: 'POST', body: payload, auth: admin.token }),
  // 文档导入解析（multipart，不走 request 的 JSON 封装）
  async importDoc(file) {
    const fd = new FormData()
    fd.append('file', file)
    const r = await fetch('/api/admin/kb/import_doc', {
      method: 'POST', headers: { Authorization: 'Bearer ' + admin.token }, body: fd
    })
    if (r.status === 401) { admin.token = ''; location.reload(); throw new Error('未登录或登录已过期') }
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.detail || `请求失败(${r.status})`)
    return j
  },
  bulkFaqs: (items, source = 'doc_import') =>
    request('/api/admin/faqs/bulk', { method: 'POST', body: { items, source }, auth: admin.token }),
  // S2.2 入库前预览（含查重结论，不写库）
  kbPreview: payload => request('/api/admin/kb/preview', { method: 'POST', body: payload, auth: admin.token }),
  // S2.2 按来源整体回滚
  deleteFaqsBySource: source =>
    request(`/api/admin/faqs/by_source?source=${encodeURIComponent(source)}`, { method: 'DELETE', auth: admin.token }),
  // S2.3 运维：检查点
  ckptStats: () => request('/api/admin/maintenance/checkpoints', { auth: admin.token }),
  ckptPrune: days => request('/api/admin/maintenance/checkpoints/prune', {
    method: 'POST', body: days == null ? {} : { days }, auth: admin.token
  }),
  // 评测回归（超管）
  evalCases: () => request('/api/admin/eval/cases', { auth: admin.token }),
  // repeats：多数投票次数（默认后端 agent=3 / 其余=1），用于区分"真回归"与"模型抖动"
  evalRun: (mode, limit = 0, repeats = 0) => request('/api/admin/eval/run', {
    method: 'POST', body: { mode, limit, repeats }, auth: admin.token
  }),
  evalRuns: () => request('/api/admin/eval/runs', { auth: admin.token }),
  evalRunDetail: id => request(`/api/admin/eval/runs/${id}`, { auth: admin.token }),
  // 第5期：线上低分样本回流
  lowRuns: (limit = 30, maxRatio = 0.85) =>
    request(`/api/admin/agent_runs/low?limit=${limit}&max_ratio=${maxRatio}`, { auth: admin.token }),
  promoteCase: (runId, overrides = {}) =>
    request('/api/admin/evals/promote', { method: 'POST', body: { run_id: runId, ...overrides }, auth: admin.token }),
  // S5.3 主动推送：推送记录与配置（管理端）
  adminNotices: (limit = 100, kind = '') =>
    request(`/api/admin/notices?limit=${limit}${kind ? '&kind=' + encodeURIComponent(kind) : ''}`, { auth: admin.token }),
  scanNotices: () => request('/api/admin/notices/scan', { method: 'POST', body: {}, auth: admin.token }),
  saveNoticeConfig: payload =>
    request('/api/admin/notices/config', { method: 'POST', body: payload, auth: admin.token })
}

export const STATUSES = [
  '已初筛通过', '测评中', '已约初面', '初面通过', '已约复试', '复试通过',
  '待传面试资料', '资料审核中', '意向沟通完成', '待传体检资料',
  '体检资料审核通过', '已发offer', '网签中', '已应约', '三方已回收', '已归档'
]
