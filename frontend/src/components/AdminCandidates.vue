<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>批量导入候选人</h4>
      <el-input v-model="importText" type="textarea" :rows="4"
        placeholder="每行：姓名,手机号,学校,专业,岗位,站点&#10;示例：赵云,13800000006,华中科技大学,网络安全,后端开发,武汉站" />
      <div class="import-bar">
        <el-button type="primary" @click="doImport">导入</el-button>
        <el-button plain @click="downloadTemplate">
          <el-icon style="margin-right:5px"><Download /></el-icon>下载导入模板
        </el-button>
        <el-upload :show-file-list="false" accept=".csv,.xlsx,.xls,.txt"
                   :auto-upload="false" :on-change="onFile">
          <el-button plain type="primary">
            <el-icon style="margin-right:5px"><Upload /></el-icon>上传文件导入（CSV / Excel）
          </el-button>
        </el-upload>
        <span class="tip">下载模板后按固定字段（姓名,手机号,学校,专业,岗位,站点）粘贴学生信息，即可直接导入；手机号重复或格式错误将自动跳过，新导入候选人初始状态为"已初筛通过"</span>
      </div>
    </el-card>
    <el-card shadow="never">
      <h4>候选人列表（{{ rows.length }}）—— 修改环节状态后候选人端实时可见</h4>
      <el-table :data="paged" size="small">
        <el-table-column prop="name" label="姓名" width="90" />
        <el-table-column prop="phone" label="手机号" width="120" />
        <el-table-column prop="school" label="学校" min-width="140" show-overflow-tooltip />
        <el-table-column prop="major" label="专业" min-width="110" show-overflow-tooltip />
        <el-table-column prop="position" label="岗位" width="110" />
        <el-table-column prop="site" label="站点" width="90" />
        <el-table-column label="当前环节" width="170">
          <template #default="{ row }">
            <el-select v-model="row.status" size="small" @change="v => upd(row, v)">
              <el-option v-for="s in STATUSES" :key="s" :label="s" :value="s" />
            </el-select>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, Download } from '@element-plus/icons-vue'
import * as XLSX from 'xlsx'
import { api, STATUSES } from '../api'

const rows = ref([]), importText = ref('')
const page = ref(1), size = ref(10)
const paged = computed(() => rows.value.slice((page.value - 1) * size.value, page.value * size.value))

async function load() { rows.value = (await api.adminCandidates()).candidates }

// ---- 下载固定模板：表头字段与候选人列表展示字段一致 ----
const TEMPLATE_HEADERS = ['姓名', '手机号', '学校', '专业', '岗位', '站点']
function downloadTemplate() {
  const wb = XLSX.utils.book_new()
  const ws = XLSX.utils.aoa_to_sheet([TEMPLATE_HEADERS])
  ws['!cols'] = [{ wch: 10 }, { wch: 14 }, { wch: 22 }, { wch: 18 }, { wch: 14 }, { wch: 10 }]
  XLSX.utils.book_append_sheet(wb, ws, '候选人名单')
  const tip = XLSX.utils.aoa_to_sheet([
    ['填写说明'],
    ['1. 请在「候选人名单」工作表中，从第 2 行开始逐行粘贴学生信息，不要修改表头或调整列顺序。'],
    ['2. 固定字段顺序：姓名、手机号、学校、专业、岗位、站点（与候选人列表展示字段一致）。'],
    ['3. 每行示例：赵云,13800000006,华中科技大学,计算机科学与技术,后端开发,武汉站'],
    ['4. 手机号重复或格式错误的行将自动跳过；新导入候选人的初始状态为「已初筛通过」。'],
    ['5. 填好后保存为 .xlsx（或另存为 .csv），回到「候选人管理」页点击「上传文件导入」选择该文件即可。'],
  ])
  tip['!cols'] = [{ wch: 100 }]
  XLSX.utils.book_append_sheet(wb, tip, '填写说明')
  XLSX.writeFile(wb, '候选人导入模板.xlsx')
  ElMessage.success('模板已下载，请按「填写说明」粘贴学生信息')
}

