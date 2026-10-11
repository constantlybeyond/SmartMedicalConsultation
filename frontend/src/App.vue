<script setup>
// 根组件：登录态网关 —— 未登录显示 LoginView，登录后显示 ChatView（讲义 §7）
import { ref, onMounted, onBeforeUnmount } from 'vue'
import ChatView from '@/views/ChatView.vue'
import LoginView from '@/views/LoginView.vue'
import { getToken, getUser, saveAuth, clearAuth, me } from '@/api/index.js'

const authed = ref(false)
const user = ref(null)

// 会话 ID 的 localStorage 键（与 ChatView 同名）：
// 主动退出时必须一并清除，否则下一个登录的账号会继承前一个用户的会话 ID，
// 因归属校验永远 404 卡死（切换账号残留问题）。
const SESSION_KEY = 'smc_session_id'

// 登录成功（由 LoginView 触发）：保存凭据并切入聊天页
function handleAuthed({ token, user: u }) {
  saveAuth(token, u)
  user.value = u
  authed.value = true
}

// 全局 401（token 过期/失效）：只清凭据，保留会话 ID——
// 同一用户重新登录后可无缝继续原会话
function handleUnauthorized() {
  clearAuth()
  user.value = null
  authed.value = false
}

// 显式退出登录（由 ChatView 触发）：额外清掉会话 ID，
// 下一个登录的账号从全新会话开始，不继承他人对话
function handleLogout() {
  localStorage.removeItem(SESSION_KEY)
  handleUnauthorized()
}

// 启动时恢复登录态：本地有 token 则向 /auth/me 验证有效性
async function restoreAuth() {
  const token = getToken()
  if (!token) return
  try {
    const { data } = await me()
    user.value = data
    authed.value = true
  } catch {
    clearAuth()  // token 无效/过期 → 清掉，停留在登录页
  }
}

onMounted(() => {
  restoreAuth()
  window.addEventListener('smc:unauthorized', handleUnauthorized)
})
onBeforeUnmount(() => {
  window.removeEventListener('smc:unauthorized', handleUnauthorized)
})
</script>

<template>
  <ChatView v-if="authed" :user="user" @logout="handleLogout" />
  <LoginView v-else @authed="handleAuthed" />
</template>

<style>
/* ============ 全局基础样式 ============ */
:root {
  /* 配色：临床青绿 + 暖色点缀（医疗的信任感与温度） */
  --ink: #123332;
  --teal-900: #0b3d3a;
  --teal-700: #0e5a54;
  --teal-600: #0f766e;
  --teal-500: #14b8a6;
  --mint: #d9f2ec;
  --paper: #f6fbf9;
  --coral: #e8896b;

  /* 字体 */
  --font-display: 'Noto Serif SC', 'Songti SC', 'SimSun', serif;
  --font-body: 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body,
#app {
  height: 100%;
}

body {
  font-family: var(--font-body);
  color: var(--ink);
  background: var(--paper);
  -webkit-font-smoothing: antialiased;
}
</style>
