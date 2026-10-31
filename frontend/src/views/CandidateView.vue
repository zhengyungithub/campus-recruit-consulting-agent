<template>
  <div class="page">
    <!-- 顶栏 -->
    <header class="topbar">
      <div class="tb-left">
        <span class="logo">校</span>
        <b>AI 问答</b>
        <span class="sub">校招咨询助手</span>
      </div>
      <div class="tb-right">
        <!-- S5.3 主动推送：进度变更 / 待办停滞会主动推站内通知，未读数在铃铛上 -->
        <el-popover v-if="logged" placement="bottom-end" :width="360" trigger="click" @show="loadNotices">
          <template #reference>
            <div class="bell-wrap" title="通知">
              <el-icon class="bell"><Bell /></el-icon>
              <span v-if="unread" class="bell-dot">{{ unread > 99 ? '99+' : unread }}</span>
            </div>
          </template>
          <div class="np">
            <div class="np-head">
              <b>通知</b>
              <el-button v-if="unread" link type="primary" size="small" @click="readAll">全部已读</el-button>
            </div>
            <div v-if="!notices.length" class="np-empty">暂无通知</div>
            <div v-for="n in notices" :key="n.id" class="np-item" :class="{ unread: !n.read_at }"
                 @click="readOne(n)">
              <div class="np-title">{{ n.title }}</div>
              <div class="np-body">{{ n.body }}</div>
              <div class="np-time">
                {{ fmtTime(n.created_at) }}
                <el-tag v-if="!n.read_at" size="small" type="danger" effect="plain">未读</el-tag>
              </div>
            </div>
          </div>
        </el-popover>
        <el-button link @click="drawerProg = true">我的进度</el-button>
        <el-button link @click="drawerTk = true">我的反馈</el-button>
        <el-button v-if="logged" link @click="openAcct">账号设置</el-button>
        <el-divider direction="vertical" />
        <div v-if="logged" class="user">
          <span class="avatar">{{ profile?.name?.slice(0,1) || '客' }}</span>
          <span class="uname">{{ profile?.name || '未登录' }}</span>
          <el-tag size="small" type="warning" effect="plain">候选人</el-tag>
          <el-button link type="danger" @click="logout" style="margin-left:6px">退出</el-button>
        </div>
      </div>
    </header>

    <!-- 登录：左品牌展示 + 右表单的分栏布局（借鉴 GitHub 上 art-design-pro / soybean-admin 等登录页风格，沿用项目青绿色调） -->
    <div v-if="!logged" class="login-wrap">
      <!-- 左侧品牌区：窄屏自动隐藏 -->
      <aside class="brand-pane">
        <div class="brand-deco d1"></div>
        <div class="brand-deco d2"></div>
        <div class="brand-deco d3"></div>
        <div class="brand-inner">
          <div class="brand-logo-row">
            <span class="brand-logo">校</span>
            <div class="brand-name">
              <b>校招 AI 问答</b>
              <span>智能校招咨询助手</span>
            </div>
          </div>
          <h1 class="brand-slogan">让校招沟通<em>更简单</em></h1>
          <p class="brand-sub">基于 AI Agent 的校招全流程咨询：知识库有据可查，进度实时同步，答不了的问题自动转人工。</p>
          <ul class="brand-feats">
            <li v-for="f in brandFeats" :key="f.t">
              <span class="fi">{{ f.i }}</span>
              <div><b>{{ f.t }}</b><p>{{ f.d }}</p></div>
            </li>
          </ul>
          <div class="brand-demo">
            <div class="bd-bubble user">体检是什么时候？</div>
            <div class="bd-bubble bot">体检预计在 10 月 30 日上午进行，请携带身份证空腹到场。<span class="bd-verify">🛡 已逐句核验</span></div>
          </div>
        </div>
      </aside>
      <!-- 右侧表单区 -->
      <main class="form-pane">
        <div class="login-card">
          <div class="lc-head">
            <span class="lc-logo">校</span>
            <div><b>校招 AI 问答</b><p>campus.zhengyun.online</p></div>
          </div>
          <h3>欢迎回来 👋</h3>
          <p class="tip">请使用校招报名时预留的手机号登录</p>
          <div class="login-mode">
            <el-radio-group v-model="loginMode" size="small">
              <el-radio-button value="code">验证码登录</el-radio-button>
              <el-radio-button value="pwd">密码登录</el-radio-button>
            </el-radio-group>
          </div>
          <el-form @submit.prevent>
            <el-form-item>
              <el-input v-model="phone" maxlength="11" placeholder="手机号" size="large" clearable>
                <template #prefix><el-icon><Iphone /></el-icon></template>
              </el-input>
            </el-form-item>
            <el-form-item v-if="loginMode === 'code'">
              <div class="code-row">
                <el-input v-model="code" maxlength="6" placeholder="验证码" size="large" @keydown.enter="doLogin">
                  <template #prefix><el-icon><Key /></el-icon></template>
                </el-input>
                <el-button size="large" class="cd-btn" :disabled="cd > 0" @click="requestCode">
                  {{ cd > 0 ? cd + 's 后重发' : '获取验证码' }}
                </el-button>
              </div>
            </el-form-item>
            <el-form-item v-else>
              <el-input v-model="pwdLogin" type="password" show-password size="large"
                        placeholder="登录密码" @keydown.enter="doLogin">
                <template #prefix><el-icon><Lock /></el-icon></template>
              </el-input>
            </el-form-item>
            <el-button type="primary" size="large" class="login-btn" @click="doLogin">登 录</el-button>
          </el-form>
          <el-alert v-if="loginMsg" :title="loginMsg" type="warning" :closable="false" class="login-alert" />
          <div class="login-hint">
            <el-icon><InfoFilled /></el-icon>
            <span>{{ loginMode === 'code' ? '演示环境：验证码直接回显，无需真实短信' : '首次使用请先用验证码登录，再在【账号设置】中设置密码' }}</span>
          </div>
        </div>
        <footer class="login-footer">© 2026 校招 AI 问答 · 让每一位候选人不再迷茫</footer>
      </main>
    </div>

    <!-- 对话页 -->
    <div v-else class="chat-page">
      <div class="chat-panel">
        <!-- 工具栏 -->
        <div class="toolbar">
          <el-checkbox v-model="delMode">删除对话</el-checkbox>
          <el-button v-if="delMode" size="small" type="danger" plain @click="clearChat">清空当前对话</el-button>
          <div class="tb-spacer"></div>
          <span class="agent-label">Agent 过程</span>
          <el-switch v-model="showProc" size="small" />
          <el-tooltip content="开启后显示 Agent 的思考与工具调用过程（检索知识库/查询进度等）" placement="bottom">
            <el-icon class="info"><InfoFilled /></el-icon>
          </el-tooltip>
        </div>

        <!-- Agent 过程面板 -->
        <div v-if="showProc" class="proc-panel">
          <template v-if="procStages.length || procSteps.length">
            <div class="stages">
              <el-tag v-for="(s,i) in procStages" :key="'s'+i" size="small" effect="plain">{{ s }}</el-tag>
            </div>
            <div v-for="(st,i) in procSteps" :key="'t'+i" class="step">· {{ st }}</div>
          </template>
          <div v-else class="tip" style="padding:6px 0">发起提问后，这里将展示 Agent 的执行过程</div>
        </div>

        <!-- 消息区 -->
        <div ref="chatBox" class="msgs">
          <div v-if="!messages.length" class="empty">
            <el-icon class="empty-icon"><ChatDotRound /></el-icon>
            <p>输入问题开始对话，我会基于知识库回答</p>
            <div class="quick">
              <el-button v-for="q in quickQs" :key="q" size="small" round plain @click="quick(q)">{{ q }}</el-button>
            </div>
          </div>
          <template v-else>
            <div v-for="(m,i) in messages" :key="i" class="msg-row" :class="m.role">
              <div class="msg" :class="m.role">
                <span v-html="m.html"></span>
                <div v-if="m.cites?.length" class="cites">📎 依据：
                  <el-tag v-for="(c,j) in m.cites" :key="j" size="small" type="info" style="margin:2px 4px 0 0">
                    {{ c.question }}</el-tag>
                </div>
                <div v-if="m.verify" class="verify-row">
                  <el-tag :type="verifyType(m.verify.status)" size="small">
                    {{ verifyLabel(m.verify) }}</el-tag>
                  <el-link v-if="anchorsOf(m).length" type="primary" :underline="false"
                           class="anchor-toggle" @click="m.showAnchors = !m.showAnchors">
                    {{ m.showAnchors ? '收起' : '逐句依据' }} ({{ anchorsOf(m).length }})
                  </el-link>
                </div>
                <!-- 第5期：句子级引用锚点，逐句展示落点，让核验结果可解释 -->
                <div v-if="m.showAnchors && anchorsOf(m).length" class="anchors">
                  <div v-for="(a,j) in anchorsOf(m)" :key="j" class="anchor-item"
                       :class="a.supported ? 'ok' : 'warn'">
                    <div class="anchor-sent">
                      <span class="anchor-ico">{{ a.supported ? '✅' : '⚠️' }}</span>{{ a.sentence }}
                    </div>
                    <div class="anchor-ev">
                      依据{{ a.supported ? '' : '（未找到明确落点）' }}：{{ a.evidence || '—' }}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- 输入区 -->
        <div class="input-area">
          <el-input
            v-model="draft" type="textarea" :rows="2" resize="none"
            placeholder="输入你的问题，按 Enter 发送，Shift+Enter 换行"
            :disabled="streaming"
            @keydown.enter.exact.prevent="send" />
          <el-button type="primary" class="send-btn" :loading="streaming" @click="send">
            <el-icon v-if="!streaming" style="margin-right:4px"><Promotion /></el-icon>
            发送
          </el-button>
        </div>
      </div>
    </div>

    <!-- 我的进度抽屉 -->
    <el-drawer v-model="drawerProg" title="我的进度" size="380px">
      <div v-if="profile">
        <el-descriptions :column="1" border size="small" style="margin-bottom:16px">
          <el-descriptions-item label="姓名">{{ profile.name }}</el-descriptions-item>
          <el-descriptions-item label="院校专业">{{ profile.school }} {{ profile.major }}</el-descriptions-item>
          <el-descriptions-item label="岗位/站点">{{ profile.position }} · {{ profile.site }}</el-descriptions-item>
          <el-descriptions-item label="当前环节"><el-tag type="primary">{{ profile.status }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="下一步">{{ profile.next_step }}</el-descriptions-item>
        </el-descriptions>
        <el-steps direction="vertical" :active="activeIdx">
          <el-step v-for="t in timeline" :key="t.status"
                   :status="t.done ? 'success' : t.current ? 'process' : 'wait'">
            <template #title>
              <span :class="{ 'step-skipped': t.skipped }">{{ t.status }}</span>
              <span v-if="t.rework" class="step-flag warn" title="这个环节返过工">↩ 返工</span>
              <span v-if="t.skipped" class="step-flag muted" title="跳步或线下补录，未经过此环节">跳过</span>
            </template>
            <template #description>
              <span v-if="t.current">{{ profile.next_step }}</span>
              <span v-else-if="t.skipped" class="muted">未经过此环节</span>
            </template>
          </el-step>
        </el-steps>

        <el-divider content-position="left">实际轨迹</el-divider>
        <el-timeline v-if="hasHistory">
          <el-timeline-item v-for="(h, i) in profile.history" :key="i"
                            :timestamp="fmtTime(h.created_at)" placement="top">
            {{ h.status }}
            <div v-if="h.reason" class="muted">原因：{{ h.reason }}</div>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else description="暂无流转记录（批量导入时会直接落在当前环节）" :image-size="60" />
      </div>
    </el-drawer>

    <!-- 我的反馈抽屉 -->
    <el-drawer v-model="drawerTk" title="我的反馈" size="380px">
      <el-empty v-if="!tickets.length" description="暂无反馈记录。机器人无法回答的问题会自动生成工单，HR 回复后在这里显示" />
      <el-card v-for="t in tickets" :key="t.id" shadow="never" style="margin-bottom:12px">
        <div class="fb-q">#{{ t.id }} {{ t.question }}
          <el-tag :type="t.status==='pending'?'warning':'success'" size="small" style="margin-left:6px">
            {{ t.status==='pending'?'待人工处理':'已回复' }}</el-tag>
        </div>
        <div class="tip" style="margin-top:4px">提交于 {{ t.created_at }}</div>
        <el-alert v-if="t.answer" :title="t.answer" type="success" :closable="false" style="margin-top:8px;white-space:pre-wrap" />
      </el-card>
    </el-drawer>

    <!-- 账号设置抽屉 -->
    <el-drawer v-model="drawerAcct" title="账号设置" size="380px">
      <el-descriptions :column="1" border size="small" style="margin-bottom:16px">
        <el-descriptions-item label="姓名">{{ profile?.name || '-' }}</el-descriptions-item>
        <el-descriptions-item label="手机号">{{ profile?.phone || '-' }}</el-descriptions-item>
        <el-descriptions-item label="登录方式">
          {{ hasPwd ? '手机号 + 密码 / 验证码' : '验证码（尚未设置密码）' }}
        </el-descriptions-item>
      </el-descriptions>
      <el-form label-width="80px" @submit.prevent>
        <el-form-item v-if="hasPwd" label="原密码">
          <el-input v-model="acctForm.old_password" type="password" show-password placeholder="当前登录密码" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="acctForm.new_password" type="password" show-password placeholder="6-64 位" />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input v-model="acctForm.confirm" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
        <el-button type="primary" style="width:100%" @click="savePwd">
          {{ hasPwd ? '修改密码' : '设置密码' }}
        </el-button>
      </el-form>
      <p class="tip" style="margin-top:12px">
        设置密码后，下次可用「手机号 + 密码」登录，无需再获取短信验证码。
      </p>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, nextTick, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Promotion, InfoFilled, Bell, Iphone, Lock, Key } from '@element-plus/icons-vue'
