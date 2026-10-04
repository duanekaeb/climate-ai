<script setup lang="ts">
// Signed-in devices: every browser or app signed in with the owner password, newest activity
// first. Signing one out takes effect at once (every call re-checks the device's session).
// "Sign out everywhere" ends them all, this one included.
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { currentSessionId } from '@/api/client'
import type { SessionOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { localDate, localTime, timeAgo } from '@/lib/format'
import { useAuth } from '@/stores/auth'
import { useSecurity } from '@/stores/security'

const security = useSecurity()
const auth = useAuth()
const router = useRouter()

const busyId = ref<number | null>(null)
const busyAll = ref(false)
const error = ref('')

const rows = computed(() => security.sessions.data ?? [])
const isThis = (s: SessionOut) => s.current || s.id === currentSessionId()

function icon(s: SessionOut): string {
  return /iPhone|iPad|Android|Mobile/.test(s.user_agent) || /iPhone|iPad|Android|phone/i.test(s.device_name) ? 'phone' : 'laptop'
}

function name(s: SessionOut): string {
  return s.device_name.trim() || 'Unnamed device'
}

function when(iso: string): string {
  return `${localDate(iso)}, ${localTime(iso)}`
}

async function signOut(s: SessionOut) {
  error.value = ''
  if (isThis(s)) {
    if (!window.confirm('Sign out on this device?')) return
  } else if (!window.confirm(`Sign out "${name(s)}"? It will need the password to get back in.`)) return
  busyId.value = s.id
  try {
    if (isThis(s)) {
      await auth.logout()
      await router.replace('/login')
    } else {
      await security.revokeSession(s.id)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busyId.value = null
  }
}

async function signOutEverywhere() {
  error.value = ''
  if (!window.confirm('Sign out on every device, this one included? Each will need the password to get back in. API tokens keep working.')) return
  busyAll.value = true
  try {
    await auth.logoutAll()
    await router.replace('/login')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busyAll.value = false
  }
}
</script>

<template>
  <Card title="Signed-in devices" subtitle="Everywhere the owner password is signed in right now.">
    <template #actions>
      <button type="button" class="btn !px-2 !py-1" title="Reload" aria-label="Reload signed-in devices"
              :disabled="security.sessions.loading" @click="security.loadSessions()">
        <Icon name="refresh" :size="16" />
      </button>
    </template>
    <AsyncState :loading="security.sessions.loading && !security.sessions.data" :error="security.sessions.data ? '' : security.sessions.error"
                :empty="rows.length === 0" empty-text="No devices are signed in.">
      <ul class="-mx-1 divide-y divide-line">
        <li v-for="s in rows" :key="s.id" class="flex items-start gap-3 px-1 py-3">
          <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-surface-2 text-muted">
            <Icon :name="icon(s)" :size="18" />
          </span>
          <div class="min-w-0 flex-1">
            <p class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="min-w-0 truncate font-medium" :title="s.user_agent">{{ name(s) }}</span>
              <span v-if="isThis(s)" class="chip bg-accent/15 text-accent">This device</span>
            </p>
            <p class="mt-0.5 text-xs text-muted">
              Last seen {{ timeAgo(s.last_seen_at) }}<template v-if="s.ip"> · <span class="num">{{ s.ip }}</span></template>
            </p>
            <p class="text-xs text-muted" :title="`This sign-in currently runs until ${when(s.expires_at)}`">
              Signed in {{ when(s.created_at) }}
            </p>
          </div>
          <button type="button" class="btn shrink-0 !px-2.5 !py-1 text-xs" :disabled="busyId !== null || busyAll"
                  :aria-label="`Sign out ${name(s)}`" @click="signOut(s)">
            {{ busyId === s.id ? 'Signing out…' : 'Sign out' }}
          </button>
        </li>
      </ul>
    </AsyncState>
    <p v-if="security.sessions.data && security.sessions.error" role="alert" class="mt-2 text-sm text-bad">{{ security.sessions.error }}</p>
    <p v-if="error" role="alert" class="mt-2 text-sm text-bad">{{ error }}</p>
    <div class="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
      <p class="text-xs text-muted">Lost a phone? Sign it out here, or everywhere, then change the password.</p>
      <button type="button" class="btn btn-danger" :disabled="busyAll || busyId !== null" @click="signOutEverywhere">
        {{ busyAll ? 'Signing out…' : 'Sign out everywhere' }}
      </button>
    </div>
  </Card>
</template>
