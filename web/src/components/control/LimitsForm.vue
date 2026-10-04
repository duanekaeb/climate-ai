<script setup lang="ts">
// Hard limits: enforced in code on every thermostat write, whoever asked for it. Owner only.
import { computed, useId } from 'vue'
import type { HardLimits, SettingsOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { useControl } from '@/stores/control'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()
const uid = useId()

interface Field {
  key: keyof HardLimits
  label: string
  unit: string
  step: number
  min: number
  max: number
  int?: boolean
}
// Sanity bounds for the inputs; the comfort-band bounds (45–80 / 65–92°F) match the API's.
const FIELDS: Field[] = [
  { key: 'min_heat_f', label: 'Lowest heat setpoint', unit: '°F', step: 0.5, min: 45, max: 80 },
  { key: 'max_heat_f', label: 'Highest heat setpoint', unit: '°F', step: 0.5, min: 45, max: 80 },
  { key: 'min_cool_f', label: 'Lowest cool setpoint', unit: '°F', step: 0.5, min: 65, max: 92 },
  { key: 'max_cool_f', label: 'Highest cool setpoint', unit: '°F', step: 0.5, min: 65, max: 92 },
  { key: 'min_deadband_f', label: 'Cool at least this far above heat', unit: '°F', step: 0.5, min: 0, max: 10 },
  { key: 'max_step_f', label: 'Largest change per write', unit: '°F', step: 0.5, min: 0.5, max: 5 },
  { key: 'min_minutes_between_changes', label: 'Minutes between changes, per unit', unit: 'min', step: 5, min: 5, max: 240, int: true },
  { key: 'min_hold_hours', label: 'Shortest hold', unit: 'h', step: 1, min: 1, max: 2, int: true },
  { key: 'max_hold_hours', label: 'Longest hold', unit: 'h', step: 1, min: 1, max: 2, int: true },
  { key: 'max_indoor_rh', label: "Don't raise cooling above this humidity", unit: '%', step: 1, min: 30, max: 80 },
  { key: 'min_run_minutes', label: 'Shortest compressor run', unit: 'min', step: 1, min: 0, max: 30, int: true },
]

const { draft, dirty, reset } = useDraft<HardLimits>(() => ({ ...props.settings.control.limits }))
const saver = useSaver()

const problems = computed(() => {
  const d = draft.value
  const out: string[] = []
  for (const f of FIELDS) {
    const v = d[f.key]
    if (typeof v !== 'number' || Number.isNaN(v)) out.push(`${f.label}: enter a number.`)
    else if (v < f.min || v > f.max) out.push(`${f.label}: ${f.min}–${f.max} ${f.unit}.`)
    else if (f.int && !Number.isInteger(v)) out.push(`${f.label}: whole numbers only.`)
  }
  if (out.length) return out
  if (d.min_heat_f >= d.max_heat_f) out.push('Lowest heat must be below highest heat.')
  if (d.min_cool_f >= d.max_cool_f) out.push('Lowest cool must be below highest cool.')
  if (d.min_hold_hours > d.max_hold_hours) out.push('Shortest hold must not exceed longest hold.')
  if (d.min_heat_f + d.min_deadband_f > d.max_cool_f) out.push('No setpoint pair fits: raise the cool ceiling or lower the heat floor.')
  return out
})

/** Comfort bands the new limits would clamp (a warning, not an error). */
const clamped = computed(() => {
  const d = draft.value
  let n = 0
  for (const uc of Object.values(props.settings.control.comfort))
    for (const b of [uc.day, uc.night, uc.away])
      if (b.heat_f < d.min_heat_f || b.heat_f > d.max_heat_f || b.cool_f < d.min_cool_f || b.cool_f > d.max_cool_f) n++
  return n
})

async function save() {
  const ok = await saver.run(() =>
    control.saveSettings({ control: { ...props.settings.control, limits: draft.value }, occupancy: null, location: null }),
  )
  if (ok) reset()
}
</script>

<template>
  <Card title="Hard limits" subtitle="Checked on every write, whoever asked for it">
    <fieldset :disabled="!isOwner">
      <legend class="sr-only">Hard limits</legend>
      <div class="grid grid-cols-2 gap-x-3 gap-y-2">
        <label v-for="f in FIELDS" :key="f.key" :for="`${uid}-${f.key}`" class="flex flex-col justify-end text-xs text-muted">
          <span>{{ f.label }}</span>
          <span class="mt-1 flex items-center gap-1">
            <input
              :id="`${uid}-${f.key}`"
              v-model.number="draft[f.key]"
              type="number"
              inputmode="decimal"
              :step="f.step"
              :min="f.min"
              :max="f.max"
              class="input num"
            />
            <span class="w-7 shrink-0">{{ f.unit }}</span>
          </span>
        </label>
      </div>
    </fieldset>
    <ul v-if="problems.length" class="mt-2 space-y-0.5 text-xs text-bad">
      <li v-for="p in problems" :key="p">{{ p }}</li>
    </ul>
    <p v-else-if="clamped" class="mt-2 text-xs text-warn">
      {{ clamped }} comfort {{ clamped === 1 ? 'band falls' : 'bands fall' }} outside these limits; the controller will
      clamp {{ clamped === 1 ? 'it' : 'them' }}.
    </p>
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
