// ECharts options for the Runtime tab. Pure functions (the theme is passed in).
import type {
  BarSeriesOption,
  EChartsOption,
  LineSeriesOption,
  TooltipComponentFormatterCallbackParams,
} from 'echarts'
import type { Intraday } from '@/api/types'
import { UNIT_NAMES, minutes, temp } from '@/lib/format'
import { type RuntimeDay, activeMinutes } from '@/stores/runtime'
import { type ChartTheme, axisStyle, clock, dayLabel, dot, escapeHtml, tooltipStyle, withAlpha, yOf } from './chartKit'

const unitName = (k: string) => UNIT_NAMES[k] ?? k

function asList(raw: TooltipComponentFormatterCallbackParams) {
  return Array.isArray(raw) ? raw : [raw]
}

export interface DailyChartInput {
  days: RuntimeDay[]
  unitKeys: string[]
  selectedDate: string
  theme: ChartTheme
}

export function dailyRuntimeOption({ days, unitKeys, selectedDate, theme: t }: DailyChartInput): EChartsOption {
  const ax = axisStyle(t)
  const dates = days.map((d) => d.date)

  const bars: BarSeriesOption[] = unitKeys.map((k, i) => ({
    name: unitName(k),
    type: 'bar',
    stack: 'runtime',
    barMaxWidth: 28,
    itemStyle: { color: t.unit(k), borderRadius: i === unitKeys.length - 1 ? [3, 3, 0, 0] : 0 },
    data: days.map((d) => {
      const r = d.byUnit[k]
      return r ? Math.round(activeMinutes(r)) : 0
    }),
  }))
  // Shade the picked day across its whole category band. Bar markers snap to axis ticks, so
  // the x axis keeps one (hidden) tick per category for the band to line up with the day.
  if (bars.length && dates.includes(selectedDate)) {
    bars[0].markArea = {
      silent: true,
      itemStyle: { color: withAlpha(t.accent, 0.14), borderWidth: 0 },
      data: [[{ xAxis: selectedDate }, { xAxis: selectedDate }]],
    }
  }

  const lines: LineSeriesOption[] = [
    {
      name: 'Expected (house)',
      type: 'line',
      data: days.map((d) => (d.expected === null ? null : Math.round(d.expected))),
      symbol: 'diamond',
      symbolSize: 8,
      connectNulls: false,
      z: 5,
      lineStyle: { width: 1.5, type: 'dashed', color: t.ink },
      itemStyle: { color: t.ink },
    },
    {
      name: 'Outdoor mean',
      type: 'line',
      yAxisIndex: 1,
      data: days.map((d) => d.outdoorMean),
      showSymbol: false,
      smooth: true,
      connectNulls: false,
      z: 4,
      lineStyle: { width: 1.5, color: t.warn },
      itemStyle: { color: t.warn },
    },
  ]

  return {
    animation: false,
    grid: { left: 4, right: 4, top: 14, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      ...tooltipStyle(t),
      formatter: (raw: TooltipComponentFormatterCallbackParams) => {
        const list = asList(raw)
        const d = list.length ? days[list[0].dataIndex] : undefined
        if (!d) return ''
        const rows = [`<b>${escapeHtml(dayLabel(d.date, true))}</b>`]
        for (const k of unitKeys) {
          const r = d.byUnit[k]
          if (!r) continue
          const mode = r.mode === 'cool' ? 'cooling' : r.mode === 'heat' ? 'heating' : 'cool + heat'
          const maxed = r.maxed_min > 0 ? ` · maxed ${escapeHtml(minutes(r.maxed_min))}` : ''
          rows.push(`${dot(t.unit(k))}${escapeHtml(unitName(k))} <b>${escapeHtml(minutes(activeMinutes(r)))}</b> ${mode}${maxed}`)
        }
        rows.push(`House <b>${escapeHtml(minutes(d.total))}</b> · expected <b>${escapeHtml(minutes(d.expected))}</b>`)
        if (d.outdoorMean !== null) rows.push(`${dot(t.warn)}Outdoor mean <b>${escapeHtml(temp(d.outdoorMean, 0))}</b>`)
        return rows.join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      data: dates,
      ...ax,
      axisTick: { show: false, interval: 0 },
      axisLabel: { ...ax.axisLabel, hideOverlap: true, formatter: (v: string) => dayLabel(v) },
    },
    yAxis: [
      {
        type: 'value',
        minInterval: 60,
        ...ax,
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => `${Math.round(v / 60)} h` },
      },
      {
        type: 'value',
        scale: true,
        position: 'right',
        ...ax,
        splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, formatter: '{value}°' },
      },
    ],
    series: [...bars, ...lines],
  }
}

export interface IntradayChartInput {
  intraday: Intraday
  unitKeys: string[]
  tz: string
  theme: ChartTheme
}

export const INTRADAY_LEFT = 44

