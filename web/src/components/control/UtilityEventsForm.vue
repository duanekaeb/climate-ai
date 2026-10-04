<script setup lang="ts">
// What the app does around utility energy-saving events (demand response): alerts, automatic
// skips and pre-cooling. While an event runs the controller stands aside; the only ways out
// are skips, which are honest opt-outs ecobee records and the utility sees. Owner only.
import { computed, useId } from 'vue'
import type { SettingsOut, UtilityEventSettings } from '@/api/types'
import Card from '@/components/Card.vue'
import { useControl } from '@/stores/control'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()
const uid = useId()

const { draft, dirty, reset } = useDraft<UtilityEventSettings>(() => ({ ...props.settings.utility_events }))
const saver = useSaver()

// An empty box turns a temperature rule off (null); v-model.number leaves '' for an empty box.
function optionalNumber(key: 'skip_above_f' | 'skip_below_f') {
  return computed<number | ''>({
    get: () => draft.value[key] ?? '',
    set: (v) => {
      draft.value = { ...draft.value, [key]: v === '' || Number.isNaN(v) ? null : Number(v) }
    },
  })
}
const above = optionalNumber('skip_above_f')
const below = optionalNumber('skip_below_f')

const problems = computed(() => {
  const d = draft.value
  const out: string[] = []
  if (d.skip_above_f !== null && !(d.skip_above_f >= 72 && d.skip_above_f <= 90)) out.push('Skip when a room reaches: 72–90°F, or leave it empty.')
  if (d.skip_below_f !== null && !(d.skip_below_f >= 55 && d.skip_below_f <= 70)) out.push('Skip when a room falls to: 55–70°F, or leave it empty.')
  const deg = d.precondition_degrees_f
  if (typeof deg !== 'number' || Number.isNaN(deg) || deg < 0.5 || deg > 3) out.push('Pre-cool by 0.5–3°F.')
  if (![1, 2].includes(d.precondition_hours)) out.push('Pre-cool for 1 or 2 hours.')
  const gap = d.precondition_end_gap_min
  if (typeof gap !== 'number' || !Number.isInteger(gap) || gap < 10 || gap > 60)
    out.push('End the pre-cooling hold 10–60 minutes (whole minutes) before the event.')
  return out
})

const noRule = computed(
  () => draft.value.auto_skip && !draft.value.skip_when_asleep && draft.value.skip_above_f === null && draft.value.skip_below_f === null,
)

async function save() {
  const ok = await saver.run(() => control.saveSettings({ utility_events: draft.value }))
  if (ok) reset()
}
</script>

<template>
  <Card title="Utility events" subtitle="Energy-saving events from your utility (demand response)">
    <p class="text-sm text-muted">
      While an event runs the app stands aside. You can skip one from Live; a skip is an opt-out that ecobee records and
      your utility sees, and it may cost that event's credit.
    </p>
    <fieldset :disabled="!isOwner" class="mt-3 space-y-4">
      <legend class="sr-only">Utility events</legend>

      <label class="flex items-start gap-2 text-sm">
        <input v-model="draft.alerts" type="checkbox" class="mt-0.5" />
        <span>Tell me when an event is announced, starts, ends or is skipped</span>
      </label>

      <section class="border-t border-line pt-3">
        <h3 class="text-sm font-medium">Automatic skips</h3>
        <label class="mt-2 flex items-start gap-2 text-sm">
          <input v-model="draft.auto_skip" type="checkbox" class="mt-0.5" />
          <span>Skip an event when one of these rules matches</span>
        </label>
        <div class="mt-2 space-y-2 pl-6" :class="!draft.auto_skip && 'opacity-60'">
          <label class="flex items-start gap-2 text-sm">
            <input v-model="draft.skip_when_asleep" type="checkbox" class="mt-0.5" :disabled="!draft.auto_skip" />
            <span>Someone is asleep in a room on that thermostat</span>
          </label>
          <div class="grid grid-cols-2 gap-2">
            <label :for="`${uid}-above`" class="flex flex-col justify-end text-xs text-muted">
              A room someone is in reaches (°F, cooling)
              <input :id="`${uid}-above`" v-model.number="above" type="number" inputmode="decimal" min="72" max="90" step="0.5"
                     placeholder="off" class="input num mt-1" :disabled="!draft.auto_skip" />
            </label>
            <label :for="`${uid}-below`" class="flex flex-col justify-end text-xs text-muted">
              A room someone is in falls to (°F, heating)
              <input :id="`${uid}-below`" v-model.number="below" type="number" inputmode="decimal" min="55" max="70" step="0.5"
                     placeholder="off" class="input num mt-1" :disabled="!draft.auto_skip" />
            </label>
          </div>
          <p class="text-xs text-muted">Leave a temperature empty to turn that rule off.</p>
        </div>
        <p class="mt-2 text-xs text-muted">
          These are honest opt-outs, the same as tapping Skip. In Act mode the app sends them for the units it may write
          to; in Suggest mode a matching rule only alerts you, and you skip from Live.
        </p>
        <p v-if="noRule" class="mt-1 text-xs text-warn">No rule is on, so nothing is skipped automatically.</p>
      </section>

      <section class="border-t border-line pt-3">
        <h3 class="text-sm font-medium">Pre-cooling</h3>
        <label class="mt-2 flex items-start gap-2 text-sm">
          <input v-model="draft.precondition" type="checkbox" class="mt-0.5" />
          <span>Pre-cool before an announced event (pre-heat in heating season)</span>
        </label>
        <div class="mt-2 grid grid-cols-3 gap-2" :class="!draft.precondition && 'opacity-60'">
          <label class="flex flex-col justify-end text-xs text-muted">
            By (°F)
            <input v-model.number="draft.precondition_degrees_f" type="number" inputmode="decimal" min="0.5" max="3" step="0.5"
                   class="input num mt-1" :disabled="!draft.precondition" />
          </label>
          <label class="flex flex-col justify-end text-xs text-muted">
            For
            <select v-model.number="draft.precondition_hours" class="input num mt-1" :disabled="!draft.precondition">
              <option :value="1">1 hour</option>
              <option :value="2">2 hours</option>
            </select>
          </label>
          <label class="flex flex-col justify-end text-xs text-muted">
            Ends before the event (min)
            <input v-model.number="draft.precondition_end_gap_min" type="number" inputmode="numeric" min="10" max="60" step="5"
                   class="input num mt-1" :disabled="!draft.precondition" />
          </label>
        </div>
        <p class="mt-2 text-xs text-muted">
          The pre-cooling hold always ends before the event starts, so the event applies to your normal temperature. Your
          hard limits and the usual write checks still apply.
        </p>
      </section>
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
