// Unit keys in display order (main, up, bed) and their names.
import type { SettingsOut } from '@/api/types'
import { UNIT_NAMES } from '@/lib/format'

const ORDER = Object.keys(UNIT_NAMES)

export function unitKeys(s: SettingsOut | null | undefined): string[] {
  const keys = s ? Object.keys(s.control.comfort) : []
  return [...(keys.length ? keys : ORDER)].sort((a, b) => rank(a) - rank(b))
}

function rank(k: string): number {
  const i = ORDER.indexOf(k)
  return i < 0 ? ORDER.length : i
}

export function unitName(k: string): string {
  return UNIT_NAMES[k] ?? k
}
