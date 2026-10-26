<template>
  <div style="padding:16px 4px">
    <el-card shadow="never" style="margin-bottom:16px">
      <h4>新建运营账号</h4>
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="超级管理员维护全系统的账号与模型能力；HR 运营账号只处理候选人、知识库、工单业务，看不到也不会操作模型配置。" />
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="用户名"><el-input v-model="form.username" placeholder="3-32 位" style="width:160px" /></el-form-item>
        <el-form-item label="初始密码"><el-input v-model="form.password" type="password" show-password
                                                placeholder="6-64 位" style="width:180px" /></el-form-item>
        <el-form-item label="姓名/备注"><el-input v-model="form.display_name" placeholder="如：招聘组-小林" style="width:170px" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width:150px">
            <el-option label="HR 运营" value="hr" />
            <el-option label="超级管理员" value="super" />
          </el-select>
        </el-form-item>
        <el-form-item><el-button type="primary" @click="create">新建账号</el-button></el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <h4>账号列表（{{ rows.length }}）</h4>
      <el-table :data="paged" size="small">
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column prop="display_name" label="姓名/备注" width="170" show-overflow-tooltip />
        <el-table-column label="角色" width="130">
          <template #default="{ row }">
            <el-tag :type="row.role === 'super' ? 'danger' : 'info'" size="small" effect="plain">
              {{ row.role === 'super' ? '超级管理员' : 'HR 运营' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
              {{ row.status === 'active' ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="权限范围" min-width="180">
          <template #default="{ row }">
            {{ row.role === 'super' ? '全部（含模型配置）' : '候选人/知识库/工单' }}
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170" />
        <el-table-column label="最近登录" width="170">
          <template #default="{ row }">{{ row.last_login_at || '从未登录' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="210">
          <template #default="{ row }">
            <el-button size="small" plain @click="edit(row)">改密/改名</el-button>
            <el-button size="small" :type="row.status === 'active' ? 'warning' : 'success'" plain @click="toggle(row)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
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

    <el-dialog v-model="dlg" :title="`编辑账号 · ${cur?.username}`" width="460">
      <el-form label-width="100px" @submit.prevent>
        <el-form-item label="用户名"><el-input v-model="editing.username" /></el-form-item>
        <el-form-item label="姓名/备注"><el-input v-model="editing.display_name" /></el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="editing.password" type="password" show-password placeholder="留空 = 不修改密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const rows = ref([])
const page = ref(1), size = ref(10)
const paged = computed(() => rows.value.slice((page.value - 1) * size.value, page.value * size.value))
const form = ref({ username: '', password: '', display_name: '', role: 'hr' })
const dlg = ref(false), cur = ref(null), editing = ref({})

async function load() { rows.value = (await api.adminAccounts()).accounts }

async function create() {
  const f = form.value
  if (!f.username || !f.password) { ElMessage.error('请填写用户名和密码'); return }
  try {
    await api.createAccount({ ...f, username: f.username.trim(), display_name: f.display_name.trim() })
    ElMessage.success('账号已创建')
    form.value = { username: '', password: '', display_name: '', role: 'hr' }
    load()
  } catch (e) { ElMessage.error(e.message) }
}

function edit(row) {
  cur.value = row
  editing.value = { username: row.username, display_name: row.display_name || '', password: '' }
  dlg.value = true
}
async function save() {
  try {
    await api.patchAccount(cur.value.id, {
      username: editing.value.username.trim(),
      display_name: editing.value.display_name.trim(),
      ...(editing.value.password ? { password: editing.value.password } : {})
    })
    ElMessage.success('已保存'); dlg.value = false; load()
  } catch (e) { ElMessage.error(e.message) }
}
async function toggle(row) {
  try {
    await api.patchAccount(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
    ElMessage.success(row.status === 'active' ? '已停用该账号' : '已启用该账号'); load()
  } catch (e) { ElMessage.error(e.message) }
}
async function del(row) {
  await ElMessageBox.confirm(`确认删除账号「${row.username}」？删除后该账号立即无法登录。`, '提示', { type: 'warning' })
  try { await api.deleteAccount(row.id); ElMessage.success('已删除'); load() }
  catch (e) { ElMessage.error(e.message) }
}
onMounted(load)
</script>

<style scoped>
h4 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #3f4b44; letter-spacing: .5px; }
.el-form-item { margin-bottom: 8px; }
.page-bar { margin-top: 12px; display: flex; justify-content: flex-end; }
</style>
