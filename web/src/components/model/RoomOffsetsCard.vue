<script setup lang="ts">
// Learned room offsets: how much warmer (+) or cooler (−) each sensored room typically runs
// than its thermostat's averaged temperature, by period and mode. The controller uses them to
// pick a setpoint that lands the priority room in its band.
import { computed } from 'vue'
import type { ModelFitOut, RoomStatus } from '@/api/types'
import { dateTime, dayRange, prettyKey, signed } from '@/components/analysis/stats'
import { fitChip } from './fitText'

const props = defineProps<{ fit: ModelFitOut; rooms: RoomStatus[]; tz?: string }>()

type Leaf = [path: string, value: number]

function leaves(v: unknown, path: string[] = []): Leaf[] {
  if (typeof v === 'number' && Number.isFinite(v)) return [[path.join(' · ') || 'offset', v]]
  if (!v || typeof v !== 'object' || Array.isArray(v)) return []
  return Object.entries(v as Record<string, unknown>).flatMap(([k, child]) => leaves(child, [...path, k]))
}

/** Rooms live either at the top of params or under one wrapper key ("rooms", "offsets"). */
const roomMap = computed<Record<string, unknown>>(() => {
  const p = props.fit.params
  for (const wrap of ['rooms', 'offsets', 'room_offsets']) {
    const inner = p[wrap]
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) return inner as Record<string, unknown>
  }
  return p
})

const names = computed(() => new Map(props.rooms.map((r) => [r.room_key, r.name])))

const rows = computed(() =>
  Object.entries(roomMap.value)
    .map(([room, v]) => ({
      room,
      name: names.value.get(room) ?? prettyKey(room),
      cells: leaves(v).sort((a, b) => a[0].localeCompare(b[0])),
    }))
    .filter((r) => r.cells.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name)),
)
</script>

<template>
  <div>
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
      <span>Trained {{ dayRange(fit.train_start, fit.train_end) }} · fitted {{ dateTime(fit.created_at, tz) }}</span>
      <span class="chip" :class="fitChip(fit.status)">{{ fit.status }}</span>
    </div>
    <p v-if="!rows.length" class="text-sm text-muted">This fit holds no room offsets.</p>
    <ul v-else class="divide-y divide-line">
      <li v-for="r in rows" :key="r.room" class="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2">
        <span class="w-28 shrink-0 text-sm font-medium">{{ r.name }}</span>
        <span class="flex min-w-0 flex-1 flex-wrap gap-1">
          <span v-for="[label, value] in r.cells" :key="label" class="chip num bg-surface-2 text-ink">
            <span class="text-muted">{{ label }}</span> {{ signed(value, 1, '°') }}
          </span>
        </span>
      </li>
    </ul>
    <p class="mt-1 text-xs text-muted">
      Median room minus thermostat average. Rooms without a sensor have no offset; their temperature stays unknown.
    </p>
    <p v-if="fit.notes" class="mt-1 text-xs break-words">{{ fit.notes }}</p>
  </div>
</template>
