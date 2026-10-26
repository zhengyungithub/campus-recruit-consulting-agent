<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="max-width:560px">
      <h4>我的账号</h4>
      <el-alert type="info" :closable="false" style="margin-bottom:16px"
        :title="`账号 ${username} · ${role === 'super' ? '超级管理员（可配置模型能力与新增 HR 账号）' : 'HR 运营（模型/密钥由超级管理员统一配置，不可见也无需操作）'}${customPwd ? '' : '　⚠️ 当前仍为系统初始密码，建议尽快修改'}`" />
      <el-form label-width="90px" @submit.prevent>
        <el-form-item label="当前密码">
          <el-input v-model="form.old_password" type="password" show-password placeholder="验证身份，必填" />
        </el-form-item>
        <el-form-item label="新用户名">
          <el-input v-model="form.username" placeholder="留空 = 保持不变；3-32 位字母/数字/_/-" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="form.new_password" type="password" show-password placeholder="留空 = 保持不变；6-64 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="form.confirm" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
        <el-button type="primary" :loading="saving" @click="save">保存修改</el-button>
        <span class="tip" style="margin-left:12px">修改后立即生效，本次登录不受影响；下次登录请使用新账号密码</span>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const username = ref(''), customPwd = ref(false), saving = ref(false), role = ref('')
const form = ref({ old_password: '', username: '', new_password: '', confirm: '' })

async function load() {
  try {
    const j = await api.adminAccount()
    username.value = j.username
    customPwd.value = j.custom_password
    role.value = j.role || ''
  } catch (e) { ElMessage.error(e.message) }
}

async function save() {
  const f = form.value
  if (!f.old_password) { ElMessage.error('请输入当前密码以验证身份'); return }
  if (f.new_password && f.new_password !== f.confirm) { ElMessage.error('两次输入的新密码不一致'); return }
  saving.value = true
  try {
    const j = await api.updateAdminAccount({
      old_password: f.old_password,
      username: f.username.trim(),
      new_password: f.new_password
    })
    username.value = j.username
    if (f.new_password) customPwd.value = true
    ElMessage.success('账号信息已更新')
    form.value = { old_password: '', username: '', new_password: '', confirm: '' }
    load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}
onMounted(load)
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #3f4b44; letter-spacing: .5px; }
.tip { color: #8a948c; font-size: 12px; }
</style>
