<script setup lang="ts">
// Detail for one room: what the app thinks right now plus the last 24 or 72 hours.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RoomStatus } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Icon from '@/components/Icon.vue'
import { UNIT_NAMES, localDate, localTime, pct, temp } from '@/lib/format'
import { NO_SENSOR_TEXT, STATE_CHIP, STATE_LABELS, floorLabel, motionText, useRooms } from '@/stores/rooms'
import RoomHistoryChart from './RoomHistoryChart.vue'
import { occupancyBands } from './roomChart'

const props = defineProps<{ room: RoomStatus; tz: string; autofocus?: boolean }>()
const emit = defineEmits<{ close: [] }>()

const store = useRooms()
const hours = ref(24)
const closeBtn = ref<HTMLButtonElement | null>(null)

function load() {
  store.loadHistory(props.room.room_key, hours.value)
}
watch(() => [props.room.room_key, hours.value], load, { immediate: true })

// Readings land every 5 minutes; refresh while the panel stays open.
let timer: number | undefined
onMounted(() => {
  if (props.autofocus) closeBtn.value?.focus()
  timer = window.setInterval(() => document.visibilityState === 'visible' && load(), 5 * 60_000)
})
onBeforeUnmount(() => window.clearInterval(timer))

const history = computed(() =>
  store.history && store.history.room_key === props.room.room_key && store.history.hours === hours.value ? store.history : null,
)
const empty = computed(() => !!history.value && !history.value.points.length && !history.value.setpoints.length)

const since = computed(() => {
  const s = props.room.since
  if (!s) return ''
  const recent = Date.now() - new Date(s).getTime() < 20 * 3600_000
  return `since ${recent ? localTime(s, props.tz) : `${localDate(s, props.tz)}, ${localTime(s, props.tz)}`}`
})

const stats = computed(() => {
  const h = history.value
  if (!h) return null
  const temps = props.room.has_sensor ? h.points.map((p) => p.temp_f).filter((v): v is number => v !== null) : []
  let inUseMs = 0
  for (const b of occupancyBands(h.points)) inUseMs += b.end - b.start
  const times = h.points.map((p) => Date.parse(p.ts))
  const spanMs = times.length > 1 ? Math.max(...times) - Math.min(...times) : 0
  return {
    low: temps.length ? Math.min(...temps) : null,
    high: temps.length ? Math.max(...temps) : null,
    avg: temps.length ? temps.reduce((a, b) => a + b, 0) / temps.length : null,
    inUsePct: spanMs > 0 ? Math.min(100, (inUseMs / spanMs) * 100) : null,
  }
})

const offsetText = computed(() => {
  const o = props.room.offset_f
  if (o === null || !props.room.has_sensor) return ''
  return `${o > 0 ? '+' : ''}${o.toFixed(1)}°F`
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-start gap-3">
      <div class="min-w-0 flex-1">
        <p class="text-xs text-muted">{{ floorLabel(room.floor) }} · {{ UNIT_NAMES[room.unit_key] ?? room.unit_key }} unit</p>
        <h2 id="room-detail-title" class="text-lg font-semibold">{{ room.name }}</h2>
      </div>
      <button ref="closeBtn" type="button" class="btn shrink-0 !p-1.5" aria-label="Close room details" @click="emit('close')">
        <Icon name="x" :size="18" />
      </button>
    </div>

    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p v-if="!room.has_sensor" class="font-medium text-muted">{{ NO_SENSOR_TEXT }}</p>
        <p v-else-if="room.temp_f === null" class="text-muted">No reading right now</p>
        <p v-else class="num text-4xl leading-none font-semibold">{{ temp(room.temp_f) }}<span class="text-base font-normal text-muted">F</span></p>
        <p class="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
          <span class="chip" :class="STATE_CHIP[room.state]">{{ STATE_LABELS[room.state] }}</span>
          <span v-if="since" class="text-muted">{{ since }}</span>
          <span v-if="room.is_priority" class="chip bg-accent/15 text-accent">Priority room</span>
          <span v-if="room.stale" class="chip bg-warn/15 text-warn">Stale data</span>
        </p>
      </div>
    </div>

    <p v-if="room.reason" class="text-sm">{{ room.reason }}</p>

    <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
      <div>
        <dt class="text-xs text-muted">Confidence</dt>
        <dd class="num">{{ pct(room.confidence * 100) }}</dd>
      </div>
      <div v-if="room.seconds_since_motion !== null">
        <dt class="text-xs text-muted">Motion</dt>
        <dd class="num">{{ motionText(room.seconds_since_motion) }}</dd>
      </div>
      <div v-if="offsetText">
        <dt class="text-xs text-muted">Offset vs thermostat</dt>
        <dd class="num">{{ offsetText }}</dd>
      </div>
      <div v-if="room.has_sensor && room.humidity !== null">
        <dt class="text-xs text-muted">Humidity</dt>
        <dd class="num">{{ pct(room.humidity) }}</dd>
      </div>
      <div>
        <dt class="text-xs text-muted">Comfort target</dt>
        <dd>{{ room.has_comfort_target ? (room.is_sleep_room ? 'Yes · sleep room' : 'Yes') : 'None (walk-through space)' }}</dd>
      </div>
    </dl>

    <p v-if="!room.has_sensor" class="rounded-xl bg-surface-2 p-3 text-xs text-muted">
      This room has no sensor, so its temperature is never estimated. The chart shows its unit's setpoints and the
      occupancy the app assumes{{ room.is_sleep_room ? ' from the sleep schedule' : '' }}.
    </p>

    <section aria-labelledby="room-history-title" class="space-y-2">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h3 id="room-history-title" class="card-title">History</h3>
        <div class="inline-flex rounded-xl border border-line p-0.5" role="group" aria-label="History length">
          <button v-for="h in [24, 72]" :key="h" type="button" :aria-pressed="hours === h"
                  class="rounded-lg px-3 py-1 text-xs font-medium"
                  :class="hours === h ? 'bg-accent text-white' : 'text-muted hover:bg-surface-2'" @click="hours = h">
            {{ h }} h
          </button>
        </div>
      </div>
      <AsyncState :loading="store.loading && !history" :error="history ? '' : store.error" :empty="empty"
                  empty-text="No history for this room yet.">
        <template v-if="history">
          <RoomHistoryChart :history="history" :has-sensor="room.has_sensor" :unit-key="room.unit_key" :tz="tz" />
          <dl v-if="stats" class="mt-3 grid grid-cols-2 gap-2 text-center sm:grid-cols-4">
            <template v-if="room.has_sensor">
              <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">Low</dt><dd class="num font-semibold">{{ temp(stats.low) }}</dd></div>
              <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">Average</dt><dd class="num font-semibold">{{ temp(stats.avg) }}</dd></div>
              <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">High</dt><dd class="num font-semibold">{{ temp(stats.high) }}</dd></div>
            </template>
            <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">In use</dt><dd class="num font-semibold">{{ pct(stats.inUsePct) }}</dd></div>
          </dl>
          <p v-if="store.error" class="mt-2 text-xs text-warn">Couldn't refresh: {{ store.error }}</p>
        </template>
      </AsyncState>
    </section>
  </div>
</template>
