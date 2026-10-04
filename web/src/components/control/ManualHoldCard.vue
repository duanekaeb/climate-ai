<script setup lang="ts">
// The owner's manual timed hold and resume. Both are queued: the worker writes them through
// the same guardrails as the controller, reads them back and logs them in the action log.
import { computed, ref, useId, watch } from 'vue'
import type { ControlActionOut, SettingsOut, UnitLive } from '@/api/types'
import Card from '@/components/Card.vue'
import { temp } from '@/lib/format'
import { errorText } from '@/components/analysis/stats'
import { useControl } from '@/stores/control'
import { unitKeys, unitName } from './units'

const props = defineProps<{ settings: SettingsOut; units: UnitLive[]; isOwner: boolean }>()
const control = useControl()
const uid = useId()

const keys = computed(() => unitKeys(props.settings))
const unit = ref(keys.value[0] ?? 'main')
const heat = ref<number>(68)
const cool = ref<number>(76)
const limits = computed(() => props.settings.control.limits)
const hourOptions = computed(() => [1, 2].filter((h) => h >= limits.value.min_hold_hours && h <= limits.value.max_hold_hours))
const hours = ref<number>(2)
const busy = ref<'hold' | 'resume' | null>(null)
const error = ref('')
const result = ref<ControlActionOut | null>(null)

const live = computed(() => props.units.find((u) => u.unit_key === unit.value) ?? null)

function prefill() {
  const band = props.settings.control.comfort[unit.value]?.day
  heat.value = live.value?.heat_sp_f ?? band?.heat_f ?? 68
  cool.value = live.value?.cool_sp_f ?? band?.cool_f ?? 76
}
// Prefill when the unit changes, and once the live setpoints arrive.
watch(() => [unit.value, live.value !== null] as const, prefill, { immediate: true })
watch(
  hourOptions,
  (opts) => {
    if (!opts.includes(hours.value)) hours.value = opts[opts.length - 1] ?? 1
  },
  { immediate: true },
)

const problems = computed(() => {
  const l = limits.value
  const out: string[] = []
  const h = heat.value
  const c = cool.value
  if (typeof h !== 'number' || Number.isNaN(h)) out.push('Enter a heat setpoint.')
  else if (h < l.min_heat_f || h > l.max_heat_f) out.push(`Heat must be ${l.min_heat_f}–${l.max_heat_f}°F (your hard limits).`)
  if (typeof c !== 'number' || Number.isNaN(c)) out.push('Enter a cool setpoint.')
  else if (c < l.min_cool_f || c > l.max_cool_f) out.push(`Cool must be ${l.min_cool_f}–${l.max_cool_f}°F (your hard limits).`)
  if (typeof h === 'number' && typeof c === 'number' && c - h < l.min_deadband_f)
    out.push(`Keep cool at least ${l.min_deadband_f}°F above heat.`)
  if (!hourOptions.value.includes(hours.value)) out.push('Pick a hold length inside your limits.')
  return out
})

async function act(kind: 'hold' | 'resume') {
  if (busy.value || (kind === 'hold' && problems.value.length)) return
  busy.value = kind
  error.value = ''
  result.value = null
  try {
    result.value =
      kind === 'hold'
        ? await control.hold({ unit_key: unit.value, heat_f: heat.value, cool_f: cool.value, hours: hours.value })
        : await control.resume(unit.value)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <Card title="Manual hold" subtitle="A timed hold that expires on its own">
    <fieldset :disabled="!isOwner" class="space-y-3">
      <legend class="sr-only">Manual hold</legend>
      <label :for="`${uid}-unit`" class="block text-xs text-muted">
        Unit
        <select :id="`${uid}-unit`" v-model="unit" class="input mt-1">
          <option v-for="k in keys" :key="k" :value="k">{{ unitName(k) }}</option>
        </select>
      </label>
      <p v-if="live" class="num text-xs text-muted">
        Now {{ temp(live.zone_temp_f) }} · heat {{ temp(live.heat_sp_f) }} · cool {{ temp(live.cool_sp_f) }}
        <template v-if="live.hold"> · on a {{ live.hold.set_by_us ? 'Climate AI' : 'manual' }} hold</template>
      </p>
      <div class="grid grid-cols-3 gap-2">
        <label class="text-xs text-muted">
          Heat °F
          <input v-model.number="heat" type="number" step="0.5" :min="limits.min_heat_f" :max="limits.max_heat_f" class="input num mt-1" />
        </label>
        <label class="text-xs text-muted">
          Cool °F
          <input v-model.number="cool" type="number" step="0.5" :min="limits.min_cool_f" :max="limits.max_cool_f" class="input num mt-1" />
        </label>
        <label class="text-xs text-muted">
          For
          <select v-model.number="hours" class="input num mt-1">
            <option v-for="h in hourOptions" :key="h" :value="h">{{ h }} h</option>
          </select>
        </label>
      </div>
      <ul v-if="problems.length" class="text-xs text-bad">
        <li v-for="p in problems" :key="p">{{ p }}</li>
      </ul>
      <div class="flex flex-wrap gap-2">
        <button type="button" class="btn btn-primary" :disabled="busy !== null || problems.length > 0" @click="act('hold')">
          {{ busy === 'hold' ? 'Queuing…' : 'Hold' }}
        </button>
        <button type="button" class="btn" :disabled="busy !== null" @click="act('resume')">
          {{ busy === 'resume' ? 'Queuing…' : 'Resume schedule' }}
        </button>
      </div>
    </fieldset>
    <p v-if="error" class="mt-2 text-sm text-bad">{{ error }}</p>
    <p v-if="result" class="mt-2 text-sm" aria-live="polite">
      {{ result.action }} for {{ unitName(result.unit_key) }}: <span class="font-medium">{{ result.status }}</span>.
      <span class="text-muted">The worker sends it within seconds, reads it back and logs the result below.</span>
    </p>
    <p class="mt-2 text-xs text-muted">
      The hold ends by itself after the time you pick; Resume schedule hands the unit back to its ecobee schedule now.
      <template v-if="!isOwner"> Only the owner can hold or resume.</template>
    </p>
  </Card>
</template>
