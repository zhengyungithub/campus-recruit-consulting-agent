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

    <!-- 登录 -->
    <div v-if="!logged" class="login-wrap">
      <el-card class="login-card" shadow="always">
        <h3>候选人登录</h3>
        <p class="tip">请使用校招报名时预留的手机号登录</p>
        <div class="login-mode">
          <el-radio-group v-model="loginMode" size="small">
            <el-radio-button value="code">验证码登录</el-radio-button>
            <el-radio-button value="pwd">密码登录</el-radio-button>
          </el-radio-group>
        </div>
        <el-form @submit.prevent>
          <el-form-item><el-input v-model="phone" maxlength="11" placeholder="手机号" size="large" /></el-form-item>
          <el-form-item v-if="loginMode === 'code'">
            <div class="code-row">
              <el-input v-model="code" maxlength="6" placeholder="验证码" size="large" />
              <el-button size="large" @click="requestCode">获取验证码</el-button>
            </div>
          </el-form-item>
          <el-form-item v-else>
            <el-input v-model="pwdLogin" type="password" show-password size="large"
                      placeholder="登录密码" @keydown.enter="doLogin" />
          </el-form-item>
          <el-button type="primary" size="large" style="width:100%" @click="doLogin">登 录</el-button>
        </el-form>
        <el-alert v-if="loginMsg" :title="loginMsg" type="warning" :closable="false" style="margin-top:12px" />
        <el-alert :title="loginMode === 'code' ? '演示环境：验证码直接回显' : '首次使用请先用验证码登录，再在【账号设置】中设置密码'"
                  type="info" :closable="false" style="margin-top:8px" />
      </el-card>
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
          <el-step v-for="t in timeline" :key="t.status" :title="t.status"
                   :status="t.done?'success':t.cur?'process':'wait'"
                   :description="t.cur ? profile.next_step : ''" />
        </el-steps>
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
import { ref, nextTick, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Promotion, InfoFilled } from '@element-plus/icons-vue'
import { api, cand } from '../api'

const logged = ref(cand.logged)
const phone = ref(''), code = ref(''), loginMsg = ref('')
const loginMode = ref('code'), pwdLogin = ref('')
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
const quickQs = ['校招流程是怎样的？', '我现在到哪一步了？', '面试资料有什么要求？', '网签怎么操作？']

const timeline = computed(() => profile.value?.timeline || [])
const activeIdx = computed(() => timeline.value.findIndex(t => t.cur))

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
function clearChat() { messages.value = []; procStages.value = []; procSteps.value = []; delMode.value = false }
function quick(q) { draft.value = q; send() }

async function requestCode() {
  try {
    const j = await api.requestCode(phone.value.trim())
    code.value = j.demo_code
    loginMsg.value = `演示验证码：${j.demo_code}（生产环境将短信下发）`
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
function logout() { cand.token = ''; logged.value = false; messages.value = []; profile.value = null }

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
    // /api/me 返回 {candidate, history, timeline, next_step}，展开候选人并保留派生字段
    profile.value = { ...d.candidate, next_step: d.next_step, timeline: d.timeline }
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

onMounted(() => { if (logged.value) enter() })
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
.user { display: flex; align-items: center; gap: 8px; }
.avatar { width: 30px; height: 30px; border-radius: 50%; background: #dce5db; color: #5f7a68;
  display: flex; align-items: center; justify-content: center; font-size: 13px; }
.uname { font-size: 13px; color: #4a524b; }

/* 登录 */
.login-wrap { flex: 1; display: flex; align-items: center; justify-content: center; background: #f5f3ef; }
.login-card { width: 380px; border-radius: 16px; border: none; padding: 10px 6px; }
.login-card h3 { margin: 0 0 4px; font-size: 18px; }
.tip { color: #8a948c; font-size: 12px; }
.login-mode { margin: 4px 0 14px; }
.code-row { display: flex; gap: 8px; width: 100%; }

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

@media (max-width: 640px) {
  .chat-panel { margin: 0; border-radius: 0; border: none; }
  .msgs { padding: 16px; }
  .tb-left .sub { display: none; }
}
</style>
