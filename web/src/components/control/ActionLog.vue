<script setup lang="ts">
// Every write the controller (or the owner) made or suggested: who, why, what, and whether
// the thermostat read back what was sent.
import { computed, ref, watch } from 'vue'
import type { ControlActionOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { dateTime } from '@/components/analysis/stats'
import { temp } from '@/lib/format'
import { ACTIONS_LIMIT, useControl } from '@/stores/control'
import { unitName } from './units'

const props = defineProps<{ units: string[]; tz?: string }>()
const control = useControl()

const unit = ref<string>('')
const actor = ref<string>('')
const status = ref<string>('')
const open = ref<Set<number>>(new Set())

watch(unit, (u) => control.loadActions(u || null), { immediate: true })

const rows = computed(() => control.actions.data ?? [])
const actors = computed(() => [...new Set(rows.value.map((a) => a.actor))].sort())
const statuses = computed(() => [...new Set(rows.value.map((a) => a.status))].sort())
const shown = computed(() =>
  rows.value.filter((a) => (!actor.value || a.actor === actor.value) && (!status.value || a.status === status.value)),
)

const STATUS_CHIP: Record<string, string> = {
  verified: 'bg-good/15 text-good',
  done: 'bg-good/15 text-good',
  sent: 'bg-accent/15 text-accent',
  suggested: 'bg-accent/15 text-accent',
  queued: 'bg-surface-2 text-muted',
  blocked: 'bg-warn/15 text-warn',
  skipped: 'bg-surface-2 text-muted',
  failed: 'bg-bad/15 text-bad',
  error: 'bg-bad/15 text-bad',
}

function toggle(id: number) {
  const next = new Set(open.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  open.value = next
}

function brief(r: Record<string, unknown> | null): string {
  if (!r) return ''
  const h = r.heat_f ?? r.heat ?? r.heat_sp_f
  const c = r.cool_f ?? r.cool ?? r.cool_sp_f
  const parts: string[] = []
  if (typeof h === 'number') parts.push(`heat ${temp(h)}`)
  if (typeof c === 'number') parts.push(`cool ${temp(c)}`)
  const hrs = r.hours ?? r.hold_hours
  if (typeof hrs === 'number') parts.push(`${hrs} h`)
  return parts.join(' · ')
}

function readback(a: ControlActionOut): { label: string; cls: string } {
  if (a.readback_ok === true) return { label: 'read back OK', cls: 'text-good' }
  if (a.readback_ok === false) return { label: 'read-back mismatch', cls: 'text-bad' }
  return { label: 'no read-back', cls: 'text-muted' }
}

function json(v: unknown): string {
  return JSON.stringify(v, null, 1)
}
</script>

<template>
  <Card title="Action log" :subtitle="`Last ${ACTIONS_LIMIT} actions; every write is logged and read back`">
    <template #actions>
      <button
        type="button"
        class="btn !px-2 !py-1"
        aria-label="Reload the action log"
        :disabled="control.actions.loading"
        @click="control.loadActions(unit || null)"
      >
        <Icon name="refresh" :size="14" />
      </button>
    </template>
    <div class="mb-3 grid grid-cols-3 gap-2">
      <label class="text-xs text-muted">
        Unit
        <select v-model="unit" class="input mt-1">
          <option value="">All</option>
          <option v-for="u in units" :key="u" :value="u">{{ unitName(u) }}</option>
        </select>
      </label>
      <label class="text-xs text-muted">
        Actor
        <select v-model="actor" class="input mt-1">
          <option value="">All</option>
          <option v-for="a in actors" :key="a" :value="a">{{ a }}</option>
        </select>
      </label>
      <label class="text-xs text-muted">
        Status
        <select v-model="status" class="input mt-1">
          <option value="">All</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
    </div>
    <AsyncState
      :loading="control.actions.loading && !control.actions.data"
      :error="control.actions.error"
      :empty="!!control.actions.data && !shown.length"
      :empty-text="rows.length ? 'No actions match these filters.' : 'No actions yet.'"
    >
      <ul class="divide-y divide-line">
        <li v-for="a in shown" :key="a.id" class="py-2">
          <button type="button" class="w-full text-left" :aria-expanded="open.has(a.id)" @click="toggle(a.id)">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="num text-xs text-muted">{{ dateTime(a.ts, tz) }}</span>
              <span class="inline-flex items-center gap-1 text-sm font-medium">
                <span class="h-2 w-2 rounded-full" :style="{ background: `var(--color-unit-${a.unit_key})` }" />
                {{ unitName(a.unit_key) }}
              </span>
              <span class="text-sm">{{ a.action.replace(/_/g, ' ') }}</span>
              <span class="chip" :class="STATUS_CHIP[a.status] ?? 'bg-surface-2 text-ink'">{{ a.status }}</span>
              <span class="ml-auto text-xs" :class="readback(a).cls">{{ readback(a).label }}</span>
            </div>
            <p class="mt-0.5 text-xs text-muted break-words">
              {{ a.actor }} · {{ a.mode }} · {{ a.channel }}<template v-if="a.rule"> · {{ a.rule.replace(/_/g, ' ') }}</template>
              <template v-if="brief(a.request)"> · <span class="num text-ink">{{ brief(a.request) }}</span></template>
            </p>
            <p class="mt-0.5 text-sm break-words">{{ a.reason }}</p>
            <p v-if="a.error" class="mt-0.5 text-xs text-bad break-words">{{ a.error }}</p>
          </button>
          <div v-if="open.has(a.id)" class="mt-2 grid gap-2 sm:grid-cols-3">
            <div v-for="part in (['before', 'request', 'readback'] as const)" :key="part" class="min-w-0 rounded-lg bg-surface-2 p-2">
              <p class="text-[11px] font-medium text-muted uppercase">{{ part }}</p>
              <pre class="mt-1 text-[11px] whitespace-pre-wrap break-all">{{ a[part] ? json(a[part]) : '—' }}</pre>
            </div>
            <p v-if="a.completed_at" class="text-xs text-muted sm:col-span-3">Completed {{ dateTime(a.completed_at, tz) }}</p>
          </div>
        </li>
      </ul>
    </AsyncState>
  </Card>
</template>
