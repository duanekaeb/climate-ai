// Daily and intraday runtime for the Runtime tab. Minutes on the wire (stage-1 runtime).
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { DailyRuntime, Intraday } from '@/api/types'

export const UNIT_ORDER = ['main', 'up', 'bed']

/** The day's conditioning minutes for one unit: cooling on cool days, heating on heat days.
 *  heat_min is already the right heat measure per unit (compHeat1 or auxHeat1), so aux is not
 *  added on top; with no mode (no runtime / mixed day) cooling and heating are summed. */
export function activeMinutes(r: DailyRuntime): number {
  if (r.mode === 'cool') return r.cool_min
  if (r.mode === 'heat') return r.heat_min
  return r.cool_min + r.heat_min
}

export interface RuntimeDay {
  date: string
  byUnit: Record<string, DailyRuntime>
  total: number
  /** House total expected (weather-normalized); null unless every unit that ran has one. */
  expected: number | null
  outdoorMean: number | null
  maxed: number
}

export function sortUnitKeys(keys: Iterable<string>): string[] {
  const rank = (k: string) => {
    const i = UNIT_ORDER.indexOf(k)
    return i < 0 ? UNIT_ORDER.length : i
  }
  return [...new Set(keys)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
}

export function summarizeDays(rows: DailyRuntime[]): RuntimeDay[] {
  const byDate = new Map<string, DailyRuntime[]>()
  for (const r of rows) {
    const list = byDate.get(r.date)
    if (list) list.push(r)
    else byDate.set(r.date, [r])
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([date, list]) => {
      const byUnit: Record<string, DailyRuntime> = {}
      let total = 0
      let maxed = 0
      let expected: number | null = 0
      let outdoorMean: number | null = null
      for (const r of list) {
        byUnit[r.unit_key] = r
        const active = activeMinutes(r)
        total += active
        maxed += r.maxed_min
        if (outdoorMean === null && r.outdoor_mean_f !== null) outdoorMean = r.outdoor_mean_f
        if (r.expected_min === null) {
          if (active > 0) expected = null
        } else if (expected !== null) {
          expected += r.expected_min
        }
      }
      if (expected !== null && !list.some((r) => r.expected_min !== null)) expected = null
      return { date, byUnit, total, expected, outdoorMean, maxed }
    })
}

export interface UnitTotals {
  unit_key: string
  active: number
  cool: number
  heat: number
  aux: number
  fan: number
  maxed: number
  expected: number | null
  daysWithExpected: number
  days: number
}

export function unitTotals(rows: DailyRuntime[]): UnitTotals[] {
  const map = new Map<string, UnitTotals>()
  for (const r of rows) {
    let t = map.get(r.unit_key)
    if (!t) {
      t = { unit_key: r.unit_key, active: 0, cool: 0, heat: 0, aux: 0, fan: 0, maxed: 0, expected: null, daysWithExpected: 0, days: 0 }
      map.set(r.unit_key, t)
    }
    t.active += activeMinutes(r)
    t.cool += r.cool_min
    t.heat += r.heat_min
    t.aux += r.aux_min
    t.fan += r.fan_min
    t.maxed += r.maxed_min
    t.days += 1
    if (r.expected_min !== null) {
      t.expected = (t.expected ?? 0) + r.expected_min
      t.daysWithExpected += 1
    }
  }
  return sortUnitKeys(map.keys()).map((k) => map.get(k) as UnitTotals)
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

let dailySeq = 0
let intradaySeq = 0

export const useRuntime = defineStore('runtime', {
  state: () => ({
    days: 30,
    daily: null as DailyRuntime[] | null,
    dailyLoading: false,
    dailyError: '',
    date: '',
    intraday: null as Intraday | null,
    intradayLoading: false,
    intradayError: '',
  }),
  actions: {
    async loadDaily(days: number) {
      const mine = ++dailySeq
      if (days !== this.days) this.daily = null
      this.days = days
      this.dailyLoading = true
      this.dailyError = ''
      try {
        const rows = await api.get<DailyRuntime[]>('/runtime/daily', { days })
        if (mine === dailySeq) this.daily = rows
      } catch (e) {
        if (mine === dailySeq) this.dailyError = message(e)
      } finally {
        if (mine === dailySeq) this.dailyLoading = false
      }
    },
    async loadIntraday(date: string) {
      const mine = ++intradaySeq
      if (date !== this.date) this.intraday = null
      this.date = date
      this.intradayLoading = true
      this.intradayError = ''
      try {
        const d = await api.get<Intraday>('/runtime/intraday', { date })
        if (mine === intradaySeq) this.intraday = d
      } catch (e) {
        if (mine === intradaySeq) this.intradayError = message(e)
      } finally {
        if (mine === intradaySeq) this.intradayLoading = false
      }
    },
  },
})