import { api, cand } from '../api'

const logged = ref(cand.logged)
const phone = ref(''), code = ref(''), loginMsg = ref('')
const loginMode = ref('code'), pwdLogin = ref('')
// 登录页左侧品牌区特性亮点（登录后才进入对话页，这里静态展示即可）
const brandFeats = [
  { i: '🤖', t: 'Agent 智能问答', d: '规划 · 检索 · 核验，复杂问题自动拆解' },
  { i: '📊', t: '进度实时同步', d: '每个环节状态变化，第一时间知晓' },
  { i: '🛡', t: '答案逐句核验', d: '每句话都有知识库依据，拒绝 AI 编造' },
  { i: '💬', t: '无答自动转人工', d: '答不了的问题生成工单，HR 跟进回复' },
]
// 验证码 60s 倒计时：防止连点刷接口
const cd = ref(0)
let cdTimer = null
const drawerAcct = ref(false), hasPwd = ref(false)
const acctForm = ref({ old_password: '', new_password: '', confirm: '' })
const messages = ref([])
const draft = ref('')
const streaming = ref(false)
const tickets = ref([])
const profile = ref(null)
const agentMode = ref('off')
const chatBox = ref(null)
const procStages = ref([]), procSteps = ref([])
const showProc = ref(false), delMode = ref(false)
const drawerProg = ref(false), drawerTk = ref(false)
// S5.3 站内通知：铃铛未读数每 60 秒轮询一次，展开面板时即时拉列表
const notices = ref([]), unread = ref(0)
let noticeTimer = null
// 通知长连接：SSE 收到事件即刷新未读数；连不上时回退到轮询（见 startNoticeStream）
let noticeEs = null
let noticeFallback = null
const fmtTime = t => (t ? String(t).replace('T', ' ').slice(0, 16) : '')
async function loadNotices() {
  if (!logged.value) return
  try {
    const j = await api.notices(30)
    notices.value = j.notices || []
    unread.value = j.unread || 0
  } catch { /* 通知拉取失败不该打断对话，静默即可 */ }
}
async function readOne(n) {
  if (n.read_at) return
  try { await api.readNotices({ ids: [n.id] }) } catch { /* ignore */ }
  await loadNotices()
}
async function readAll() {
  try { await api.readNotices({ all: true }) } catch { /* ignore */ }
  await loadNotices()
}
const quickQs =['校招流程是怎样的？', '我现在到哪一步了？', '面试资料有什么要求？', '网签怎么操作？']

