<script setup lang="ts">
// Sleep windows per sleep room (they count as occupied all night; motion sensors can't see
// sleepers) and how long a room or the house must be quiet before it counts as empty.
import { computed } from 'vue'
import type { OccupancySettings, RoomStatus, SettingsOut, SleepWindow } from '@/api/types'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { prettyKey } from '@/components/analysis/stats'
import { useControl } from '@/stores/control'
import DaysPicker from './DaysPicker.vue'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'

const props = defineProps<{ settings: SettingsOut; rooms: RoomStatus[]; isOwner: boolean }>()
const control = useControl()

type Part = Pick<OccupancySettings, 'sleep_windows' | 'empty_after_min' | 'house_empty_after_min'>
const { draft, dirty, reset } = useDraft<Part>(() => ({
  sleep_windows: { ...props.settings.occupancy.sleep_windows },
  empty_after_min: props.settings.occupancy.empty_after_min,
  house_empty_after_min: props.settings.occupancy.house_empty_after_min,
}))
const saver = useSaver()

const names = computed(() => new Map(props.rooms.map((r) => [r.room_key, r.name])))
const noSensor = computed(() => new Set(props.rooms.filter((r) => !r.has_sensor).map((r) => r.room_key)))
const roomKeys = computed(() => {
  const keys = new Set([...props.rooms.filter((r) => r.is_sleep_room).map((r) => r.room_key), ...Object.keys(draft.value.sleep_windows)])
  return [...keys].sort((a, b) => (names.value.get(a) ?? a).localeCompare(names.value.get(b) ?? b))
})

function windows(room: string): SleepWindow[] {
  return draft.value.sleep_windows[room] ?? []
}
function add(room: string) {
  draft.value.sleep_windows = { ...draft.value.sleep_windows, [room]: [...windows(room), { start: '21:00', end: '07:00', days: [0, 1, 2, 3, 4, 5, 6] }] }
}
function remove(room: string, i: number) {
  const next = windows(room).filter((_, j) => j !== i)
  draft.value.sleep_windows = { ...draft.value.sleep_windows, [room]: next }
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/
function windowError(w: SleepWindow): string {
  if (!HHMM.test(w.start) || !HHMM.test(w.end)) return 'Pick a start and an end time.'
  if (w.start === w.end) return 'Start and end must differ.'
  if (!w.days.length) return 'Pick at least one night.'
  return ''
}

const problems = computed(() => {
  const out: string[] = []
  for (const k of roomKeys.value)
    windows(k).forEach((w, i) => {
      const e = windowError(w)
      if (e) out.push(`${names.value.get(k) ?? prettyKey(k)}, window ${i + 1}: ${e}`)
    })
  const e = draft.value.empty_after_min
  const h = draft.value.house_empty_after_min
  if (!Number.isInteger(e) || e < 5 || e > 240) out.push('A room counts as empty after 5–240 minutes.')
  if (!Number.isInteger(h) || h < 15 || h > 480) out.push('The house counts as empty after 15–480 minutes.')
  return out
})

async function save() {
  const ok = await saver.run(() =>
    control.saveSettings({ control: null, occupancy: { ...props.settings.occupancy, ...draft.value }, location: null }),
  )
  if (ok) reset()
}
</script>

<template>
  <Card title="Sleep and empty" subtitle="Sleep rooms count as occupied all night">
    <fieldset :disabled="!isOwner" class="space-y-3">
      <legend class="sr-only">Sleep windows</legend>
      <section v-for="k in roomKeys" :key="k" class="rounded-xl border border-line p-3">
        <div class="flex items-center justify-between gap-2">
          <h3 class="text-sm font-medium">
            {{ names.get(k) ?? prettyKey(k) }}
            <span v-if="noSensor.has(k)" class="chip ml-1 bg-surface-2 font-normal text-muted">no sensor</span>
          </h3>
          <button type="button" class="btn !px-2 !py-1 text-xs" @click="add(k)">+ Window</button>
        </div>
        <p v-if="!windows(k).length" class="mt-1 text-xs text-muted">No sleep window: occupancy comes from motion only.</p>
        <div v-for="(w, i) in windows(k)" :key="i" class="mt-2 space-y-2 rounded-lg bg-surface-2 p-2">
          <div class="flex flex-wrap items-end gap-2">
            <label class="text-xs text-muted">
              From
              <input v-model="w.start" type="time" class="input num mt-1 !w-32 dark:[color-scheme:dark]" />
            </label>
            <label class="text-xs text-muted">
              Until
              <input v-model="w.end" type="time" class="input num mt-1 !w-32 dark:[color-scheme:dark]" />
            </label>
            <button type="button" class="btn ml-auto !px-2 !py-1" :aria-label="`Remove window ${i + 1}`" @click="remove(k, i)">
              <Icon name="x" :size="14" />
            </button>
          </div>
          <div>
            <p class="mb-1 text-[11px] text-muted">Nights it starts on</p>
            <DaysPicker v-model="w.days" :label="`Nights for window ${i + 1}`" :disabled="!isOwner" />
          </div>
          <p v-if="windowError(w)" class="text-[11px] text-bad">{{ windowError(w) }}</p>
        </div>
      </section>
      <p v-if="!roomKeys.length" class="text-sm text-muted">No sleep rooms are configured.</p>

      <div class="grid grid-cols-2 gap-2">
        <label class="text-xs text-muted">
          A room is empty after (min)
          <input v-model.number="draft.empty_after_min" type="number" min="5" max="240" step="5" class="input num mt-1" />
        </label>
        <label class="text-xs text-muted">
          The house is empty after (min)
          <input v-model.number="draft.house_empty_after_min" type="number" min="15" max="480" step="5" class="input num mt-1" />
        </label>
      </div>
      <p class="text-xs text-muted">
        The house only counts as empty when the adults' phones are away, there's been no motion anywhere for that long,
        and it's outside every sleep window. When unsure, a room counts as occupied.
      </p>
    </fieldset>
    <ul v-if="problems.length" class="mt-2 space-y-0.5 text-xs text-bad">
      <li v-for="p in problems" :key="p">{{ p }}</li>
    </ul>
    <SaveBar
      :dirty="dirty"
      :valid="!problems.length"
      :busy="saver.busy.value"
      :error="saver.error.value"
      :saved="saver.saved.value"
      :disabled="!isOwner"
      @save="save"
      @reset="reset"
    />
  </Card>
</template>
