<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>评测回归</h4>
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="改 prompt / 换模型 / 调意图规则前后各跑一次，用指标判断是变好还是变坏，而不是靠问两句感觉" />
      <div class="bar">
        <el-radio-group v-model="mode" size="small">
          <el-radio-button label="intent">只测意图（最快）</el-radio-button>
          <el-radio-button label="fixed">固定管线</el-radio-button>
          <el-radio-button label="agent">完整 Agent（慢）</el-radio-button>
        </el-radio-group>
        <el-input-number v-model="limit" :min="0" :max="200" size="small" controls-position="right"
                         style="width:120px" />
        <span class="tip">条数（0=全部）</span>
        <el-button type="primary" size="small" :loading="running" @click="doRun">运行评测</el-button>
      </div>
    </el-card>

    <el-card v-if="summary" shadow="never" style="margin-bottom:16px">
      <h4>本轮结果（第 #{{ runId }} 轮 · {{ summary.mode }} · 共 {{ summary.total }} 条）</h4>
      <div class="kpis">
        <div class="kpi"><div class="kpi-v" :class="cls(summary.passed_rate)">{{ pct(summary.passed_rate) }}</div><div class="kpi-k">整体通过率</div></div>
        <div class="kpi"><div class="kpi-v" :class="cls(summary.intent_acc)">{{ pct(summary.intent_acc) }}</div><div class="kpi-k">意图准确率</div></div>
        <div class="kpi"><div class="kpi-v" :class="cls(summary.must_rate)">{{ pct(summary.must_rate) }}</div><div class="kpi-k">关键事实命中</div></div>
        <div class="kpi"><div class="kpi-v" :class="cls(summary.cite_rate)">{{ pct(summary.cite_rate) }}</div><div class="kpi-k">引用率</div></div>
        <div class="kpi"><div class="kpi-v" :class="cls(summary.ticket_acc)">{{ pct(summary.ticket_acc) }}</div><div class="kpi-k">转人工判定</div></div>
        <div class="kpi"><div class="kpi-v" :class="cls(summary.verify_pass_rate)">{{ pct(summary.verify_pass_rate) }}</div><div class="kpi-k">核验通过</div></div>
        <div v-if="summary.mode!=='intent'" class="kpi"><div class="kpi-v" :class="(summary.redline_hits||0) ? 'bad' : 'ok'">{{ summary.redline_hits || 0 }}</div><div class="kpi-k">合规红线拦截</div></div>
        <div v-if="summary.mode!=='intent'" class="kpi"><div class="kpi-v" :class="(summary.judge_bad||0) ? 'warn' : 'ok'">{{ summary.judge_calls || 0 }}</div><div class="kpi-k">模型裁判调用</div></div>
        <div v-if="summary.mode==='agent'" class="kpi"><div class="kpi-v" :class="cls(summary.tool_rate)">{{ pct(summary.tool_rate) }}</div><div class="kpi-k">工具序列命中</div></div>
        <div v-if="summary.mode==='agent'" class="kpi"><div class="kpi-v" :class="summary.degraded ? 'warn' : 'ok'">{{ summary.degraded || 0 }}</div><div class="kpi-k">降级兜底次数</div></div>
        <div class="kpi"><div class="kpi-v">{{ summary.avg_latency_ms }}ms</div><div class="kpi-k">平均耗时</div></div>
        <div class="kpi"><div class="kpi-v">{{ summary.p95_latency_ms }}ms</div><div class="kpi-k">P95 耗时</div></div>
      </div>
      <div class="by-intent">
        <el-tag v-for="(v, k) in summary.per_intent" :key="k" size="small" effect="plain"
                :type="v.ok === v.total ? 'success' : 'warning'" style="margin-right:6px">
          {{ k }} {{ v.ok }}/{{ v.total }}
        </el-tag>
      </div>
    </el-card>

    <el-card v-if="details.length" shadow="never" style="margin-bottom:16px">
      <h4>逐条明细（只看失败）</h4>
      <el-switch v-model="onlyFail" active-text="仅失败" inactive-text="全部" size="small" style="margin-bottom:10px" />
      <el-table :data="shown" size="small" max-height="420">
        <el-table-column prop="q" label="问题" min-width="180" show-overflow-tooltip />
        <el-table-column label="意图" width="150">
          <template #default="{ row }">
            {{ row.actual_intent }}
            <el-tag v-if="!row.intent_ok" type="danger" size="small">期望 {{ row.expect_intent }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="intent_source" label="来源" width="80" />
        <el-table-column label="工具/降级" width="150">
          <template #default="{ row }">
            <span v-if="row.tools">{{ row.tools.join(' → ') || '未调用' }}</span>
            <el-tag v-if="row.degraded" type="warning" size="small" style="margin-left:4px">降级</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="引用" width="60">
          <template #default="{ row }">{{ row.citations }}</template>
        </el-table-column>
        <el-table-column v-if="summary.mode!=='intent'" label="核验/红线" width="170">
          <template #default="{ row }">
            <el-tag size="small" :type="vtype(row.verify)">{{ vlabel(row.verify) }}</el-tag>
            <el-tag v-for="c in (row.redlines || [])" :key="c" type="danger" size="small"
                    style="margin-left:4px">{{ RL[c] || c }}</el-tag>
            <el-tag v-if="row.judge && row.judge !== 'skip'" type="info" size="small"
                    style="margin-left:4px">裁判 {{ row.judge }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="summary.mode==='agent'" label="追问还原/重复" width="150">
          <template #default="{ row }">
            <span v-if="row.resolved" class="why">{{ row.resolved }}</span>
            <el-tag v-else-if="row.repeat" type="warning" size="small">已识别重复提问</el-tag>
            <span v-else class="tip">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="耗时" width="70" />
        <el-table-column label="结果" width="70">
          <template #default="{ row }">
            <el-tag :type="row.passed ? 'success' : 'danger'" size="small">{{ row.passed ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="原因" min-width="200">
          <template #default="{ row }">
            <span class="why">{{ (row.reasons || []).join('；') }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 第5期：线上低分样本回流 -->
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>线上低分样本回流</h4>
      <el-alert type="warning" :closable="false" style="margin-bottom:12px"
        title="核验未过 / 裁判存疑 / 命中红线 / 降级兜底的运行会自动出现在这里。确认是问题后可一键固化为评测用例，下次改动就会被回归覆盖" />
      <div class="bar" style="margin-bottom:10px">
        <el-button size="small" :loading="lowLoading" @click="loadLow">刷新</el-button>
        <span class="tip">共 {{ lowRuns.length }} 条</span>
      </div>
      <el-table :data="lowRuns" size="small" max-height="320" v-loading="lowLoading">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
        <el-table-column label="核验" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="vtype(row.verify_status)">{{ vlabel(row.verify_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="支撑度" width="90">
          <template #default="{ row }">{{ row.verify_ratio === null ? '—' : (row.verify_ratio * 100).toFixed(0) + '%' }}</template>
        </el-table-column>
        <el-table-column label="裁判" width="90">
          <template #default="{ row }">
            <span v-if="row.judge && row.judge !== 'skip'">{{ { ok: '有据', unsupported: '存疑', off_topic: '偏离', error: '异常' }[row.judge] || row.judge }}</span>
            <span v-else class="tip">—</span>
          </template>
        </el-table-column>
        <el-table-column label="红线" width="90">
          <template #default="{ row }">
            <el-tag v-for="c in (row.redlines || [])" :key="c" type="danger" size="small" effect="plain">
              {{ RL[c] || c }}</el-tag>
            <span v-if="!(row.redlines || []).length" class="tip">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="160" />
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button size="small" plain @click="showLow(row)">详情</el-button>
            <el-popconfirm :title="`把这条问题固化为评测用例？标注取自本次运行的实际结果`"
                           @confirm="promote(row)">
              <template #reference>
                <el-button size="small" type="primary">回流</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <el-dialog :model-value="!!lowDetail" @update:model-value="lowDetail = null"
                 title="低分样本详情" width="720px">
        <template v-if="lowDetail">
          <el-descriptions :column="2" size="small" border style="margin-bottom:12px">
            <el-descriptions-item label="运行 #">{{ lowDetail.id }}</el-descriptions-item>
            <el-descriptions-item label="模式">{{ lowDetail.mode }}</el-descriptions-item>
            <el-descriptions-item label="核验">
              <el-tag size="small" :type="vtype(lowDetail.verify_status)">{{ vlabel(lowDetail.verify_status) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="支撑度">
              {{ lowDetail.verify_ratio === null ? '—' : (lowDetail.verify_ratio * 100).toFixed(0) + '%' }}
            </el-descriptions-item>
            <el-descriptions-item label="裁判">
              {{ { ok: '有据', unsupported: '存疑', off_topic: '偏离', error: '异常' }[lowDetail.judge] || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="提示词版本">{{ lowDetail.prompt_version || '—' }}</el-descriptions-item>
            <el-descriptions-item label="工具序列">{{ (lowDetail.tools || []).join(' → ') || '无' }}</el-descriptions-item>
            <el-descriptions-item label="时间">{{ lowDetail.created_at }}</el-descriptions-item>
          </el-descriptions>
          <div class="low-q">Q：{{ lowDetail.question }}</div>
          <div class="low-a">A：{{ lowDetail.answer }}</div>
          <el-alert type="info" :closable="false" style="margin-top:12px"
            title="回流后标注取自本次运行的实际结果，must / must_not 留空由人工补充——自动生成关键词断言会把模型当时的措辞当成标准答案，反而制造假失败" />
        </template>
        <template #footer>
          <el-button @click="lowDetail = null">关闭</el-button>
          <el-button type="primary" @click="promote(lowDetail); lowDetail = null">加入评测集</el-button>
        </template>
      </el-dialog>
    </el-card>

    <el-card shadow="never">
      <h4>历史记录（用于改动前后对比）</h4>
      <el-table :data="runs" size="small" max-height="260">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="mode" label="模式" width="90" />
        <el-table-column label="通过率" width="90">
          <template #default="{ row }">{{ pct(row.metrics.passed_rate) }}</template>
        </el-table-column>
        <el-table-column label="意图" width="80">
          <template #default="{ row }">{{ pct(row.metrics.intent_acc) }}</template>
        </el-table-column>
        <el-table-column label="事实" width="80">
          <template #default="{ row }">{{ pct(row.metrics.must_rate) }}</template>
        </el-table-column>
        <el-table-column label="引用" width="80">
          <template #default="{ row }">{{ pct(row.metrics.cite_rate) }}</template>
        </el-table-column>
        <el-table-column label="平均耗时" width="100">
          <template #default="{ row }">{{ row.metrics.avg_latency_ms }}ms</template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="160" />
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button size="small" plain @click="open(row.id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const mode = ref('intent'), limit = ref(0), running = ref(false)
const summary = ref(null), details = ref([]), runId = ref(null)
const runs = ref([]), onlyFail = ref(true)

const shown = computed(() => onlyFail.value ? details.value.filter(d => !d.passed) : details.value)
const pct = v => v === null || v === undefined ? '—' : (v * 100).toFixed(0) + '%'
const cls = v => v === null || v === undefined ? '' : (v >= 0.9 ? 'ok' : v >= 0.7 ? 'warn' : 'bad')
// 第2期：block = 合规红线拦截；judge = LLM 语义裁判裁定
const RL = { promise_offer: '承诺录用', invent_schedule: '无据排期', privacy: '个人信息', internal_leak: '内部细节' }
const VLABEL = { pass: '核验通过', partial: '部分支撑', fail: '缺乏依据', block: '红线拦截' }
const vlabel = s => VLABEL[s] || (s ? s : '—')
const vtype = s => ({ pass: 'success', partial: 'warning', fail: 'danger', block: 'danger' }[s] || 'info')

async function doRun() {
  running.value = true
  try {
    const r = await api.evalRun(mode.value, limit.value)
    runId.value = r.run_id
    summary.value = r.summary
    details.value = r.details
    await loadRuns()
    const bad = r.details.filter(d => !d.passed).length
    ElMessage[bad ? 'warning' : 'success'](bad ? `完成，${bad} 条未通过` : '全部通过')
  } catch (e) { ElMessage.error(e.message) } finally { running.value = false }
}

async function open(id) {
  try {
    const d = await api.evalRunDetail(id)
    runId.value = d.id; summary.value = d.summary; details.value = d.details
  } catch (e) { ElMessage.error(e.message) }
}
async function loadRuns() {
  try { runs.value = (await api.evalRuns()).runs } catch { /* ignore */ }
}
onMounted(() => { loadRuns(); loadLow() })

// 第5期：线上低分样本
const lowRuns = ref([]), lowLoading = ref(false), lowDetail = ref(null)
async function loadLow() {
  lowLoading.value = true
  try { lowRuns.value = (await api.lowRuns(30)).runs } catch (e) { ElMessage.error(e.message) } finally { lowLoading.value = false }
}
function showLow(row) { lowDetail.value = row }
async function promote(row) {
  try {
    const r = await api.promoteCase(row.id, {})
    ElMessage.success(`已固化为用例 #${r.case.id}（当前共 ${r.total} 条）`)
    await loadLow()
  } catch (e) { ElMessage.error(e.message) }
}
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #3f4b44; letter-spacing: .5px; }
.tip { color: #8a948c; font-size: 12px; }
.bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.kpis { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.kpi { flex: 1; min-width: 100px; background: #f8f7f3; border: 1px solid #ece9e1; border-radius: 10px; padding: 12px 10px; text-align: center; }
.kpi-v { font-size: 20px; font-weight: 600; color: #3f4b44; }
.kpi-v.ok { color: #5b8c6b; } .kpi-v.warn { color: #c98b3b; } .kpi-v.bad { color: #c4826f; }
.kpi-k { font-size: 12px; color: #8a948c; margin-top: 4px; }
.by-intent { margin-top: 4px; }
.why { color: #a2705d; font-size: 12px; }
.low-q { font-size: 13px; font-weight: 600; color: #3f4b44; line-height: 1.7; word-break: break-word; }
.low-a { margin-top: 8px; font-size: 13px; color: #5a6259; line-height: 1.75;
  white-space: pre-wrap; word-break: break-word; background: #f8f7f3;
  border: 1px solid #ece9e1; border-radius: 10px; padding: 10px 12px; }
</style>
