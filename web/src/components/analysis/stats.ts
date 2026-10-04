// Formatting and date helpers for the analysis, experiment, model and control screens.
// Statistics on screen are always a point estimate plus its 90% interval, so the helpers
// here format both together and say plainly when an interval straddles zero.

const MINUS = '−'

/** A number with an explicit sign ("+4.2", "−3.1"), or "—" when missing. */
export function signed(v: number | null | undefined, digits = 1, unit = ''): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const r = Number(v.toFixed(digits))
  if (r === 0) return `0${unit}`
  return `${r > 0 ? '+' : MINUS}${Math.abs(r).toFixed(digits)}${unit}`
}

/** A plain number with fixed digits, or "—". */
export function num(v: number | null | undefined, digits = 1, unit = ''): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${v < 0 ? MINUS : ''}${Math.abs(v).toFixed(digits)}${unit}`
}

/** "−2.0% to +9.1%" */
export function interval(lo: number | null | undefined, hi: number | null | undefined, digits = 1, unit = ''): string {
  if (lo === null || lo === undefined || hi === null || hi === undefined) return 'no interval'
  return `${signed(lo, digits, unit)} to ${signed(hi, digits, unit)}`
}

/** True when the interval includes zero (the data can't tell the effect from none). */
export function crossesZero(lo: number | null | undefined, hi: number | null | undefined): boolean {
  if (lo === null || lo === undefined || hi === null || hi === undefined) return true
  return lo <= 0 && hi >= 0
}

/** The API sends 90% intervals as untyped pairs; keep them only when both ends are numbers. */
export function pair(v: readonly unknown[] | null | undefined): [number, number] | null {
  if (!v || v.length !== 2) return null
  const [a, b] = v
  if (typeof a !== 'number' || typeof b !== 'number' || !Number.isFinite(a) || !Number.isFinite(b)) return null
  return a <= b ? [a, b] : [b, a]
}

/** Minutes as "45 min" / "3.2 h" with an explicit sign. */
export function signedMinutes(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const sign = v > 0 ? '+' : v < 0 ? MINUS : ''
  const a = Math.abs(v)
  if (a >= 90) return `${sign}${(a / 60).toFixed(1)} h`
  return `${sign}${Math.round(a)} min`
}

/** Hours with one decimal, for chart axes that hold daily or weekly runtime. */
export function hours(minutesValue: number): number {
  return Math.round((minutesValue / 60) * 10) / 10
}

/** A safe number read from an untyped JSON dict (model metrics, gate details). */
export function numberAt(obj: Record<string, unknown> | null | undefined, ...keys: string[]): number | null {
  if (!obj) return null
  for (const k of keys) {
    const v = obj[k]
    if (typeof v === 'number' && Number.isFinite(v)) return v
  }
  return null
}

/** A short "key: value · key: value" summary of the primitive fields in an untyped dict. */
export function summarize(obj: unknown, max = 5): string {
  if (obj === null || obj === undefined) return ''
  if (typeof obj === 'string') return obj
  if (typeof obj === 'number') return Number.isInteger(obj) ? String(obj) : obj.toFixed(2)
  if (typeof obj === 'boolean') return obj ? 'yes' : 'no'
  if (Array.isArray(obj)) return obj.slice(0, max).map((v) => summarize(v, 2)).join(', ')
  if (typeof obj !== 'object') return ''
  const parts: string[] = []
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    if (v === null || typeof v === 'object') continue
    parts.push(`${k.replace(/_/g, ' ')}: ${summarize(v)}`)
    if (parts.length >= max) break
  }
  return parts.join(' · ')
}

// ---------------------------------------------------------------------------------------
// Calendar days ("YYYY-MM-DD", the house's local days). Arithmetic runs in UTC so a
// browser in another timezone never shifts a day.
// ---------------------------------------------------------------------------------------

function parseDay(iso: string): Date {
  return new Date(`${iso.slice(0, 10)}T00:00:00Z`)
}

function toDay(d: Date): string {
  return d.toISOString().slice(0, 10)
}

/** Today's date in the house timezone (falls back to the browser's). */
export function todayIn(tz?: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date())
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    const d = new Date()
    return toDay(new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())))
  }
}

export function addDays(iso: string, n: number): string {
  const d = parseDay(iso)
  d.setUTCDate(d.getUTCDate() + n)
  return toDay(d)
}

/** Monday = 0 … Sunday = 6 (the backend's convention). */
export function weekdayIndex(iso: string): number {
  return (parseDay(iso).getUTCDay() + 6) % 7
}

export function mondayOf(iso: string): string {
  return addDays(iso, -weekdayIndex(iso))
}

export function daysBetween(a: string, b: string): number {
  return Math.round((parseDay(b).getTime() - parseDay(a).getTime()) / 86_400_000)
}

/** "Sep 22" */
export function dayLabel(iso: string | null | undefined): string {
  if (!iso) return '—'
  return parseDay(iso).toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

/** "Mon, Sep 22" */
export function dayLabelLong(iso: string | null | undefined): string {
  if (!iso) return '—'
  return parseDay(iso).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
}

/** "Sep 22 – Oct 5" */
export function dayRange(a: string | null | undefined, b: string | null | undefined): string {
  if (!a && !b) return '—'
  return `${dayLabel(a)} – ${dayLabel(b)}`
}

/** A UTC timestamp shown in the house timezone: "Oct 3, 9:41 PM". */
export function dateTime(iso: string | null | undefined, tz?: string): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: tz,
  })
}

/** Elapsed time between two timestamps: "42 s", "3 min", "1.2 h". */
export function duration(fromIso: string | null | undefined, toIso: string | null | undefined): string {
  if (!fromIso || !toIso) return '—'
  const s = Math.max(0, (new Date(toIso).getTime() - new Date(fromIso).getTime()) / 1000)
  if (s < 90) return `${Math.round(s)} s`
  if (s < 5400) return `${Math.round(s / 60)} min`
  return `${(s / 3600).toFixed(1)} h`
}

export const WEEKDAYS_SHORT = ['M', 'T', 'W', 'T', 'F', 'S', 'S']
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** Error text for anything thrown by the API client. */
export function errorText(e: unknown): string {
  if (e instanceof Error) return e.message
  return String(e)
}

/** Room key -> readable name when the status payload isn't loaded ("girls_room" -> "Girls room"). */
export function prettyKey(key: string): string {
  const s = key.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** ECharts tooltips render formatter output as HTML; escape any text that came from the API. */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c)
}
