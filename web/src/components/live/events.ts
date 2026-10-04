// Utility events on Live: one group per event (the same event on several thermostats shares
// an event_key), and times in house time ("Tue 3:00–6:00 PM").
import type { UtilityEventOut } from '@/api/types'
import { addDays } from '@/components/analysis/stats'
import { localDate, localTime } from '@/lib/format'

/** The local calendar day ('YYYY-MM-DD') of an instant in the house timezone. */
export function localDay(iso: string, tz?: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' })
      .formatToParts(new Date(iso))
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return iso.slice(0, 10)
  }
}

/** 'Today', 'Tomorrow', 'Yesterday' (lowercase mid-sentence), a weekday within the coming
 *  week, else 'Oct 12'. */
export function dayWord(iso: string, tz: string | undefined, nowIso: string, capital = true): string {
  const day = localDay(iso, tz)
  const today = localDay(nowIso, tz)
  const word = (w: string) => (capital ? w : w.toLowerCase())
  if (day === today) return word('Today')
  if (day === addDays(today, 1)) return word('Tomorrow')
  if (day === addDays(today, -1)) return word('Yesterday')
  if (day > today && day <= addDays(today, 6)) {
    const [y, m, d] = day.split('-').map(Number)
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString([], { weekday: 'short', timeZone: 'UTC' })
  }
  return localDate(iso, tz)
}

/** '6:10 PM' on the same house day as now, else 'tomorrow 6:10 PM' / 'Thu 6:10 PM' /
 *  'Oct 12, 6:10 PM' (for use mid-sentence). */
export function when(iso: string, tz: string | undefined, nowIso: string): string {
  if (localDay(iso, tz) === localDay(nowIso, tz)) return localTime(iso, tz)
  const word = dayWord(iso, tz, nowIso, false)
  return /\d/.test(word) ? `${word}, ${localTime(iso, tz)}` : `${word} ${localTime(iso, tz)}`
}

/** An event's window in house time: 'Today 3:00 PM–6:00 PM', 'Today 11:00 PM – tomorrow 2:00 AM'. */
export function eventWindow(start: string | null, end: string | null, tz: string | undefined, nowIso: string): string {
  if (start && end) {
    const sameDay = localDay(start, tz) === localDay(end, tz)
    const from = `${dayWord(start, tz, nowIso)} ${localTime(start, tz)}`
    return sameDay ? `${from}–${localTime(end, tz)}` : `${from} – ${dayWord(end, tz, nowIso, false)} ${localTime(end, tz)}`
  }
  if (start) return `From ${dayWord(start, tz, nowIso, false)} ${localTime(start, tz)}`
  if (end) return `Until ${dayWord(end, tz, nowIso, false)} ${localTime(end, tz)}`
  return 'Times not given'
}

export type GroupStatus = 'announced' | 'running' | 'skipped' | 'ended' | 'cancelled'

export const GROUP_STATUS: Record<GroupStatus, { label: string; cls: string }> = {
  announced: { label: 'Announced', cls: 'bg-accent/15 text-accent' },
  running: { label: 'Running', cls: 'bg-warn/15 text-warn' },
  skipped: { label: 'Skipped', cls: 'bg-good/15 text-good' },
  ended: { label: 'Ended', cls: 'bg-surface-2 text-muted' },
  cancelled: { label: 'Cancelled', cls: 'bg-surface-2 text-muted' },
}

export interface EventGroup {
  key: string
  name: string | null
  status: GroupStatus
  start_at: string | null
  end_at: string | null
  rows: UtilityEventOut[] // one per thermostat, in unit order
}

const OPEN = new Set(['announced', 'running'])

function groupStatus(rows: UtilityEventOut[]): GroupStatus {
  if (rows.some((r) => r.status === 'running')) return 'running'
  if (rows.some((r) => r.status === 'announced')) return 'announced'
  if (rows.every((r) => r.status === 'opted_out')) return 'skipped'
  if (rows.every((r) => r.status === 'cancelled')) return 'cancelled'
  return 'ended'
}

const ms = (iso: string | null) => (iso ? Date.parse(iso) : Number.POSITIVE_INFINITY)

function byTime(values: (string | null)[]): string[] {
  return values.filter((x): x is string => !!x).sort((a, b) => ms(a) - ms(b))
}

function earliest(values: (string | null)[]): string | null {
  return byTime(values)[0] ?? null
}

function latest(values: (string | null)[]): string | null {
  const v = byTime(values)
  return v[v.length - 1] ?? null
}

/**
 * The events Live shows, grouped by event_key: every event a unit card carries (running, the
 * next announced one, or one that just ended or was skipped), plus every announced or running
 * event in the list. Rows from `list` replace the same rows from the units (the list is
 * loaded after a skip, so it is the fresher copy). Running first, then by start.
 */
export function groupEvents(fromUnits: UtilityEventOut[], list: UtilityEventOut[], unitOrder: string[]): EventGroup[] {
  const keys = new Set<string>()
  for (const r of fromUnits) keys.add(r.event_key)
  for (const r of list) if (OPEN.has(r.status)) keys.add(r.event_key)

  const byId = new Map<number, UtilityEventOut>()
  for (const r of [...fromUnits, ...list]) if (keys.has(r.event_key)) byId.set(r.id, r)

  const groups = new Map<string, UtilityEventOut[]>()
  for (const r of byId.values()) groups.set(r.event_key, [...(groups.get(r.event_key) ?? []), r])

  const rank = (k: string) => {
    const i = unitOrder.indexOf(k)
    return i < 0 ? unitOrder.length : i
  }
  const out: EventGroup[] = [...groups.entries()].map(([key, rows]) => {
    rows.sort((a, b) => rank(a.unit_key) - rank(b.unit_key) || a.id - b.id)
    return {
      key,
      name: rows.find((r) => r.name)?.name ?? null,
      status: groupStatus(rows),
      start_at: earliest(rows.map((r) => r.start_at)),
      end_at: latest(rows.map((r) => r.end_at)),
      rows,
    }
  })
  const order: Record<GroupStatus, number> = { running: 0, announced: 1, skipped: 2, ended: 3, cancelled: 4 }
  return out.sort((a, b) => order[a.status] - order[b.status] || ms(a.start_at) - ms(b.start_at))
}