const timeline = computed(() => profile.value?.timeline || [])
// 注意字段名是后端返回的 current（不是 cur）—— 写错会导致当前环节不高亮
const activeIdx = computed(() => timeline.value.findIndex(t => t.current))
const hasHistory = computed(() => (profile.value?.history || []).length > 0)

const esc = s => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML }
const scroll = () => nextTick(() => { if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight })

// 第2期：block=合规红线拦截，danger-red 显示；judge=模型裁判裁定
const VLABEL = { pass: '✅ 核验通过', partial: '⚠️ 部分支撑', fail: '❌ 未经核验', no_source: 'ℹ️ 无引用来源', block: '🛡 合规红线拦截' }
const REDLINE = { promise_offer: '承诺录用', invent_schedule: '无据排期', privacy: '个人信息', internal_leak: '内部细节' }
const VTYPE = { pass: 'success', partial: 'warning', fail: 'danger', no_source: 'info', block: 'danger' }
const verifyLabel = v => {
  const base = (VLABEL[v.status] || v.status) + ' ' + ((v.ratio || 0) * 100).toFixed(0) + '%'
  const rl = (v.redlines || []).map(c => REDLINE[c] || c)
  const jd = v.judge && (v.judge.verdict || v.judge) !== 'skip' ? ' · 裁判判定：' + (v.judge.verdict || v.judge) : ''
  return base + (rl.length ? ' · 已拦截：' + rl.join('/') : '') + jd
}
const verifyType = s => VTYPE[s] || 'info'

