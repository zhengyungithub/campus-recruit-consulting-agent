<template>
  <div style="padding:16px 4px">
    <el-card shadow="never">
      <h4>候选人反馈（{{ rows.length }}）</h4>
      <div class="sla-bar" v-if="stats.total">
        <span>待处理 <b>{{ stats.pending }}</b></span>
        <span>处理中 <b>{{ stats.handling }}</b></span>
        <span :class="{ danger: stats.overdue }">逾期 <b>{{ stats.overdue }}</b></span>
        <span class="tip">SLA：加急 {{ stats.urgent_sla_hours }}h / 普通 {{ stats.sla_hours }}h</span>
      </div>
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
        title="机器人无法回答的问题会自动生成反馈。认领后进入「处理中」并记录负责人；勾选「沉淀为知识库」的条目会先入草稿，审核通过后才对机器人可见。" />
      <el-table :data="paged" size="small">
        <el-table-column label="编号" width="70"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column prop="question" label="问题" min-width="180" show-overflow-tooltip />
        <el-table-column label="候选人" width="150">
          <template #default="{ row }">{{ row.name }}<div class="tip">{{ row.phone }}</div></template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'resolved' ? 'success' : (row.status === 'handling' ? 'primary' : 'warning')"
                size="small">{{ statusLabel(row.status) }}</el-tag>
            <el-tag v-if="row.overdue" type="danger" size="small" style="margin-top:4px">
              逾期 {{ row.overdue_hours }}h</el-tag>
            <el-tag v-if="row.priority === 'urgent'" type="danger" size="small" style="margin-top:4px">加急</el-tag>
            <el-tag v-if="row.faq_added" type="success" size="small" style="margin-top:4px">已沉淀</el-tag>
            <div class="tip" v-if="row.assignee">负责人 {{ row.assignee }} · {{ row.priority_label }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="answer" label="人工回复" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="open(row)">
              {{ row.status === 'resolved' ? '查看' : '处理' }}</el-button>
            <el-button v-if="row.status !== 'resolved'" size="small" plain @click="claim(row)">
              {{ row.assignee ? '重新认领' : '认领' }}</el-button>
            <el-button v-if="row.status !== 'resolved' && row.priority !== 'urgent'"
                       size="small" plain @click="urgent(row)">加急</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="page-bar">
        <el-pagination v-model:current-page="page" v-model:page-size="size"
          :total="rows.length" :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next" background small />
      </div>
    </el-card>

    <el-dialog v-model="dlg" :title="dlgTitle" width="620">
      <p><b>问题：</b>{{ current?.question }}</p>
      <div v-if="ctxText" class="ctx">{{ ctxText }}</div>
      <el-input v-model="reply" type="textarea" :rows="4"
        placeholder="人工回复（将同步到候选人端【我的反馈】和对话中）" />
      <el-checkbox v-model="reflow" style="margin-top:10px">沉淀为知识库条目（机器人下次可自动回答）</el-checkbox>
      <template #footer>
        <el-button @click="dlg = false">关闭</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">提交回复</el-button>
      </template>
    </el-dialog>

    <!-- S2.2 沉淀预览确认：命中相似条目时让 HR 看一眼再决定 -->
    <el-dialog v-model="dupDlg" width="700" :close-on-click-modal="false">
      <template #header>
        <b>{{ dupLevel === 'dup' ? '知识库中可能已有该条目' : '发现相近条目，请确认' }}</b>
      </template>
      <el-alert :type="dupLevel === 'dup' ? 'warning' : 'info'" :closable="false" style="margin-bottom:12px"
                :title="dupInfo.message || '已有相似条目，建议先确认是否重复'" />
      <p class="tip" style="margin:0 0 6px">入库前可修改问题表述（改完点「重新查重」刷新结论）：</p>
      <el-input v-model="dupQuestion" size="small" style="margin-bottom:12px" />
      <el-table :data="dupInfo.candidates || []" size="small" max-height="260">
        <el-table-column label="编号" width="70"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column prop="question" label="已有问题" min-width="240" show-overflow-tooltip />
        <el-table-column label="相似度" width="90">
          <template #default="{ row }">{{ (row.sim * 100).toFixed(0) }}%</template>
        </el-table-column>
        <el-table-column label="命中通道" width="90">
          <template #default="{ row }">{{ row.via === 'dense' ? '语义' : '关键词' }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="dupDlg = false">取消</el-button>
        <el-button @click="recheck">重新查重</el-button>
        <el-button plain @click="submitOnly">跳过沉淀，仅回复</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForce">仍然入库</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const emit = defineEmits(['pending'])
const rows = ref([]), dlg = ref(false), current = ref(null)
const page = ref(1), size = ref(10)
const paged = computed(() => rows.value.slice((page.value - 1) * size.value, page.value * size.value))
const reply = ref(''), reflow = ref(false), submitting = ref(false)
// S6.1 工单 SLA：逾期时长、负责人、优先级
const stats = ref({})
const statusLabel = s => ({ pending: '待处理', handling: '处理中', resolved: '已回复' }[s] || s)

async function claim(row) {
  try {
    await api.claimTicket(row.id)
    ElMessage.success(`已认领工单 #${row.id}，后续由你负责`)
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function urgent(row) {
  try {
    await api.setTicketPriority(row.id, 'urgent')
    ElMessage.success(`工单 #${row.id} 已置为加急（SLA 缩短到 8 小时）`)
    load()
  } catch (e) { ElMessage.error(e.message) }
}
// S2.2 沉淀预览确认
const dupDlg = ref(false), dupInfo = ref({}), dupQuestion = ref(''), dupLevel = computed(() => dupInfo.value?.level || 'ok')
const dlgTitle = computed(() => current.value
  ? `工单 #${current.value.id} · ${current.value.name}（当前环节：${current.value.candidate_status}）` : '')
const ctxText = computed(() => {
  if (!current.value?.context) return ''
  try { return JSON.parse(current.value.context).map(m => `${m.role === 'user' ? '候选人' : '机器人'}：${m.content}`).join('\n') }
  catch { return '' }
})

function open(row) {
  current.value = row; reply.value = row.answer || ''; reflow.value = !!row.faq_added
  dupInfo.value = {}; dupQuestion.value = row.question || ''
  dlg.value = true
}

// S2.2：提交前先预览查重；命中相似条目则弹确认框，由 HR 决定「仍要入库 / 跳过沉淀」
async function submit() {
  if (!reflow.value) return doSubmit(false)
  submitting.value = true
  try {
    const p = await api.kbPreview({
      question: dupQuestion.value || current.value.question,
      answer: reply.value, source: `ticket#${current.value.id}`
    })
    dupInfo.value = p.dedupe || {}
    dupQuestion.value = p.question || dupQuestion.value
    if (p.dedupe?.level === 'ok') return doSubmit(false)
    dupDlg.value = true
  } catch (e) { ElMessage.error(e.message) }
  finally { submitting.value = false }
}

async function recheck() {
  const p = await api.kbPreview({
    question: dupQuestion.value, answer: reply.value,
    source: `ticket#${current.value.id}`
  }).catch(e => { ElMessage.error(e.message); return null })
  if (!p) return
  dupInfo.value = p.dedupe || {}
  if (p.dedupe?.level === 'ok') { dupDlg.value = false; doSubmit(false) }
}

function submitOnly() { dupDlg.value = false; doSubmit(false, false) }
function submitForce() { dupDlg.value = false; doSubmit(true) }

async function doSubmit(force, precipitate = true) {
  submitting.value = true
  try {
    const j = await api.replyTicket(current.value.id, {
      answer: reply.value,
      add_to_faq: precipitate && reflow.value,
      force: force ? 1 : 0,
      faq_question: precipitate ? dupQuestion.value : ''
    })
    dlg.value = false
    const f = j.faq
    if (f && f.skipped) ElMessage.warning(f.dedupe?.message || '已跳过沉淀')
    else if (f && f.ok) ElMessage.success(f.review_status === 'draft'
      ? '已回复；沉淀条目进入草稿，审核通过后生效'
      : '已回复并沉淀为知识库')
    else ElMessage.success('已回复')
    load()
  } catch (e) { ElMessage.error(e.message) }
  finally { submitting.value = false }
}
onMounted(load)
async function load() {
  const j = await api.tickets()
  rows.value = j.tickets || []
  stats.value = j.stats || {}
  // S6.1：菜单角标只数"没人认领"的，处理中的不该再被当待办催
  emit('pending', rows.value.filter(t => t.status === 'pending').length)
}
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 14px; }
.tip { color: #64748b; font-size: 12px; }
.ctx { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; font-size: 12px;
  max-height: 170px; overflow-y: auto; margin-bottom: 12px; white-space: pre-wrap; }
.page-bar { margin-top: 12px; display: flex; justify-content: flex-end; }
.sla-bar { display: flex; align-items: center; gap: 18px; margin-bottom: 12px; font-size: 13px; color: #475569; }
.sla-bar b { font-size: 15px; }
.sla-bar .danger { color: #dc2626; }
</style>
