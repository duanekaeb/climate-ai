<script setup lang="ts">
// ecobee account sign-in (email + password, then an MFA code when the account asks for one).
// The password lives only in this component's input and is cleared as soon as it is sent.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { EcobeeSetup } from '@/api/types'
import Card from '@/components/Card.vue'
import { useSetup } from '@/stores/setup'
import { useAction } from './useAction'

const props = defineProps<{ ecobee: EcobeeSetup; secretsOk: boolean }>()
const setup = useSetup()
const { busy, error, done, run } = useAction()

const email = ref('')
const password = ref('')
const code = ref('')
const mfaLocal = ref(false)
const mfaType = ref<string | null>(null)
const mfaDismissed = ref(false)

const step = computed<'signed_in' | 'mfa' | 'login'>(() => {
  if (props.ecobee.signed_in) return 'signed_in'
  if (mfaLocal.value || (props.ecobee.mfa_pending && !mfaDismissed.value)) return 'mfa'
  return 'login'
})
const kind = computed(() => (mfaType.value ?? props.ecobee.mfa_type ?? '').toLowerCase())
const mfaPrompt = computed(() => {
  if (kind.value.includes('sms') || kind.value.includes('text')) return 'Enter the code ecobee just texted to your phone.'
  if (kind.value.includes('otp') || kind.value.includes('totp') || kind.value.includes('app'))
    return 'Enter the current code from your authenticator app.'
  return 'Enter the verification code ecobee sent you.'
})

async function login() {
  error.value = ''
  if (!email.value.trim() || !password.value) {
    error.value = 'Enter the email and password of your ecobee account.'
    return
  }
  const pw = password.value
  password.value = ''
  const ok = await run(async () => {
    const res = await setup.ecobeeLogin(email.value.trim(), pw)
    if (res.status === 'mfa_required') {
      mfaLocal.value = true
      mfaDismissed.value = false
      mfaType.value = res.mfa_type
    } else if (res.status === 'error') {
      throw new Error(res.error || 'ecobee sign-in failed.')
    }
  })
  if (ok && props.ecobee.signed_in) done.value = 'Signed in to ecobee.'
}

async function verify() {
  error.value = ''
  const c = code.value.replace(/\s/g, '')
  if (!/^\d{4,8}$/.test(c)) {
    error.value = 'The code is 4 to 8 digits.'
    return
  }
  code.value = ''
  const ok = await run(async () => {
    const res = await setup.ecobeeMfa(c)
    if (res.status === 'error') throw new Error(res.error || 'That code was not accepted.')
    if (res.status === 'signed_in') mfaLocal.value = false
  })
  if (ok && props.ecobee.signed_in) done.value = 'Signed in to ecobee.'
}

const codeInput = ref<HTMLInputElement | null>(null)
watch(step, (s) => s === 'mfa' && nextTick(() => codeInput.value?.focus()))

function startOver() {
  mfaLocal.value = false
  mfaDismissed.value = true
  code.value = ''
  error.value = ''
}

async function signOut() {
  if (!window.confirm('Sign out of ecobee? The worker stops reading the thermostats until you sign in again.')) return
  await run(() => setup.ecobeeSignout(), 'Signed out of ecobee.')
  mfaLocal.value = false
}

onBeforeUnmount(() => {
  password.value = ''
  code.value = ''
})
</script>

<template>
  <Card title="ecobee account">
    <template #actions>
      <span class="chip" :class="ecobee.signed_in ? 'bg-good/15 text-good' : 'bg-surface-2 text-muted'">
        {{ ecobee.signed_in ? 'Signed in' : 'Signed out' }}
      </span>
    </template>

    <div class="space-y-3">
      <p v-if="ecobee.last_error && step !== 'signed_in'" class="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm text-warn">
        Last problem: {{ ecobee.last_error }}
      </p>

      <!-- signed in -->
      <div v-if="step === 'signed_in'" class="space-y-3">
        <p class="text-sm text-muted">
          The worker reads the thermostats with this account and keeps its sign-in fresh. Your password is not stored.
        </p>
        <button type="button" class="btn" :disabled="busy" @click="signOut">{{ busy ? 'Signing out…' : 'Sign out of ecobee' }}</button>
      </div>

      <!-- MFA -->
      <form v-else-if="step === 'mfa'" class="space-y-3" novalidate @submit.prevent="verify">
        <p class="text-sm font-medium">{{ mfaPrompt }}</p>
        <label class="block max-w-xs space-y-1">
          <span class="text-sm text-muted">Verification code</span>
          <input ref="codeInput" v-model="code" class="input num tracking-widest" inputmode="numeric" autocomplete="one-time-code"
                 maxlength="8" pattern="\d{4,8}" required />
        </label>
        <p class="rounded-xl bg-surface-2 p-2.5 text-xs text-muted">
          Only authenticator-app (TOTP) or SMS verification works with this sign-in. If your ecobee account uses
          another method, switch it to an authenticator app or SMS in the ecobee app's security settings first.
          The code request expires after 10 minutes; start over if it has.
        </p>
        <div class="flex flex-wrap gap-2">
          <button type="submit" class="btn btn-primary" :disabled="busy">{{ busy ? 'Checking…' : 'Verify' }}</button>
          <button type="button" class="btn" :disabled="busy" @click="startOver">Start over</button>
        </div>
      </form>

      <!-- email + password -->
      <form v-else class="space-y-3" novalidate @submit.prevent="login">
        <p v-if="!secretsOk" class="text-sm text-bad">Sign-in is disabled until the server's secret key is set (see the warning above).</p>
        <fieldset :disabled="!secretsOk || busy" class="grid gap-3 sm:grid-cols-2">
          <legend class="sr-only">ecobee account</legend>
          <label class="block space-y-1">
            <span class="text-sm font-medium">Email</span>
            <input v-model="email" class="input" type="email" autocomplete="username" inputmode="email" required />
          </label>
          <label class="block space-y-1">
            <span class="text-sm font-medium">Password</span>
            <input v-model="password" class="input" type="password" autocomplete="current-password" required />
          </label>
        </fieldset>
        <p class="text-xs text-muted">
          Sent once to sign in, then forgotten; only ecobee's refresh token is kept, encrypted.
        </p>
        <button type="submit" class="btn btn-primary" :disabled="!secretsOk || busy">{{ busy ? 'Signing in…' : 'Sign in to ecobee' }}</button>
      </form>

      <p v-if="error" role="alert" class="rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad">{{ error }}</p>
      <p v-if="done && !error" role="status" class="text-sm text-good">{{ done }}</p>

      <details class="text-xs text-muted">
        <summary class="cursor-pointer">Advanced</summary>
        <p class="mt-1 break-all">Web client ID: <span class="font-mono">{{ ecobee.web_client_id }}</span></p>
        <p class="mt-1">ecobee's account sign-in is unofficial and has changed before; if it breaks, the client ID is configurable on the server.</p>
      </details>
    </div>
  </Card>
</template>
