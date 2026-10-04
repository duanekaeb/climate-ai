// Chart helpers shared by the Rooms and Runtime charts: theme colors read from the CSS tokens,
// a reactive "theme key" so option computeds re-read them when dark mode flips, and small
// formatting helpers for tooltips and time axes.
import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import { cssVar } from '@/lib/format'

/** Bumps whenever the class on <html> changes (dark / light), so computeds that call
 *  chartTheme() re-run and hand EChart a fresh option with the new colors. */
export function useThemeKey(): Ref<number> {
  const key = ref(0)
  let mo: MutationObserver | null = null
  onMounted(() => {
    mo = new MutationObserver(() => key.value++)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  })
  onBeforeUnmount(() => mo?.disconnect())
  return key
}

export interface ChartTheme {
  ink: string
  muted: string
  line: string
  surface: string
  accent: string
  good: string
  warn: string
  bad: string
  occupied: string
  asleep: string
  unit: (key: string) => string
}

export function chartTheme(): ChartTheme {
  const accent = cssVar('--color-accent')
  return {
    ink: cssVar('--color-ink'),
    muted: cssVar('--color-muted'),
    line: cssVar('--color-line'),
    surface: cssVar('--color-surface'),
    accent,
    good: cssVar('--color-good'),
    warn: cssVar('--color-warn'),
    bad: cssVar('--color-bad'),
    occupied: cssVar('--color-st-occupied'),
    asleep: cssVar('--color-st-asleep'),
    unit: (key: string) => cssVar(`--color-unit-${key}`) || accent,
  }
}

/** Hex color plus alpha, for translucent bands (#rrggbb -> rgba). Falls back to the input. */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return hex
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

export function axisStyle(t: ChartTheme) {
  return {
    axisLine: { lineStyle: { color: t.line } },
    axisTick: { lineStyle: { color: t.line } },
    axisLabel: { color: t.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: t.line, opacity: 0.7 } },
    nameTextStyle: { color: t.muted, fontSize: 11 },
  }
}

export function tooltipStyle(t: ChartTheme) {
  return {
    backgroundColor: t.surface,
    borderColor: t.line,
    textStyle: { color: t.ink, fontSize: 12 },
    confine: true,
    extraCssText: 'border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.12);',
  }
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c)
}

/** A colored dot for tooltip rows (the color comes from our own tokens, never from data). */
export function dot(color: string): string {
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:9999px;margin-right:6px;background:${color}"></span>`
}

/** Clock time in the house's time zone for a millisecond timestamp. */
export function clock(ms: number, tz?: string, withDay = false): string {
  const opts: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit', timeZone: tz }
  if (withDay) Object.assign(opts, { weekday: 'short' })
  return new Date(ms).toLocaleString([], opts)
}

/** 'YYYY-MM-DD' (a local calendar day of the house) -> 'Sep 3' without any time-zone shift. */
export function dayLabel(d: string, withWeekday = false): string {
  const [y, m, day] = d.split('-').map(Number)
  if (!y || !m || !day) return d
  const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', timeZone: 'UTC' }
  if (withWeekday) opts.weekday = 'short'
  return new Date(Date.UTC(y, m - 1, day)).toLocaleDateString([], opts)
}

/** Read a numeric y value out of an ECharts tooltip param value ([x, y] pairs or a scalar). */
export function yOf(value: unknown): number | null {
  const v = Array.isArray(value) ? value[1] : value
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