export function intradayOption({ intraday, unitKeys, tz, theme: t }: IntradayChartInput): EChartsOption {
  const ax = axisStyle(t)
  const single = unitKeys.length === 1
  const units = intraday.units.filter((u) => unitKeys.includes(u.unit_key))
  const colors: Record<string, string> = {}
  const kinds: Record<string, 'temp' | 'run' | 'sp'> = {}

  const series: (LineSeriesOption | BarSeriesOption)[] = []
  let minT = Infinity
  let maxT = -Infinity
  for (const u of units) {
    const name = unitName(u.unit_key)
    const color = t.unit(u.unit_key)
    const pts = [...u.points].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
    for (const p of pts) {
      const ms = Date.parse(p.ts)
      minT = Math.min(minT, ms)
      maxT = Math.max(maxT, ms)
    }
    const runName = `${name} runtime`
    colors[runName] = color
    kinds[runName] = 'run'
    series.push({
      name: runName,
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      stack: 'runtime',
      barMaxWidth: 8,
      itemStyle: { color },
      data: pts.map((p) => [Date.parse(p.ts), Math.round(((p.cool_s + p.heat_s) / 60) * 10) / 10]),
    })
    const tempName = `${name} temperature`
    colors[tempName] = color
    kinds[tempName] = 'temp'
    series.push({
      name: tempName,
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      showSymbol: false,
      connectNulls: false,
      z: 4,
      lineStyle: { width: 2, color },
      itemStyle: { color },
      data: pts.map((p) => [Date.parse(p.ts), p.zone_temp_f]),
    })
    if (single) {
      for (const key of ['heat', 'cool'] as const) {
        const spName = key === 'heat' ? 'Heat setpoint' : 'Cool setpoint'
        const spColor = key === 'heat' ? t.bad : t.accent
        colors[spName] = spColor
        kinds[spName] = 'sp'
        series.push({
          name: spName,
          type: 'line',
          step: 'end',
          xAxisIndex: 0,
          yAxisIndex: 0,
          showSymbol: false,
          connectNulls: false,
          z: 3,
          lineStyle: { width: 1.5, type: 'dashed', color: spColor },
          itemStyle: { color: spColor },
          data: pts.map((p) => [Date.parse(p.ts), key === 'heat' ? p.heat_sp_f : p.cool_sp_f]),
        })
      }
    }
  }
  const outdoor = [...intraday.outdoor].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  if (outdoor.length) {
    colors.Outdoor = t.warn
    kinds.Outdoor = 'temp'
    series.push({
      name: 'Outdoor',
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      showSymbol: false,
      smooth: true,
      connectNulls: true,
      z: 2,
      lineStyle: { width: 1.5, type: 'dotted', color: t.warn },
      itemStyle: { color: t.warn },
      data: outdoor.map((p) => [Date.parse(p.ts), p.temp_f]),
    })
    for (const p of outdoor) {
      const ms = Date.parse(p.ts)
      minT = Math.min(minT, ms)
      maxT = Math.max(maxT, ms)
    }
  }
  const bounds = Number.isFinite(minT) ? { min: minT, max: maxT + 5 * 60_000 } : {}
  const hourLabel = (v: number) => new Date(v).toLocaleTimeString([], { hour: 'numeric', timeZone: tz })

  return {
    animation: false,
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: INTRADAY_LEFT, right: 10, top: 10, bottom: '38%' },
      { left: INTRADAY_LEFT, right: 10, top: '68%', bottom: 24 },
    ],
    tooltip: {
      trigger: 'axis',
      ...tooltipStyle(t),
      formatter: (raw: TooltipComponentFormatterCallbackParams) => {
        const list = asList(raw)
        if (!list.length) return ''
        const xv = Array.isArray(list[0].value) ? list[0].value[0] : null
        const x = typeof xv === 'number' ? xv : Date.parse(String(xv))
        const rows = [`<b>${escapeHtml(clock(x, tz))}</b>`]
        const seen = new Set<string>()
        for (const p of list) {
          const name = p.seriesName ?? ''
          if (!name || seen.has(name)) continue
          seen.add(name)
          const y = yOf(p.value)
          const kind = kinds[name]
          const val = y === null ? '—' : kind === 'run' ? `${y.toFixed(1)} of 5 min` : `${y.toFixed(1)}°`
          rows.push(`${dot(colors[name] ?? t.muted)}${escapeHtml(name)} <b>${escapeHtml(val)}</b>`)
        }
        return rows.join('<br/>')
      },
    },
    xAxis: [
      {
        type: 'time',
        gridIndex: 0,
        ...bounds,
        ...ax,
        splitLine: { show: false },
        axisLabel: { show: false },
        axisTick: { show: false },
      },
      {
        type: 'time',
        gridIndex: 1,
        ...bounds,
        ...ax,
        splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, hideOverlap: true, formatter: hourLabel },
      },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, scale: true, ...ax, axisLabel: { ...ax.axisLabel, formatter: '{value}°' } },
      {
        type: 'value',
        gridIndex: 1,
        min: 0,
        max: single ? 5 : undefined,
        minInterval: 1,
        splitNumber: 2,
        ...ax,
        axisLabel: { ...ax.axisLabel, formatter: '{value} m' },
      },
    ],
    series,
  }
}

/** Minutes each unit ran on the picked day (from the 5-minute points). */
export function intradayTotals(intraday: Intraday): Record<string, number> {
  const out: Record<string, number> = {}
  for (const u of intraday.units) out[u.unit_key] = u.points.reduce((s, p) => s + (p.cool_s + p.heat_s) / 60, 0)
  return out
}