// 第5期：句子级锚点列表。后端已在 verify.sentences 里算好，这里只负责展示。
// 只展示"有落点信息"的句子——双方都为 0 条时不显示入口，避免噪声。
function anchorsOf(m) {
  const all = m?.verify?.sentences || []
  return all.filter(a => a && a.sentence && (a.supported || a.evidence))
}

function push(role, html, extra = {}) {
  messages.value.push({ role, html, ...extra })
  scroll()
  // 返回响应式代理：流式更新 bot.html 依赖它触发重渲染，缺失会导致空气泡
  return messages.value[messages.value.length - 1]
}
async function clearChat() {
  // 真删除：先落库（messages 表 + Qdrant 向量记忆），再清前端数组。
  // 以前只清内存 → 刷新后对话原样回来，而记录仍在被 Agent 用于指代还原和向量召回，
  // 等于一个声称删除却不删除的隐私按钮。
  try {
    const r = await api.deleteChatHistory()
    messages.value = []; procStages.value = []; procSteps.value = []; delMode.value = false
    ElMessage.success(`对话已删除（${r.deleted} 条消息 / ${r.purged} 条记忆），刷新后不会恢复`)
  } catch (e) {
    ElMessage.error('删除失败：' + e.message)
  }
}
function quick(q) { draft.value = q; send() }

