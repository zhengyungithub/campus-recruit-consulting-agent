<template>
  <div v-if="!logged" class="login-wrap">
    <div class="login-side">
      <div class="brand">
        <div class="logo">招</div>
        <h2>校招咨询助手</h2>
        <p>Campus Recruitment Agent · LangGraph + Vue3</p>
      </div>
    </div>
    <el-card class="login-card" shadow="never">
      <h3>欢迎回来 👋</h3>
      <p class="tip">HR 后台登录</p>
      <el-form size="large" @submit.prevent>
        <el-form-item><el-input v-model="u" placeholder="用户名" :prefix-icon="User" /></el-form-item>
        <el-form-item><el-input v-model="p" type="password" show-password placeholder="密码"
                                :prefix-icon="Lock" @keydown.enter="doLogin" /></el-form-item>
        <el-button type="primary" size="large" style="width:100%" @click="doLogin">登 录</el-button>
      </el-form>
      <!-- 不在页面上写任何凭据：口令由部署方配置（环境变量 / 首次启动随机生成见服务日志） -->
      <div class="tip" style="margin-top:14px">忘记口令请联系系统管理员重置，勿使用弱口令</div>
    </el-card>
  </div>

  <el-container v-else class="layout">
    <!-- 侧边栏（SoybeanAdmin 风格深色导航） -->
    <el-aside :width="collapsed ? '64px' : '220px'" class="aside" :class="{ 'aside-collapsed': collapsed }">
      <div class="aside-brand">
        <div class="logo">招</div>
        <div v-show="!collapsed" class="brand-text"><b>校招咨询助手</b><div class="tip">HR 运营后台</div></div>
      </div>
      <el-menu :default-active="tab" class="menu" background-color="transparent"
               text-color="#6b7269" active-text-color="#fff"
               :collapse="collapsed" :collapse-transition="false"
               @select="k => tab = k">
        <template v-if="role === 'hr'">
          <el-menu-item index="dash"><el-icon><Odometer /></el-icon><template #title>数据看板</template></el-menu-item>
          <el-menu-item index="cand"><el-icon><User /></el-icon><template #title>候选人</template></el-menu-item>
          <el-menu-item index="kb"><el-icon><Collection /></el-icon><template #title>知识库</template></el-menu-item>
          <el-menu-item index="tk">
            <el-icon><ChatDotRound /></el-icon>
            <template #title>
              反馈工单
              <span v-if="pendingCount" class="menu-badge">{{ pendingCount > 99 ? '99+' : pendingCount }}</span>
            </template>
          </el-menu-item>
          <el-menu-item index="notice"><el-icon><Bell /></el-icon><template #title>主动推送</template></el-menu-item>
        </template>
        <template v-if="role === 'super'">
          <el-menu-item index="agent"><el-icon><MagicStick /></el-icon><template #title>Agent 灰度</template></el-menu-item>
          <el-menu-item index="notice"><el-icon><Bell /></el-icon><template #title>主动推送</template></el-menu-item>
          <el-menu-item index="model"><el-icon><Key /></el-icon><template #title>模型配置</template></el-menu-item>
          <el-menu-item index="accounts"><el-icon><Avatar /></el-icon><template #title>HR 账号管理</template></el-menu-item>
          <el-menu-item index="eval"><el-icon><DataAnalysis /></el-icon><template #title>评测回归</template></el-menu-item>
          <el-menu-item index="audit"><el-icon><Document /></el-icon><template #title>操作日志</template></el-menu-item>
        </template>
        <el-menu-item index="account"><el-icon><UserFilled /></el-icon><template #title>账号设置</template></el-menu-item>
      </el-menu>
      <div class="aside-foot">
        <div v-show="!collapsed" class="who">
          <div class="who-name">{{ username || '未登录' }}</div>
          <el-tag :type="role === 'super' ? 'danger' : 'info'" size="small" effect="plain">
            {{ role === 'super' ? '超级管理员' : 'HR 运营' }}
          </el-tag>
        </div>
        <el-button link type="info" @click="logout">
          <el-icon style="margin-right:4px"><SwitchButton /></el-icon><span v-show="!collapsed">退出登录</span>
        </el-button>
      </div>
    </el-aside>

    <el-container>
      <el-header class="top">
        <div class="top-left">
          <el-icon class="hamburger" :title="collapsed ? '展开菜单' : '收起菜单'" @click="toggleCollapse">
            <Expand v-if="collapsed" /><Fold v-else />
          </el-icon>
          <b>{{ titles[tab] }}</b>
        </div>
        <span class="tip">校招咨询 Agent · {{ today }}</span>
      </el-header>
      <el-main class="main">
        <DashTab v-if="tab === 'dash'" />
        <CandTab v-else-if="tab === 'cand'" />
        <KbTab v-else-if="tab === 'kb'" />
        <TicketTab v-else-if="tab === 'tk'" @pending="pendingCount = $event" />
        <NoticeTab v-else-if="tab === 'notice'" />
        <AgentTab v-else-if="tab === 'agent'" />
        <AccountTab v-else-if="tab === 'account'" />
        <ModelTab v-else-if="tab === 'model'" />
        <AccountsTab v-else-if="tab === 'accounts'" />
        <EvalTab v-else-if="tab === 'eval'" />
        <AuditTab v-else-if="tab === 'audit'" />
        <DashTab v-else />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { User, Lock, Odometer, Collection, ChatDotRound, MagicStick, Key, Fold, Expand, SwitchButton, UserFilled, Avatar, DataAnalysis, Bell, Document } from '@element-plus/icons-vue'
