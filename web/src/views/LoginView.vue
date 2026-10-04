<script setup lang="ts">
// Owner sign-in. On a fresh install (no password yet) the same screen chooses the password.
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { useAuth } from '@/stores/auth'
import { useStatus } from '@/stores/status'

const MIN_LENGTH = 8

const auth = useAuth()
const route = useRoute()
const router = useRouter()

const password = ref('')
const confirm = ref('')
const busy = ref(false)
const error = ref('')

const choosing = computed(() => auth.state?.password_set === false)

/** Only same-app paths are allowed as the post-login destination. */
const next = computed(() => {
  const q = route.query.next
  const v = Array.isArray(q) ? q[0] : q
  return typeof v === 'string' && v.startsWith('/') && !v.startsWith('//') && !v.startsWith('/login') ? v : '/'
})

function describe(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return 'Too many attempts. Wait a few minutes, then try again.'
    if (e.status === 401 || e.status === 403) return 'That password is not right.'
    if (e.status === 409) return 'A password has already been set. Sign in instead.'
    return e.message
  }
  return e instanceof Error ? e.message : 'Something went wrong. Try again.'
}

async function done() {
  useStatus().start()
  await router.replace(next.value)
}

async function submit() {
  error.value = ''
  if (choosing.value) {
    if (password.value.length < MIN_LENGTH) {
      error.value = `Use at least ${MIN_LENGTH} characters.`
      return
    }
    if (password.value !== confirm.value) {
      error.value = 'The two passwords do not match.'
      return
    }
  } else if (!password.value) {
    error.value = 'Enter your password.'
    return
  }
  busy.value = true
  try {
    if (choosing.value) await auth.setup(password.value)
    else await auth.login(password.value)
    if (!auth.state?.authenticated) {
      error.value = 'That password is not right.'
      return
    }
    password.value = ''
    confirm.value = ''
    await done()
  } catch (e) {
    error.value = describe(e)
    if (e instanceof ApiError && e.status === 409) await auth.refresh()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  if (!auth.loaded) await auth.refresh()
  if (auth.state?.authenticated) await done()
})
</script>

<template>
  <div class="grid min-h-dvh place-items-center px-4 py-10">
    <form class="card w-full max-w-sm space-y-4 !p-6" novalidate @submit.prevent="submit">
      <div class="flex items-center gap-3">
        <img src="/icon.svg" alt="" class="h-9 w-9" />
        <div>
          <p class="text-sm font-semibold">Climate AI</p>
          <h1 class="text-lg font-semibold">{{ choosing ? 'Choose a password' : 'Sign in' }}</h1>
        </div>
      </div>

      <p v-if="choosing" class="text-sm text-muted">
        This is the first visit. Choose the owner password for this house; you'll use it on every
        device. At least {{ MIN_LENGTH }} characters.
      </p>

      <div class="space-y-1">
        <label for="login-password" class="text-sm font-medium">{{ choosing ? 'New password' : 'Password' }}</label>
        <input id="login-password" v-model="password" class="input" type="password" required
               :minlength="choosing ? MIN_LENGTH : undefined"
               :autocomplete="choosing ? 'new-password' : 'current-password'"
               :aria-invalid="!!error" aria-describedby="login-error" autofocus />
      </div>

      <div v-if="choosing" class="space-y-1">
        <label for="login-confirm" class="text-sm font-medium">Type it again</label>
        <input id="login-confirm" v-model="confirm" class="input" type="password" required :minlength="MIN_LENGTH"
               autocomplete="new-password" :aria-invalid="!!error" aria-describedby="login-error" />
      </div>

      <p id="login-error" role="alert" aria-live="assertive"
         :class="error ? 'rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad' : 'sr-only'">
        {{ error }}
      </p>

      <button type="submit" class="btn btn-primary w-full" :disabled="busy">
        {{ busy ? (choosing ? 'Saving…' : 'Signing in…') : choosing ? 'Set password and continue' : 'Sign in' }}
      </button>
    </form>
  </div>
</template>