async function requestCode() {
  if (cd.value > 0) return
  const p = phone.value.trim()
  if (!/^1\d{10}$/.test(p)) { loginMsg.value = ''; ElMessage.warning('请输入 11 位手机号'); return }
  try {
    const j = await api.requestCode(p)
    code.value = j.demo_code
    loginMsg.value = `演示验证码：${j.demo_code}（生产环境将短信下发）`
    cd.value = 60
    clearInterval(cdTimer)
    cdTimer = setInterval(() => { if (--cd.value <= 0) clearInterval(cdTimer) }, 1000)
  } catch (e) { loginMsg.value = e.message; ElMessage.error(e.message) }
}
async function doLogin() {
  try {
    const j = loginMode.value === 'pwd'
      ? await api.loginPassword(phone.value.trim(), pwdLogin.value)
      : await api.verify(phone.value.trim(), code.value.trim())
    cand.token = j.token
    logged.value = true
    await enter()
  } catch (e) { ElMessage.error(e.message) }
}
async function openAcct() {
  drawerAcct.value = true
  acctForm.value = { old_password: '', new_password: '', confirm: '' }
  try { hasPwd.value = (await api.myPassword()).has_password } catch { /* ignore */ }
}
async function savePwd() {
  const f = acctForm.value
  if (!f.new_password || f.new_password !== f.confirm) { ElMessage.error('两次输入的新密码不一致'); return }
  try {
    await api.saveMyPassword({ old_password: f.old_password, new_password: f.new_password })
    ElMessage.success(hasPwd.value ? '密码已修改' : '密码已设置，下次可直接用密码登录')
    hasPwd.value = true
    drawerAcct.value = false
  } catch (e) { ElMessage.error(e.message) }
}
function logout() {
  cand.token = ''; logged.value = false; messages.value = []; profile.value = null
  notices.value = []; unread.value = 0
  // 断开通知长连接，否则它会带着空 token 一直重连
  if (noticeEs) { noticeEs.close(); noticeEs = null }
  if (noticeFallback) { clearInterval(noticeFallback); noticeFallback = null }
}

async function enter() {
  try { agentMode.value = (await api.agentStatus()).mode } catch { /* ignore */ }
  await Promise.all([loadHistory(), loadMe(), loadTickets()])
}
async function loadHistory() {
  const { messages: ms } = await api.chatHistory()
  if (!ms.length) push('bot', '你好，我是校招咨询助手！可以问我：校招流程、面试形式、资料上传、体检、offer、网签等问题。')
  else ms.forEach(m => push(m.role === 'user' ? 'user' : 'bot', m.role === 'user' ? esc(m.content) : renderBot(m.content)))
}
const renderBot = t => esc(t).replace(/（参考条目：.+?）/s, m => `<span class="src">${m}</span>`)
async function loadMe() {
  try {
    const d = await api.me()
    // /api/me 返回 {candidate, history, timeline, next_step}，展开候选人并保留派生字段。
    // history 必须一起留下来——步骤条的「跳过/返工」标记和下面的实际轨迹都靠它，
    // 以前只取 timeline，等于把真实流水丢掉了。
    profile.value = { ...d.candidate, next_step: d.next_step, timeline: d.timeline,
                      history: d.history || [] }
  } catch { /* ignore */ }
}
async function loadTickets() { try { tickets.value = (await api.myTickets()).tickets } catch { /* ignore */ } }

async function send() {
  const text = draft.value.trim()
  if (!text || streaming.value) return
  draft.value = ''
  push('user', esc(text))
  streaming.value = true
  procStages.value = []; procSteps.value = []
  const bot = push('bot', '<span class="cursor"></span>')
  let buf = '', cites = [], verify = null, note = ''
  const paint = () => { bot.html = esc(buf) + '<span class="cursor"></span>'; scroll() }
  try {
    await api.chatStream(text, (ev, d) => {
      if (ev === 'intent') procStages.value.push(`🎯 意图：${d.label || d.intent}`)
      else if (ev === 'stage') procStages.value.push(d.label || d.name)
      else if (ev === 'plan') {
        procStages.value.push(d.steps?.length ? `🧭 计划：${d.steps.join(' → ')}` : '🧭 计划：无需工具')
        if (d.source === 'llm') procSteps.value.push('· 规划方式：模型拆解')
      }
      else if (ev === 'degrade') { procStages.value.push('⚠️ 降级固定管线'); note = '<span class="src">⚠️ 本次推理超出预算，已由固定管线兜底回答</span>' }
      else if (ev === 'notice') {
        // 限流等系统提示：不是错误，仍然有回答，只是额外告知一句
        note += `<span class="src">${esc(d.text || '')}</span>`
        procStages.value.push('⏳ ' + (d.text || ''))
      }
      else if (ev === 'tool') { procSteps.value.push(`🔧 ${d.name}(${JSON.stringify(d.args || {})})`); procStages.value.push('🔍 ' + d.name) }
      else if (ev === 'tool_result') procSteps.value.push('↳ ' + d.summary)
      else if (ev === 'delta') { buf += d.text || ''; paint() }
      else if (ev === 'citation') cites = d.items || []
      else if (ev === 'verify') verify = d
      else if (ev === 'judge') procSteps.value.push(`⚖️ 裁判：${{ ok: '有据', unsupported: '存疑', off_topic: '偏离' }[d.verdict] || d.verdict}${d.issues ? ' · ' + d.issues + ' 处待改进' : ''}`)
      else if (ev === 'memory') {
        procStages.value.push(d.repeat ? '🧠 记忆：重复提问，收敛作答'
          : d.resolved ? `🧠 追问还原：${d.resolved}` : '🧠 已加载候选人画像')
        if (d.profile) procSteps.value.push('· 画像：' + String(d.profile).replace(/\n/g, ' / '))
      }
      else if (ev === 'fallback') {
        buf = d.text || ''; bot.html = esc(buf)
        if (d.ticket_id) bot.html += `<span class="src">已生成反馈工单 #${d.ticket_id}，可在【我的反馈】查看进度</span>`
      }
    })
  } catch (e) {
    // 双格式兼容：流式不可用退回固定管线
    try {
      const j = await api.chatFixed(text)
      buf = j.reply; bot.html = esc(buf)
      if (j.ticket_id) bot.html += `<span class="src">已生成反馈工单 #${j.ticket_id}</span>`
      if (j.notice) note += `<span class="src">${esc(j.notice)}</span>`
    } catch (e2) { buf = '发送失败：' + e2.message; bot.html = esc(buf) }
  }
  // 流中断/无内容兜底：避免留下空气泡让用户以为还在生成
  if (!buf) buf = '抱歉，刚才的回答没有生成成功，请重新发送试试；如多次失败请联系管理员。'
  bot.html = esc(buf) + note
  bot.cites = cites
  bot.verify = verify
  streaming.value = false
  loadTickets(); loadMe(); scroll()
}

