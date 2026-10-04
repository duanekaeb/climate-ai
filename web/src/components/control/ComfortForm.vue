<script setup lang="ts">
// Comfort bands per unit: day, night (sleep) and away (whole house empty), heat and cool.
// Heat must sit below cool by at least the hard limit's minimum gap.
import { computed } from 'vue'
import type { ComfortBand, SettingsOut, UnitComfort } from '@/api/types'
import Card from '@/components/Card.vue'
import { useControl } from '@/stores/control'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'
import { unitKeys, unitName } from './units'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()

// The API's ComfortBand bounds.
const HEAT = { min: 45, max: 80 }
const COOL = { min: 65, max: 92 }
const PERIODS: { key: keyof UnitComfort; label: string; help: string }[] = [
  { key: 'day', label: 'Day', help: 'occupied' },
  { key: 'night', label: 'Night', help: 'sleep windows' },
  { key: 'away', label: 'Away', help: 'house empty' },
]

const { draft, dirty, reset } = useDraft<Record<string, UnitComfort>>(() => ({ ...props.settings.control.comfort }))
const saver = useSaver()
const units = computed(() => unitKeys(props.settings).filter((k) => draft.value[k]))
const limits = computed(() => props.settings.control.limits)

function bandError(b: ComfortBand): string {
  const { heat_f: h, cool_f: c } = b
  if (typeof h !== 'number' || Number.isNaN(h) || typeof c !== 'number' || Number.isNaN(c)) return 'Enter both setpoints.'
  if (h < HEAT.min || h > HEAT.max) return `Heat ${HEAT.min}–${HEAT.max}°F.`
  if (c < COOL.min || c > COOL.max) return `Cool ${COOL.min}–${COOL.max}°F.`
  if (c - h < limits.value.min_deadband_f) return `Cool must be at least ${limits.value.min_deadband_f}°F above heat.`
  return ''
}

function bandWarning(b: ComfortBand): string {
  const l = limits.value
  if (b.heat_f < l.min_heat_f || b.heat_f > l.max_heat_f || b.cool_f < l.min_cool_f || b.cool_f > l.max_cool_f)
    return 'Outside your hard limits: the controller will clamp it.'
  return ''
}

const errorCount = computed(() =>
  units.value.reduce((n, k) => n + PERIODS.filter((p) => bandError(draft.value[k][p.key])).length, 0),
)

async function save() {
  const ok = await saver.run(() =>
    control.saveSettings({ control: { ...props.settings.control, comfort: draft.value }, occupancy: null, location: null }),
  )
  if (ok) reset()
}
</script>

<template>
  <Card title="Comfort bands" subtitle="°F the controller keeps occupied rooms inside">
    <fieldset :disabled="!isOwner" class="grid gap-3 lg:grid-cols-3">
      <legend class="sr-only">Comfort bands</legend>
      <section v-for="k in units" :key="k" class="rounded-xl border border-line p-3">
        <h3 class="mb-2 inline-flex items-center gap-1.5 text-sm font-medium">
          <span class="h-2.5 w-2.5 rounded-full" :style="{ background: `var(--color-unit-${k})` }" />{{ unitName(k) }}
        </h3>
        <div class="grid grid-cols-[4.5rem_1fr_1fr] items-end gap-x-2 gap-y-1 text-xs text-muted">
          <span />
          <span>Heat</span>
          <span>Cool</span>
          <template v-for="p in PERIODS" :key="p.key">
            <span class="self-center leading-tight">
              <span class="block text-sm text-ink">{{ p.label }}</span>{{ p.help }}
            </span>
            <input
              v-model.number="draft[k][p.key].heat_f"
              type="number"
              step="0.5"
              :min="HEAT.min"
              :max="HEAT.max"
              class="input num"
              :aria-label="`${unitName(k)} ${p.label} heat`"
            />
            <input
              v-model.number="draft[k][p.key].cool_f"
              type="number"
              step="0.5"
              :min="COOL.min"
              :max="COOL.max"
              class="input num"
              :aria-label="`${unitName(k)} ${p.label} cool`"
            />
            <p
              v-if="bandError(draft[k][p.key]) || bandWarning(draft[k][p.key])"
              class="col-span-3 text-[11px]"
              :class="bandError(draft[k][p.key]) ? 'text-bad' : 'text-warn'"
            >
              {{ p.label }}: {{ bandError(draft[k][p.key]) || bandWarning(draft[k][p.key]) }}
            </p>
          </template>
        </div>
      </section>
    </fieldset>
    <p class="mt-2 text-xs text-muted">
      An empty main floor by day has no band of its own: the linked-floors rule ties it to the upstairs.
    </p>
    <SaveBar
      :dirty="dirty"
      :valid="errorCount === 0"
      :busy="saver.busy.value"
      :error="saver.error.value"
      :saved="saver.saved.value"
      :disabled="!isOwner"
      @save="save"
      @reset="reset"
    />
  </Card>
</template>
