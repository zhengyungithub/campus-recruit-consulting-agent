<template>
  <div style="padding:16px 4px; max-width:720px">
    <el-alert type="warning" :closable="false" style="margin-bottom:16px"
      title="模型能力由超级管理员统一配置（超管权限）" />
    <el-alert type="info" :closable="false" style="margin-bottom:16px"
      title="全系统共用一套模型能力：超管配置完成后，候选人端的 AI 问答、知识库向量检索、Agent 灰度全部按此配置运行；HR 运营账号与普通候选人不接触任何模型 / Key 相关信息。" />
    <el-alert type="success" :closable="false" style="margin-bottom:16px"
      title="保存后立即生效，无需重启服务；留空的密钥表示保持现有配置不变" />

    <el-card shadow="never" style="margin-bottom:16px">
      <h4>Agent 档位</h4>
      <el-radio-group v-model="form.agent_mode">
        <el-radio-button value="off">off · 固定管线</el-radio-button>
        <el-radio-button value="shadow">shadow · 旁路对比</el-radio-button>
        <el-radio-button value="on">on · Agent 接管</el-radio-button>
      </el-radio-group>
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <h4>LLM 配置</h4>
      <el-form label-width="130px" label-position="left">
        <el-form-item label="LLM Base URL">
          <el-input v-model="form.llm_base_url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item label="LLM API Key">
          <el-input v-model="form.llm_api_key" type="password" show-password
                    placeholder="输入新的 LLM API Key" />
          <div class="cfg-tip">
            已配置：
            <el-tag :type="loaded.has_llm_key ? 'success' : 'danger'" size="small">
              {{ loaded.has_llm_key ? '是' : '否' }}
            </el-tag>
          </div>
        </el-form-item>
        <el-form-item label="LLM 模型">
          <el-input v-model="form.llm_model" placeholder="qwen-plus" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never" style="margin-bottom:16px">
      <h4>对话限流</h4>
      <el-form label-width="130px" label-position="left">
        <el-form-item label="每人每小时">
          <el-input-number v-model="form.rate_limit_per_hour" :min="0" :max="1000" />
          <div class="cfg-tip">
            单个候选人 1 小时滑动窗口内允许的提问次数；<b>0 表示不限流</b>。
            超限仍会回答（改走知识库固定管线），并提示稍后再试。
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <h4>Embedding 配置</h4>
      <el-form label-width="130px" label-position="left">
        <el-form-item label="Embedding Key">
          <el-input v-model="form.embed_api_key" type="password" show-password
                    placeholder="留空则使用 LLM API Key" />
          <div class="cfg-tip">
            已配置：
            <el-tag :type="loaded.has_embed_key ? 'success' : 'danger'" size="small">
              {{ loaded.has_embed_key ? '是' : '否' }}
            </el-tag>
          </div>
        </el-form-item>
        <el-form-item label="Embedding 模型">
          <el-input v-model="form.embed_model" placeholder="text-embedding-v3" />
          <div class="cfg-tip">⚠️ 更换模型后需重建向量索引（保存时自动执行，对话向量记忆将清空）</div>
        </el-form-item>
      </el-form>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </el-card>

    <el-card shadow="never" style="margin-top:16px">
      <h4>运维 · 检查点清理</h4>
      <el-form label-width="130px" label-position="left">
        <el-form-item label="保留天数">
          <el-input-number v-model="form.ckpt_retention_days" :min="0" :max="365" />
          <div class="cfg-tip">
            每次服务启动时自动回收超过该天数的会话检查点；<b>0 表示不清理</b>。
            按「整条会话线程」删而不是按单条检查点删——检查点是一条父子链，
            只删中间节点会让剩下的指向已不存在的父节点。
          </div>
        </el-form-item>
        <el-form-item label="当前占用">
          <span v-if="ckpt.exists">
            <el-tag size="small" type="info">{{ (ckpt.size / 1048576).toFixed(1) }} MB</el-tag>
            <span class="cfg-tip" style="display:inline;margin-left:8px">
              {{ ckpt.threads }} 个会话 / {{ ckpt.checkpoints }} 条检查点
            </span>
          </span>
          <span v-else class="cfg-tip">暂无检查点文件</span>
          <el-button size="small" plain style="margin-left:10px" :loading="pruning" @click="prune">
            立即清理
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const form = reactive({
  agent_mode: 'off', llm_base_url: '', llm_api_key: '', llm_model: '',
  embed_api_key: '', embed_model: '', rate_limit_per_hour: 30, ckpt_retention_days: 7
})
const loaded = ref({ has_llm_key: false, has_embed_key: false })
const saving = ref(false)
// S2.3 运维
const ckpt = ref({ exists: false, size: 0, threads: 0, checkpoints: 0 })
const pruning = ref(false)

async function load() {
  try {
    const s = await api.adminSettings()
    loaded.value = s
    form.agent_mode = s.agent_mode || 'off'
    form.llm_base_url = s.llm_base_url || ''
    form.llm_model = s.llm_model || ''
    form.embed_model = s.embed_model || ''
    form.rate_limit_per_hour = s.rate_limit_per_hour ?? 30
    form.ckpt_retention_days = s.ckpt_retention_days ?? 7
  } catch (e) { ElMessage.error(e.message) }
  try { ckpt.value = await api.ckptStats() } catch (e) { /* 非超管忽略 */ }
}

async function prune() {
  pruning.value = true
  try {
    const r = await api.ckptPrune(form.ckpt_retention_days)
    ElMessage.success(`已清理 ${r.threads} 个会话 / ${r.deleted} 条，`
      + `${(r.size_before / 1048576).toFixed(1)}MB → ${(r.size_after / 1048576).toFixed(1)}MB`)
    ckpt.value = await api.ckptStats()
  } catch (e) { ElMessage.error(e.message) }
  finally { pruning.value = false }
}

async function save() {
  saving.value = true
  try {
    const r = await api.saveSettings({ ...form })
    ElMessage.success('配置已保存并生效' + (r.embed_reindexed ? '，向量索引已重建' : ''))
    form.llm_api_key = ''; form.embed_api_key = ''   // 密钥不回显，清空输入框
    load()
  } catch (e) { ElMessage.error(e.message) }
  saving.value = false
}

onMounted(load)
</script>

<style scoped>
h4 { margin: 0 0 12px; font-size: 14px; }
.cfg-tip { font-size: 12px; color: #8a948c; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
</style>
