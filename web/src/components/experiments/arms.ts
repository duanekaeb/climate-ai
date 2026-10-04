// Shared look for experiments: one color per arm (by position) and status chips.
import type { ExperimentAnalysis, ExperimentOut } from '@/api/types'

export const ARM_COLORS = ['var(--color-accent)', 'var(--color-unit-up)', 'var(--color-unit-bed)']

export function armColor(index: number): string {
  return ARM_COLORS[index % ARM_COLORS.length]
}

export const STATUS_CHIP: Record<ExperimentOut['status'], string> = {
  proposed: 'bg-warn/15 text-warn',
  approved: 'bg-accent/15 text-accent',
  running: 'bg-good/15 text-good',
  stopped: 'bg-surface-2 text-muted',
  completed: 'bg-surface-2 text-ink',
  rejected: 'bg-bad/15 text-bad',
}

export const STATUS_LABEL: Record<ExperimentOut['status'], string> = {
  proposed: 'awaiting your approval',
  approved: 'approved, starts soon',
  running: 'running',
  stopped: 'stopped',
  completed: 'completed',
  rejected: 'rejected',
}

export const DECISION: Record<ExperimentAnalysis['decision'], { label: string; chip: string }> = {
  not_started: { label: 'not started', chip: 'bg-surface-2 text-muted' },
  continue: { label: 'keep going', chip: 'bg-accent/15 text-accent' },
  stop_win: { label: 'stop: a winner', chip: 'bg-good/15 text-good' },
  stop_futile: { label: 'stop: no useful difference', chip: 'bg-warn/15 text-warn' },
  inconclusive: { label: 'inconclusive', chip: 'bg-surface-2 text-ink' },
}
