<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>Agent 运行状态</h4>
      <p>
        当前灰度档位：<el-tag :type="modeType">{{ info.mode }}</el-tag>
        <span class="tip">（off=固定管线 / shadow=旁路对比 / on=Agent 接管）</span>
      </p>
      <p>
        语义检索：
        <template v-if="info.retrieval?.dense">
          <el-tag type="success">开启</el-tag>
          <span class="tip"> 知识条目 {{ info.retrieval.kb_points }} · 对话记忆 {{ info.retrieval.chat_points }}</span>
        </template>
        <el-tag v-else type="info">未启用</el-tag>
        <span v-if="!info.retrieval?.dense" class="tip">（未配置 LLM_API_KEY，已降级关键词检索）</span>
      </p>
      <p class="tip">启用方式：在 app/.env 配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL，并设 AGENT_MODE=shadow 或 on 后重启服务。</p>
    </el-card>
    <el-card shadow="never">
      <h4>Agent 轨迹（{{ rows.length }}）</h4>
      <el-table v-if="rows.length" :data="rows" size="small">
        <el-table-column label="编号" width="70"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column prop="question" label="问题" min-width="160" show-overflow-tooltip />
        <el-table-column label="模式" width="80">
          <template #default="{ row }"><el-tag size="small" :type="row.mode === 'on' ? 'success' : 'warning'">{{ row.mode }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Agent 回答" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ (row.answer || '').slice(0, 60) || '—' }}</template>
        </el-table-column>
        <el-table-column label="核验" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="verifyType(row.verify_status)">{{ verifyLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="80"><template #default="{ row }">{{ row.latency_ms }}ms</template></el-table-column>
        <el-table-column prop="created_at" label="时间" width="150" />
      </el-table>
      <el-empty v-else description="暂无 Agent 轨迹 —— 配置 LLM_API_KEY 并将 AGENT_MODE 设为 shadow/on 后产生数据" :image-size="60" />
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'

const info = ref({}), rows = ref([])
const modeType = computed(() => ({ on: 'success', shadow: 'warning', off: 'info' }[info.value.mode] || 'info'))
const verifyLabel = r => ({ pass: '✅ 通过', partial: '⚠️ 部分支撑', fail: '❌ 未经核验', no_source: 'ℹ️ 无引用', none: '—' }[r.verify_status] || r.verify_status)
  + ' ' + ((r.verify_ratio || 0) * 100).toFixed(0) + '%'
const verifyType = s => ({ pass: 'success', partial: 'warning', fail: 'danger', no_source: 'info', none: 'info' }[s] || 'info')

onMounted(async () => { info.value = await api.agentRuns() })
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 14px; }
.tip { color: #64748b; font-size: 12px; }
p { margin: 6px 0; }
</style>
