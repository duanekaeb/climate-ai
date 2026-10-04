<script setup lang="ts">
// One room in the Rooms list. A button: tapping it opens the room's history.
import { computed } from 'vue'
import type { RoomStatus } from '@/api/types'
import { STATE_COLORS, localDate, localTime, pct, temp } from '@/lib/format'
import { NO_SENSOR_TEXT, STATE_CHIP, STATE_LABELS, motionText } from '@/stores/rooms'

const props = defineProps<{ room: RoomStatus; selected: boolean; tz: string }>()
const emit = defineEmits<{ select: [] }>()

const since = computed(() => {
  const s = props.room.since
  if (!s) return ''
  const recent = Date.now() - new Date(s).getTime() < 20 * 3600_000
  return `since ${recent ? localTime(s, props.tz) : localDate(s, props.tz)}`
})
const motion = computed(() => motionText(props.room.seconds_since_motion))
</script>

<template>
  <button type="button" :aria-expanded="selected" aria-controls="room-detail"
          class="card block w-full !p-3 text-left transition-colors hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          :class="selected ? '!border-accent ring-1 ring-accent' : ''" @click="emit('select')">
    <div class="flex items-start gap-3">
      <div class="min-w-0 flex-1">
        <p class="flex flex-wrap items-center gap-1.5">
          <span class="h-2.5 w-2.5 shrink-0 rounded-full" :style="{ background: STATE_COLORS[room.state] }" aria-hidden="true" />
          <span class="min-w-0 font-semibold break-words">{{ room.name }}</span>
          <span v-if="room.is_priority" class="chip bg-accent/15 text-accent">Priority</span>
          <span v-if="room.stale" class="chip bg-warn/15 text-warn">Stale</span>
        </p>
        <p v-if="!room.has_sensor" class="mt-0.5 text-sm text-muted">{{ NO_SENSOR_TEXT }}</p>
        <p class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs">
          <span class="chip" :class="STATE_CHIP[room.state]">{{ STATE_LABELS[room.state] }}</span>
          <span v-if="since" class="text-muted">{{ since }}</span>
        </p>
      </div>
      <div v-if="room.has_sensor" class="shrink-0 text-right">
        <p v-if="room.temp_f === null" class="text-sm text-muted">no reading</p>
        <p v-else class="num text-2xl leading-none font-semibold">{{ temp(room.temp_f) }}</p>
        <p v-if="room.humidity !== null" class="num mt-1 text-xs text-muted">{{ pct(room.humidity) }} RH</p>
      </div>
    </div>
    <p v-if="room.reason" class="mt-2 line-clamp-2 text-sm text-muted">{{ room.reason }}</p>
    <p v-if="motion || room.is_sleep_room" class="mt-1 text-xs text-muted">
      <span v-if="motion" class="num">{{ motion }}</span>
      <span v-if="motion && room.is_sleep_room"> · </span>
      <span v-if="room.is_sleep_room">sleep room</span>
    </p>
  </button>
</template>
