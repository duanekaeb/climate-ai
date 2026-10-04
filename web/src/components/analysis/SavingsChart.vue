<script setup lang="ts">
// Expected (weather baseline) vs actual whole-house runtime per day, with the day's mean
// outdoor temperature on a second axis.
import { computed } from 'vue'
import type { SavingsDay } from '@/api/types'
import EChart from '@/components/EChart.vue'
import { axisStyle, baseOption, useChartPalette, type ChartOption } from './chart'
import { dayLabel, hours } from './stats'

const props = defineProps<{ days: SavingsDay[] }>()
const palette = useChartPalette()

const hasOutdoor = computed(() => props.days.some((d) => d.outdoor_mean_f !== null))

const option = computed<ChartOption>(() => {
  const p = palette.value
  const ax = axisStyle(p)
  const fmtH = (v: unknown) => (typeof v === 'number' ? `${v.toFixed(1)} h` : '—')
  const fmtF = (v: unknown) => (typeof v === 'number' ? `${v.toFixed(0)}°F` : '—')
  return {
    ...baseOption(p),
    grid: { left: 4, right: hasOutdoor.value ? 4 : 8, top: 34, bottom: 4, containLabel: true },
    legend: { top: 0, left: 0, itemWidth: 14, itemHeight: 8, textStyle: { color: p.muted, fontSize: 11 } },
    tooltip: { ...baseOption(p).tooltip, trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: props.days.map((d) => dayLabel(d.Date)),
      ...ax,
      splitLine: { show: false },
      axisLabel: { ...ax.axisLabel, hideOverlap: true },
    },
    yAxis: [
      { type: 'value', name: 'h', ...ax, min: 0 },
      { type: 'value', name: '°F', ...ax, splitLine: { show: false }, show: hasOutdoor.value, scale: true },
    ],
    series: [
      {
        name: 'Actual',
        type: 'bar',
        data: props.days.map((d) => hours(d.actual_min)),
        itemStyle: { color: p.accent, borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 22,
        tooltip: { valueFormatter: fmtH },
      },
      {
        name: 'Expected (weather)',
        type: 'line',
        data: props.days.map((d) => (d.expected_min === null ? null : hours(d.expected_min))),
        lineStyle: { color: p.ink, width: 2, type: 'dashed' },
        itemStyle: { color: p.ink },
        symbolSize: 5,
        connectNulls: false,
        tooltip: { valueFormatter: fmtH },
      },
      ...(hasOutdoor.value
        ? [
            {
              name: 'Outdoor mean',
              type: 'line' as const,
              yAxisIndex: 1,
              data: props.days.map((d) => d.outdoor_mean_f),
              lineStyle: { color: p.warn, width: 1.5, type: 'dotted' as const },
              itemStyle: { color: p.warn },
              symbol: 'none',
              tooltip: { valueFormatter: fmtF },
            },
          ]
        : []),
    ],
  }
})
</script>

<template>
  <EChart :option="option" height="260px" />
</template>
