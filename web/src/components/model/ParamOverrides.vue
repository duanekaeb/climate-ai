<script setup lang="ts">
// Pick policy settings to override and give each a value (experiment arms, backtests,
// simulations, proposed changes). Shows the current value and the allowed range next to
// each input; the API validates again on submit.
import { computed, ref, useId } from 'vue'
import type { PolicyParams } from '@/api/types'
import Icon from '@/components/Icon.vue'
import {
  POLICY_KEYS,
  formatParam,
  paramMeta,
  validateOverrides,
  type ParamOverrides,
  type PolicyKey,
} from './policyMeta'

const props = withDefaults(
  defineProps<{ current: PolicyParams | null; disabled?: boolean; emptyText?: string }>(),
  { disabled: false, emptyText: 'No overrides: the current policy.' },
)
const model = defineModel<ParamOverrides>({ required: true })
const uid = useId()
const adding = ref<PolicyKey | ''>('')

const keys = computed(() => POLICY_KEYS.filter((k) => model.value[k] !== undefined))
const available = computed(() => POLICY_KEYS.filter((k) => model.value[k] === undefined))

function errorFor(k: PolicyKey): string {
  return validateOverrides({ [k]: model.value[k] })[0] ?? ''
}

function add() {
  const k = adding.value
  adding.value = ''
  if (!k) return
  const m = paramMeta(k)
  const start = props.current?.[k] ?? m.default ?? (m.kind === 'boolean' ? false : (m.min ?? 0))
  model.value = { ...model.value, [k]: start }
}

function remove(k: PolicyKey) {
  const next = { ...model.value }
  delete next[k]
  model.value = next
}

function setNumber(k: PolicyKey, e: Event) {
  model.value = { ...model.value, [k]: (e.target as HTMLInputElement).valueAsNumber }
}

function setBool(k: PolicyKey, e: Event) {
  model.value = { ...model.value, [k]: (e.target as HTMLSelectElement).value === 'on' }
}

function rangeText(k: PolicyKey): string {
  const m = paramMeta(k)
  if (m.kind === 'boolean' || m.min === null || m.max === null) return ''
  return `range ${formatParam(k, m.min)}–${formatParam(k, m.max)}`
}
</script>

<template>
  <div class="space-y-2">
    <p v-if="!keys.length" class="text-xs text-muted">{{ emptyText }}</p>
    <div v-for="k in keys" :key="k" class="rounded-xl border border-line p-2.5">
      <div class="flex items-start justify-between gap-2">
        <label :for="`${uid}-${k}`" class="min-w-0 text-sm font-medium">{{ paramMeta(k).label }}</label>
        <button
          type="button"
          class="btn !px-1.5 !py-1"
          :disabled="disabled"
          :aria-label="`Remove ${paramMeta(k).label}`"
          @click="remove(k)"
        >
          <Icon name="x" :size="14" />
        </button>
      </div>
      <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <select
          v-if="paramMeta(k).kind === 'boolean'"
          :id="`${uid}-${k}`"
          class="input !w-28"
          :disabled="disabled"
          :value="model[k] ? 'on' : 'off'"
          @change="setBool(k, $event)"
        >
          <option value="on">on</option>
          <option value="off">off</option>
        </select>
        <input
          v-else
          :id="`${uid}-${k}`"
          type="number"
          inputmode="decimal"
          class="input num !w-28"
          :disabled="disabled"
          :min="paramMeta(k).min ?? undefined"
          :max="paramMeta(k).max ?? undefined"
          :step="paramMeta(k).step"
          :value="model[k]"
          @input="setNumber(k, $event)"
        />
        <span class="text-xs text-muted">
          now {{ formatParam(k, current?.[k]) }}<template v-if="rangeText(k)"> · {{ rangeText(k) }}</template>
        </span>
      </div>
      <p v-if="errorFor(k)" class="mt-1 text-xs text-bad">{{ errorFor(k) }}</p>
    </div>
    <select
      v-if="available.length"
      v-model="adding"
      class="input"
      :disabled="disabled"
      aria-label="Add a policy setting to override"
      @change="add"
    >
      <option value="">+ Override a setting…</option>
      <option v-for="k in available" :key="k" :value="k">{{ paramMeta(k).label }}</option>
    </select>
  </div>
</template>
