<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <div class="head">
        <h4>主动推送记录</h4>
        <div>
          <el-button size="small" :loading="scanning" @click="scan">立即扫描一次</el-button>
          <el-button size="small" @click="load" :loading="loading">刷新</el-button>
        </div>
      </div>
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
        title="HR 改动候选人状态后会自动推送站内通知；待办态（测评中 / 待传资料 / 网签中）停留超阈值也会自动提醒。
               文案全部由模板生成，不含模型编造的内容。" />
      <div class="stats">
        <div class="stat"><div class="n">{{ stats.total || 0 }}</div><div class="l">累计推送</div></div>
        <div class="stat"><div class="n">{{ stats.read || 0 }}</div><div class="l">已读</div></div>
        <div class="stat"><div class="n">{{ stats.status_change || 0 }}</div><div class="l">状态变更</div></div>
        <div class="stat"><div class="n">{{ stats.stale_todo || 0 }}</div><div class="l">待办停滞</div></div>
        <div class="stat"><div class="n">{{ stats.sent_users || 0 }}</div><div class="l">覆盖候选人数</div></div>
      </div>
    </el-card>

    <!-- 阈值配置：只有超管能改 -->
    <el-card v-if="isSuper" shadow="never" style="margin-bottom:16px">
      <h4>推送开关与阈值</h4>
      <el-form :inline="true" size="small" style="margin-top:10px">
        <el-form-item label="启用推送">
          <el-switch v-model="cfg.notice_enabled" :active-value="1" :inactive-value="0" />
        </el-form-item>
        <el-form-item label="待办停滞(天)">
          <el-input-number v-model="cfg.notice_stale_days" :min="1" :max="60" controls-position="right" style="width:110px" />
        </el-form-item>
        <el-form-item label="重复提醒间隔(天)">
          <el-input-number v-model="cfg.notice_stale_cooldown" :min="1" :max="60" controls-position="right" style="width:110px" />
        </el-form-item>
        <el-form-item label="扫描周期(秒)">
          <el-input-number v-model="cfg.notice_scan_interval" :min="10" :max="3600" :step="10" controls-position="right" style="width:120px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="small" :loading="saving" @click="saveCfg">保存</el-button>
        </el-form-item>
      </el-form>
      <p class="tip">扫描周期是后台线程启动时读取的，改完需重启服务才生效；其余配置即时生效。</p>
    </el-card>

    <el-card shadow="never">
      <div class="head" style="margin-bottom:10px">
        <h4>推送明细（{{ rows.length }}）</h4>
        <el-radio-group v-model="kind" size="small" @change="load">
          <el-radio-button label="">全部</el-radio-button>
          <el-radio-button label="status_change">状态变更</el-radio-button>
          <el-radio-button label="stale_todo">待办停滞</el-radio-button>
        </el-radio-group>
      </div>
      <el-table :data="paged" size="small">
        <el-table-column label="编号" width="70"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column label="推送时间" width="150">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="候选人" width="150">
          <template #default="{ row }">
            {{ row.name || '（已删除）' }}<div class="tip">{{ row.phone || '-' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="row.kind === 'status_change' ? 'success' : 'warning'" size="small">
              {{ row.kind === 'status_change' ? '状态变更' : '待办停滞' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
        <el-table-column prop="body" label="正文" min-width="220" show-overflow-tooltip />
        <el-table-column label="阅读状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.read_at ? 'info' : 'danger'" size="small" effect="plain">
              {{ row.read_at ? '已读' : '未读' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div class="page-bar">
        <el-pagination v-model:current-page="page" v-model:page-size="size"
          :total="rows.length" :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next" background small />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const rows = ref([])
const stats = ref({})
const cfg = ref({ notice_enabled: 1, notice_scan_interval: 60, notice_stale_days: 3, notice_stale_cooldown: 3 })
const kind = ref('')
const loading = ref(false)
const scanning = ref(false)
const saving = ref(false)
const page = ref(1)
const size = ref(20)
// 阈值配置走超管专属接口，HR 只看记录不显示配置卡片
const isSuper = localStorage.getItem('campus_admin_role') === 'super'

const paged = computed(() => rows.value.slice((page.value - 1) * size.value, page.value * size.value))

function fmt(t) {
  if (!t) return '-'
  return String(t).replace('T', ' ').slice(0, 16)
}

async function load() {
  loading.value = true
  try {
    const j = await api.adminNotices(200, kind.value)
    rows.value = j.notices || []
    stats.value = j.stats || {}
    if (j.config) cfg.value = { ...cfg.value, ...j.config }
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function scan() {
  scanning.value = true
  try {
    const j = await api.scanNotices()
    const r = j.result || {}
    if (r.error) { ElMessage.error('扫描异常：' + r.error); return }
    ElMessage.success(`扫描完成：状态变更 ${r.status_change || 0} 条 / 待办停滞 ${r.stale_todo || 0} 条`)
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { scanning.value = false }
}

async function saveCfg() {
  saving.value = true
  try {
    await api.saveNoticeConfig({ ...cfg.value })
    ElMessage.success('已保存')
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

onMounted(load)
</script>

<style scoped>
.head { display: flex; align-items: center; justify-content: space-between; }
h4 { margin: 0; color: #3f4b44; }
.tip { color: #8a948c; font-size: 12px; margin: 6px 0 0; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 14px; }
.stat { flex: 1; min-width: 100px; background: #f7f6f2; border-radius: 10px; padding: 12px 14px; }
.stat .n { font-size: 22px; font-weight: 700; color: #5c7a62; }
.stat .l { font-size: 12px; color: #8a948c; margin-top: 2px; }
.page-bar { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
