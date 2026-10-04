<script setup lang="ts">
// One thermostat on the Live screen: zone temperature, setpoints, what it's doing, whose hold
// is running (a hold a person set always wins), the policy's target and reason, and today's
// runtime / duty / maxed-out minutes. On a person's hold the owner can choose "Back to
// automatic" or "Resume schedule"; both are queued and the worker sends them.
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { ControlActionOut, ControllerInfo, RoomStatus, UnitLive } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { dateTime, errorText } from '@/components/analysis/stats'
import { UNIT_COLORS, localTime, minutes, pct, temp } from '@/lib/format'
import { useControl } from '@/stores/control'
import { when } from './events'
import { CALL_INFO, HOLD_BADGES, equipment, ruleLabel } from './labels'

const props = defineProps<{
  unit: UnitLive
  rooms: RoomStatus[]
  mode: ControllerInfo['mode']
  tz: string
  now: string
  isOwner: boolean
  /** control.resume_backoff_hours (null until the settings load) */
  resumeBackoffHours: number | null
}>()
const control = useControl()

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

// Whose hold is running, as the API labels it ("On your hold since 2:10 PM (until you change
// it)", "Utility event until 6:00 PM (cooling +2°F)", "Our hold until 3:40 PM", ...).
const holdLine = computed(() => {
  const u = props.unit
  if (u.hold_label) {
    // Smart Away / Smart Home: the label is the badge.
    if (u.hold_owner === 'ecobee_auto') return { badge: { ...HOLD_BADGES.ecobee_auto, label: u.hold_label }, label: null }
    return { badge: u.hold_owner ? HOLD_BADGES[u.hold_owner] : null, label: u.hold_label }
  }
  const h = u.hold
  if (!h) return null
  // An older API without hold labels: say what the hold is, not whose.
  const what =
    h.kind === 'temperature' ? `Hold ${temp(h.heat_f)}–${temp(h.cool_f)}` : `Hold: ${h.climate_ref ?? 'comfort setting'}`
  return { badge: null, label: `${what} ${h.end ? `until ${localTime(h.end, props.tz)}` : 'until changed'}` }
})

const personHold = computed(
  () => !!props.unit.person_hold || props.unit.hold_owner === 'person' || props.unit.hold_owner === 'app',
)
// After someone pressed Resume, the ecobee schedule runs until then (a person's hold wins over it).
const backoffUntil = computed(() => {
  const t = props.unit.resume_backoff_until
  if (!t || personHold.value || Date.parse(t) <= Date.parse(props.now)) return null
  return when(t, props.tz, props.now)
})

/** Why the controller leaves this unit alone right now, if it does. */
const standAside = computed(() => {
  const owner = props.unit.hold_owner
  if (personHold.value) return 'The app writes nothing to this unit while the hold runs.'
  if (owner === 'utility') return 'The app stands aside during the utility event.'
  if (owner === 'vacation') return 'The app stands aside during the vacation.'
  if (owner === 'unknown_event') return 'The app is hands-off until this ecobee event ends.'
  if (backoffUntil.value) return `The app waits until ${backoffUntil.value}, then ${props.mode === 'act' ? 'steers' : 'plans'} again.`
  return null
})

// Vacations ahead (utility events have their own card).
const nextVacation = computed(() => {
  const v = (props.unit.upcoming_events ?? []).find((e) => e.event_type === 'vacation' && !e.running && e.start)
  if (!v?.start) return null
  return `Vacation ahead: ${dateTime(v.start, props.tz)}${v.end ? ` – ${dateTime(v.end, props.tz)}` : ''}`
})

// "Back to automatic" / "Resume schedule" (owner; refused while the controller is off).
const canAct = computed(() => props.isOwner && props.mode !== 'off')
const busy = ref<'automatic' | 'resume' | null>(null)
const actionError = ref('')
const result = ref<{ label: string; action: ControlActionOut } | null>(null)

function hoursText(h: number): string {
  return `${Number.isInteger(h) ? h : h.toFixed(1)} h`
}

const choiceText = computed(() => {
  const steer =
    props.mode === 'act' ? 'the app steers again now' : 'your hold ends now and the app plans again (Suggest mode writes nothing)'
  const h = props.resumeBackoffHours
  const resume =
    h === null
      ? 'your ecobee schedule runs for a while first'
      : h > 0
        ? `your ecobee schedule runs for ${hoursText(h)} first`
        : 'the app takes over again at once too (no wait after Resume is set)'
  return `Back to automatic: ${steer}. Resume schedule: ${resume}.`
})

// Once the hold or the wait changes, the card itself shows the outcome: drop the queued note.
watch(
  () => `${props.unit.hold_owner}|${props.unit.person_hold?.detection_id}|${props.unit.resume_backoff_until}`,
  () => (result.value = null),
)

