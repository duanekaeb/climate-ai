<script setup lang="ts">
// One utility energy-saving event (demand response) on Live: its name, status, window in house
// time, what it changes, which thermostats it reaches, any pre-cooling for it (the API words it
// as a fact only while the controller is doing it; in Suggest mode or on a suggest-only unit it
// says it would, and that nothing is written), and the owner's "Skip this event". A skip is an
// honest opt-out that ecobee records and the utility sees; it is only requested here, and the
// worker sends it once the event runs on that thermostat. Undo works until the worker starts
// sending it; after that the API answers 409 and its message is shown.
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { ControllerInfo, UtilityEventOut } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { errorText } from '@/components/analysis/stats'
import { useUtilityEvents } from '@/stores/utilityEvents'
import { GROUP_STATUS, eventWindow, when, type EventGroup } from './events'

const props = defineProps<{ group: EventGroup; tz: string; now: string; mode: ControllerInfo['mode']; isOwner: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const events = useUtilityEvents()

const status = computed(() => GROUP_STATUS[props.group.status])
const rows = computed(() => props.group.rows)
const names = (list: UtilityEventOut[]) => list.map((r) => r.unit_name).join(', ')
const headingId = computed(() => `event-${props.group.key}`)

const span = computed(() => eventWindow(props.group.start_at, props.group.end_at, props.tz, props.now))
// The change usually reads the same on every thermostat; when it doesn't, say it per unit.
const change = computed(() => {
  const labels = new Set(rows.value.map((r) => r.change_label))
  if (labels.size <= 1) return rows.value[0]?.change_label ?? ''
  return rows.value.map((r) => `${r.unit_name}: ${r.change_label}`).join('; ')
})
const preps = computed(() => [...new Set(rows.value.map((r) => r.prep_label).filter((p): p is string => !!p))])

const open = computed(() => props.group.status === 'announced' || props.group.status === 'running')
// A requested skip only matters while the event is announced or running.
const pending = computed(() => (open.value ? rows.value.filter((r) => r.skip === 'requested') : []))
const done = computed(() => rows.value.filter((r) => r.skip === 'done'))
const failed = computed(() => rows.value.filter((r) => r.skip === 'failed'))
const mandatory = computed(() => rows.value.filter((r) => r.is_optional === false || r.skip === 'refused'))
const skippable = computed(() => rows.value.filter((r) => r.can_skip))
// The rule's own sentence ("Girls' Room reached 79.5°F"), without its full stop.
const ruleReason = computed(() => pending.value.find((r) => r.skip_by === 'rule')?.skip_reason?.replace(/\.\s*$/, '') ?? null)
const doneAt = computed(() => {
  const t = done.value.map((r) => r.skip_done_at).find((x): x is string => !!x)
  return t ? when(t, props.tz, props.now) : null
})

const confirming = ref(false)
const busy = ref<'skip' | 'unskip' | null>(null)
const error = ref('')

async function skip() {
  const row = skippable.value[0]
  if (!row || busy.value) return
  confirming.value = false
  busy.value = 'skip'
  error.value = ''
  try {
    await events.skip(row.id)
    emit('changed')
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = null
  }
}

async function unskip() {
  const row = pending.value[0]
  if (!row || busy.value) return
  busy.value = 'unskip'
  error.value = ''
  try {
    await events.unskip(row.id)
    emit('changed')
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <article class="card border-accent/40" :aria-labelledby="headingId">
    <header class="flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0">
        <p class="card-title !text-xs">Utility event</p>
        <h2 :id="headingId" class="mt-0.5 font-semibold break-words">{{ group.name || 'Energy-saving event' }}</h2>
      </div>
      <span class="chip shrink-0" :class="status.cls">{{ status.label }}</span>
    </header>

    <dl class="mt-2 grid gap-x-3 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
      <dt class="text-muted">When</dt>
      <dd class="num">{{ span }}</dd>
      <template v-if="change">
        <dt class="text-muted">Change</dt>
        <dd class="break-words">{{ change }}</dd>
      </template>
      <dt class="text-muted">Thermostats</dt>
      <dd>{{ names(rows) }}</dd>
    </dl>
    <p v-if="group.status === 'running' && !pending.length" class="mt-2 text-sm text-muted">
      The app stands aside while it runs.
    </p>
    <p v-for="p in preps" :key="p" class="mt-2 flex items-start gap-1.5 text-sm">
      <Icon name="runtime" :size="16" class="mt-0.5 shrink-0 text-accent" />{{ p }}
    </p>

    <!-- skip state -->
    <div v-if="pending.length" class="mt-3 rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm" role="status">
      <p class="font-medium">
        Skip requested<template v-if="pending.length < rows.length"> on {{ names(pending) }}</template>.
      </p>
      <p v-if="ruleReason" class="mt-0.5">Your skip rule matched: {{ ruleReason }}.</p>
      <p class="mt-0.5 text-muted">
        <template v-if="mode === 'off'">The controller is off, so it waits until you switch to Suggest or Act.</template>
        <template v-else-if="group.status === 'announced'">It is sent when the event starts.</template>
        <template v-else>It is sent at the controller's next check; Undo works until it goes out.</template>
      </p>
      <button v-if="isOwner" type="button" class="btn mt-2 !py-1.5" :disabled="busy !== null" @click="unskip">
        {{ busy === 'unskip' ? 'Undoing…' : 'Undo' }}
      </button>
    </div>
    <p v-if="done.length" class="mt-3 flex items-start gap-1.5 text-sm text-good">
      <Icon name="check" :size="16" class="mt-0.5 shrink-0" />
      <span>
        Skipped<template v-if="done.length < rows.length"> on {{ names(done) }}</template><template v-if="doneAt"> at {{ doneAt }}</template>.
        ecobee recorded the opt-out; normal temperatures are back.
      </span>
    </p>
    <p v-if="failed.length" class="mt-3 text-sm text-bad">
      The skip didn't go through on {{ names(failed) }}.
      <RouterLink to="/guardrails?tab=log" class="underline">See the action log</RouterLink>.
    </p>
    <p v-if="mandatory.length" class="mt-3 text-sm text-muted">
      <template v-if="mandatory.length === rows.length">This event is mandatory; it can't be skipped.</template>
      <template v-else>Mandatory on {{ names(mandatory) }}; it can't be skipped there.</template>
    </p>

    <template v-if="isOwner && open && skippable.length && !pending.length">
      <div v-if="confirming" class="mt-3 rounded-xl border border-warn/40 bg-warn/10 p-3" role="alertdialog" :aria-labelledby="`${headingId}-confirm`">
        <p :id="`${headingId}-confirm`" class="text-sm">
          Skipping is an opt-out. ecobee records it and your utility sees it; it may cost this event's credit. Normal
          temperatures come back right away (or when it starts, if it hasn't yet).
        </p>
        <div class="mt-2 flex flex-wrap gap-2">
          <button type="button" class="btn btn-danger" :disabled="busy !== null" @click="skip">Skip</button>
          <button type="button" class="btn" :disabled="busy !== null" @click="confirming = false">Keep</button>
        </div>
      </div>
      <div v-else class="mt-3">
        <button type="button" class="btn" :disabled="busy !== null || mode === 'off'" @click="confirming = true">
          {{ busy === 'skip' ? 'Skipping…' : failed.length ? 'Try skipping again' : 'Skip this event' }}
        </button>
        <p v-if="mode === 'off'" class="mt-1 text-xs text-muted">
          The controller is off. Switch it to Suggest or Act to skip; the worker sends the opt-out.
        </p>
      </div>
    </template>
    <p v-if="error" role="alert" class="mt-2 text-sm text-bad">{{ error }}</p>

    <p class="mt-3 text-xs text-muted">
      <RouterLink to="/guardrails?tab=limits" class="underline">Utility event settings</RouterLink>
    </p>
  </article>
</template>
