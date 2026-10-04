// What each API-token role may do, in plain words (the server enforces it; see
// docs/specs/users-and-tokens.md, "Who may call what"). No role gets the owner's full rights.
import type { ApiTokenOut } from '@/api/types'

export type TokenRole = ApiTokenOut['role']

export const TOKEN_ROLES: { value: TokenRole; label: string; help: string }[] = [
  {
    value: 'agent',
    label: 'Agent',
    help: "What the Claude agent and the MCP server need: read everything, propose changes, and sign off or hold the models' changes, each still checked against your limits. No holds, settings or mode.",
  },
  {
    value: 'viewer',
    label: 'Viewer',
    help: 'Read only: a dashboard, a wall display, a script that just looks.',
  },
  {
    value: 'control',
    label: 'Control',
    help: 'Read, plus everyday controls: timed holds, resume schedule, back to automatic, presence, and skipping utility events. No settings, mode or setup.',
  },
]

export const ROLE_LABEL: Record<TokenRole, string> = { agent: 'Agent', viewer: 'Viewer', control: 'Control' }

export const ROLE_CHIP: Record<TokenRole, string> = {
  agent: 'bg-accent/15 text-accent',
  viewer: 'bg-surface-2 text-muted',
  control: 'bg-warn/15 text-warn',
}

/** Expiry choices for a new token (value in days; '' = never). */
export const EXPIRY_OPTIONS: { value: string; label: string }[] = [
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
  { value: '', label: 'Never' },
]
