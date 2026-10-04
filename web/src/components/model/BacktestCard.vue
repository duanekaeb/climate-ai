<script setup lang="ts">
// Replay recent days with candidate policy settings vs the current policy. Only a candidate
// that beats the model's own uncertainty is worth a real test day.
import { computed, ref, useId } from 'vue'
import type { BacktestOut, PolicyParams } from '@/api/types'
import Card from '@/components/Card.vue'
import { minutes } from '@/lib/format'
import IntervalBar from '@/components/analysis/IntervalBar.vue'
import { crossesZero, errorText, interval, pair, signed } from '@/components/analysis/stats'
import { useModels } from '@/stores/models'
import ParamOverrides from './ParamOverrides.vue'
import { effectiveOverrides, validateOverrides, type ParamOverrides as Overrides } from './policyMeta'

const props = defineProps<{ current: PolicyParams | null }>()
const models = useModels()
const uid = useId()

const params = ref<Overrides>({})
const days = ref(28)
const busy = ref(false)
const error = ref('')
const result = ref<BacktestOut | null>(null)

const effective = computed(() => effectiveOverrides(params.value, props.current))
const problems = computed(() => {
  const out = validateOverrides(params.value)
  if (!Number.isInteger(days.value) || days.value < 7 || days.value > 120) out.push('Days: 7 to 120.')
  if (!Object.keys(effective.value).length) out.push('Change at least one setting from its current value.')
  return out
})
const ci = computed(() => pair(result.value?.ci90_pct))

async function run() {
  if (problems.value.length || busy.value) return
  busy.value = true
  error.value = ''
  try {
    result.value = await models.backtest({ params: { ...effective.value }, days: days.value })
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Card title="Backtest" subtitle="Replay recent days with different settings">
    <form class="space-y-3" novalidate @submit.prevent="run">
      <ParamOverrides v-model="params" :current="current" empty-text="Add the settings you want to try." />
      <div class="flex flex-wrap items-end gap-2">
        <label :for="`${uid}-days`" class="text-xs text-muted">
          Days to replay
          <input :id="`${uid}-days`" v-model.number="days" type="number" min="7" max="120" step="1" class="input num mt-1 !w-28" />
        </label>
        <button type="submit" class="btn btn-primary" :disabled="busy || problems.length > 0">
          {{ busy ? 'Running…' : 'Run backtest' }}
        </button>
      </div>
      <ul v-if="problems.length && Object.keys(params).length" class="space-y-0.5 text-xs text-muted">
        <li v-for="p in problems" :key="p">{{ p }}</li>
      </ul>
      <p v-if="error" class="text-sm text-bad">{{ error }}</p>
    </form>

    <div v-if="result" class="mt-4 space-y-3 border-t border-line pt-4" aria-live="polite">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <p class="text-sm">
          Runtime change:
          <span class="num text-lg font-semibold">{{ signed(result.delta_pct, 1, '%') }}</span>
          <span class="text-muted"> over {{ result.days }} days</span>
        </p>
        <span class="chip" :class="result.beats_model_uncertainty ? 'bg-good/15 text-good' : 'bg-surface-2 text-muted'">
          {{ result.beats_model_uncertainty ? 'beats model uncertainty' : 'within model uncertainty' }}
        </span>
      </div>
      <IntervalBar
        v-if="result.delta_pct !== null"
        :value="result.delta_pct"
        :low="ci ? ci[0] : null"
        :high="ci ? ci[1] : null"
        unit="%"
        better="lower"
        :min-span="5"
        label="Backtest runtime change"
      />
      <p class="text-sm">
        90% interval: <span class="num font-medium">{{ interval(ci?.[0], ci?.[1], 1, '%') }}</span>.
        <span v-if="!result.beats_model_uncertainty" class="text-muted">
          {{ ci && crossesZero(ci[0], ci[1]) ? 'It includes zero.' : '' }} Not worth a real test day yet.
        </span>
        <span v-else class="text-muted">Worth testing for real (shadow days, then a switchback).</span>
      </p>
      <dl class="num grid grid-cols-2 gap-2 text-sm">
        <div class="rounded-xl bg-surface-2 p-2">
          <dt class="text-[11px] text-muted">Runtime, current → candidate</dt>
          <dd>{{ minutes(result.current_runtime_min) }} → {{ minutes(result.candidate_runtime_min) }}</dd>
        </div>
        <div class="rounded-xl bg-surface-2 p-2">
          <dt class="text-[11px] text-muted">Out-of-band minutes</dt>
          <dd>{{ minutes(result.comfort_violation_min_current) }} → {{ minutes(result.comfort_violation_min_candidate) }}</dd>
        </div>
      </dl>
      <p class="text-xs text-muted">
        Model: {{ result.model === 'rc' ? 'the RC house model' : 'a rule of thumb from the baselines (no house model has passed its checks yet)' }}.
        {{ result.note }}
      </p>
    </div>
  </Card>
</template>
