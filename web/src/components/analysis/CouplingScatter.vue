<script setup lang="ts">
// Each dot is one cooling hour: how much warmer the main floor was than the upstairs (x)
// against the upstairs unit's duty that hour (y), colored by the outdoor temperature.
import { computed } from 'vue'
import type { CouplingPoint } from '@/api/types'
import EChart from '@/components/EChart.vue'
import { axisStyle, baseOption, useChartPalette, type ChartOption } from './chart'
import { dateTime, signed } from './stats'

const props = defineProps<{ points: CouplingPoint[]; tz?: string }>()
const palette = useChartPalette()

type Dot = [number, number, number, string]
type DotNoTemp = [number, number, string]

const withTemp = computed<Dot[]>(() =>
  props.points
    .filter((p) => p.outdoor_f !== null)
    .map((p) => [p.main_minus_up_f, p.up_duty_pct, p.outdoor_f as number, p.ts]),
)
const noTemp = computed<DotNoTemp[]>(() =>
  props.points.filter((p) => p.outdoor_f === null).map((p) => [p.main_minus_up_f, p.up_duty_pct, p.ts]),
)
const tempRange = computed(() => {
  const t = withTemp.value.map((d) => d[2])
  if (!t.length) return null
  return [Math.floor(Math.min(...t)), Math.ceil(Math.max(...t))] as const
})

function dataOf(params: unknown): unknown {
  const one = Array.isArray(params) ? params[0] : params
  return one && typeof one === 'object' && 'data' in one ? (one as { data: unknown }).data : null
}

function tip(data: unknown): string {
  if (!Array.isArray(data)) return ''
  const [x, y] = data as [number, number]
  const t = data.length === 4 ? (data[2] as number) : null
  const ts = String(data[data.length - 1])
  const out = t === null ? 'outdoor unknown' : `outdoor ${t.toFixed(0)}°F`
  return `${dateTime(ts, props.tz)}<br>main − up ${signed(x, 1, '°F')} · upstairs duty ${y.toFixed(0)}%<br>${out}`
}

const option = computed<ChartOption>(() => {
  const p = palette.value
  const ax = axisStyle(p)
  const range = tempRange.value
  return {
    ...baseOption(p),
    grid: { left: 4, right: 12, top: 30, bottom: range ? 44 : 8, containLabel: true },
    tooltip: { ...baseOption(p).tooltip, trigger: 'item', formatter: (params: unknown) => tip(dataOf(params)) },
    xAxis: {
      type: 'value',
      name: 'main − upstairs °F',
      nameLocation: 'middle',
      nameGap: 22,
      ...ax,
      scale: true,
    },
    yAxis: { type: 'value', name: 'upstairs duty %', min: 0, max: 100, ...ax },
    visualMap: range
      ? {
          type: 'continuous',
          seriesIndex: 0,
          dimension: 2,
          min: range[0],
          max: range[1] > range[0] ? range[1] : range[0] + 1,
          orient: 'horizontal',
          left: 'center',
          bottom: 0,
          itemWidth: 10,
          itemHeight: 140,
          calculable: false,
          text: [`${range[1]}°F outdoor`, `${range[0]}°F`],
          textStyle: { color: p.muted, fontSize: 11 },
          inRange: { color: [p.unit.main, p.warn, p.bad] },
        }
      : undefined,
    series: [
      {
        name: 'Hours',
        type: 'scatter',
        data: withTemp.value,
        symbolSize: 5,
        itemStyle: { opacity: 0.65 },
        markLine: {
          silent: true,
          symbol: 'none',
          label: { show: false },
          lineStyle: { color: p.muted, type: 'dashed' },
          data: [{ xAxis: 0 }],
        },
      },
      {
        name: 'Hours (no outdoor reading)',
        type: 'scatter',
        data: noTemp.value,
        symbolSize: 5,
        itemStyle: { color: p.muted, opacity: 0.5 },
      },
    ],
  }
})
</script>

<template>
  <EChart :option="option" height="320px" />
</template>
