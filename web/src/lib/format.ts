// Display helpers shared by every view.
import { ref } from 'vue'

// Bumps when <html>'s class flips between light and dark. cssVar() reads it, so any computed
// chart option that calls cssVar() re-runs with the new theme's colors.
const themeTick = ref(0)
if (typeof document !== 'undefined' && typeof MutationObserver !== 'undefined') {
  new MutationObserver(() => themeTick.value++).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class'],
  })
}
export const UNIT_NAMES: Record<string, string> = { main: 'Main floor', up: 'Upstairs', bed: 'Bed / Office' }
export const UNIT_COLORS: Record<string, string> = {
  main: 'var(--color-unit-main)',
  up: 'var(--color-unit-up)',
  bed: 'var(--color-unit-bed)',
}
export const STATE_COLORS: Record<string, string> = {
  occupied: 'var(--color-st-occupied)',
  asleep: 'var(--color-st-asleep)',
  empty: 'var(--color-st-empty)',
  unknown: 'var(--color-st-unknown)',
  no_target: 'var(--color-st-unknown)',
}

export function temp(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(digits)}°`
}
export function minutes(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (Math.abs(v) >= 90) return `${(v / 60).toFixed(1)} h`
  return `${Math.round(v)} min`
}
export function pct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(digits)}%`
}
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000)) // clamp clock skew
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}
export function localTime(iso: string, tz?: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: tz })
}
export function localDate(iso: string, tz?: string): string {
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: tz })
}
/** Read a CSS variable (for chart colors that must follow the theme). Reactive to theme flips. */
export function cssVar(name: string): string {
  void themeTick.value
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** Label a local calendar day given as 'YYYY-MM-DD' without any time-zone shift. */
export function dayLabel(ymd: string, opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }): string {
  const [y, m, d] = ymd.split('-').map(Number)
  if (!y || !m || !d) return ymd
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString([], { ...opts, timeZone: 'UTC' })
}
