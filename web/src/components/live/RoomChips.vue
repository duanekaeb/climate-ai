<script setup lang="ts">
// Rooms at a glance on Live: one chip per room, grouped by floor, colored by occupancy state.
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { RoomStatus } from '@/api/types'
import { STATE_COLORS, temp } from '@/lib/format'
import { NO_SENSOR_TEXT, STATE_LABELS, groupByFloor } from '@/stores/rooms'

const props = defineProps<{ rooms: RoomStatus[] }>()
const floors = computed(() => groupByFloor(props.rooms))

function reading(r: RoomStatus): string {
  if (!r.has_sensor) return NO_SENSOR_TEXT
  return r.temp_f === null ? 'no reading' : temp(r.temp_f)
}
function label(r: RoomStatus): string {
  const bits = [r.name, STATE_LABELS[r.state], reading(r)]
  if (r.is_priority) bits.push('priority room')
  if (r.stale) bits.push('data is stale')
  return bits.join(', ')
}
</script>

<template>
  <div class="space-y-3">
    <div v-for="f in floors" :key="f.key">
      <h3 class="mb-1.5 text-xs font-semibold text-muted">{{ f.label }}</h3>
      <ul class="flex flex-wrap gap-1.5">
        <li v-for="r in f.rooms" :key="r.room_key" class="min-w-0">
          <RouterLink :to="{ path: '/rooms', query: { room: r.room_key } }" :aria-label="label(r)"
                      class="chip max-w-full border border-line bg-surface-2 !py-1 text-ink hover:border-accent">
            <span class="h-2 w-2 shrink-0 rounded-full" :style="{ background: STATE_COLORS[r.state] }" aria-hidden="true" />
            <span class="truncate" :class="r.is_priority ? 'font-semibold' : 'font-normal'">{{ r.name }}</span>
            <span class="num shrink-0" :class="r.has_sensor && r.temp_f !== null ? '' : 'font-normal text-muted'">{{ reading(r) }}</span>
            <span v-if="r.stale" class="shrink-0 text-warn" aria-hidden="true">!</span>
          </RouterLink>
        </li>
      </ul>
    </div>
    <ul class="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted" aria-label="Legend">
      <li v-for="s in (['occupied', 'asleep', 'empty', 'unknown'] as const)" :key="s" class="flex items-center gap-1">
        <span class="h-2 w-2 rounded-full" :style="{ background: STATE_COLORS[s] }" aria-hidden="true" />{{ STATE_LABELS[s] }}
      </li>
      <li class="font-semibold">Bold = priority room</li>
    </ul>
  </div>
</template>
