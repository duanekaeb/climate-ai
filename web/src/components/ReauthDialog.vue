<script setup lang="ts">
// Asks for the owner password again when the server answers 403 REAUTHENTICATION_REQUIRED
// (creating an API token needs the password re-entered within the last few minutes, so a
// borrowed unlocked phone cannot mint a long-lived credential). The api client pauses that
// call and asks this dialog; on success /auth/reauth has opened the window and the call
// replays once. Cancel (or Escape) fails the call. Feature code never sees any of it.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, setReauthHandler } from '@/api/client'
import type { MeOut, ReauthBody } from '@/api/types'
import { authErrorText } from '@/lib/device'
import { useAuth } from '@/stores/auth'

const auth = useAuth()
const open = ref(false)
const password = ref('')
const error = ref('')
const busy = ref(false)
const input = ref<HTMLInputElement | null>(null)

let resolver: ((ok: boolean) => void) | null = null
let returnFocus: HTMLElement | null = null

function ask(): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    resolver?.(false)
    resolver = resolve
    password.value = ''
    error.value = ''
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    open.value = true
    nextTick(() => input.value?.focus())
  })
}

function settle(ok: boolean) {
  const r = resolver
  resolver = null
  open.value = false
  password.value = ''
  error.value = ''
  r?.(ok)
  const el = returnFocus
  returnFocus = null
  nextTick(() => el?.focus())
}

async function submit() {
  if (!password.value) {
    error.value = 'Enter your password.'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const body: ReauthBody = { password: password.value }
    await api.post<MeOut>('/auth/reauth', body)
    settle(true)
  } catch (e) {
    if (open.value) error.value = authErrorText(e)
  } finally {
    busy.value = false
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && open.value && !busy.value) settle(false)
}

// Signed out while the dialog was up (e.g. this device was signed out elsewhere): give up.
watch(() => auth.signedIn, (signedIn) => !signedIn && open.value && settle(false))

onMounted(() => {
  setReauthHandler(ask)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  setReauthHandler(null)
  document.removeEventListener('keydown', onKey)
  if (resolver) settle(false)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 grid place-items-center px-4">
      <div class="absolute inset-0 bg-black/40" aria-hidden="true" @click="!busy && settle(false)" />
      <form role="dialog" aria-modal="true" aria-labelledby="reauth-title" aria-describedby="reauth-why"
            class="card relative w-full max-w-sm space-y-4 !p-5 shadow-xl" novalidate @submit.prevent="submit">
        <div>
          <h2 id="reauth-title" class="text-lg font-semibold">Confirm it's you</h2>
          <p id="reauth-why" class="mt-1 text-sm text-muted">
            Enter the owner password again to continue. This keeps someone with your unlocked
            device from creating a long-lived token.
          </p>
        </div>
        <div class="space-y-1">
          <label for="reauth-password" class="text-sm font-medium">Password</label>
          <input id="reauth-password" ref="input" v-model="password" class="input" type="password" maxlength="200"
                 autocomplete="current-password" :aria-invalid="!!error" aria-describedby="reauth-error" />
        </div>
        <p id="reauth-error" role="alert" :class="error ? 'rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad' : 'sr-only'">
          {{ error }}
        </p>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn" :disabled="busy" @click="settle(false)">Cancel</button>
          <button type="submit" class="btn btn-primary" :disabled="busy">{{ busy ? 'Checking…' : 'Continue' }}</button>
        </div>
      </form>
    </div>
  </Teleport>
</template>
