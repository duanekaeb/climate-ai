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

const table = computed(() => {
  const rows = Object.entries(roomMap.value)
    .map(([room, v]) => ({ room, name: names.value.get(room) ?? prettyKey(room), cells: new Map(leaves(v)) }))
    .filter((r) => r.cells.size > 0)
  const cols = [...new Set(rows.flatMap((r) => [...r.cells.keys()]))].sort()
  return { rows, cols }
})
</script>

<template>
  <div>
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
      <span>Trained {{ dayRange(fit.train_start, fit.train_end) }} · fitted {{ dateTime(fit.created_at, tz) }}</span>
      <span class="chip" :class="fitChip(fit.status)">{{ fit.status }}</span>
    </div>
    <p v-if="!table.rows.length" class="text-sm text-muted">This fit holds no room offsets.</p>
    <div v-else class="overflow-x-auto">
      <table class="num w-full text-sm">
        <thead>
          <tr class="text-left text-xs text-muted">
            <th class="py-1 pr-3 font-medium">Room</th>
            <th v-for="c in table.cols" :key="c" class="py-1 pr-3 text-right font-medium whitespace-nowrap">{{ c }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in table.rows" :key="r.room" class="border-t border-line">
            <td class="py-1.5 pr-3 whitespace-nowrap">{{ r.name }}</td>
            <td v-for="c in table.cols" :key="c" class="py-1.5 pr-3 text-right">
              {{ r.cells.has(c) ? signed(r.cells.get(c), 1, '°') : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-1 text-xs text-muted">
      Median room minus thermostat average. Rooms without a sensor have no offset; their temperature stays unknown.
    </p>
    <p v-if="fit.notes" class="mt-1 text-xs break-words">{{ fit.notes }}</p>
  </div>
</template>
