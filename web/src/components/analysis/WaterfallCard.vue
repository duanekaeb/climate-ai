<script setup lang="ts">
// Weekly attribution waterfall: Last week -> Weather -> Strategy & other -> This week. The
// strategy bar carries its 90% interval as a whisker; weeks start on Monday.
import { computed, ref, watch } from 'vue'
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemReturn } from 'echarts'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import EChart from '@/components/EChart.vue'
import Icon from '@/components/Icon.vue'
import { minutes } from '@/lib/format'
import { useAnalysis } from '@/stores/analysis'
import { axisStyle, baseOption, useChartPalette, type ChartOption } from './chart'
import { addDays, crossesZero, dayLabel, escapeHtml, hours, mondayOf, pair, signedMinutes, todayIn } from './stats'

const props = defineProps<{ tz?: string }>()
const analysis = useAnalysis()
const palette = useChartPalette()

/** undefined = let the backend pick (last full week). */
const requested = ref<string | undefined>(undefined)
watch(requested, (w) => analysis.loadWaterfall(w), { immediate: true })

const lastFull = computed(() => addDays(mondayOf(todayIn(props.tz)), -7))
const data = computed(() => analysis.waterfall.data)
const shown = computed(() => data.value?.week_start ?? requested.value ?? lastFull.value)
const canNext = computed(() => shown.value < lastFull.value)

function shift(weeks: number) {
  const next = addDays(shown.value, 7 * weeks)
  requested.value = next > lastFull.value ? lastFull.value : next
}

interface Row {
  label: string
  kind: 'total' | 'delta'
  minutes: number
  start: number
  end: number
}

const rows = computed<Row[]>(() => {
  let running = 0
  return (data.value?.items ?? []).map((it) => {
    if (it.kind === 'total') {
      running = it.minutes
      return { label: it.label, kind: it.kind, minutes: it.minutes, start: 0, end: it.minutes }
    }
    const start = running
    running += it.minutes
    return { label: it.label, kind: it.kind, minutes: it.minutes, start, end: running }
  })
})

const strategyIndex = computed(() => {
  const i = rows.value.findIndex((r) => r.kind === 'delta' && /strategy/i.test(r.label))
  if (i >= 0) return i
  const deltas = rows.value.map((r, idx) => (r.kind === 'delta' ? idx : -1)).filter((idx) => idx >= 0)
  return deltas.length ? deltas[deltas.length - 1] : -1
})
const ci = computed(() => pair(data.value?.strategy_ci90_min))
const strategy = computed(() => (strategyIndex.value >= 0 ? rows.value[strategyIndex.value] : null))
const weather = computed(() => rows.value.find((r, i) => r.kind === 'delta' && i !== strategyIndex.value) ?? null)

