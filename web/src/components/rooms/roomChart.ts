// ECharts option for a room's history: room temperature, the unit's heat / cool setpoint steps,
// and occupancy shading. Pure (the theme is passed in) so it can be rendered anywhere.
import type { EChartsOption, LineSeriesOption, TooltipComponentFormatterCallbackParams } from 'echarts'
import type { RoomHistory, RoomPoint } from '@/api/types'
import { STATE_LABELS } from '@/stores/rooms'
import { type ChartTheme, axisStyle, clock, dot, escapeHtml, tooltipStyle, withAlpha, yOf } from '@/components/runtime/chartKit'

export type BandState = 'occupied' | 'asleep'
export interface Band {
  state: BandState
  start: number
  end: number
}

function bandOf(p: RoomPoint): BandState | null {
  if (p.state === 'asleep') return 'asleep'
  if (p.state === 'occupied') return 'occupied'
  if (p.state === null && p.occupied === true) return 'occupied'
  return null
}

/** Contiguous runs of occupied / asleep points; each run lasts until the next point. */
export function occupancyBands(points: RoomPoint[]): Band[] {
  const sorted = [...points].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  const out: Band[] = []
  let cur: Band | null = null
  sorted.forEach((p, i) => {
    const t = Date.parse(p.ts)
    const prev = i > 0 ? Date.parse(sorted[i - 1].ts) : t
    const next = i + 1 < sorted.length ? Date.parse(sorted[i + 1].ts) : t + (t - prev)
    const st = bandOf(p)
    if (cur && cur.state === st) {
      cur.end = next
      return
    }
    if (cur) out.push(cur)
    cur = st ? { state: st, start: t, end: next } : null
  })
  if (cur) out.push(cur)
  return out
}

export interface RoomChartInput {
  history: RoomHistory
  hasSensor: boolean
  unitKey: string
  tz: string
  theme: ChartTheme
}

export const ROOM_SERIES = { room: 'Room', heat: 'Heat setpoint', cool: 'Cool setpoint', occupied: 'Occupied', asleep: 'Asleep' }

export function roomHistoryOption({ history, hasSensor, unitKey, tz, theme: t }: RoomChartInput): EChartsOption {
  const pts = [...history.points].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  const sps = [...history.setpoints].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
  const long = history.hours > 24
  const unitColor = t.unit(unitKey)
  const colors: Record<string, string> = {
    [ROOM_SERIES.room]: unitColor,
    [ROOM_SERIES.heat]: t.bad,
    [ROOM_SERIES.cool]: t.accent,
  }
  const bands = occupancyBands(pts)

  // Pin the axis to the whole window: a room without a sensor has no temperature line, and
  // setpoints only change a few times a day, so neither alone spans the bands.
  const times = [...pts.map((p) => Date.parse(p.ts)), ...sps.map((s) => Date.parse(s.ts)), ...bands.map((b) => b.end)]
  const minT = times.length ? Math.min(...times) : NaN
  const maxT = times.length ? Math.max(...times) : NaN
  const bounds = Number.isFinite(minT) && maxT > minT ? { min: minT, max: maxT } : {}
  // Carry the last setpoint to the end of the window so the step reads as "still in force".
  const lastSp = sps[sps.length - 1]
  const spData = (key: 'heat' | 'cool') => {
    const data: [number, number | null][] = sps.map((s) => [Date.parse(s.ts), key === 'heat' ? s.heat_sp_f : s.cool_sp_f])
    if (lastSp && Number.isFinite(maxT) && maxT > Date.parse(lastSp.ts)) {
      data.push([maxT, key === 'heat' ? lastSp.heat_sp_f : lastSp.cool_sp_f])
    }
    return data
  }

  const series: LineSeriesOption[] = []
  if (hasSensor) {
    series.push({
      name: ROOM_SERIES.room,
      type: 'line',
      data: pts.map((p) => [Date.parse(p.ts), p.temp_f]),
      showSymbol: false,
      connectNulls: false,
      z: 4,
      lineStyle: { width: 2, color: unitColor },
      itemStyle: { color: unitColor },
    })
  }
  for (const key of ['heat', 'cool'] as const) {
    const name = ROOM_SERIES[key]
    series.push({
      name,
      type: 'line',
      step: 'end',
      data: spData(key),
      showSymbol: false,
      connectNulls: false,
      z: 3,
      lineStyle: { width: 1.5, type: 'dashed', color: colors[name] },
      itemStyle: { color: colors[name] },
    })
  }
  for (const st of ['occupied', 'asleep'] as const) {
    series.push({
      name: ROOM_SERIES[st],
      type: 'line',
      data: [],
      silent: true,
      markArea: {
        silent: true,
        itemStyle: { color: withAlpha(st === 'occupied' ? t.occupied : t.asleep, 0.16), borderWidth: 0 },
        data: bands.filter((b) => b.state === st).map((b) => [{ xAxis: b.start }, { xAxis: b.end }]),
      },
    })
  }

  const ax = axisStyle(t)
  const bandNames = new Set([ROOM_SERIES.occupied, ROOM_SERIES.asleep])

  return {
    animation: false,
    grid: { left: 4, right: 12, top: 12, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'axisLabel' },
    tooltip: {
      trigger: 'axis',
      ...tooltipStyle(t),
      formatter: (raw: TooltipComponentFormatterCallbackParams) => {
        const list = (Array.isArray(raw) ? raw : [raw]).filter((p) => p.seriesName && !bandNames.has(p.seriesName))
        if (!list.length) return ''
        const xv = Array.isArray(list[0].value) ? list[0].value[0] : null
        const x = typeof xv === 'number' ? xv : Date.parse(String(xv))
        const rows = list.map((p) => {
          const name = p.seriesName ?? ''
          const y = yOf(p.value)
          return `${dot(colors[name] ?? t.muted)}${escapeHtml(name)} <b>${y === null ? '—' : `${y.toFixed(1)}°`}</b>`
        })
        let at: RoomPoint | undefined
        for (const p of pts) {
          if (Date.parse(p.ts) <= x) at = p
          else break
        }
        const state = at?.state && at.state in STATE_LABELS ? STATE_LABELS[at.state as keyof typeof STATE_LABELS] : null
        if (state) rows.push(`<span style="color:${t.muted}">${escapeHtml(state)}</span>`)
        return [`<b>${escapeHtml(clock(x, tz, long))}</b>`, ...rows].join('<br/>')
      },
    },
    xAxis: {
      type: 'time',
      ...bounds,
      ...ax,
      splitLine: { show: false },
      axisLabel: {
        ...ax.axisLabel,
        hideOverlap: true,
        formatter: (v: number) =>
          new Date(v).toLocaleString([], long ? { weekday: 'short', hour: 'numeric', timeZone: tz } : { hour: 'numeric', timeZone: tz }),
      },
    },
    yAxis: {
      type: 'value',
      scale: true,
      ...ax,
      axisLabel: { ...ax.axisLabel, formatter: '{value}°' },
    },
    series,
  }
}
