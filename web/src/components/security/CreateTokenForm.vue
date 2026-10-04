<script setup lang="ts">
// New API token: a name, what it may do (role), when it expires, and whether it only works
// from the home network (on by default). Creating one asks for the password again if it was
// not entered in the last few minutes (the global ReauthDialog), then the token is handed to
// the parent to show once.
import { ref } from 'vue'
import type { ApiTokenCreateBody, ApiTokenCreated } from '@/api/types'
import { useSecurity } from '@/stores/security'
import { EXPIRY_OPTIONS, TOKEN_ROLES, type TokenRole } from './tokenRoles'

const emit = defineEmits<{ created: [token: ApiTokenCreated]; cancel: [] }>()
const security = useSecurity()

const name = ref('')
const role = ref<TokenRole>('agent')
const expiry = ref('365')
const localOnly = ref(true)
const busy = ref(false)
const error = ref('')

async function submit() {
  error.value = ''
  const n = name.value.trim()
  if (!n) {
    error.value = 'Give the token a name, such as "Claude agent" or "Hallway panel".'
    return
  }
  busy.value = true
  try {
    const body: ApiTokenCreateBody = {
      name: n.slice(0, 100),
      role: role.value,
      expires_in_days: expiry.value ? Number(expiry.value) : null,
      local_only: localOnly.value,
    }
    const created = await security.createToken(body)
    name.value = ''
    emit('created', created)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="space-y-4 rounded-xl border border-line bg-surface-2/50 p-3" novalidate aria-labelledby="new-token-title" @submit.prevent="submit">
    <h3 id="new-token-title" class="font-medium">New token</h3>

    <div class="space-y-1">
      <label for="token-name" class="text-sm font-medium">Name</label>
      <input id="token-name" v-model="name" class="input" type="text" maxlength="100" autocomplete="off"
             placeholder="e.g. Claude agent, hallway panel" aria-describedby="token-error" />
    </div>

    <fieldset class="space-y-2">
      <legend class="mb-1 text-sm font-medium">What it may do</legend>
      <label v-for="r in TOKEN_ROLES" :key="r.value"
             class="flex cursor-pointer items-start gap-3 rounded-xl border p-2.5"
             :class="role === r.value ? 'border-accent bg-accent/5' : 'border-line bg-surface'">
        <input v-model="role" type="radio" name="token-role" :value="r.value" class="mt-1 accent-[var(--color-accent)]" />
        <span class="min-w-0">
          <span class="block text-sm font-medium">{{ r.label }}</span>
          <span class="block text-xs text-muted">{{ r.help }}</span>
        </span>
      </label>
    </fieldset>

    <div class="grid gap-3 sm:grid-cols-2">
      <div class="space-y-1">
        <label for="token-expiry" class="text-sm font-medium">Expires after</label>
        <select id="token-expiry" v-model="expiry" class="input">
          <option v-for="o in EXPIRY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <label class="flex cursor-pointer items-start justify-between gap-3 sm:pt-6">
        <span class="min-w-0">
          <span class="block text-sm font-medium">Home network only</span>
          <span class="block text-xs text-muted">Refused from the internet; works at home, in Docker and over Tailscale.</span>
        </span>
        <input v-model="localOnly" type="checkbox" role="switch" class="peer sr-only" />
        <span aria-hidden="true"
              class="relative mt-0.5 h-6 w-11 shrink-0 rounded-full bg-line transition-colors peer-checked:bg-accent peer-focus-visible:ring-2 peer-focus-visible:ring-accent peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface after:absolute after:top-0.5 after:left-0.5 after:h-5 after:w-5 after:rounded-full after:bg-white after:shadow after:transition-transform peer-checked:after:translate-x-5" />
      </label>
    </div>
    <p v-if="!localOnly" class="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm">
      This token will work from anywhere on the internet. Turn this off only for a service outside
      your home network, and keep the token secret.
    </p>

    <p id="token-error" role="alert" :class="error ? 'rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad' : 'sr-only'">{{ error }}</p>

    <div class="flex flex-wrap justify-end gap-2">
      <button type="button" class="btn" :disabled="busy" @click="emit('cancel')">Cancel</button>
      <button type="submit" class="btn btn-primary" :disabled="busy">{{ busy ? 'Creating…' : 'Create token' }}</button>
    </div>
  </form>
</template>
