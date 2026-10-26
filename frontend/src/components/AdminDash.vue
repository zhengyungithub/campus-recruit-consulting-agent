<template>
  <div>
    <!-- SoybeanAdmin 风格：渐变图标 + 数值统计卡 -->
    <el-row :gutter="14" style="margin-bottom:16px">
      <el-col :span="4" v-for="s in cards" :key="s.k">
        <el-card shadow="never" class="stat">
          <div class="stat-row">
            <div class="stat-icon" :style="{ background: s.bg }">
              <el-icon :size="20" color="#fff"><component :is="s.icon" /></el-icon>
            </div>
            <div>
              <div class="v">{{ s.v }}</div>
              <div class="k">{{ s.k }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-row :gutter="14">
      <el-col :span="13">
        <el-card shadow="never">
          <h4>候选人环节分布</h4>
          <div class="bar"><div v-for="seg in barSegs" :key="seg.st" :style="{ width: seg.w, background: seg.color }" :title="`${seg.st}: ${seg.n}`" /></div>
          <div class="legend">
            <span v-for="seg in barSegs" :key="seg.st"><i :style="{ background: seg.color }" />{{ seg.st }}({{ seg.n }})</span>
          </div>
        </el-card>
      </el-col>
      <el-col :span="11">
        <el-card shadow="never">
          <h4>高频咨询 TOP 5（机器人未能自动回答）</h4>
          <el-table v-if="stats.top_questions?.length" :data="stats.top_questions" size="small">
            <el-table-column type="index" label="#" width="45" />
            <el-table-column prop="question" label="问题" />
            <el-table-column prop="c" label="次数" width="70" align="right" />
          </el-table>
          <el-empty v-else description="暂无数据 —— 说明机器人目前都答上了 🎉" :image-size="60" />
          <el-alert type="info" :closable="false" style="margin-top:10px"
            title="建议：对高频问题补充 FAQ 或宣讲材料说明" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { User, Collection, ChatDotRound, CircleCheck, MagicStick } from '@element-plus/icons-vue'
import { api, STATUSES } from '../api'

const stats = ref({})
/* 莫兰迪淡色系：雾霾蓝 / 鼠尾草绿 / 杏仁橘 / 浅灰绿 / 干枯紫 */
const COLORS = ['#b3c2cd', '#a8bfa4', '#d8b8a6', '#c4ae9d', '#c9b2c2', '#b5c2b0', '#a3b8c8', '#d8c8b0',
  '#c2ccd2', '#b8a89a', '#cfc0ae', '#c8b6c8', '#aec4bb', '#d2bcb8', '#bfc9d6', '#c5b8d4']

const cards = computed(() => [
  { k: '候选人总数', v: stats.value.total ?? 0, icon: User, bg: 'linear-gradient(135deg,#a3b8c8,#bfced8)' },
  { k: '知识库条目', v: stats.value.faq_count ?? 0, icon: Collection, bg: 'linear-gradient(135deg,#a8bfa4,#c3d4bf)' },
  { k: '待处理反馈', v: stats.value.pending ?? 0, icon: ChatDotRound, bg: 'linear-gradient(135deg,#d8b8a6,#e7cfc0)' },
  { k: '已处理反馈', v: stats.value.resolved ?? 0, icon: CircleCheck, bg: 'linear-gradient(135deg,#aec4bb,#cbdbd2)' },
  { k: '已沉淀FAQ', v: stats.value.reflow ?? 0, icon: MagicStick, bg: 'linear-gradient(135deg,#c9b2c2,#ddcdd7)' }
])
const barSegs = computed(() => {
  const by = stats.value.by_status || {}
  const total = Math.max(stats.value.total || 0, 1)
  return STATUSES.map((st, i) => ({ st, n: by[st] || 0, color: COLORS[i] }))
    .filter(s => s.n > 0)
    .map(s => ({ ...s, w: (s.n / total * 100) + '%' }))
})

onMounted(async () => { stats.value = await api.stats() })
</script>

<style scoped>
.stat :deep(.el-card__body) { padding: 16px; }
.stat-row { display: flex; align-items: center; gap: 12px; }
.stat-icon { width: 44px; height: 44px; border-radius: 12px; flex: none;
  display: flex; align-items: center; justify-content: center; }
.v { font-size: 22px; font-weight: 700; color: #3f4b44; line-height: 1.2; }
.k { font-size: 12px; color: #8a948c; }
.bar { display: flex; height: 10px; border-radius: 99px; overflow: hidden; margin: 12px 0 8px; }
.bar div { height: 100%; transition: width .4s; }
.legend { font-size: 11px; color: #6b7269; display: flex; gap: 10px; flex-wrap: wrap; }
.legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; }
h4 { margin: 0 0 10px; font-size: 14px; color: #3f4b44; }
</style>