const option = computed<ChartOption>(() => {
  const p = palette.value
  const ax = axisStyle(p)
  const r = rows.value
  const color = (row: Row) => (row.kind === 'total' ? p.accent : row.minutes > 0 ? p.warn : p.good)
  const whisker =
    ci.value && strategy.value
      ? [[strategyIndex.value, hours(strategy.value.start + ci.value[0]), hours(strategy.value.start + ci.value[1])]]
      : []
  const ink = p.ink
  return {
    ...baseOption(p),
    grid: { left: 4, right: 8, top: 24, bottom: 4, containLabel: true },
    tooltip: { ...baseOption(p).tooltip, trigger: 'item' },
    xAxis: {
      type: 'category',
      data: r.map((x) => x.label),
      ...ax,
      splitLine: { show: false },
      axisLabel: { ...ax.axisLabel, interval: 0, width: 72, overflow: 'break' },
    },
    yAxis: { type: 'value', name: 'h', ...ax },
    series: [
      {
        name: 'base',
        type: 'bar',
        stack: 'wf',
        silent: true,
        itemStyle: { color: 'transparent' },
        data: r.map((x) => hours(Math.min(x.start, x.end))),
        tooltip: { show: false },
      },
      {
        name: 'Runtime',
        type: 'bar',
        stack: 'wf',
        barMaxWidth: 56,
        data: r.map((x) => ({ value: hours(Math.abs(x.end - x.start)), itemStyle: { color: color(x), borderRadius: 3 } })),
        label: {
          show: true,
          position: 'top',
          color: p.ink,
          fontSize: 11,
          formatter: (params: { dataIndex: number }) => {
            const row = r[params.dataIndex]
            return row.kind === 'total' ? minutes(row.minutes) : signedMinutes(row.minutes)
          },
        },
        tooltip: {
          formatter: (params: { dataIndex: number }) => {
            const row = r[params.dataIndex]
            return `${escapeHtml(row.label)}: ${row.kind === 'total' ? minutes(row.minutes) : signedMinutes(row.minutes)}`
          },
        },
      },
      {
        name: '90% interval',
        type: 'custom',
        z: 10,
        data: whisker,
        encode: { x: 0, y: [1, 2] },
        tooltip: {
          formatter: () =>
            ci.value ? `Strategy & other, 90% interval: ${signedMinutes(ci.value[0])} to ${signedMinutes(ci.value[1])}` : '',
        },
        renderItem: (_params: unknown, api: CustomSeriesRenderItemAPI): CustomSeriesRenderItemReturn => {
          const x = Number(api.value(0))
          const lo = api.coord([x, Number(api.value(1))])
          const hi = api.coord([x, Number(api.value(2))])
          const band = api.size ? api.size([1, 0]) : 24
          const half = (Array.isArray(band) ? band[0] : band) * 0.16
          const style = { stroke: ink, lineWidth: 1.5 }
          return {
            type: 'group',
            children: [
              { type: 'line', shape: { x1: lo[0], y1: lo[1], x2: hi[0], y2: hi[1] }, style },
              { type: 'line', shape: { x1: lo[0] - half, y1: lo[1], x2: lo[0] + half, y2: lo[1] }, style },
              { type: 'line', shape: { x1: hi[0] - half, y1: hi[1], x2: hi[0] + half, y2: hi[1] }, style },
            ],
          }
        },
      },
    ],
  }
})
</script>

<template>
  <Card title="What changed the total" :subtitle="`Week of ${dayLabel(shown)} vs the week before`">
    <template #actions>
      <div class="flex items-center gap-1">
        <button type="button" class="btn !px-2 !py-1" aria-label="Previous week" @click="shift(-1)">
          <span aria-hidden="true">‹</span>
        </button>
        <button type="button" class="btn !px-2 !py-1" aria-label="Next week" :disabled="!canNext" @click="shift(1)">
          <span aria-hidden="true">›</span>
        </button>
        <button
          type="button"
          class="btn !px-2 !py-1"
          aria-label="Reload"
          :disabled="analysis.waterfall.loading"
          @click="analysis.loadWaterfall(requested)"
        >
          <Icon name="refresh" :size="14" />
        </button>
      </div>
    </template>
    <AsyncState
      :loading="analysis.waterfall.loading && !data"
      :error="analysis.waterfall.error"
      :empty="!!data && !rows.length"
      empty-text="No runtime recorded for these two weeks."
    >
      <template v-if="data">
        <EChart :option="option" height="250px" />
        <div class="mt-3 space-y-1 text-sm">
          <p v-if="weather">
            Weather: <span class="num font-medium">{{ signedMinutes(weather.minutes) }}</span>
            <span class="text-muted"> (what the baselines expect from this week's weather vs last week's)</span>
          </p>
          <p v-if="strategy">
            Strategy &amp; other: <span class="num font-medium">{{ signedMinutes(strategy.minutes) }}</span>
            <template v-if="ci">
              , 90% interval <span class="num">{{ signedMinutes(ci[0]) }} to {{ signedMinutes(ci[1]) }}</span>.
              <span v-if="crossesZero(ci[0], ci[1])" class="text-muted">It includes zero: not distinguishable from noise.</span>
            </template>
            <span v-else class="text-muted"> (no interval yet, so not a claim)</span>
          </p>
          <p class="text-xs text-muted">{{ data.note }}</p>
        </div>
      </template>
    </AsyncState>
  </Card>
</template>
