// Human labels for the Live screen (controller modes, calls, policy rules, holds, equipment).
import type { ControllerInfo, UnitLive, UnitTarget } from '@/api/types'

export const MODE_INFO: Record<ControllerInfo['mode'], { label: string; hint: string; cls: string }> = {
  off: { label: 'Off', hint: 'Not touching the thermostats', cls: 'bg-surface-2 text-muted' },
  suggest: { label: 'Suggest', hint: 'Plans and suggests; no writes', cls: 'bg-accent/15 text-accent' },
  act: { label: 'Act', hint: 'Writes timed holds, read back', cls: 'bg-good/15 text-good' },
}

export const CALL_INFO: Record<UnitLive['call'], { label: string; icon: string; cls: string }> = {
  cool: { label: 'Cooling', icon: 'snow', cls: 'bg-accent/15 text-accent' },
  heat: { label: 'Heating', icon: 'flame', cls: 'bg-bad/15 text-bad' },
  fan: { label: 'Fan only', icon: 'fan', cls: 'bg-surface-2 text-ink' },
  idle: { label: 'Idle', icon: 'check', cls: 'bg-surface-2 text-muted' },
  unknown: { label: 'Unknown', icon: 'alert', cls: 'bg-warn/15 text-warn' },
}

export const RULE_LABELS: Record<UnitTarget['rule'], string> = {
  comfort: 'Comfort',
  sleep: 'Sleep',
  linked_floors: 'Linked floors',
  house_setback: 'House setback',
  recovery: 'Recovery',
  precool: 'Pre-cool',
  independent: 'Independent',
  hold_off: 'Holding off',
  event_prep: 'Pre-cooling before a utility event',
}

/** The rule's label; 'event_prep' says pre-heating when the plan's reason does (heating season). */
export function ruleLabel(target: Pick<UnitTarget, 'rule' | 'reason'>): string {
  if (target.rule === 'event_prep' && /^pre-heat/i.test(target.reason)) return 'Pre-heating before a utility event'
  return RULE_LABELS[target.rule]
}

type HoldOwner = NonNullable<UnitLive['hold_owner']>

/** The small badge next to a unit's hold line: whose hold is running. */
export const HOLD_BADGES: Record<HoldOwner, { label: string; cls: string }> = {
  person: { label: 'Your hold', cls: 'bg-warn/15 text-warn' },
  app: { label: 'Your hold', cls: 'bg-warn/15 text-warn' },
  utility: { label: 'Utility event', cls: 'bg-accent/15 text-accent' },
  vacation: { label: 'Vacation', cls: 'bg-surface-2 text-ink' },
  ecobee_auto: { label: 'Smart Away', cls: 'bg-surface-2 text-ink' },
  controller: { label: 'Ours', cls: 'bg-good/15 text-good' },
  unknown_event: { label: 'ecobee event', cls: 'bg-warn/15 text-warn' },
}

/** ecobee equipment names ('compCool1', 'auxHeat1', 'fan', ...) -> icon + label. */
export function equipment(name: string): { icon: string; label: string } {
  const n = name.toLowerCase()
  const stage = /(\d)$/.exec(n)?.[1]
  const suffix = stage && stage !== '1' ? ` stage ${stage}` : ''
  if (n.includes('aux')) return { icon: 'flame', label: `Aux heat${suffix}` }
  if (n.includes('cool')) return { icon: 'snow', label: `Cooling${suffix}` }
  if (n.includes('heat')) return { icon: 'flame', label: `Heating${suffix}` }
  if (n.includes('fan')) return { icon: 'fan', label: 'Fan' }
  if (n.includes('humid')) return { icon: 'fan', label: 'Humidifier' }
  return { icon: 'fan', label: name }
}
