// Words for the action log: what each logged action was and which rule asked for it.
import type { ControlActionOut } from '@/api/types'

/** control_actions.rule -> words (lowercase: they sit in the log's "who · mode · channel" line). */
export const LOG_RULES: Record<string, string> = {
  comfort: 'comfort',
  sleep: 'sleep',
  linked_floors: 'linked floors',
  house_setback: 'house setback',
  recovery: 'recovery',
  precool: 'pre-cool',
  independent: 'independent',
  hold_off: 'holding off',
  event_prep: 'pre-cooling before a utility event',
  utility_opt_out: 'skipped utility event',
  handback: 'hand back to ecobee',
  manual: 'from the app',
  sensor_set: 'sensor set',
  settings: 'ecobee settings',
}

export function ruleText(rule: string): string {
  return LOG_RULES[rule] ?? rule.replace(/_/g, ' ')
}

/** control_actions.actor -> words. */
const LOG_ACTORS: Record<string, string> = { controller: 'controller', owner: 'you', homekit_service: 'HomeKit' }

export function actorText(actor: string): string {
  return LOG_ACTORS[actor] ?? actor.replace(/_/g, ' ')
}

/** What the row did. resume_program rows say which resume (request.kind); the controller's
 *  "hold seen" / "resume seen" rows are logged as skipped set_hold rows with their own kind. */
export function actionText(a: Pick<ControlActionOut, 'action' | 'request'>): string {
  const kind = typeof a.request?.kind === 'string' ? a.request.kind : null
  if (a.action === 'opt_out_event') return 'opt out of utility event'
  if (a.action === 'resume_program') {
    if (kind === 'automatic') return 'back to automatic'
    if (kind === 'handback') return 'resume (hand back)'
    return 'resume schedule'
  }
  if (a.action === 'set_hold') {
    if (kind === 'manual_hold_detected') return 'hold seen'
    if (kind === 'manual_resume_detected') return 'resume seen'
    return 'set hold'
  }
  if (a.action === 'update_program') return 'sensor set'
  if (a.action === 'update_settings') return 'ecobee settings'
  return a.action.replace(/_/g, ' ')
}
