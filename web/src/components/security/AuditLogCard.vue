<script setup lang="ts">
// Recent security activity: sign-ins (and wrong passwords), sign-outs, password changes,
// device and token changes, newest first. Payloads never hold secrets; the few readable
// fields (device, token name, role, reason) are shown next to each event.
import { computed, ref } from 'vue'
import type { AuditEventOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { useSecurity } from '@/stores/security'
import { ROLE_LABEL, type TokenRole } from './tokenRoles'

const security = useSecurity()

const EVENTS: Record<string, { label: string; tone?: 'warn' | 'bad' }> = {
  'auth.setup': { label: 'Password chosen (first run)' },
  'auth.login': { label: 'Signed in' },
  'auth.login_failed': { label: 'Wrong password', tone: 'warn' },
  'auth.login_paused': { label: 'Internet sign-in paused', tone: 'bad' },
  'auth.logout': { label: 'Signed out' },
  'auth.logout_all': { label: 'Signed out everywhere' },
  'auth.refresh_reuse': { label: 'Old sign-in replayed; device signed out', tone: 'bad' },
  'auth.reauth': { label: 'Password re-entered' },
  'password.change': { label: 'Password changed' },
  'session.revoke': { label: 'Device signed out' },
  'token.create': { label: 'API token created' },
  'token.revoke': { label: 'API token revoked' },
}

const FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'Everything' },
  { value: 'auth.login', label: 'Sign-ins' },
  { value: 'auth.login_failed', label: 'Wrong passwords' },
  { value: 'session.revoke', label: 'Devices signed out' },
  { value: 'password.change', label: 'Password changes' },
  { value: 'token.create', label: 'Tokens created' },
  { value: 'token.revoke', label: 'Tokens revoked' },
]

const filter = ref('')
const rows = computed(() => security.audit.data ?? [])

function label(e: AuditEventOut): string {
  return EVENTS[e.event_type]?.label ?? e.event_type
}

function tone(e: AuditEventOut): string {
  const t = EVENTS[e.event_type]?.tone
  return t === 'bad' ? 'text-bad' : t === 'warn' ? 'text-warn' : ''
}

function when(iso: string): string {
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

const ISO = /^\d{4}-\d{2}-\d{2}T/

function fullDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
}

/** One payload field in plain words, or null to leave it out. */
function field(key: string, v: unknown): string | null {
  if (v === null || v === '' || !['string', 'number', 'boolean'].includes(typeof v)) return null
  switch (key) {
    case 'device_name':
      return `device “${String(v)}”`
    case 'name':
      return `“${String(v)}”`
    case 'role':
      return `role ${ROLE_LABEL[v as TokenRole] ?? String(v)}`
    case 'local_only':
      return v ? 'home network only' : 'works from anywhere'
    case 'public':
      return v ? 'from the internet' : 'from the home network'
    case 'reason':
    case 'via':
      return String(v).replace(/_/g, ' ')
    case 'expires_at':
      return typeof v === 'string' && ISO.test(v) ? `expires ${fullDate(v)}` : null
  }
  const k = key.replace(/_/g, ' ')
  if (typeof v === 'boolean') return `${k}: ${v ? 'yes' : 'no'}`
  if (typeof v === 'string' && ISO.test(v)) return `${k} ${fullDate(v)}`
  return `${k}: ${String(v)}`
}

/** Readable payload fields ("device “iPhone”", "role Agent"), primitives only. */
function details(e: AuditEventOut): string {
  return Object.entries(e.payload ?? {})
    .map(([k, v]) => field(k, v))
    .filter((x): x is string => !!x)
    .slice(0, 5)
    .join(' · ')
}

function who(e: AuditEventOut): string {
  if (e.actor_type === 'system') return 'system'
  if (e.actor_type === 'api_token') return `token “${e.actor_label}”`
  if (e.actor_label === 'unauthenticated') return 'not signed in'
  return e.actor_label || 'owner'
}

function onFilter() {
  security.loadAudit(filter.value)
}
</script>

<template>
  <Card title="Recent activity" subtitle="Sign-ins, sign-outs, password and token changes.">
    <template #actions>
      <button type="button" class="btn !px-2 !py-1" title="Reload" aria-label="Reload recent activity"
              :disabled="security.audit.loading" @click="security.loadAudit(filter)">
        <Icon name="refresh" :size="16" />
      </button>
    </template>
    <div class="mb-2 flex items-center gap-2">
      <label for="audit-filter" class="text-sm text-muted">Show</label>
      <select id="audit-filter" v-model="filter" class="input !w-auto !py-1 text-sm" @change="onFilter">
        <option v-for="f in FILTERS" :key="f.value" :value="f.value">{{ f.label }}</option>
      </select>
    </div>
    <AsyncState :loading="security.audit.loading && !security.audit.data" :error="security.audit.data ? '' : security.audit.error"
                :empty="rows.length === 0" empty-text="Nothing recorded yet.">
      <ul class="-mx-1 divide-y divide-line">
        <li v-for="e in rows" :key="e.id" class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-1 py-2 text-sm">
          <span class="num w-28 shrink-0 text-xs text-muted">{{ when(e.ts) }}</span>
          <span class="min-w-0 flex-1">
            <span class="font-medium" :class="tone(e)">{{ label(e) }}</span>
            <span class="text-muted"> · {{ who(e) }}</span>
            <span v-if="e.ip" class="num text-muted"> · {{ e.ip }}</span>
            <span v-if="details(e)" class="block text-xs break-words text-muted">{{ details(e) }}</span>
          </span>
        </li>
      </ul>
    </AsyncState>
    <p v-if="security.audit.data && security.audit.error" role="alert" class="mt-2 text-sm text-bad">{{ security.audit.error }}</p>
  </Card>
</template>
