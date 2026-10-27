<template>
  <div style="padding:16px 4px">
    <el-card shadow="never">
      <h4>操作日志</h4>
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
        title="记录谁在什么时候改了什么。**被拒绝的操作也会记**（红色 denied）——「有人想改但没改成」本身就是要看的信号。" />
      <div class="hd-right" style="margin-bottom:12px">
        <el-select v-model="action" size="small" style="width:190px" @change="load">
          <el-option label="全部动作" value="" />
          <el-option v-for="a in ACTIONS" :key="a.v" :label="a.l" :value="a.v" />
        </el-select>
        <el-button size="small" plain @click="load">刷新</el-button>
      </div>
      <el-table :data="rows" size="small" max-height="560">
        <el-table-column prop="created_at" label="时间" width="160" />
        <el-table-column prop="actor" label="操作人" width="110" />
        <el-table-column label="动作" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ actionLabel(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target" label="对象" width="130" />
        <el-table-column prop="detail" label="详情" min-width="240" show-overflow-tooltip />
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.result === 'ok' ? 'success' : 'danger'">
              {{ row.result === 'ok' ? '成功' : '已拒绝' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!rows.length" class="tip" style="padding:16px 0">暂无操作记录</div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const rows = ref([]), action = ref('')
const ACTIONS = [
  { v: 'status_change', l: '改候选人状态' },
  { v: 'ticket_reply', l: '回复工单' },
  { v: 'ticket_claim', l: '认领工单' },
  { v: 'faq_publish', l: '知识通过审核' },
  { v: 'faq_reject', l: '知识被拒绝' }
]
const actionLabel = a => (ACTIONS.find(x => x.v === a) || {}).l || a

async function load() {
  try {
    const j = await api.audit(200)
    rows.value = action.value ? (j.logs || []).filter(l => l.action === action.value) : (j.logs || [])
  } catch (e) { ElMessage.error(e.message) }
}
onMounted(load)
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 14px; }
.tip { color: #64748b; font-size: 12px; }
.hd-right { display: flex; align-items: center; gap: 10px; }
</style>
