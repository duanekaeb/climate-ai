// Plain words for the house model's parameters and helpers to read the untyped
// model_fits.metrics / params dicts safely.
import type { ModelFitOut } from '@/api/types'
import { numberAt } from '@/components/analysis/stats'

const ZONES: Record<string, string> = {
  main: 'main floor',
  m: 'main floor',
  up: 'upstairs',
  u: 'upstairs',
  bed: 'bed wing',
  b: 'bed wing',
}

// The RC model's per-zone rate parameters, named "<zone>.<param>" by the API
// (api/climate/models/thermal_rc.py PARAM_NAMES): main has ua_out, ua_up, ua_bed, sun, q,
// gain; up has ua_out, ua_main, k_stack, sun, q, gain; bed has ua_out, ua_main, sun, q, gain.
const RATE: Record<string, string> = {
  ua_out: 'how quickly it gains or loses heat to the outdoors (insulation)',
  ua_up: 'how easily heat moves between it and the upstairs',
  ua_main: 'how easily heat moves between it and the main floor',
  ua_bed: 'how easily heat moves between it and the bed wing',
  k_stack: 'extra heat rising up the stairwell when the main floor is warmer',
  k_s: 'extra heat rising up the stairwell when the main floor is warmer',
  sun: 'how much the sun heats it',
  q: 'how much its unit heats or cools it per minute of runtime',
  gain: 'steady heat from people and appliances',
}

// Older physical-form names ("R_mu", "C_up"), kept so stored fits still read in plain words.
const BASE: Record<string, string> = {
  C: 'how much heat it stores (thermal mass)',
  R: 'how well it is insulated from outdoors',
  R_out: 'how well it is insulated from outdoors',
  R_mu: 'how easily heat moves between the main floor and the upstairs',
  R_mb: 'how easily heat moves between the main floor and the bed wing',
  k_s: 'extra heat rising up the stairwell when the main floor is warmer',
  a: 'how much the sun heats it',
  a_sun: 'how much the sun heats it',
  Q: 'how much heat the unit moves per minute of runtime',
  g: 'steady heat from people and appliances',
}

/** "main.ua_up" -> "Main floor: how easily heat moves between it and the upstairs";
 *  "up.k_stack" -> "Upstairs: extra heat rising up the stairwell …". Unknown names pass through. */
export function plainParam(name: string): string {
  const dot = name.indexOf('.')
  if (dot > 0) {
    const zone = ZONES[name.slice(0, dot)]
    const words = RATE[name.slice(dot + 1)] ?? BASE[name.slice(dot + 1)]
    if (words) return zone ? `${capitalize(zone)}: ${words}` : capitalize(words)
    return name
  }
  if (RATE[name]) return capitalize(RATE[name])
  if (BASE[name]) return capitalize(BASE[name])
  const i = name.lastIndexOf('_')
  if (i > 0) {
    const base = name.slice(0, i)
    const zone = ZONES[name.slice(i + 1)]
    if (zone && BASE[base]) return `${capitalize(zone)}: ${BASE[base]}`
  }
  return name
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function stringList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

export interface RcScores {
  rmse1h: number | null
  rmse24h: number | null
  persist1h: number | null
  persist24h: number | null
  unidentified: string[]
}

export function rcScores(fit: ModelFitOut): RcScores {
  const m = fit.metrics
  return {
    rmse1h: numberAt(m, 'rmse_1h_f', 'rmse_1h'),
    rmse24h: numberAt(m, 'rmse_24h_f', 'rmse_24h'),
    persist1h: numberAt(m, 'persistence_rmse_1h_f', 'persistence_1h_f', 'persistence_rmse_1h', 'persistence_1h'),
    persist24h: numberAt(m, 'persistence_rmse_24h_f', 'persistence_24h_f', 'persistence_rmse_24h', 'persistence_24h'),
    unidentified: stringList(m.unidentified ?? fit.params.unidentified),
  }
}

export const FIT_STATUS_CHIP: Record<string, string> = {
  active: 'bg-good/15 text-good',
  candidate: 'bg-accent/15 text-accent',
  shadow: 'bg-accent/15 text-accent',
  failed: 'bg-bad/15 text-bad',
  rejected: 'bg-bad/15 text-bad',
  retired: 'bg-surface-2 text-muted',
}

export function fitChip(status: string): string {
  return FIT_STATUS_CHIP[status] ?? 'bg-surface-2 text-muted'
}
