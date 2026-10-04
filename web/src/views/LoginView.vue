<script setup lang="ts">
// The owner sign-in: one password for the house, no user accounts. On a fresh install (no
// password yet) the same screen chooses it, which only works from the home network (or
// Tailscale) unless the server allows otherwise. Each sign-in is a "device" with a name,
// prefilled from the platform, that Security -> Signed-in devices shows and can sign out.
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { PASSWORD_MIN_LENGTH, authErrorText, deviceName, rememberDeviceName } from '@/lib/device'
import { useAuth } from '@/stores/auth'
import { useStatus } from '@/stores/status'

const auth = useAuth()
const route = useRoute()
const router = useRouter()

const password = ref('')
const confirm = ref('')
const device = ref(deviceName())
const busy = ref(false)
const checking = ref(false)
const error = ref('')

const choosing = computed(() => auth.state?.password_set === false)
const setupBlocked = computed(() => choosing.value && auth.state?.setup_allowed === false)
const title = computed(() => (setupBlocked.value ? 'Not set up yet' : choosing.value ? 'Choose a password' : 'Sign in'))

/** Only same-app paths are allowed as the post-login destination. */
const next = computed(() => {
  const q = route.query.next
  const v = Array.isArray(q) ? q[0] : q
  return typeof v === 'string' && v.startsWith('/') && !v.startsWith('//') && !v.startsWith('/login') ? v : '/'
})

async function done() {
  useStatus().start()
  await router.replace(next.value)
}

async function submit() {
  error.value = ''
  if (choosing.value) {
    if (password.value.length < PASSWORD_MIN_LENGTH) {
      error.value = `Use at least ${PASSWORD_MIN_LENGTH} characters.`
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
  const name = device.value.trim().slice(0, 100)
  busy.value = true
  try {
    if (choosing.value) await auth.setup(password.value, name)
    else await auth.login(password.value, name)
    rememberDeviceName(name)
    password.value = ''
    confirm.value = ''
    if (!auth.signedIn) {
      error.value = 'Signed in, but the server did not confirm it. Try again.'
      return
    }
    await done()
  } catch (e) {
    error.value = authErrorText(e)
    // The password was set (or not) since this page loaded: show the right form.
    if (e instanceof ApiError && (e.code === 'ALREADY_SET' || e.code === 'PASSWORD_NOT_SET' || e.code === 'SETUP_NOT_ALLOWED')) {
      await auth.loadState()
      if (e.code === 'PASSWORD_NOT_SET') error.value = 'No password is set yet. Choose one.'
    }
  } finally {
    busy.value = false
  }
}

/** Try the server again (after "could not reach", or to re-check the setup rule). */
async function retry() {
  checking.value = true
  error.value = ''
  try {
    await auth.bootstrap()
    if (auth.signedIn) await done()
  } finally {
    checking.value = false
  }
}

onMounted(async () => {
  if (!auth.loaded) await auth.bootstrap()
  if (auth.signedIn) await done()
})
</script>

<template>
  <div class="grid min-h-dvh place-items-center px-4 py-10">
    <form class="card w-full max-w-sm space-y-4 !p-6" novalidate @submit.prevent="submit">
      <div class="flex items-center gap-3">
        <img src="/icon.svg" alt="" class="h-9 w-9" />
        <div>
          <p class="text-sm font-semibold">Climate AI</p>
          <h1 class="text-lg font-semibold">{{ title }}</h1>
        </div>
      </div>

      <p v-if="auth.notice" role="status" class="rounded-xl border border-line bg-surface-2 p-2.5 text-sm">{{ auth.notice }}</p>

      <div v-if="auth.unreachable" role="alert" class="flex items-start justify-between gap-3 rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm">
        <span>Could not reach Climate AI. Check the connection.</span>
        <button type="button" class="btn shrink-0 !px-2 !py-1 text-xs" :disabled="checking" @click="retry">
          {{ checking ? 'Trying…' : 'Try again' }}
        </button>
      </div>

      <template v-if="setupBlocked">
        <p class="text-sm">
          <span class="font-medium">Setup only works from your home network.</span>
          No password has been chosen for this house yet, and for safety the first one can only
          be set from a device at home (or over Tailscale).
        </p>
        <button type="button" class="btn w-full" :disabled="checking" @click="retry">
          {{ checking ? 'Checking…' : 'Check again' }}
        </button>
      </template>

      <template v-else>
        <p v-if="choosing" class="text-sm text-muted">
          This is the first visit. Choose the owner password for this house; it is the one login,
          used on every device. At least {{ PASSWORD_MIN_LENGTH }} characters.
        </p>

        <div class="space-y-1">
          <label for="login-password" class="text-sm font-medium">{{ choosing ? 'New password' : 'Password' }}</label>
          <input id="login-password" v-model="password" class="input" type="password" required maxlength="200"
                 :minlength="choosing ? PASSWORD_MIN_LENGTH : undefined"
                 :autocomplete="choosing ? 'new-password' : 'current-password'"
                 :aria-invalid="!!error" aria-describedby="login-error" autofocus />
        </div>

        <div v-if="choosing" class="space-y-1">
          <label for="login-confirm" class="text-sm font-medium">Type it again</label>
          <input id="login-confirm" v-model="confirm" class="input" type="password" required maxlength="200"
                 :minlength="PASSWORD_MIN_LENGTH" autocomplete="new-password" :aria-invalid="!!error"
                 aria-describedby="login-error" />
        </div>

        <div class="space-y-1">
          <label for="login-device" class="text-sm font-medium">This device</label>
          <input id="login-device" v-model="device" class="input" type="text" maxlength="100" autocomplete="off"
                 autocapitalize="words" aria-describedby="login-device-help" />
          <p id="login-device-help" class="text-xs text-muted">Names this sign-in under Security, where you can sign it out.</p>
        </div>

        <p id="login-error" role="alert" aria-live="assertive"
           :class="error ? 'rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad' : 'sr-only'">
          {{ error }}
        </p>

        <button type="submit" class="btn btn-primary w-full" :disabled="busy">
          {{ busy ? (choosing ? 'Saving…' : 'Signing in…') : choosing ? 'Set password and continue' : 'Sign in' }}
        </button>
      </template>
    </form>
  </div>
</template>
