<script setup lang="ts">
// Propose a randomized switchback. The success measure (weather-normalized total-house
// runtime), the length and the checkpoints are fixed here, before the test starts.
import { computed, ref, useId } from 'vue'
import type { ArmIn, PolicyParams, ProposeExperimentBody } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { errorText } from '@/components/analysis/stats'
import ParamOverrides from '@/components/model/ParamOverrides.vue'
import { validateOverrides, type ParamOverrides as Overrides } from '@/components/model/policyMeta'
import { useExperiments } from '@/stores/experiments'
import { armColor } from './arms'

defineProps<{ current: PolicyParams | null }>()
const emit = defineEmits<{ created: [id: number]; cancel: [] }>()
const store = useExperiments()
const uid = useId()

interface ArmDraft {
  key: string
  label: string
  params: Overrides
}

const name = ref('')
const hypothesis = ref('')
const arms = ref<ArmDraft[]>([
  { key: 'a', label: 'Current policy', params: {} },
  { key: 'b', label: '', params: {} },
])
const nDays = ref(28)
const blockDays = ref(2)
const nCheckpoints = ref(3)
const alpha = ref(0.1)
const busy = ref(false)
const submitError = ref('')
const touched = ref(false)

const KEY_RE = /^[a-z0-9_]{1,24}$/

function addArm() {
  if (arms.value.length >= 3) return
  const used = new Set(arms.value.map((a) => a.key))
  const key = ['c', 'd', 'e'].find((k) => !used.has(k)) ?? `arm${arms.value.length + 1}`
  arms.value.push({ key, label: '', params: {} })
}

function removeArm(i: number) {
  if (arms.value.length > 2) arms.value.splice(i, 1)
}

const isInt = (v: number, lo: number, hi: number) => Number.isInteger(v) && v >= lo && v <= hi

const errors = computed(() => {
  const out: string[] = []
  const n = name.value.trim()
  const h = hypothesis.value.trim()
  if (n.length < 3 || n.length > 120) out.push('Name: 3 to 120 characters.')
  if (h.length < 3 || h.length > 2000) out.push('Hypothesis: 3 to 2000 characters.')
  const keys = arms.value.map((a) => a.key)
  if (new Set(keys).size !== keys.length) out.push('Each arm needs its own key.')
  arms.value.forEach((a, i) => {
    if (!KEY_RE.test(a.key)) out.push(`Arm ${i + 1}: key is lowercase letters, digits or _ (max 24).`)
    if (!a.label.trim()) out.push(`Arm ${i + 1}: give it a label.`)
    for (const msg of validateOverrides(a.params)) out.push(`Arm ${i + 1}: ${msg}`)
  })
  const shapes = new Set(arms.value.map((a) => JSON.stringify(Object.entries(a.params).sort())))
  if (shapes.size !== arms.value.length) out.push('Arms must differ: give each a different setting.')
  if (!isInt(nDays.value, 6, 180)) out.push('Length: 6 to 180 days.')
  if (!isInt(blockDays.value, 1, 7)) out.push('Switch every 1 to 7 days.')
  if (!isInt(nCheckpoints.value, 1, 3)) out.push('Checkpoints: 1 to 3.')
  if (!(alpha.value > 0 && alpha.value < 0.5)) out.push('False-win rate: between 0 and 0.5.')
  return out
})

const shortWarning = computed(() =>
  isInt(nDays.value, 6, 180) && isInt(blockDays.value, 1, 7) && nDays.value < blockDays.value * arms.value.length * 2
    ? 'That gives each arm fewer than two blocks; the result will mostly reflect the weather on those days.'
    : '',
)