import { api, admin } from '../api'
import DashTab from '../components/AdminDash.vue'
import CandTab from '../components/AdminCandidates.vue'
import KbTab from '../components/AdminFaq.vue'
import TicketTab from '../components/AdminTickets.vue'
import AgentTab from '../components/AdminAgent.vue'
import SettingsTab from '../components/AdminSettings.vue'
import AccountTab from '../components/AdminAccount.vue'
import ModelTab from '../components/AdminSettings.vue'
import AccountsTab from '../components/AdminAccounts.vue'
import EvalTab from '../components/AdminEval.vue'
import NoticeTab from '../components/AdminNotices.vue'
import AuditTab from '../components/AdminAudit.vue'

const logged = ref(admin.logged)
const role = ref(localStorage.getItem('campus_admin_role') || '')
const username = ref(localStorage.getItem('campus_admin_user') || '')
// 角色对应的功能首页：超管 → Agent 灰度，HR → 数据看板
const homeTab = r => (r === 'super' ? 'agent' : 'dash')
const tab = ref(homeTab(role.value))
const pendingCount = ref(0)
const collapsed = ref(localStorage.getItem('campus_aside_collapsed') === '1')
function toggleCollapse() {
  collapsed.value = !collapsed.value
  localStorage.setItem('campus_aside_collapsed', collapsed.value ? '1' : '0')
}
const u = ref(''), p = ref('')
const titles = { dash: '数据看板', cand: '候选人管理', kb: '知识库', tk: '反馈工单', agent: 'Agent 灰度',
                 notice: '主动推送', model: '模型配置（超管）', accounts: 'HR 账号管理',
                 eval: '评测回归', audit: '操作日志', account: '账号设置' }
const today = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })

async function doLogin() {
  try {
    const j = await api.adminLogin(u.value.trim(), p.value)
    admin.token = j.token
    role.value = j.role || 'super'
    username.value = j.username || ''
    localStorage.setItem('campus_admin_role', role.value)
    localStorage.setItem('campus_admin_user', username.value)
    logged.value = true
    // 按角色落到各自的功能首页
    tab.value = homeTab(role.value)
  } catch (e) { ElMessage.error(e.message) }
}
function logout() {
  admin.token = ''
  localStorage.removeItem('campus_admin_role'); localStorage.removeItem('campus_admin_user')
  role.value = ''; username.value = ''
  logged.value = false
}
onMounted(async () => {
  if (!logged.value) return
  // 刷新后从服务端恢复角色，保证菜单权限与登录态一致
  try {
    const m = await api.adminMe()
    role.value = m.role; username.value = m.username
    localStorage.setItem('campus_admin_role', role.value)
    localStorage.setItem('campus_admin_user', username.value)
    // 角色与当前页面不匹配时回到该角色的首页
    const allowed = role.value === 'super'
      ? ['agent', 'notice', 'model', 'accounts', 'eval', 'audit', 'account']
      : ['dash', 'cand', 'kb', 'tk', 'notice', 'account']
    if (!allowed.includes(tab.value)) tab.value = homeTab(role.value)
  } catch { /* ignore */ }
  try { pendingCount.value = (await api.tickets()).tickets.filter(t => t.status === 'pending').length } catch { /* ignore */ }
})
</script>

