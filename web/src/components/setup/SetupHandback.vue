<script setup lang="ts">
// "Hand back to ecobee": switch the controller off, release its own holds and put back the
// ecobee settings it may have changed (Smart Away, Follow Me, the Home comfort setting's
// sensors) as they were captured before its first change. People's holds, vacations and
// utility events are left alone. The worker runs it as a job; this card polls while it runs
// and shows the app's writes as they are logged, then the job's steps.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { ControlActionOut, EcobeeOriginal, SensorOut, UnitOut } from '@/api/types'
import { onEvent } from '@/api/ws'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { dateTime } from '@/components/analysis/stats'
import { actionText } from '@/components/control/actionText'
import { MODE_INFO } from '@/components/live/labels'
import { useControl } from '@/stores/control'
import { useStatus } from '@/stores/status'
import { errorText } from './useAction'

const props = defineProps<{ units: UnitOut[]; sensors: SensorOut[] }>()
const control = useControl()
const status = useStatus()

const info = computed(() => control.handback.data)
const tz = computed(() => status.data?.tz)
const job = computed(() => info.value?.last_job ?? null)
const running = computed(() => job.value?.status === 'queued' || job.value?.status === 'running')

const sensorNames = computed(() => new Map(props.sensors.map((s) => [s.key, s.name])))
const unitName = (key: string | null) => (key ? (props.units.find((u) => u.key === key)?.name ?? key) : '')

/** What the hand-back puts back on one unit, as captured before the controller's first change. */
function restores(o: EcobeeOriginal | undefined): string[] {
  if (!o) return []
  const out: string[] = []
  if (o.auto_away !== null) out.push(`Smart Away ${o.auto_away ? 'on' : 'off'}`)
  if (o.follow_me !== null) out.push(`Follow Me ${o.follow_me ? 'on' : 'off'}`)
  if (o.home_sensors !== null) {
    const names = o.home_sensors.map((k) => sensorNames.value.get(k) ?? k)
    out.push(`Home sensors: ${names.length ? names.join(', ') : 'none'}`)
  }
  return out
}

const rows = computed(() =>
  props.units.map((u) => {
    const o = info.value?.original[u.key]
    return { key: u.key, name: u.name, captured: o?.captured_at ?? null, items: restores(o) }
  }),
)

// The app's hand-back writes logged since the job started (shown while it runs).
const progress = ref<ControlActionOut[]>([])
async function loadProgress() {
  const since = job.value?.created_at
  if (!since) return
  try {
    const list = await api.get<ControlActionOut[]>('/control/actions', { limit: 30 })
    progress.value = list.filter((a) => a.rule === 'handback' && Date.parse(a.ts) >= Date.parse(since)).reverse()
  } catch {
    /* the job's own steps still arrive when it finishes */
  }
}

const confirming = ref(false)
const busy = ref(false)
const error = ref('')

async function start() {
  if (busy.value) return
  confirming.value = false
  busy.value = true
  error.value = ''
  progress.value = []
  try {
    await control.startHandback()
    void control.loadHandback()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

// Poll every 2 s while the job is queued or running; stops by itself.
let timer: number | undefined
function stopPolling() {
  window.clearInterval(timer)
  timer = undefined
}
watch(
  running,
  (active) => {
    if (!active) {
      stopPolling()
      return
    }
    void loadProgress()
    if (timer !== undefined) return
    timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      void control.loadHandback()
      void loadProgress()
    }, 2000)
  },
  { immediate: true },
)

let off: (() => void) | undefined
onMounted(() => {
  void control.loadHandback()
  // The worker publishes the mode change and every logged write.
  off = onEvent((e) => {
    if (e.type === 'status' || e.type === 'action') void control.loadHandback()
  })
})
onBeforeUnmount(() => {
  off?.()
  stopPolling()
})

const steps = computed(() => info.value?.steps ?? [])
const failedSteps = computed(() => steps.value.filter((s) => !s.ok).length)
</script>

