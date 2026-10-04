<script setup lang="ts">
// What the controller wants right now, per unit: target vs current setpoints, the rule and
// reason, what the guardrails changed or blocked, and whether it would write.
import { computed } from 'vue'
import type { PlanOut, RoomStatus, UnitTarget } from '@/api/types'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { temp } from '@/lib/format'
import { ago } from '@/components/analysis/stats'
import { unitName } from './units'

const props = defineProps<{ plan: PlanOut; loading: boolean; rooms: RoomStatus[] }>()
const emit = defineEmits<{ refresh: [] }>()

const RULES: Record<UnitTarget['rule'], string> = {
  comfort: 'comfort',
  sleep: 'sleep',
  linked_floors: 'linked floors',
  house_setback: 'house setback',
  recovery: 'recovery',
  precool: 'pre-cool',
  independent: 'independent',
  hold_off: 'hands off',
  event_prep: 'pre-cooling before a utility event',
}

/** The rule's chip; 'event_prep' says pre-heating when the plan's reason does (heating season). */
function ruleChip(t: UnitTarget): string {
  if (t.rule === 'event_prep' && /^pre-heat/i.test(t.reason)) return 'pre-heating before a utility event'
  return RULES[t.rule]
}

const rows = computed(() =>
  [...props.plan.rows].sort((a, b) => ['main', 'up', 'bed'].indexOf(a.target.unit_key) - ['main', 'up', 'bed'].indexOf(b.target.unit_key)),
)
const modeText = computed(() =>
  props.plan.mode === 'act'
    ? 'Act mode: rows marked "will write" are sent on the next tick.'
    : props.plan.mode === 'suggest'
      ? 'Suggest mode: nothing is written; this is what it would do.'
      : 'Controller off: shown for reference only.',
)

const roomNames = computed(() => new Map(props.rooms.map((r) => [r.room_key, r.name])))

function changed(a: number | null, b: number): boolean {
  return a === null || Math.abs(a - b) >= 0.05
}
</script>

<template>
  <Card title="Current plan" :subtitle="`Planned ${ago(plan.at)}`">
    <template #actions>
      <button type="button" class="btn !px-2 !py-1" aria-label="Refresh plan" :disabled="loading" @click="emit('refresh')">
        <Icon name="refresh" :size="14" />
      </button>
    </template>
    <p class="mb-3 text-sm text-muted">{{ modeText }}</p>
    <p v-if="!rows.length" class="text-sm text-muted">No plan rows (no unit has live data yet).</p>
    <ul class="space-y-2">
      <li v-for="r in rows" :key="r.target.unit_key" class="rounded-xl border border-line p-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="inline-flex items-center gap-1.5 font-medium">
            <span class="h-2.5 w-2.5 rounded-full" :style="{ background: `var(--color-unit-${r.target.unit_key})` }" />
            {{ unitName(r.target.unit_key) }}
          </span>
          <span class="flex flex-wrap gap-1">
            <span class="chip bg-surface-2 text-muted">{{ ruleChip(r.target) }}</span>
            <span v-if="r.target.desired === 'program'" class="chip bg-surface-2 text-muted">schedule</span>
            <span class="chip" :class="r.would_write ? 'bg-accent/15 text-accent' : 'bg-surface-2 text-muted'">
              {{ r.would_write ? (plan.mode === 'act' ? 'will write' : 'would write') : 'no write needed' }}
            </span>
          </span>
        </div>
        <dl class="num mt-2 grid grid-cols-2 gap-2 text-sm">
          <div class="rounded-lg bg-surface-2 p-2">
            <dt class="text-[11px] text-muted">Heat · now → target</dt>
            <dd>
              {{ temp(r.current_heat_f) }} →
              <span :class="changed(r.current_heat_f, r.guard.heat_f) && 'font-semibold'">{{ temp(r.guard.heat_f) }}</span>
            </dd>
          </div>
          <div class="rounded-lg bg-surface-2 p-2">
            <dt class="text-[11px] text-muted">Cool · now → target</dt>
            <dd>
              {{ temp(r.current_cool_f) }} →
              <span :class="changed(r.current_cool_f, r.guard.cool_f) && 'font-semibold'">{{ temp(r.guard.cool_f) }}</span>
            </dd>
          </div>
        </dl>
        <p class="mt-2 text-sm break-words">{{ r.target.reason }}</p>
        <p v-if="r.target.priority_room" class="mt-0.5 text-xs text-muted">Steering for: {{ roomNames.get(r.target.priority_room) ?? r.target.priority_room }}</p>
        <p v-if="r.guard.clamped" class="mt-1 text-xs text-warn">
          Guardrails adjusted the policy's {{ temp(r.target.heat_f) }} / {{ temp(r.target.cool_f) }} to stay inside your
          limits.
        </p>
        <p v-if="r.guard.blocked_reason" class="mt-1 text-xs text-bad break-words">Blocked: {{ r.guard.blocked_reason }}</p>
        <ul v-if="r.guard.violations.length" class="mt-1 list-disc pl-5 text-xs text-warn">
          <li v-for="v in r.guard.violations" :key="v" class="break-words">{{ v }}</li>
        </ul>
      </li>
    </ul>
  </Card>
</template>
