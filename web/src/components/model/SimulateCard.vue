<script setup lang="ts">
// Simulate a day (default tomorrow, on the forecast) per unit. With overrides, the current
// policy is simulated too, so the two can be compared on the same weather.
import { computed, ref, useId } from 'vue'
import type { PolicyParams, SimulateOut } from '@/api/types'
import Card from '@/components/Card.vue'
import EChart from '@/components/EChart.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import { UNIT_NAMES, localTime, minutes } from '@/lib/format'
import { axisStyle, baseOption, unitColor, useChartPalette, type ChartOption } from '@/components/analysis/chart'
import { addDays, dayLabelLong, errorText, escapeHtml, signedMinutes, todayIn } from '@/components/analysis/stats'
import { useModels } from '@/stores/models'
import ParamOverrides from './ParamOverrides.vue'
import { effectiveOverrides, validateOverrides, type ParamOverrides as Overrides } from './policyMeta'

const props = defineProps<{ current: PolicyParams | null; tz?: string }>()
const models = useModels()
const palette = useChartPalette()
const uid = useId()

const params = ref<Overrides>({})
const day = ref('')
const busy = ref(false)
const error = ref('')
const candidate = ref<SimulateOut | null>(null)
const baseline = ref<SimulateOut | null>(null)

const effective = computed(() => effectiveOverrides(params.value, props.current))
const problems = computed(() => validateOverrides(params.value))
const tomorrow = computed(() => addDays(todayIn(props.tz), 1))

async function run() {
  if (problems.value.length || busy.value) return
  busy.value = true
  error.value = ''
  const date = day.value || null
  const compare = Object.keys(effective.value).length > 0
  try {
    const [cand, base] = await Promise.all([
      models.simulate({ params: { ...effective.value }, date }),
      compare ? models.simulate({ params: {}, date }) : Promise.resolve(null),
    ])
    candidate.value = cand
    baseline.value = base
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

const unitKeys = computed(() => {
  const keys = Object.keys(candidate.value?.units ?? {})
  const order = Object.keys(UNIT_NAMES)
  return keys.sort((a, b) => order.indexOf(a) - order.indexOf(b))
})

const option = computed<ChartOption>(() => {
  const p = palette.value
  const ax = axisStyle(p)
  const c = candidate.value
  const b = baseline.value
  const fmt = (v: unknown) => (typeof v === 'number' ? `${v.toFixed(1)}°F` : '—')
  const lines = (sim: SimulateOut, dashed: boolean, suffix: string) =>
    unitKeys.value.map((k, i) => ({
      name: `${UNIT_NAMES[k] ?? k}${suffix}`,
      type: 'line' as const,
      showSymbol: false,
      data: (sim.units[k] ?? []).map((pt) => [new Date(pt.ts).getTime(), pt.temp_f]),
      lineStyle: { color: unitColor(p, k, i), width: dashed ? 1.5 : 2, type: dashed ? ('dashed' as const) : ('solid' as const) },
      itemStyle: { color: unitColor(p, k, i) },
      tooltip: { valueFormatter: fmt },
    }))
  return {
    ...baseOption(p),
    grid: { left: 4, right: 8, top: b ? 52 : 30, bottom: 4, containLabel: true },
    legend: { top: 0, left: 0, itemWidth: 14, itemHeight: 8, textStyle: { color: p.muted, fontSize: 11 } },
    tooltip: {
      ...baseOption(p).tooltip,
      trigger: 'axis',
      axisPointer: {
        label: { formatter: (v: { value: unknown }) => escapeHtml(localTime(new Date(Number(v.value)).toISOString(), props.tz)) },
      },
    },
    xAxis: {
      type: 'time',
      ...ax,
      splitLine: { show: false },
      axisLabel: {
        ...ax.axisLabel,
        hideOverlap: true,
        formatter: (v: number) => localTime(new Date(v).toISOString(), props.tz),
      },
    },
    yAxis: { type: 'value', name: '°F', scale: true, ...ax },
    series: c ? [...lines(c, false, b ? ' (candidate)' : ''), ...(b ? lines(b, true, ' (current)') : [])] : [],
  }
})
</script>

<template>
  <Card title="Simulate a day" subtitle="Per-unit temperatures from the model, on the forecast">
    <form class="space-y-3" novalidate @submit.prevent="run">
      <ParamOverrides v-model="params" :current="current" empty-text="No overrides: simulate the current policy." />
      <div class="flex flex-wrap items-end gap-2">
        <label :for="`${uid}-day`" class="text-xs text-muted">
          Day (blank = tomorrow)
          <input :id="`${uid}-day`" v-model="day" type="date" class="input mt-1 !w-44" :placeholder="tomorrow" />
        </label>
        <button type="submit" class="btn btn-primary" :disabled="busy || problems.length > 0">
          {{ busy ? 'Simulating…' : day ? 'Simulate' : 'Simulate tomorrow' }}
        </button>
      </div>
      <p v-if="error" class="text-sm text-bad">{{ error }}</p>
    </form>

    <div v-if="candidate" class="mt-4 space-y-2 border-t border-line pt-4" aria-live="polite">
      <p class="text-sm">
        {{ dayLabelLong(candidate.Date) }}: <span class="num font-semibold">{{ minutes(candidate.total_runtime_min) }}</span>
        of total runtime
        <template v-if="baseline">
          vs <span class="num">{{ minutes(baseline.total_runtime_min) }}</span> with the current policy
          (<span class="num font-medium">{{ signedMinutes(candidate.total_runtime_min - baseline.total_runtime_min) }}</span>)
        </template>.
      </p>
      <EChart v-if="unitKeys.length" :option="option" height="280px" />
      <p v-else class="text-sm text-muted">The simulation returned no unit trajectories.</p>
      <p class="text-xs text-muted">
        A single simulated day has no interval: use it to rule ideas out, and the backtest above to judge the size of an
        effect. Model: {{ candidate.model === 'rc' ? 'RC house model' : 'rule of thumb' }}. {{ candidate.note }}
      </p>
      <OpenMeteoAttribution />
    </div>
  </Card>
</template>
