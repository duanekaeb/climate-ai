// Display helpers shared by every view.
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
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
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
/** Read a CSS variable (for chart colors that must follow the theme). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