/** 通知推送：优先 SSE，失败回退轮询。
 *
 * 原来是固定 60 秒轮询：1000 人在线就是每分钟 1000 次请求，绝大多数返回"没新通知"。
 * 通知是低频突发事件，改用长连接后只有真有通知时才产生流量。
 *
 * 为什么保留轮询：SSE 可能被代理缓冲、被防火墙掐断、或浏览器标签页休眠后掉线。
 * 推送只用来降低延迟，不作为唯一通道——连不上就回到 60 秒轮询，功能不降级。
 */
function startNoticeStream() {
  if (typeof EventSource === 'undefined') { startNoticeFallback(); return }
  try {
    // EventSource 不能带自定义 header，token 走 query（后端 auth() 已支持）
    const es = new EventSource(`/api/notices/stream?token=${encodeURIComponent(cand.token)}`)
    noticeEs = es
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data)
        if (typeof d.unread === 'number') {
          unread.value = d.unread
          loadNotices()                       // 拉正文；推只是信号
        }
      } catch { /* 心跳或非法帧，忽略 */ }
    }
    es.onerror = () => {
      // 浏览器会自动重连（服务端发了 retry: 3000）；这里同时兜一层轮询，
      // 避免长时间连不上期间通知完全静默。
      if (!noticeFallback) startNoticeFallback()
    }
  } catch {
    startNoticeFallback()
  }
}
function startNoticeFallback() {
  if (noticeFallback) return
  noticeFallback = setInterval(loadNotices, 60000)
}

onMounted(() => {
  if (logged.value) {
    enter()
    loadNotices()
    startNoticeStream()
  }
})
onUnmounted(() => {
  if (noticeTimer) clearInterval(noticeTimer)
  if (noticeFallback) clearInterval(noticeFallback)
  if (noticeEs) noticeEs.close()
  if (cdTimer) clearInterval(cdTimer)
})
</script>

<style scoped>
.page { height: 100vh; display: flex; flex-direction: column; }

