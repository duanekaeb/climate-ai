// Daily and intraday runtime for the Runtime tab. Minutes on the wire (stage-1 runtime).
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { BaselineOut, DailyRuntime, Intraday } from '@/api/types'
import { UNIT_NAMES } from '@/lib/format'

export const UNIT_ORDER = ['main', 'up', 'bed']

// ASHRAE Guideline 14 checks a weather baseline must pass before its expectation is used.
export const BASELINE_CV_MAX = 0.2
export const BASELINE_NMBE_MAX = 0.005

/** Baselines that fail their checks, keyed `unit:mode`. */
export type FailingBaselines = Map<string, BaselineOut>

export function failingBaselines(list: BaselineOut[] | null | undefined): FailingBaselines {
  const out: FailingBaselines = new Map()
  for (const b of list ?? []) if (!b.passes) out.set(`${b.unit_key}:${b.mode}`, b)
  return out
}

/** The failing baseline behind a day's expectation, if any. */
export function failedBaseline(r: DailyRuntime, failing: FailingBaselines): BaselineOut | null {
  if (r.expected_min === null || r.mode === null) return null
  return failing.get(`${r.unit_key}:${r.mode}`) ?? null
}

/** A day's expectation, or null when there is none or its baseline fails its checks. */
export function trustedExpected(r: DailyRuntime, failing: FailingBaselines): number | null {
  return failedBaseline(r, failing) ? null : r.expected_min
}

/** "Upstairs cooling baseline fails its checks (CV(RMSE) 21.3%)" */
export function baselineFailNote(b: BaselineOut): string {
  const pct = (v: number, d: number) => `${v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(d)}%`
  const why: string[] = []
  const cvBad = b.cvrmse > BASELINE_CV_MAX
  const nmbeBad = Math.abs(b.nmbe) > BASELINE_NMBE_MAX
  if (cvBad || !nmbeBad) why.push(`CV(RMSE) ${pct(b.cvrmse, 1)}`)
  if (nmbeBad) why.push(`NMBE ${pct(b.nmbe, 2)}`)
  const mode = b.mode === 'cool' ? 'cooling' : 'heating'
  return `${UNIT_NAMES[b.unit_key] ?? b.unit_key} ${mode} baseline fails its checks (${why.join(', ')})`
}

/** The failing baselines that actually sit behind some expectation in these rows. */
export function failingInUse(rows: DailyRuntime[], failing: FailingBaselines): BaselineOut[] {
  const seen = new Map<string, BaselineOut>()
  for (const r of rows) {
    const b = failedBaseline(r, failing)
    if (b) seen.set(`${b.unit_key}:${b.mode}`, b)
  }
  const rank = (k: string) => (UNIT_ORDER.indexOf(k) < 0 ? UNIT_ORDER.length : UNIT_ORDER.indexOf(k))
  return [...seen.values()].sort((a, b) => rank(a.unit_key) - rank(b.unit_key) || a.mode.localeCompare(b.mode))
}

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
  /** House total expected (weather-normalized); null unless every unit that ran has one whose
   *  baseline passes its checks (a failing baseline's expectation is never summed in). */
  expected: number | null
  /** Units that ran this day whose expectation was left out because the baseline fails. */
  excluded: string[]
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

export function summarizeDays(rows: DailyRuntime[], failing: FailingBaselines = new Map()): RuntimeDay[] {
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
      const excluded: string[] = []
      for (const r of list) {
        byUnit[r.unit_key] = r
        const active = activeMinutes(r)
        total += active
        maxed += r.maxed_min
        if (outdoorMean === null && r.outdoor_mean_f !== null) outdoorMean = r.outdoor_mean_f
        const exp = trustedExpected(r, failing)
        if (failedBaseline(r, failing)) excluded.push(r.unit_key)
        if (exp === null) {
          if (active > 0) expected = null
        } else if (expected !== null) {
          expected += exp
        }
      }
      if (expected !== null && !list.some((r) => trustedExpected(r, failing) !== null)) expected = null
      return { date, byUnit, total, expected, excluded, outdoorMean, maxed }
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
  /** Sum of the expectations whose baseline passes its checks. */
  expected: number | null
  daysWithExpected: number
  /** Days whose expectation was left out because the baseline fails its checks. */
  daysFailing: number
  days: number
}

export function unitTotals(rows: DailyRuntime[], failing: FailingBaselines = new Map()): UnitTotals[] {
  const map = new Map<string, UnitTotals>()
  for (const r of rows) {
    let t = map.get(r.unit_key)
    if (!t) {
      t = { unit_key: r.unit_key, active: 0, cool: 0, heat: 0, aux: 0, fan: 0, maxed: 0, expected: null, daysWithExpected: 0, daysFailing: 0, days: 0 }
      map.set(r.unit_key, t)
    }
    t.active += activeMinutes(r)
    t.cool += r.cool_min
    t.heat += r.heat_min
    t.aux += r.aux_min
    t.fan += r.fan_min
    t.maxed += r.maxed_min
    t.days += 1
    const exp = trustedExpected(r, failing)
    if (exp !== null) {
      t.expected = (t.expected ?? 0) + exp
      t.daysWithExpected += 1
    } else if (failedBaseline(r, failing)) {
      t.daysFailing += 1
    }
  }
  return sortUnitKeys(map.keys()).map((k) => map.get(k) as UnitTotals)
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

let dailySeq = 0
let intradaySeq = 0
let baselineSeq = 0

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
    /** Active weather baselines: an expectation whose baseline fails is never presented as working. */
    baselines: null as BaselineOut[] | null,
    baselinesLoading: false,
    baselinesError: '',
  }),
  getters: {
    failing: (s): FailingBaselines => failingBaselines(s.baselines),
    /** True once we know which baselines pass (or know we can't tell). */
    baselinesKnown: (s): boolean => s.baselines !== null || !!s.baselinesError,
  },
  actions: {
    async loadBaselines() {
      const mine = ++baselineSeq
      this.baselinesLoading = true
      this.baselinesError = ''
      try {
        const list = await api.get<BaselineOut[]>('/analytics/baselines')
        if (mine === baselineSeq) this.baselines = list
      } catch (e) {
        if (mine === baselineSeq) this.baselinesError = message(e)
      } finally {
        if (mine === baselineSeq) this.baselinesLoading = false
      }
    },
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
