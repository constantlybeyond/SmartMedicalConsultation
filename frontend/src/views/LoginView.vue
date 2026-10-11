<!--
  ============================================================
  LoginView.vue —— 登录 / 注册页（讲义 §7 用户认证）
  - 未登录时的唯一入口：登录（默认）/ 注册两个模式切换
  - 登录走 OAuth2 密码模式（表单编码），成功返回 JWT
  - 成功后 emit('authed', { token, user })，由 App.vue 保存并切入聊天页
  ============================================================
-->
<script setup>
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { login, register } from '@/api/index.js'

const emit = defineEmits(['authed'])

const mode = ref('login') // 'login' | 'register'
const loading = ref(false)
const form = ref({ username: '', password: '', confirm: '' })

const title = computed(() => (mode.value === 'login' ? '欢迎回来' : '创建账号'))
const submitText = computed(() => (mode.value === 'login' ? '登 录' : '注 册'))

function switchMode(m) {
  if (loading.value) return
  mode.value = m
}

async function submit() {
  const { username, password, confirm } = form.value
  if (!username.trim() || !password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  if (mode.value === 'register') {
    if (username.trim().length < 3) {
      ElMessage.warning('用户名至少 3 个字符')
      return
    }
    if (password.length < 8) {
      ElMessage.warning('密码至少 8 位')
      return
    }
    if (password !== confirm) {
      ElMessage.warning('两次输入的密码不一致')
      return
    }
  }

  loading.value = true
  try {
    if (mode.value === 'register') {
      await register(username.trim(), password)
      ElMessage.success('注册成功，请登录')
      mode.value = 'login'
      form.value.password = ''
      form.value.confirm = ''
    } else {
      const { data } = await login(username.trim(), password)
      emit('authed', { token: data.access_token, user: data.user })
    }
  } catch (err) {
    const detail = err?.response?.data?.detail
    if (err?.response?.status === 401) ElMessage.error(detail || '用户名或密码错误')
    else if (err?.response?.status === 409) ElMessage.error(detail || '用户名已存在')
    else if (err?.response?.status === 422) ElMessage.error('输入格式不符合要求（用户名 3-32 位，密码 8-64 位）')
    else ElMessage.error('网络异常，请确认后端已启动')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-shell">
    <!-- 环境氛围层（与聊天页同款） -->
    <div class="ambient" aria-hidden="true">
      <span class="glow glow-a"></span>
      <span class="glow glow-b"></span>
      <span class="cross-pattern"></span>
    </div>

    <div class="login-card">
      <div class="login-brand">
        <span class="brand-mark"><el-icon :size="26"><FirstAidKit /></el-icon></span>
        <div>
          <h1 class="brand-name">智慧问诊Agent系统</h1>
          <p class="brand-sub">智能医疗 · 知识图谱驱动</p>
        </div>
      </div>

      <div class="mode-switch">
        <button :class="{ active: mode === 'login' }" @click="switchMode('login')">登录</button>
        <button :class="{ active: mode === 'register' }" @click="switchMode('register')">注册</button>
      </div>

      <h2 class="login-title">{{ title }}</h2>

      <el-form class="login-form" @submit.prevent="submit">
        <el-input
          v-model="form.username"
          size="large"
          placeholder="用户名（3-32 位字母 / 数字 / 下划线 / 中文）"
          clearable
          @keyup.enter="submit"
        >
          <template #prefix><el-icon><User /></el-icon></template>
        </el-input>

        <el-input
          v-model="form.password"
          size="large"
          type="password"
          placeholder="密码（8-64 位）"
          show-password
          @keyup.enter="submit"
        >
          <template #prefix><el-icon><Lock /></el-icon></template>
        </el-input>

        <el-input
          v-if="mode === 'register'"
          v-model="form.confirm"
          size="large"
          type="password"
          placeholder="确认密码"
          show-password
          @keyup.enter="submit"
        >
          <template #prefix><el-icon><Lock /></el-icon></template>
        </el-input>

        <el-button
          class="submit-btn"
          type="primary"
          size="large"
          :loading="loading"
          @click="submit"
        >
          {{ submitText }}
        </el-button>
      </el-form>

      <p class="login-note">
        登录即表示同意：本系统建议仅供参考，不能替代专业医疗诊断。
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-shell {
  position: relative; height: 100%; display: flex;
  align-items: center; justify-content: center; overflow: hidden;
}

/* 环境氛围（与 ChatView 一致） */
.ambient { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
.glow { position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.5; }
.glow-a { width: 520px; height: 520px; top: -180px; left: -140px;
  background: radial-gradient(circle, rgba(20, 184, 166, 0.32), transparent 70%);
  animation: drift 18s ease-in-out infinite alternate; }
.glow-b { width: 460px; height: 460px; bottom: -160px; right: -120px;
  background: radial-gradient(circle, rgba(232, 137, 107, 0.26), transparent 70%);
  animation: drift 22s ease-in-out infinite alternate-reverse; }
.cross-pattern { position: absolute; inset: 0;
  background-image: linear-gradient(rgba(15,118,110,.05) 2px, transparent 2px),
    linear-gradient(90deg, rgba(15,118,110,.05) 2px, transparent 2px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse at 50% 0%, black 0%, transparent 80%); }
@keyframes drift { from { transform: translate(0,0) scale(1); } to { transform: translate(60px,40px) scale(1.12); } }

.login-card {
  position: relative; z-index: 1; width: 400px; max-width: calc(100vw - 40px);
  padding: 34px 32px 26px; border-radius: 22px;
  background: rgba(255, 255, 255, 0.88); backdrop-filter: blur(12px);
  border: 1px solid rgba(15, 118, 110, 0.14);
  box-shadow: 0 18px 48px rgba(11, 61, 58, 0.14);
  animation: rise 0.5s ease both;
}
@keyframes rise { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }

.login-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
.brand-mark { display: grid; place-items: center; width: 50px; height: 50px;
  border-radius: 15px; color: #fff;
  background: linear-gradient(145deg, var(--teal-600), var(--teal-900));
  box-shadow: 0 6px 16px rgba(11, 61, 58, 0.28); }
.brand-name { font-family: var(--font-display); font-size: 19px; font-weight: 900; color: var(--teal-900); }
.brand-sub { font-size: 11px; color: #5c8480; letter-spacing: 0.06em; margin-top: 2px; }

.mode-switch { display: flex; gap: 4px; padding: 4px; border-radius: 12px;
  background: rgba(217, 242, 236, 0.55); margin-bottom: 18px; }
.mode-switch button {
  flex: 1; padding: 9px 0; border: none; border-radius: 9px; cursor: pointer;
  background: transparent; color: #5c8480; font-size: 14px; font-weight: 600;
  font-family: inherit; transition: all 0.18s;
}
.mode-switch button.active { background: #fff; color: var(--teal-700);
  box-shadow: 0 2px 8px rgba(15, 118, 110, 0.16); }

.login-title { font-family: var(--font-display); font-size: 24px; font-weight: 900;
  color: var(--teal-900); margin-bottom: 16px; }

.login-form { display: flex; flex-direction: column; gap: 14px; }
.login-form :deep(.el-input__wrapper) { border-radius: 12px; padding: 4px 12px;
  box-shadow: 0 2px 10px rgba(11, 61, 58, 0.06); }

.submit-btn { margin-top: 6px; border-radius: 13px; height: 44px; font-size: 15px;
  font-weight: 700; letter-spacing: 0.2em;
  background: linear-gradient(145deg, var(--teal-500), var(--teal-700)); border: none;
  box-shadow: 0 6px 18px rgba(15, 118, 110, 0.3);
  transition: transform 0.15s, box-shadow 0.15s; }
.submit-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 22px rgba(15, 118, 110, 0.4); }

.login-note { margin-top: 18px; font-size: 12px; line-height: 1.6; color: #8aa6a2; text-align: center; }
</style>