<template>
  <Card title="Hand back to ecobee" subtitle="Stop the app and put the thermostats back the way they were.">
    <AsyncState :loading="control.handback.loading && !info" :error="info ? '' : control.handback.error">
      <template v-if="info">
        <p class="text-sm">
          <template v-if="info.mode === 'off'">The controller is off. Handing back</template>
          <template v-else>
            The controller is in <span class="font-medium">{{ MODE_INFO[info.mode].label }}</span> mode. Handing back
            switches it off,
          </template>
          releases the app's own holds and puts back what it changed:
        </p>
        <ul class="mt-2 divide-y divide-line">
          <li v-for="r in rows" :key="r.key" class="py-2 text-sm first:pt-0 last:pb-0">
            <p class="font-medium">{{ r.name }}</p>
            <ul v-if="r.items.length" class="mt-0.5 list-disc pl-5 text-muted">
              <li v-for="item in r.items" :key="item" class="break-words">{{ item }}</li>
            </ul>
            <p v-else-if="r.captured" class="mt-0.5 text-xs text-muted">ecobee didn't report these settings for this thermostat.</p>
            <p v-else class="mt-0.5 text-xs text-muted">
              Nothing captured yet; the app records these settings from the first ecobee reading.
            </p>
            <p v-if="r.captured && r.items.length" class="mt-0.5 text-xs text-muted">As they were on {{ dateTime(r.captured, tz) }}</p>
          </li>
        </ul>
        <p class="mt-2 text-xs text-muted">Only settings that differ now are written, and each write is read back.</p>

        <!-- running -->
        <div v-if="running" class="mt-3 rounded-xl border border-accent/30 bg-accent/10 p-3 text-sm" role="status" aria-live="polite">
          <p class="font-medium">Handing back… ({{ job?.status }})</p>
          <ul v-if="info.mode === 'off' || progress.length" class="mt-1 space-y-0.5">
            <li v-if="info.mode === 'off'" class="flex items-start gap-1.5">
              <Icon name="check" :size="14" class="mt-0.5 shrink-0 text-good" />Controller off
            </li>
            <li v-for="a in progress" :key="a.id" class="flex items-start gap-1.5">
              <Icon :name="a.status === 'failed' ? 'x' : a.status === 'sent' ? 'refresh' : 'check'" :size="14"
                    class="mt-0.5 shrink-0" :class="a.status === 'failed' ? 'text-bad' : a.status === 'sent' ? 'text-muted' : 'text-good'" />
              <span class="min-w-0 break-words">{{ unitName(a.unit_key) }}: {{ actionText(a) }} · {{ a.status }}</span>
            </li>
          </ul>
        </div>

        <!-- confirm / start -->
        <template v-else>
          <div v-if="confirming" class="mt-3 rounded-xl border border-warn/40 bg-warn/10 p-3" role="alertdialog" aria-labelledby="handback-confirm">
            <p id="handback-confirm" class="text-sm">
              The app stops steering (mode Off), releases its own holds and restores these ecobee settings. Your own holds,
              vacations and utility events are left alone.
            </p>
            <div class="mt-2 flex flex-wrap gap-2">
              <button type="button" class="btn btn-primary" :disabled="busy" @click="start">Hand back</button>
              <button type="button" class="btn" :disabled="busy" @click="confirming = false">Cancel</button>
            </div>
          </div>
          <button v-else type="button" class="btn mt-3" :disabled="busy" @click="confirming = true">
            {{ busy ? 'Queuing…' : 'Hand back to ecobee' }}
          </button>
        </template>
        <p v-if="error" role="alert" class="mt-2 text-sm text-bad">{{ error }}</p>

        <!-- the last finished hand-back -->
        <div v-if="!running && job && (steps.length || job.error)" class="mt-4 border-t border-line pt-3">
          <p class="text-sm font-medium">
            Last hand-back<template v-if="job.finished_at">, {{ dateTime(job.finished_at, tz) }}</template>
            <span v-if="job.status === 'failed' || failedSteps" class="text-bad">
              · {{ job.status === 'failed' ? 'failed' : `${failedSteps} step${failedSteps === 1 ? '' : 's'} failed` }}
            </span>
          </p>
          <p v-if="job.error" class="mt-1 text-xs text-bad break-words">{{ job.error }}</p>
          <ul class="mt-1 space-y-1">
            <li v-for="(s, i) in steps" :key="i" class="flex items-start gap-1.5 text-sm">
              <Icon :name="s.ok ? 'check' : 'x'" :size="14" class="mt-1 shrink-0" :class="s.ok ? 'text-good' : 'text-bad'" />
              <span class="min-w-0 break-words">
                <template v-if="s.unit_key">{{ unitName(s.unit_key) }}: </template>{{ s.what }}
                <span v-if="s.detail" class="text-muted"> · {{ s.detail }}</span>
              </span>
            </li>
          </ul>
        </div>
      </template>
    </AsyncState>
  </Card>
</template>
