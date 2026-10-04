<script setup lang="ts">
// Controller mode (off / suggest / act) and how it behaves when it acts. Switching to Act asks
// for confirmation, because from then on the controller writes holds to the thermostats.
import { computed, ref } from 'vue'
import type { ControlSettings, ModeBody, SettingsOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { errorText } from '@/components/analysis/stats'
import { useControl } from '@/stores/control'
import { useStatus } from '@/stores/status'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'
import { unitKeys, unitName } from './units'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()
const status = useStatus()

type Mode = ModeBody['mode']
const MODES: { value: Mode; label: string; text: string }[] = [
  { value: 'off', label: 'Off', text: 'The controller does nothing. Each ecobee runs its own schedule.' },
  { value: 'suggest', label: 'Suggest', text: 'Plans every 3 minutes and logs what it would write. Touches nothing.' },
  { value: 'act', label: 'Act', text: 'Writes timed 1–2 h holds inside your hard limits, reads each one back and logs it.' },
]

const mode = computed(() => props.settings.control.mode)
const confirming = ref(false)
const switching = ref<Mode | null>(null)
const modeError = ref('')

async function choose(m: Mode) {
  if (m === mode.value || switching.value) return
  if (m === 'act' && !confirming.value) {
    confirming.value = true
    return
  }
  confirming.value = false
  switching.value = m
  modeError.value = ''
  try {
    await control.setMode(m)
    void status.load()
  } catch (e) {
    modeError.value = errorText(e)
  } finally {
    switching.value = null
  }
}

// Behavior when acting
type Behavior = Pick<ControlSettings, 'act_units' | 'hold_hours' | 'manual_backoff_hours'>
const { draft, dirty, reset } = useDraft<Behavior>(() => ({
  act_units: [...props.settings.control.act_units],
  hold_hours: props.settings.control.hold_hours,
  manual_backoff_hours: props.settings.control.manual_backoff_hours,
}))
const saver = useSaver()
const units = computed(() => unitKeys(props.settings))
const actUnitsText = computed(() => props.settings.control.act_units.map(unitName).join(', ') || 'no units')

const problems = computed(() => {
  const out: string[] = []
  const d = draft.value
  if (![1, 2].includes(d.hold_hours)) out.push('Hold length is 1 or 2 hours.')
  if (!(typeof d.manual_backoff_hours === 'number' && d.manual_backoff_hours >= 0 && d.manual_backoff_hours <= 24))
    out.push('Back-off is 0 to 24 hours.')
  return out
})

function toggleUnit(k: string) {
  const set = new Set(draft.value.act_units)
  if (set.has(k)) set.delete(k)
  else set.add(k)
  draft.value = { ...draft.value, act_units: units.value.filter((u) => set.has(u)) }
}

async function save() {
  const ok = await saver.run(() =>
    control.saveSettings({ control: { ...props.settings.control, ...draft.value }, occupancy: null, location: null }),
  )
  if (ok) reset()
}
</script>

<template>
  <Card title="Controller">
    <div role="radiogroup" aria-label="Controller mode" class="grid grid-cols-3 gap-1 rounded-xl bg-surface-2 p-1">
      <button
        v-for="m in MODES"
        :key="m.value"
        type="button"
        role="radio"
        :aria-checked="mode === m.value"
        :disabled="!isOwner || switching !== null"
        class="rounded-lg px-2 py-2 text-sm font-medium disabled:cursor-not-allowed"
        :class="
          mode === m.value
            ? m.value === 'act'
              ? 'bg-good/20 text-good shadow-sm'
              : 'bg-surface text-ink shadow-sm'
            : 'text-muted hover:text-ink'
        "
        @click="choose(m.value)"
      >
        {{ switching === m.value ? '…' : m.label }}
      </button>
    </div>
    <p class="mt-2 text-sm">{{ MODES.find((m) => m.value === mode)?.text }}</p>

    <div v-if="confirming" class="mt-3 rounded-xl border border-warn/40 bg-warn/10 p-3" role="alertdialog" aria-label="Confirm Act mode">
      <p class="text-sm font-medium">Let the controller write to the thermostats?</p>
      <p class="mt-1 text-sm">
        In Act mode it writes timed holds ({{ settings.control.hold_hours }} h, renewed while healthy) to
        {{ actUnitsText }}, never outside your hard limits, at most one change per unit every
        {{ settings.control.limits.min_minutes_between_changes }} minutes. Holds expire on their own if the server stops.
      </p>
      <div class="mt-2 flex flex-wrap gap-2">
        <button type="button" class="btn btn-primary" @click="choose('act')">Switch to Act</button>
        <button type="button" class="btn" @click="confirming = false">Cancel</button>
      </div>
    </div>
    <p v-if="modeError" class="mt-2 text-sm text-bad">{{ modeError }}</p>
    <p v-if="!isOwner" class="mt-2 text-xs text-muted">Only the owner can change the mode.</p>

    <fieldset :disabled="!isOwner" class="mt-4 border-t border-line pt-3">
      <legend class="sr-only">When acting</legend>
      <p class="mb-2 text-xs font-semibold text-muted uppercase">When acting</p>
      <div class="space-y-3">
        <div>
          <p class="text-sm">Units it may write to</p>
          <div class="mt-1 flex flex-wrap gap-2">
            <label v-for="k in units" :key="k" class="inline-flex items-center gap-1.5 rounded-lg border border-line px-2 py-1 text-sm">
              <input type="checkbox" :checked="draft.act_units.includes(k)" @change="toggleUnit(k)" />
              {{ unitName(k) }}
            </label>
          </div>
          <p class="mt-1 text-xs text-muted">Unchecked units stay suggest-only.</p>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <label class="text-xs text-muted">
            Hold length
            <select v-model.number="draft.hold_hours" class="input num mt-1">
              <option :value="1">1 hour</option>
              <option :value="2">2 hours</option>
            </select>
          </label>
          <label class="text-xs text-muted">
            Back off after a hand change (h)
            <input v-model.number="draft.manual_backoff_hours" type="number" min="0" max="24" step="0.5" class="input num mt-1" />
          </label>
        </div>
      </div>
      <ul v-if="problems.length" class="mt-2 text-xs text-bad">
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
    </fieldset>
  </Card>
</template>
