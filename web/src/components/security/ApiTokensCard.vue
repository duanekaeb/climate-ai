<script setup lang="ts">
// API tokens: how services call Climate AI (the Claude agent and MCP server, a dashboard, a
// wall panel). Each has a role that never reaches the owner's full rights, an optional
// expiry, and by default only works from the home network. The list shows the last four
// characters, never the token; revoking one stops it on its next use.
import { computed, ref } from 'vue'
import type { ApiTokenCreated, ApiTokenOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { timeAgo } from '@/lib/format'
import { useSecurity } from '@/stores/security'
import CreateTokenForm from './CreateTokenForm.vue'
import TokenReveal from './TokenReveal.vue'
import { ROLE_CHIP, ROLE_LABEL } from './tokenRoles'

const security = useSecurity()

const creating = ref(false)
const revealed = ref<ApiTokenCreated | null>(null)
const busyId = ref<number | null>(null)
const error = ref('')

const now = () => Date.now()
const isExpired = (t: ApiTokenOut) => !!t.expires_at && new Date(t.expires_at).getTime() <= now()
const active = computed(() => (security.tokens.data ?? []).filter((t) => !t.revoked_at && !isExpired(t)))
const inactive = computed(() => (security.tokens.data ?? []).filter((t) => t.revoked_at || isExpired(t)))

function fullDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
}

function expiryText(t: ApiTokenOut): string {
  if (!t.expires_at) return 'Never expires'
  const days = Math.ceil((new Date(t.expires_at).getTime() - now()) / 86_400_000)
  if (days <= 0) return `Expired ${fullDate(t.expires_at)}`
  if (days === 1) return 'Expires within a day'
  return days <= 60 ? `Expires in ${days} days` : `Expires ${fullDate(t.expires_at)}`
}

function onCreated(t: ApiTokenCreated) {
  creating.value = false
  revealed.value = t
}

function startCreate() {
  revealed.value = null
  creating.value = true
}

async function revoke(t: ApiTokenOut) {
  error.value = ''
  if (!window.confirm(`Revoke "${t.name}"? Anything using it stops working right away.`)) return
  busyId.value = t.id
  try {
    await security.revokeToken(t.id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busyId.value = null
  }
}
</script>

<template>
  <Card title="API tokens" subtitle="For services: the Claude agent and MCP server, a dashboard, a wall panel. None gets the owner's full rights.">
    <template #actions>
      <button type="button" class="btn !px-2 !py-1" title="Reload" aria-label="Reload API tokens"
              :disabled="security.tokens.loading" @click="security.loadTokens()">
        <Icon name="refresh" :size="16" />
      </button>
    </template>

    <div class="space-y-3">
      <button v-if="!creating" type="button" class="btn btn-primary" @click="startCreate">
        <Icon name="key" :size="16" />New token
      </button>
      <CreateTokenForm v-if="creating" @created="onCreated" @cancel="creating = false" />
      <TokenReveal v-if="revealed" :token="revealed" @done="revealed = null" />

      <AsyncState :loading="security.tokens.loading && !security.tokens.data" :error="security.tokens.data ? '' : security.tokens.error"
                  :empty="active.length === 0 && inactive.length === 0" empty-text="No API tokens yet.">
        <p v-if="active.length === 0" class="py-2 text-sm text-muted">No active tokens.</p>
        <ul v-else class="-mx-1 divide-y divide-line">
          <li v-for="t in active" :key="t.id" class="flex items-start gap-3 px-1 py-3">
            <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-surface-2 text-muted">
              <Icon name="key" :size="18" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="min-w-0 truncate font-medium">{{ t.name }}</span>
                <span class="chip" :class="ROLE_CHIP[t.role]">{{ ROLE_LABEL[t.role] }}</span>
                <span class="font-mono text-xs text-muted" :title="'Ends in ' + t.token_hint">…{{ t.token_hint }}</span>
              </p>
              <p class="mt-0.5 text-xs text-muted">
                {{ t.local_only ? 'Home network only' : 'Works from anywhere' }} · {{ expiryText(t) }}
              </p>
              <p class="text-xs text-muted">
                <template v-if="t.last_used_at">Last used {{ timeAgo(t.last_used_at) }}<template v-if="t.last_used_ip"> from <span class="num">{{ t.last_used_ip }}</span></template></template>
                <template v-else>Never used</template>
                · created {{ fullDate(t.created_at) }}
              </p>
            </div>
            <button type="button" class="btn shrink-0 !px-2.5 !py-1 text-xs" :disabled="busyId !== null"
                    :aria-label="`Revoke ${t.name}`" @click="revoke(t)">
              {{ busyId === t.id ? 'Revoking…' : 'Revoke' }}
            </button>
          </li>
        </ul>

        <details v-if="inactive.length" class="mt-2 rounded-xl border border-line">
          <summary class="cursor-pointer px-3 py-2 text-sm text-muted">Revoked and expired ({{ inactive.length }})</summary>
          <ul class="divide-y divide-line border-t border-line">
            <li v-for="t in inactive" :key="t.id" class="px-3 py-2 text-sm">
              <span class="font-medium text-muted line-through">{{ t.name }}</span>
              <span class="ml-2 chip bg-surface-2 text-muted">{{ ROLE_LABEL[t.role] }}</span>
              <span class="block text-xs text-muted">
                {{ t.revoked_at ? `Revoked ${fullDate(t.revoked_at)}` : expiryText(t) }}
                <template v-if="t.last_used_at"> · last used {{ timeAgo(t.last_used_at) }}</template>
              </span>
            </li>
          </ul>
        </details>
      </AsyncState>
      <p v-if="security.tokens.data && security.tokens.error" role="alert" class="text-sm text-bad">{{ security.tokens.error }}</p>
      <p v-if="error" role="alert" class="text-sm text-bad">{{ error }}</p>
    </div>
  </Card>
</template>