async function act(kind: 'automatic' | 'resume') {
  if (busy.value) return
  busy.value = kind
  actionError.value = ''
  result.value = null
  try {
    const a = kind === 'automatic' ? await control.automatic(props.unit.unit_key) : await control.resume(props.unit.unit_key)
    result.value = { label: kind === 'automatic' ? 'Back to automatic' : 'Resume schedule', action: a }
  } catch (e) {
    actionError.value = errorText(e)
  } finally {
    busy.value = null
  }
}

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
        <div><dt class="inline text-muted">Heat </dt><dd class="inline font-semibold text-bad">{{ temp(unit.heat_sp_f) }}</dd></div>
        <div><dt class="inline text-muted">Cool </dt><dd class="inline font-semibold text-accent">{{ temp(unit.cool_sp_f) }}</dd></div>
      </dl>
    </div>

    <div class="mt-3">
      <h3 class="sr-only">Running now</h3>
      <ul v-if="running.length" class="flex flex-wrap gap-1.5">
        <li v-for="(e, i) in running" :key="i" class="chip bg-surface-2"><Icon :name="e.icon" :size="14" />{{ e.label }}</li>
      </ul>
      <p v-else class="text-xs text-muted">No equipment running.</p>
    </div>

    <div class="mt-2 text-sm">
      <p v-if="holdLine" class="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span v-if="holdLine.badge" class="chip" :class="holdLine.badge.cls">{{ holdLine.badge.label }}</span>
        <span v-if="holdLine.label" class="num font-medium">{{ holdLine.label }}</span>
      </p>
      <p v-if="backoffUntil" class="flex flex-wrap items-center gap-x-2 gap-y-1" :class="holdLine && 'mt-1'">
        <span class="chip bg-surface-2 text-ink">Schedule</span>
        <span class="font-medium">Following your ecobee schedule until {{ backoffUntil }}</span>
      </p>
      <p v-if="!holdLine && !backoffUntil">
        <span class="text-muted">Following its schedule</span><template v-if="unit.climate_ref"> · {{ unit.climate_ref }}</template>
      </p>
      <p v-if="nextVacation" class="mt-1 text-xs text-muted">{{ nextVacation }}</p>

      <div v-if="canAct && (personHold || backoffUntil)" class="mt-2">
        <div class="flex flex-wrap gap-2">
          <button type="button" class="btn btn-primary !py-1.5" :disabled="busy !== null" @click="act('automatic')">
            {{ busy === 'automatic' ? 'Queuing…' : 'Back to automatic' }}
          </button>
          <button v-if="personHold" type="button" class="btn !py-1.5" :disabled="busy !== null" @click="act('resume')">
            {{ busy === 'resume' ? 'Queuing…' : 'Resume schedule' }}
          </button>
        </div>
        <p v-if="personHold" class="mt-1 text-xs text-muted">{{ choiceText }}</p>
        <p v-else class="mt-1 text-xs text-muted">
          Back to automatic: the wait ends and the app {{ mode === 'act' ? 'steers' : 'plans' }} again now.
        </p>
      </div>
      <p v-if="actionError" role="alert" class="mt-1 text-xs text-bad">{{ actionError }}</p>
      <p v-if="result" class="mt-1 text-xs" aria-live="polite">
        {{ result.label }}: <span class="font-medium">{{ result.action.status }}</span>.
        <span class="text-muted">
          The worker sends it within seconds and reads it back; the outcome is in the
          <RouterLink to="/guardrails?tab=log" class="underline">action log</RouterLink>.
        </span>
      </p>
    </div>

    <div class="mt-3 rounded-xl bg-surface-2 p-3">
      <template v-if="unit.target">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-xs font-semibold tracking-wide text-muted uppercase">{{ targetLabel }}</h3>
          <span class="chip border border-line bg-surface text-muted">{{ ruleLabel(unit.target) }}</span>
        </div>
        <p class="num mt-1 font-semibold">
          Heat {{ temp(unit.target.heat_f) }} · Cool {{ temp(unit.target.cool_f) }}
          <span v-if="unit.target.desired === 'program' && !standAside" class="text-xs font-normal text-muted">· schedule already matches</span>
        </p>
        <p v-if="standAside" class="mt-1 text-sm font-medium">{{ standAside }}</p>
        <p class="mt-1 text-sm text-muted">{{ unit.target.reason }}</p>
        <p v-if="priorityRoom" class="mt-1 text-xs text-muted">Priority room: {{ priorityRoom }}</p>
      </template>
      <template v-else>
        <p class="text-sm text-muted">No policy target right now.</p>
        <p v-if="standAside" class="mt-1 text-sm font-medium">{{ standAside }}</p>
      </template>
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
