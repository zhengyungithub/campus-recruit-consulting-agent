<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>新增 FAQ</h4>
      <div style="display:flex;gap:10px;margin-bottom:10px">
        <el-input v-model="form.question" placeholder="问题（候选人可能的问法）" style="flex:2" />
        <el-select v-model="form.category" style="width:110px">
          <el-option value="通用" /><el-option value="回流" /><el-option value="内部" />
        </el-select>
      </div>
      <el-input v-model="form.answer" type="textarea" :rows="3" placeholder="答案" />
      <div style="margin-top:10px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <el-button type="primary" @click="add">保存</el-button>
        <el-upload :show-file-list="false" accept=".txt,.md,.csv,.docx,.pdf"
                   :auto-upload="false" :on-change="onFile" :disabled="parsing">
          <el-button plain type="primary" :loading="parsing">
            <el-icon v-if="!parsing" style="margin-right:5px"><Document /></el-icon>
            {{ parsing ? '解析中，请稍候…' : '导入文档解析（Word / PDF / TXT）' }}
          </el-button>
        </el-upload>
        <span class="tip">上传后由 LLM 抽取问答对，确认后批量入库；单文件 ≤ 10MB</span>
      </div>
    </el-card>
    <el-card shadow="never">
      <div class="hd">
        <h4>知识库（{{ rows.length }}）</h4>
        <div class="hd-right">
          <!-- S6.1：草稿来源（文档导入 / 工单回流）需审核后才对机器人可见 -->
          <el-radio-group v-model="reviewFilter" size="small" @change="load">
            <el-radio-button label="全部" value="" />
            <el-radio-button :label="`待审核（${reviewStats.draft || 0}）`" value="draft" />
            <el-radio-button label="已发布" value="published" />
          </el-radio-group>
          <el-select v-model="srcFilter" size="small" style="width:190px" @change="load">
            <el-option label="全部来源" value="" />
            <el-option v-for="s in sources" :key="s.source"
                       :label="`${srcLabel(s.source)}（${s.count}）`" :value="s.source" />
          </el-select>
          <el-button size="small" type="danger" plain :disabled="!srcFilter || srcFilter === 'manual'"
                     @click="rollback">回滚该来源</el-button>
        </div>
      </div>
      <el-table :data="paged" size="small">
        <el-table-column prop="question" label="问题" width="280" show-overflow-tooltip />
        <el-table-column prop="answer" label="答案" show-overflow-tooltip />
        <el-table-column label="分类" width="100">
          <template #default="{ row }">
            {{ row.category }}
            <el-tag v-if="row.category === '回流'" type="success" size="small">回流</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.source === 'manual' ? 'info' : 'success'">
              {{ srcLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.review_status === 'draft' ? 'warning' : 'success'">
              {{ row.review_status === 'draft' ? '待审核' : '已发布' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <template v-if="row.review_status === 'draft'">
              <el-button size="small" type="success" plain @click="reviewFaqRow(row, 'publish')">通过</el-button>
              <el-button size="small" type="danger" plain @click="reviewFaqRow(row, 'reject')">拒绝</el-button>
            </template>
            <el-button size="small" plain @click="edit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="del(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="page-bar">
        <el-pagination v-model:current-page="page" v-model:page-size="size"
          :total="rows.length" :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next" background small />
      </div>
    </el-card>

    <el-dialog v-model="dlg" title="编辑 FAQ" width="560">
      <el-input v-model="editing.question" placeholder="问题" style="margin-bottom:10px" />
      <el-input v-model="editing.answer" type="textarea" :rows="4" placeholder="答案" />
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 文档解析结果确认 -->
    <el-dialog v-model="impDlg" width="860" top="4vh" :close-on-click-modal="false">
      <template #header>
        <b>文档解析结果确认</b>
        <span class="tip" style="margin-left:10px">
          {{ impMeta.filename }} · 解析 {{ impMeta.chars }} 字 · 抽取 {{ proposals.length }} 条问答
        </span>
      </template>
      <el-alert v-if="impMeta.failed_chunks" type="warning" :closable="false" style="margin-bottom:10px"
                :title="`有 ${impMeta.failed_chunks} 个片段解析失败，可重试或手动补充`" />
      <el-alert v-if="dupHits.length" type="warning" :closable="false" style="margin-bottom:10px">
        <template #title>
          <div>{{ dupHits.length }} 条与已有知识重复，已跳过入库：</div>
          <div v-for="(d, i) in dupHits" :key="i" class="tip" style="margin-top:2px">
            · {{ d.question }} —— {{ d.msg }}
          </div>
        </template>
      </el-alert>
      <el-table ref="impTable" :data="proposals" size="small" max-height="440"
                @selection-change="s => sel = s">
        <el-table-column type="selection" width="42" />
        <el-table-column label="问题" min-width="220">
          <template #default="{ row }"><el-input v-model="row.question" size="small" /></template>
        </el-table-column>
        <el-table-column label="答案" min-width="320">
          <template #default="{ row }"><el-input v-model="row.answer" size="small" type="textarea" :rows="2" /></template>
        </el-table-column>
        <el-table-column label="分类" width="100">
          <template #default="{ row }">
            <el-select v-model="row.category" size="small">
              <el-option value="通用" /><el-option value="回流" /><el-option value="内部" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ $index }">
            <el-button size="small" type="danger" plain @click="proposals.splice($index, 1)">删</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="impDlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!sel.length" @click="confirmImport">
          入库所选（{{ sel.length }} 条）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document } from '@element-plus/icons-vue'
import { api } from '../api'

const rows = ref([])
const page = ref(1), size = ref(10)
const paged = computed(() => rows.value.slice((page.value - 1) * size.value, page.value * size.value))
const form = ref({ question: '', answer: '', category: '通用' })
const dlg = ref(false), editing = ref({})
const parsing = ref(false), impDlg = ref(false), saving = ref(false)
const proposals = ref([]), sel = ref([]), impMeta = ref({})
// S2.2 来源筛选与回滚
const sources = ref([]), srcFilter = ref(''), forceAdd = ref(false)
// S6.1 知识库审核：草稿对外不可见，通过后才会被检索到
const reviewFilter = ref(''), reviewStats = ref({})

async function reviewFaqRow(row, action) {
  try {
    await api.reviewFaq(row.id, action)
    ElMessage.success(action === 'publish' ? '已通过，该条目对机器人可见' : '已拒绝')
    load()
  } catch (e) { ElMessage.error(e.message) }
}
const dupHits = computed(() => proposals.value
  .filter(p => p._dup && p._dup.level !== 'ok')
  .map(p => ({ question: p.question, msg: p._dup.message || '已有相似条目' })))

async function load() {
  const j = await api.faqs(reviewFilter.value, srcFilter.value)
  rows.value = j.faqs; sources.value = j.sources || []; reviewStats.value = j.review || {}
}
async function add() {
  // S2.1：命中重复/相近条目时不直接入库，先让 HR 确认（force=1 再提一次即强制入库）
  try {
    const j = await api.createFaq({ ...form.value, source: 'manual', force: forceAdd.value ? 1 : 0 })
    if (j.skipped) {
      forceAdd.value = true
      ElMessage.warning((j.dedupe?.message || '已有相似条目') + '，确认无误请再点一次「保存」强制入库')
      return
    }
    form.value = { question: '', answer: '', category: '通用' }; forceAdd.value = false
    ElMessage.success('已保存'); load()
  } catch (e) { ElMessage.error(e.message) }
}

function srcLabel(s) {
  if (!s || s === 'manual') return '手工录入'
  if (s === 'kb_seed') return '内置种子'
  if (s === 'doc_import') return '文档导入'
  if (s.startsWith('ticket#')) return '工单 ' + s.slice(7)
  return s
}
async function rollback() {
  const n = (sources.value.find(s => s.source === srcFilter.value) || {}).count || 0
  await ElMessageBox.confirm(
    `将删除「${srcLabel(srcFilter.value)}」来源的全部 ${n} 条知识条目，且不可恢复。确认继续？`,
    '按来源回滚', { type: 'warning' })
  const j = await api.deleteFaqsBySource(srcFilter.value)
  ElMessage.success(`已回滚 ${j.deleted} 条`)
  srcFilter.value = ''; load()
}
function edit(row) { editing.value = { ...row }; dlg.value = true }
async function save() {
  try { await api.updateFaq(editing.value.id, editing.value); dlg.value = false; ElMessage.success('已更新'); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function del(row) {
  await ElMessageBox.confirm(`确认删除「${row.question}」？`, '提示', { type: 'warning' })
  await api.deleteFaq(row.id); ElMessage.success('已删除'); load()
}

// ---- 文档导入解析 ----
async function onFile(file) {
  parsing.value = true
  ElMessage.info(`正在解析「${file.name}」，由 LLM 抽取问答对，可能需要十几秒…`)
  try {
    const j = await api.importDoc(file.raw)
    proposals.value = j.items
    impMeta.value = { filename: j.filename, chars: j.chars, failed_chunks: j.failed_chunks || 0 }
    sel.value = j.items
    impDlg.value = true
  } catch (e) { ElMessage.error(e.message) }
  finally { parsing.value = false }
}
async function confirmImport() {
  saving.value = true
  try {
    const j = await api.bulkFaqs(sel.value, 'doc_import')
    const skipped = j.skipped || []
    if (skipped.length) {
      // 被判重复的留在列表里，HR 可改写问题表述后再入库
      const kept = skipped.map(s => {
        const src = sel.value.find(x => x.question === s.question) || {}
        return { ...src, _dup: s.dedupe }
      })
      proposals.value = kept
      sel.value = kept
      ElMessage.warning(
        `已入库 ${j.added} 条；${skipped.length} 条与已有知识重复已跳过，可改写后重试`)
      load()
      return
    }
    ElMessage.success(`已入库 ${j.added} 条，向量索引已同步`)
    impDlg.value = false
    load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}
onMounted(load)
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #3f4b44; letter-spacing: .5px; }
.tip { color: #8a948c; font-size: 12px; }
.page-bar { margin-top: 12px; display: flex; justify-content: flex-end; }
.hd { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.hd-right { display: flex; align-items: center; gap: 8px; }
</style>