<style scoped>
/* 侧边栏：莫兰迪浅灰绿 */
.layout { height: 100vh; }
.aside { background: linear-gradient(180deg, #ecefe9 0%, #e2e8e2 100%); display: flex; flex-direction: column;
  transition: width .25s ease; overflow: hidden; }
.aside-brand { display: flex; align-items: center; gap: 10px; padding: 18px 16px; color: #3f4b44; }
.aside-collapsed .aside-brand { justify-content: center; padding: 18px 8px; }
.brand-text b { font-size: 17px; font-weight: 700; letter-spacing: 1.5px; display: block; }
.brand-text .tip { font-size: 11px; color: #8a948c; margin-top: 2px; }
.aside-collapsed .aside-foot { text-align: center; padding: 14px 6px; }
.aside-collapsed .aside-foot .el-icon { margin-right: 0; }
.logo { width: 38px; height: 38px; border-radius: 10px; flex: none;
  background: linear-gradient(135deg, #a8bfa4, #8fae96); color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 17px; }
.menu { border-right: none; flex: 1; }
.menu:not(.el-menu--collapse) { width: 100%; }
.menu.el-menu--collapse { width: 64px; border-right: none; }
.menu :deep(.el-menu-item) { border-radius: 10px; margin: 4px 10px; height: 44px; }
.menu.el-menu--collapse :deep(.el-menu-item) { margin: 6px 9px; width: 46px; padding: 0 !important; }
.menu :deep(.el-menu-item.is-active) { background: linear-gradient(135deg, #a8bfa4, #8fae96); }
.menu :deep(.el-menu-item:hover) { background: rgba(143, 174, 150, .14); }
.menu-badge { margin-left: auto; flex: none; min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px; background: #c4826f; color: #fff; font-size: 11px; font-weight: 600;
  line-height: 18px; text-align: center; letter-spacing: .2px;
  box-shadow: 0 0 0 2px rgba(245, 243, 239, .55); }
.aside-foot { padding: 14px 16px; border-top: 1px solid rgba(63, 75, 68, .1); }
.who { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.who-name { font-size: 13px; color: #3f4b44; font-weight: 600; }
.top { background: #fbfaf7; border-bottom: 1px solid #e9e6df; display: flex; justify-content: space-between; align-items: center; }
.top-left { display: flex; align-items: center; gap: 14px; }
.hamburger { font-size: 20px; color: #5c6b62; cursor: pointer; transition: color .2s; }
.hamburger:hover { color: #3f4b44; background: rgba(143, 174, 150, .16); border-radius: 6px; }
.top b { font-size: 20px; font-weight: 700; letter-spacing: 2px; color: #3f4b44; }
.main { background: #f5f3ef; overflow-y: auto; }
.tip { color: #8a948c; font-size: 12px; }

/* 登录页（分屏品牌区）：雾霾绿渐变 */
.login-wrap { display: flex; min-height: 100vh; }
.login-side { flex: 1; background: linear-gradient(135deg, #d7e1da 0%, #c2d2c8 55%, #a9bfca 100%);
  display: flex; align-items: center; justify-content: center; }
.brand { text-align: center; color: #3f4b44; }
.brand .logo { width: 64px; height: 64px; font-size: 28px; margin: 0 auto 16px; border-radius: 18px; }
.brand p { color: #5c6b62; font-size: 13px; margin-top: 8px; }
.login-card { width: 400px; margin: auto; border: none; border-radius: 16px; box-shadow: 0 20px 60px rgba(90, 100, 90, .14); padding: 24px 10px; }
.login-card h3 { margin: 0 0 2px; color: #3f4b44; }
</style>