async function importItems(items) {
  try {
    const j = await api.importCandidates(items)
    ElMessage.success(`导入成功 ${j.added} 人，跳过 ${j.skipped} 条`)
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function doImport() {
  const items = importText.value.trim().split('\n').filter(Boolean).map(l => {
    const [name, phone, school = '', major = '', position = '', site = ''] = l.split(/[,，\t]/).map(s => s.trim())
    return { name, phone, school, major, position, site }
  })
  await importItems(items)
  importText.value = ''
}

// ---- 文件导入：CSV / XLSX 统一走 SheetJS，自动识别表头 ----
const HEADER_ALIASES = {
  name: ['姓名', '名字', 'name'],
  phone: ['手机号', '手机', '电话', '联系电话', 'phone', 'mobile'],
  school: ['学校', '院校', '毕业院校', '大学', 'school'],
  major: ['专业', 'major'],
  position: ['岗位', '应聘岗位', '职位', 'position'],
  site: ['站点', '城市', '考点', 'site'],
}

function mapRow(obj) {
  const out = { name: '', phone: '', school: '', major: '', position: '', site: '' }
  for (const k in obj) {
    const key = String(k).trim().toLowerCase()
    for (const [field, aliases] of Object.entries(HEADER_ALIASES)) {
      if (aliases.some(a => a.toLowerCase() === key)) { out[field] = String(obj[k] ?? '').trim(); break }
    }
  }
  return out
}

async function onFile(file) {
  try {
    const wb = XLSX.read(await file.raw.arrayBuffer(), { type: 'array', codepage: 936 })
    const grid = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1, defval: '' })
    if (!grid.length) { ElMessage.warning('文件为空'); return }
    const header = grid[0].map(c => String(c).trim())
    const known = Object.values(HEADER_ALIASES).flat().map(a => a.toLowerCase())
    const hasHeader = header.some(c => known.includes(c.toLowerCase()))
    let items
    if (hasHeader) {
      items = grid.slice(1).map(r => {
        const o = {}
        header.forEach((h, i) => { if (h) o[h] = r[i] })
        return mapRow(o)
      })
    } else {
      // 无表头：按固定列序 姓名,手机号,学校,专业,岗位,站点
      items = grid.map(r => ({
        name: String(r[0] ?? '').trim(), phone: String(r[1] ?? '').trim(),
        school: String(r[2] ?? '').trim(), major: String(r[3] ?? '').trim(),
        position: String(r[4] ?? '').trim(), site: String(r[5] ?? '').trim(),
      }))
    }
    const valid = items.filter(i => i.name && i.phone)
    if (!valid.length) { ElMessage.warning('未解析到有效数据（需包含姓名和手机号）'); return }
    await importItems(valid)
    ElMessage.info(`共解析 ${valid.length} 条有效记录`)
  } catch (e) { ElMessage.error('文件解析失败：' + e.message) }
}

// S6.1：状态变更要过流转校验。跨环节跳步与回退都必须填原因，
// 否则后端直接 400——前端要提前把原因问出来，不能让 HR 撞上报错。
const flowRule = ref({ order: [], jump_threshold: 2 })

function needReason(cur, nxt) {
  const a = flowRule.value.order.indexOf(cur)
  const b = flowRule.value.order.indexOf(nxt)
  if (a < 0 || b < 0) return true      // 未知状态：一律留痕
  if (a === b) return false
  if (b < a) return true               // 回退一定有原因（资料打回、重新约面…）
  return (b - a) >= (flowRule.value.jump_threshold || 2)
}

async function upd(row, status) {
  if (row.status === status) return
  let reason = ''
  if (needReason(row.status, status)) {
    try {
      const { value } = await ElMessageBox.prompt(
        `从「${row.status}」变更为「${status}」属于${status && flowRule.value.order.indexOf(status) < flowRule.value.order.indexOf(row.status) ? '回退' : '跨环节跳步'}，请填写原因（会记入操作日志）。`,
        '需要变更原因',
        { inputPattern: /\S+/, inputErrorMessage: '原因不能为空', inputPlaceholder: '如：破格免测评 / 资料不合格需重传' })
      reason = (value || '').trim()
    } catch {
      load()                            // 取消填写：把下拉回滚到服务端真实值
      return
    }
  }
  try {
    await api.updateStatus(row.id, status, '', reason)
    ElMessage.success(`已更新「${row.name}」当前环节：${status}`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
    load()  // 失败回滚：从服务端重载真实状态
  }
}

async function loadFlow() {
  try { flowRule.value = await api.flow() } catch { /* 规则拉不到就退化成"总填原因" */ }
}
onMounted(() => { load(); loadFlow() })
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #3f4b44; letter-spacing: .5px; }
.tip { color: #8a948c; font-size: 12px; }
.import-bar { margin-top: 10px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.import-bar :deep(.el-upload) { margin-right: 0; }
.page-bar { margin-top: 12px; display: flex; justify-content: flex-end; }
</style>
