<script setup lang="ts">
// One thermostat on the Live screen: zone temperature, setpoints, what it's doing, the hold,
// the policy's target and reason, and today's runtime / duty / maxed-out minutes.
import { computed } from 'vue'
import type { ControllerInfo, RoomStatus, UnitLive } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { UNIT_COLORS, localTime, minutes, pct, temp } from '@/lib/format'
import { CALL_INFO, RULE_LABELS, equipment } from './labels'

const props = defineProps<{ unit: UnitLive; rooms: RoomStatus[]; mode: ControllerInfo['mode']; tz: string }>()

const color = computed(() => UNIT_COLORS[props.unit.unit_key] ?? 'var(--color-accent)')
const call = computed(() => CALL_INFO[props.unit.call])
const running = computed(() => props.unit.running.map(equipment))
const headingId = computed(() => `unit-${props.unit.unit_key}`)

function age(s: number): string {
  if (s < 60) return `${Math.round(s)} s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  return `${Math.round(s / 3600)} h ago`
}

const meta = computed(() => {
  const u = props.unit
  const parts: string[] = []
  if (u.hvac_mode) parts.push(`${u.hvac_mode[0].toUpperCase()}${u.hvac_mode.slice(1)} mode`)
  if (u.age_s !== null) parts.push(`updated ${age(u.age_s)}`)
  return parts.join(' · ')
})
const stale = computed(() => props.unit.age_s !== null && props.unit.age_s > 15 * 60)

const hold = computed(() => {
  const h = props.unit.hold
  if (!h) return null
  const what =
    h.kind === 'temperature' ? `Hold ${temp(h.heat_f, 0)}–${temp(h.cool_f, 0)}` : `Hold: ${h.climate_ref ?? 'comfort setting'}`
  const until = h.end ? `until ${localTime(h.end, props.tz)}` : 'until changed'
  return { what, until, byUs: h.set_by_us }
})

const targetLabel = computed(() =>
  props.mode === 'act' ? 'Target' : props.mode === 'suggest' ? 'Suggested target' : 'Policy target (controller off)',
)
const priorityRoom = computed(() => {
  const key = props.unit.target?.priority_room
  if (!key) return null
  return props.rooms.find((r) => r.room_key === key)?.name ?? key
})

const duty = computed(() => Math.max(0, Math.min(100, props.unit.duty_last_hour_pct ?? 0)))
// The upstairs running flat out is the problem this app exists to fix: make it loud.
const maxedAlarm = computed(() => props.unit.unit_key === 'up' && props.unit.maxed_minutes_today > 0)
</script>

<template>
  <article class="card relative overflow-hidden !pl-5" :aria-labelledby="headingId">
    <span class="absolute inset-y-0 left-0 w-1.5" :style="{ background: color }" aria-hidden="true" />

    <header class="flex items-start gap-2">
      <div class="min-w-0 flex-1">
        <h2 :id="headingId" class="truncate font-semibold">{{ unit.name }}</h2>
        <p class="text-xs" :class="!unit.connected || stale ? 'text-warn' : 'text-muted'">
          <template v-if="!unit.connected">Thermostat offline<template v-if="meta"> · </template></template>{{ meta }}
        </p>
      </div>
      <span class="chip shrink-0" :class="call.cls"><Icon :name="call.icon" :size="14" />{{ call.label }}</span>
    </header>

    <div class="mt-3 flex items-end justify-between gap-3">
      <div class="min-w-0">
        <p class="num text-5xl leading-none font-semibold">
          {{ unit.zone_temp_f === null ? '—' : unit.zone_temp_f.toFixed(1) }}<span class="text-lg font-normal text-muted">°F</span>
        </p>
        <p class="mt-1 text-xs text-muted">
          Zone temperature<template v-if="unit.zone_humidity !== null"> · {{ pct(unit.zone_humidity) }} humidity</template>
        </p>
      </div>
      <dl class="num shrink-0 space-y-0.5 text-right text-sm">
        <div><dt class="inline text-muted">Heat </dt><dd class="inline font-semibold text-bad">{{ temp(unit.heat_sp_f, 0) }}</dd></div>
        <div><dt class="inline text-muted">Cool </dt><dd class="inline font-semibold text-accent">{{ temp(unit.cool_sp_f, 0) }}</dd></div>
      </dl>
    </div>

    <div class="mt-3">
      <h3 class="sr-only">Running now</h3>
      <ul v-if="running.length" class="flex flex-wrap gap-1.5">
        <li v-for="(e, i) in running" :key="i" class="chip bg-surface-2"><Icon :name="e.icon" :size="14" />{{ e.label }}</li>
      </ul>
      <p v-else class="text-xs text-muted">No equipment running.</p>
    </div>

    <p class="mt-2 text-sm">
      <template v-if="hold">
        <span class="num font-medium">{{ hold.what }}</span> <span class="text-muted">{{ hold.until }} ·</span>
        <span :class="hold.byUs ? 'text-muted' : 'font-medium text-warn'">{{ hold.byUs ? 'set by Climate AI' : 'set by hand' }}</span>
      </template>
      <template v-else>
        <span class="text-muted">Following its schedule</span><template v-if="unit.climate_ref"> · {{ unit.climate_ref }}</template>
      </template>
    </p>

    <div class="mt-3 rounded-xl bg-surface-2 p-3">
      <template v-if="unit.target">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-xs font-semibold tracking-wide text-muted uppercase">{{ targetLabel }}</h3>
          <span class="chip border border-line bg-surface text-muted">{{ RULE_LABELS[unit.target.rule] }}</span>
        </div>
        <p class="num mt-1 font-semibold">
          Heat {{ temp(unit.target.heat_f, 0) }} · Cool {{ temp(unit.target.cool_f, 0) }}
          <span v-if="unit.target.desired === 'program'" class="text-xs font-normal text-muted">· schedule already matches</span>
        </p>
        <p class="mt-1 text-sm text-muted">{{ unit.target.reason }}</p>
        <p v-if="priorityRoom" class="mt-1 text-xs text-muted">Priority room: {{ priorityRoom }}</p>
      </template>
      <p v-else class="text-sm text-muted">No policy target right now.</p>
    </div>

    <dl class="mt-3 grid grid-cols-3 gap-2 text-center">
      <div class="rounded-xl bg-surface-2 p-2">
        <dt class="text-[11px] leading-tight text-muted">Runtime today</dt>
        <dd class="num mt-0.5 font-semibold">{{ minutes(unit.today_runtime_min) }}</dd>
      </div>
      <div class="rounded-xl bg-surface-2 p-2">
        <dt class="text-[11px] leading-tight text-muted">Duty, last hour</dt>
        <dd class="num mt-0.5 font-semibold">{{ pct(unit.duty_last_hour_pct) }}</dd>
        <div class="mt-1 h-1.5 overflow-hidden rounded-full bg-line" aria-hidden="true">
          <div class="h-full rounded-full" :style="{ width: `${duty}%`, background: color }" />
        </div>
      </div>
      <div class="rounded-xl p-2" :class="maxedAlarm ? 'bg-bad/10 ring-1 ring-bad/50' : 'bg-surface-2'">
        <dt class="text-[11px] leading-tight" :class="maxedAlarm ? 'font-medium text-bad' : 'text-muted'">Maxed out today</dt>
        <dd class="num mt-0.5 font-semibold" :class="maxedAlarm ? 'text-bad' : ''">{{ minutes(unit.maxed_minutes_today) }}</dd>
      </div>
    </dl>
    <p v-if="maxedAlarm" class="mt-2 flex items-start gap-1.5 text-xs text-bad">
      <Icon name="alert" :size="14" class="mt-px shrink-0" />
      The upstairs ran flat out (duty 95% or more) for {{ minutes(unit.maxed_minutes_today) }} today. Keeping this near
      zero is the main goal.
    </p>
  </article>
</template>
