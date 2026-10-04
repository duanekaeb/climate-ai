// Plain-language labels for the tunable policy (PolicyParams). Bounds and types come from
// the backend's JSON Schema so the inputs here can never drift from the validation the API
// applies. Only the PolicyParams definition is imported (policyParams.schema.json, a copy of
// $defs.PolicyParams in src/api/schema.json, written by `npm run gen:types` and checked by
// api/tests/test_web_policy_schema.py): the whole schema.json would ship every API request and
// response shape in the public bundle.
import policyParamsSchema from './policyParams.schema.json'
import type { PolicyParams } from '@/api/types'

export type PolicyKey = keyof PolicyParams
export type ParamValue = number | boolean
export type ParamOverrides = Partial<Record<PolicyKey, ParamValue>>

interface JsonProp {
  type: string
  minimum?: number
  maximum?: number
  default?: number | boolean
}

const PROPS: Record<string, JsonProp> = policyParamsSchema.properties

export interface ParamMeta {
  key: PolicyKey
  label: string
  help: string
  kind: 'number' | 'integer' | 'boolean'
  min: number | null
  max: number | null
  step: number
  unit: '°F' | 'min' | 'hour' | ''
  default: ParamValue | null
}

const TEXT: Record<PolicyKey, { label: string; help: string; unit: ParamMeta['unit']; step?: number }> = {
  linked_floors_enabled: {
    label: 'Linked floors rule',
    help: 'When the main floor is empty by day and someone is upstairs, keep it tied to the upstairs instead of floating warm.',
    unit: '',
  },
  linked_offset_f: {
    label: 'Main floor below upstairs (cooling)',
    help: 'How far under the upstairs cool setpoint the empty main floor is held.',
    unit: '°F',
    step: 0.5,
  },
  linked_heat_gap_f: {
    label: 'Main floor heat gap (heating)',
    help: 'How far under the upstairs heat setpoint the main floor may sit.',
    unit: '°F',
    step: 0.5,
  },
  setback_gap_f: {
    label: 'Setback gap, house empty',
    help: 'While everyone is away, the main floor stays this much cooler than the upstairs.',
    unit: '°F',
    step: 0.5,
  },
  recovery_lead_min: {
    label: 'Main floor recovery lead',
    help: 'The main floor starts recovering this long before the upstairs.',
    unit: 'min',
    step: 5,
  },
  precool_enabled: {
    label: 'Pre-cool on hot, sunny days',
    help: 'Cool a little deeper before the afternoon peak.',
    unit: '',
  },
  precool_degrees_f: {
    label: 'Pre-cool depth',
    help: 'How much lower the cool setpoint goes while pre-cooling.',
    unit: '°F',
    step: 0.5,
  },
  precool_start_hour: {
    label: 'Pre-cool start',
    help: 'Local hour (24 h clock) when pre-cooling starts.',
    unit: 'hour',
  },
  precool_min_forecast_high_f: {
    label: 'Pre-cool when the high reaches',
    help: 'Only pre-cool when the forecast high is at least this.',
    unit: '°F',
    step: 1,
  },
  bed_wing_independent: {
    label: 'Bed / Office runs independently',
    help: 'The wing keeps its own comfort unless the data shows it is coupled to the main floor.',
    unit: '',
  },
}

export const POLICY_KEYS = Object.keys(TEXT) as PolicyKey[]

export function isPolicyKey(k: string): k is PolicyKey {
  return Object.prototype.hasOwnProperty.call(TEXT, k)
}

export function paramMeta(key: PolicyKey): ParamMeta {
  const p = PROPS[key]
  const t = TEXT[key]
  const kind: ParamMeta['kind'] = p?.type === 'boolean' ? 'boolean' : p?.type === 'integer' ? 'integer' : 'number'
  return {
    key,
    label: t.label,
    help: t.help,
    kind,
    min: p?.minimum ?? null,
    max: p?.maximum ?? null,
    step: t.step ?? (kind === 'integer' ? 1 : 0.5),
    unit: t.unit,
    default: p?.default ?? null,
  }
}

export function formatParam(key: string, value: unknown): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value !== 'number' || !Number.isFinite(value)) return value === undefined || value === null ? '—' : String(value)
  if (!isPolicyKey(key)) return String(value)
  const unit = TEXT[key].unit
  if (unit === 'hour') return `${String(value).padStart(2, '0')}:00`
  if (unit === 'min') return `${value} min`
  if (unit === '°F') return `${Number.isInteger(value) ? value : value.toFixed(1)}°F`
  return String(value)
}

export function labelFor(key: string): string {
  return isPolicyKey(key) ? TEXT[key].label : key.replace(/_/g, ' ')
}

/** Problems with a set of overrides, in plain words (empty = valid). */
export function validateOverrides(params: Record<string, unknown>): string[] {
  const errors: string[] = []
  for (const [k, v] of Object.entries(params)) {
    if (!isPolicyKey(k)) {
      errors.push(`${k} is not a policy setting.`)
      continue
    }
    const m = paramMeta(k)
    if (m.kind === 'boolean') {
      if (typeof v !== 'boolean') errors.push(`${m.label}: choose on or off.`)
      continue
    }
    if (typeof v !== 'number' || !Number.isFinite(v)) {
      errors.push(`${m.label}: enter a number.`)
      continue
    }
    if (m.kind === 'integer' && !Number.isInteger(v)) errors.push(`${m.label}: whole numbers only.`)
    if ((m.min !== null && v < m.min) || (m.max !== null && v > m.max))
      errors.push(`${m.label}: must be between ${formatParam(k, m.min)} and ${formatParam(k, m.max)}.`)
  }
  return errors
}

/** Overrides that change nothing compared with the current policy are dropped. */
export function effectiveOverrides(params: ParamOverrides, current: PolicyParams | null): ParamOverrides {
  if (!current) return { ...params }
  const out: ParamOverrides = {}
  for (const k of Object.keys(params) as PolicyKey[]) {
    const v = params[k]
    if (v !== undefined && v !== current[k]) out[k] = v
  }
  return out
}