/* 顶栏 */
.topbar { height: 56px; flex: none; background: #fff; border-bottom: 1px solid #e8eae6;
  display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
.tb-left { display: flex; align-items: center; gap: 10px; }
.tb-left b { font-size: 16px; color: #3d443e; }
.tb-left .sub { font-size: 12px; color: #9aa39a; }
.logo { width: 30px; height: 30px; border-radius: 8px; background: linear-gradient(135deg, #a8bfa4, #8fae96);
  color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; }
.tb-right { display: flex; align-items: center; gap: 2px; }
/* S5.3 通知铃铛 */
.bell-wrap { position: relative; display: flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 8px; cursor: pointer; color: #5c6b62; }
.bell-wrap:hover { background: rgba(143, 174, 150, .16); color: #3f4b44; }
.bell { font-size: 18px; }
.bell-dot { position: absolute; top: -1px; right: -1px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: #c4826f; color: #fff; font-size: 10px; font-weight: 600;
  line-height: 16px; text-align: center; box-shadow: 0 0 0 2px #fff; }
.np { max-height: 420px; overflow-y: auto; }
.np-head { display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 8px; border-bottom: 1px solid #eee; color: #3f4b44; }
.np-empty { padding: 24px 0; text-align: center; color: #9aa39a; font-size: 13px; }
.np-item { padding: 10px 8px; border-bottom: 1px solid #f2f2f0; cursor: pointer; border-radius: 6px; }
.np-item:hover { background: #f7f8f5; }
.np-item.unread { background: #f4f8f4; }
.np-title { font-size: 13px; font-weight: 600; color: #3f4b44; }
.np-body { font-size: 12px; color: #6b756c; margin-top: 3px; line-height: 1.6; }
.np-time { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-size: 11px; color: #a3aba4; }
.user { display: flex; align-items: center; gap: 8px; }
.avatar { width: 30px; height: 30px; border-radius: 50%; background: #dce5db; color: #5f7a68;
  display: flex; align-items: center; justify-content: center; font-size: 13px; }
.uname { font-size: 13px; color: #4a524b; }

/* ===== 登录：左品牌 + 右表单分栏 ===== */
.login-wrap { flex: 1; display: flex; overflow: hidden; background: #f5f3ef; }

/* 左侧品牌区 */
.brand-pane { flex: 1.15; position: relative; overflow: hidden; display: flex; color: #fff;
  background: linear-gradient(150deg, #a3bda4 0%, #8aab94 45%, #6f9380 100%); }
.brand-deco { position: absolute; border-radius: 50%; pointer-events: none; }
.brand-deco.d1 { width: 420px; height: 420px; right: -120px; top: -140px;
  background: radial-gradient(circle, rgba(255,255,255,.18), rgba(255,255,255,0) 70%); }
.brand-deco.d2 { width: 340px; height: 340px; left: -110px; bottom: -120px;
  background: radial-gradient(circle, rgba(255,255,255,.14), rgba(255,255,255,0) 70%); }
.brand-deco.d3 { width: 160px; height: 160px; right: 18%; bottom: 12%;
  background: radial-gradient(circle, rgba(255,255,255,.10), rgba(255,255,255,0) 70%); }
.brand-inner { position: relative; z-index: 2; margin: auto; width: min(460px, 82%); padding: 48px 0; }
.brand-logo-row { display: flex; align-items: center; gap: 12px; margin-bottom: 40px; }
.brand-logo { width: 46px; height: 46px; border-radius: 13px; background: rgba(255,255,255,.92);
  color: #6f9380; display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 22px; box-shadow: 0 6px 18px rgba(40,60,48,.18); }
.brand-name b { display: block; font-size: 17px; letter-spacing: .5px; }
.brand-name span { font-size: 12px; opacity: .82; }
.brand-slogan { margin: 0 0 14px; font-size: 34px; line-height: 1.3; font-weight: 700; letter-spacing: 1px; }
.brand-slogan em { font-style: normal;
  background: linear-gradient(90deg, #fdf6e3, #ffe8c2); -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; }
.brand-sub { margin: 0 0 30px; font-size: 14px; line-height: 1.9; opacity: .88; max-width: 400px; }
.brand-feats { list-style: none; margin: 0 0 32px; padding: 0; display: grid;
  grid-template-columns: 1fr 1fr; gap: 16px 20px; }
.brand-feats li { display: flex; gap: 10px; align-items: flex-start; }
.brand-feats .fi { flex: none; width: 36px; height: 36px; border-radius: 10px; background: rgba(255,255,255,.16);
  display: flex; align-items: center; justify-content: center; font-size: 17px; backdrop-filter: blur(2px); }
.brand-feats b { display: block; font-size: 13.5px; margin-bottom: 2px; }
.brand-feats p { margin: 0; font-size: 11.5px; line-height: 1.55; opacity: .78; }
/* 模拟对话气泡：暗示这是问答产品 */
.brand-demo { display: flex; flex-direction: column; gap: 8px; max-width: 340px; }
.bd-bubble { padding: 10px 14px; border-radius: 13px; font-size: 12.5px; line-height: 1.65;
  box-shadow: 0 4px 14px rgba(40,60,48,.10); animation: floaty 5s ease-in-out infinite; }
.bd-bubble.user { align-self: flex-end; background: rgba(255,255,255,.92); color: #4a554e;
  border-bottom-right-radius: 4px; }
.bd-bubble.bot { align-self: flex-start; background: rgba(255,255,255,.16); color: #fff;
  border-bottom-left-radius: 4px; backdrop-filter: blur(4px); animation-delay: 1.2s; }
.bd-verify { display: inline-block; margin-top: 5px; font-size: 10.5px; opacity: .85; }
@keyframes floaty { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }

/* 右侧表单区 */
.form-pane { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 32px 24px; overflow-y: auto; position: relative; }
.tip { color: #8a948c; font-size: 12px; }
.login-card { width: 100%; max-width: 400px; background: #fff; border-radius: 20px;
  padding: 34px 34px 26px; border: 1px solid #eceae4;
  box-shadow: 0 18px 50px rgba(90,110,95,.10); }
.lc-head { display: flex; align-items: center; gap: 10px; margin-bottom: 22px; }
.lc-logo { width: 34px; height: 34px; border-radius: 9px;
  background: linear-gradient(135deg, #a8bfa4, #8fae96); color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 15px; }
.lc-head b { font-size: 14.5px; color: #3d443e; display: block; }
.lc-head p { margin: 0; font-size: 10.5px; color: #a9b2a8; }
.login-card h3 { margin: 0 0 4px; font-size: 21px; color: #2f352f; }
.login-card .tip { color: #8a948c; font-size: 12.5px; margin: 0 0 14px; }
.login-mode { margin: 0 0 18px; }
.login-mode :deep(.el-radio-button__inner) { border-radius: 8px; }
.code-row { display: flex; gap: 8px; width: 100%; }
.code-row .el-input { flex: 1; }
.cd-btn { flex: none; min-width: 112px; }
.login-card :deep(.el-input__wrapper) { border-radius: 10px; padding: 4px 12px; }
.login-btn { width: 100%; margin-top: 4px; border: none; border-radius: 10px; font-weight: 600;
  background: linear-gradient(135deg, #a3bfa0, #8fae96);
  box-shadow: 0 6px 16px rgba(143, 174, 150, .38); transition: transform .15s, box-shadow .15s; }
.login-btn:hover { transform: translateY(-1px); box-shadow: 0 9px 20px rgba(143, 174, 150, .46); }
.login-btn:active { transform: translateY(0); }
.login-alert { margin-top: 12px; border-radius: 8px; }
.login-hint { display: flex; gap: 6px; align-items: flex-start; margin-top: 14px; padding: 9px 12px;
  background: #f4f6f2; border-radius: 8px; font-size: 11.5px; color: #8a948c; line-height: 1.6; }
.login-hint .el-icon { flex: none; margin-top: 2px; }
.login-footer { margin-top: 22px; font-size: 11.5px; color: #a9b2a8; }

/* 对话页 */
.chat-page { flex: 1; display: flex; justify-content: center; background: #f5f3ef; overflow: hidden; }
.chat-panel { width: 100%; max-width: 1000px; display: flex; flex-direction: column;
  background: #fff; margin: 16px; border-radius: 14px; border: 1px solid #eceae4; overflow: hidden; }
.toolbar { flex: none; display: flex; align-items: center; gap: 10px; padding: 10px 18px;
  background: #faf9f6; border-bottom: 1px solid #f0eee8; }
.tb-spacer { flex: 1; }
.agent-label { font-size: 13px; color: #6b7269; }
.info { color: #b0b8ae; margin-left: 2px; cursor: help; }
.proc-panel { flex: none; max-height: 160px; overflow: auto; padding: 8px 18px;
  background: #f7f8f5; border-bottom: 1px solid #eef0ea; }
.proc-panel .stages { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
.proc-panel .step { font-size: 12px; color: #6b7269; margin: 2px 0 2px 4px; }

/* 消息区 */
.msgs { flex: 1; overflow-y: auto; padding: 24px 32px; }
.empty { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #a7b0a6; }
.empty-icon { font-size: 52px; margin-bottom: 14px; }
.empty p { font-size: 14px; margin: 0 0 18px; }
.quick { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
.msg-row { display: flex; margin-bottom: 14px; }
.msg-row.user { justify-content: flex-end; }
.msg { max-width: 78%; padding: 11px 15px; border-radius: 14px; font-size: 14px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word; }
.msg.user { background: linear-gradient(135deg, #a3bfa0, #8fae96); color: #fff; border-top-right-radius: 4px; }
.msg.bot { background: #f4f3ee; color: #43483f; border-top-left-radius: 4px; }
.msg :deep(.src) { display: block; margin-top: 6px; font-size: 11px; color: #8a948c; }
.cites { margin-top: 6px; font-size: 11px; color: #6b7269; }

/* 第5期：核验行 + 句子级依据面板 */
.verify-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.anchor-toggle { font-size: 12px; }
.anchors { margin-top: 8px; border-left: 2px solid #c9d6cb; padding-left: 10px; }
.anchor-item { margin-bottom: 8px; font-size: 12.5px; line-height: 1.6; }
.anchor-item:last-child { margin-bottom: 0; }
.anchor-sent { color: #43483f; word-break: break-word; }
.anchor-ico { margin-right: 4px; }
.anchor-ev { margin-top: 1px; font-size: 11px; color: #8a948c; word-break: break-word; }
.anchor-item.warn .anchor-ev { color: #b08a5a; }

/* 输入区 */
.input-area { flex: none; display: flex; align-items: flex-end; gap: 12px; padding: 14px 18px;
  border-top: 1px solid #f0eee8; background: #fff; }
.input-area :deep(.el-textarea__inner) { border-radius: 10px; padding: 10px 14px; }
.send-btn { height: 56px; padding: 0 22px; border-radius: 10px; font-size: 14px; }

.cursor { display: inline-block; width: 8px; height: 14px; background: #8fae96; vertical-align: -2px; animation: blink 1s infinite; }

/* 我的反馈抽屉：问题标题与回复字号 */
.fb-q { font-size: 14px; font-weight: 600; color: #3d443e; line-height: 1.65; word-break: break-word; }
.fb-q .el-tag { vertical-align: 1px; }

@keyframes blink { 50% { opacity: 0; } }

@media (max-width: 960px) {
  /* 窄屏隐藏品牌区，登录表单全屏居中 */
  .brand-pane { display: none; }
  .form-pane { background: linear-gradient(160deg, #eef1ec, #f5f3ef); }
}
@media (max-width: 640px) {
  .chat-panel { margin: 0; border-radius: 0; border: none; }
  .msgs { padding: 16px; }
  .tb-left .sub { display: none; }
  .login-card { padding: 26px 22px 20px; }
}

/* 进度条：跳过的环节灰虚线、返工的环节打 ↩ 标记 —— 区分"标准模板"与"真实走过" */
.step-skipped { color: #9aa0a6; text-decoration: line-through; text-decoration-style: dashed; }
.step-flag { margin-left: 6px; font-size: 11px; padding: 1px 6px; border-radius: 8px;
  border: 1px solid #ece9e1; white-space: nowrap; }
.step-flag.warn { color: #c98b3b; border-color: #f0dcbd; background: #fdf6e9; }
.step-flag.muted { color: #9aa0a6; }
.muted { color: #9aa0a6; font-size: 12px; }
</style>