async function submit() {
  touched.value = true
  if (errors.value.length || busy.value) return
  busy.value = true
  submitError.value = ''
  const list: ArmIn[] = arms.value.map((a) => ({ key: a.key, label: a.label.trim(), params: { ...a.params } }))
  const tuple: ProposeExperimentBody['arms'] =
    list.length === 3 ? [list[0], list[1], list[2]] : [list[0], list[1]]
  const body: ProposeExperimentBody = {
    name: name.value.trim(),
    hypothesis: hypothesis.value.trim(),
    arms: tuple,
    n_days: nDays.value,
    block_days: blockDays.value,
    n_checkpoints: nCheckpoints.value,
    alpha: alpha.value,
  }
  try {
    const e = await store.propose(body)
    emit('created', e.id)
  } catch (err) {
    submitError.value = errorText(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="space-y-4" novalidate @submit.prevent="submit">
    <div>
      <label :for="`${uid}-name`" class="text-sm font-medium">Name</label>
      <input :id="`${uid}-name`" v-model="name" class="input mt-1" maxlength="120" placeholder="Main floor 1° vs 2° cooler" />
    </div>
    <div>
      <label :for="`${uid}-hyp`" class="text-sm font-medium">Hypothesis</label>
      <textarea
        :id="`${uid}-hyp`"
        v-model="hypothesis"
        rows="3"
        maxlength="2000"
        class="input mt-1"
        placeholder="Holding the empty main floor 2°F under the upstairs cuts total-house runtime on hot afternoons."
      />
    </div>

    <fieldset class="space-y-3">
      <legend class="text-sm font-medium">Arms <span class="font-normal text-muted">(2 or 3; the first is the comparison)</span></legend>
      <div v-for="(a, i) in arms" :key="i" class="rounded-xl border border-line p-3">
        <div class="mb-2 flex items-center justify-between gap-2">
          <span class="inline-flex items-center gap-2 text-sm font-medium">
            <span class="h-3 w-3 rounded" :style="{ background: armColor(i) }" />Arm {{ i + 1 }}
          </span>
          <button v-if="arms.length > 2" type="button" class="btn !px-2 !py-1 text-xs" @click="removeArm(i)">
            <Icon name="x" :size="14" />Remove
          </button>
        </div>
        <div class="grid grid-cols-[5rem_1fr] gap-2">
          <label class="text-xs text-muted">
            Key
            <input v-model="a.key" class="input mt-1 font-mono" maxlength="24" autocapitalize="off" spellcheck="false" />
          </label>
          <label class="text-xs text-muted">
            Label
            <input v-model="a.label" class="input mt-1" maxlength="80" :placeholder="i === 0 ? 'Current policy' : 'Main 2° cooler'" />
          </label>
        </div>
        <div class="mt-2">
          <ParamOverrides v-model="a.params" :current="current" empty-text="No overrides: this arm runs the current policy." />
        </div>
      </div>
      <button v-if="arms.length < 3" type="button" class="btn w-full" @click="addArm">+ Add a third arm</button>
    </fieldset>

    <fieldset class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <legend class="mb-2 text-sm font-medium">Design <span class="font-normal text-muted">(fixed before the start)</span></legend>
      <label class="text-xs text-muted">
        Length (days)
        <input v-model.number="nDays" type="number" min="6" max="180" step="1" class="input num mt-1" />
      </label>
      <label class="text-xs text-muted">
        Switch every (days)
        <input v-model.number="blockDays" type="number" min="1" max="7" step="1" class="input num mt-1" />
      </label>
      <label class="text-xs text-muted">
        Checkpoints
        <select v-model.number="nCheckpoints" class="input num mt-1">
          <option :value="1">1</option>
          <option :value="2">2</option>
          <option :value="3">3</option>
        </select>
      </label>
      <label class="text-xs text-muted">
        False-win rate (alpha)
        <input v-model.number="alpha" type="number" min="0.01" max="0.49" step="0.01" class="input num mt-1" />
      </label>
    </fieldset>
    <p class="text-xs text-muted">
      The measure is weather-normalized total-house runtime. Size the length with the power calculator: a 10–15% change
      usually shows within weeks, a 5% change can take months.
    </p>
    <p v-if="shortWarning" class="text-xs text-warn">{{ shortWarning }}</p>

    <ul v-if="touched && errors.length" class="space-y-0.5 text-xs text-bad">
      <li v-for="m in errors" :key="m">{{ m }}</li>
    </ul>
    <p v-if="submitError" class="text-sm text-bad">{{ submitError }}</p>

    <div class="flex flex-wrap gap-2">
      <button type="submit" class="btn btn-primary" :disabled="busy || (touched && errors.length > 0)">
        {{ busy ? 'Proposing…' : 'Propose experiment' }}
      </button>
      <button type="button" class="btn" @click="emit('cancel')">Cancel</button>
    </div>
    <p class="text-xs text-muted">A proposal does nothing until you approve it.</p>
  </form>
</template>
