<script setup lang="ts">
// Where a change stands in the gates: validation -> backtest -> shadow -> sign-off -> trial
// -> active, with what each gate recorded.
import { computed } from 'vue'
import type { ChangeOut } from '@/api/types'
import { dateTime, summarize } from '@/components/analysis/stats'

const props = defineProps<{ change: ChangeOut; tz?: string }>()

interface Stage {
  key: string
  label: string
  aliases: string[]
}
const STAGES: Stage[] = [
  { key: 'validation', label: 'Check', aliases: ['validate', 'limits'] },
  { key: 'backtest', label: 'Backtest', aliases: ['simulation'] },
  { key: 'shadow', label: 'Shadow', aliases: [] },
  { key: 'signoff', label: 'Sign-off', aliases: ['sign_off', 'decision', 'claude', 'owner'] },
  { key: 'trial', label: 'Trial', aliases: [] },
  { key: 'active', label: 'Active', aliases: [] },
]
const STATUS_STAGE: Record<string, number> = {
  proposed: 0,
  validation: 0,
  backtest: 1,
  shadow: 2,
  awaiting_signoff: 3,
  signoff: 3,
  held: 3,
  trial: 4,
  active: 5,
}

type State = 'done' | 'current' | 'held' | 'failed' | 'skipped' | 'pending'

function gate(s: Stage): unknown {
  const g = props.change.gates
  for (const k of [s.key, ...s.aliases]) if (k in g) return g[k]
  return undefined
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}
function failed(v: unknown): boolean {
  if (!isObj(v)) return false
  return v.ok === false || v.passed === false || v.pass === false || v.status === 'failed' || v.result === 'fail'
}
function skipped(v: unknown): boolean {
  return isObj(v) && (v.skipped === true || v.status === 'skipped')
}

const states = computed<State[]>(() => {
  const status = props.change.status
  if (status === 'active') return STAGES.map((s) => (skipped(gate(s)) ? 'skipped' : 'done'))
  if (status === 'rejected') {
    let at = STAGES.findIndex((s) => failed(gate(s)))
    if (at < 0) at = props.change.decided_by ? 3 : 0
    return STAGES.map((s, i) => (i < at ? (skipped(gate(s)) ? 'skipped' : 'done') : i === at ? 'failed' : 'pending'))
  }
  const cur = STATUS_STAGE[status]
  if (cur === undefined) {
    const last = STAGES.reduce((acc, s, i) => (gate(s) !== undefined ? i : acc), -1)
    return STAGES.map((_, i) => (i <= last ? 'done' : 'pending'))
  }
  return STAGES.map((s, i) =>
    i < cur ? (skipped(gate(s)) ? 'skipped' : 'done') : i === cur ? (status === 'held' ? 'held' : 'current') : 'pending',
  )
})

const DOT: Record<State, string> = {
  done: 'bg-good border-good',
  current: 'bg-accent border-accent animate-pulse',
  held: 'bg-warn border-warn',
  failed: 'bg-bad border-bad',
  skipped: 'bg-surface border-muted',
  pending: 'bg-surface border-line',
}
const LINE: Record<State, string> = {
  done: 'bg-good',
  skipped: 'bg-muted/50',
  current: 'bg-line',
  held: 'bg-line',
  failed: 'bg-line',
  pending: 'bg-line',
}

const details = computed(() => {
  const c = props.change
  const out: { label: string; text: string }[] = []
  STAGES.forEach((s) => {
    const g = gate(s)
    let text = g === undefined ? '' : summarize(g)
    if (s.key === 'shadow' && c.shadow_start) text = [`since ${dateTime(c.shadow_start, props.tz)}`, text].filter(Boolean).join(' · ')
    if (s.key === 'trial' && c.trial_start)
      text = [`${dateTime(c.trial_start, props.tz)} – ${dateTime(c.trial_end, props.tz)}`, text].filter(Boolean).join(' · ')
    if (text) out.push({ label: s.label, text })
  })
  return out
})
</script>

<template>
  <div>
    <ol class="flex" aria-label="Gates">
      <li v-for="(s, i) in STAGES" :key="s.key" class="relative min-w-0 flex-1 text-center">
        <span
          v-if="i > 0"
          class="absolute top-[6px] right-1/2 h-0.5 w-full"
          :class="LINE[states[i - 1]]"
          aria-hidden="true"
        />
        <span
          class="relative mx-auto block h-3.5 w-3.5 rounded-full border-2"
          :class="DOT[states[i]]"
          :aria-label="`${s.label}: ${states[i]}`"
        />
        <span
          class="mt-1 block truncate text-[10px] leading-tight"
          :class="states[i] === 'pending' ? 'text-muted' : states[i] === 'failed' ? 'text-bad font-medium' : 'text-ink'"
          >{{ s.label }}</span
        >
      </li>
    </ol>
    <dl v-if="details.length" class="mt-2 space-y-0.5 text-xs">
      <div v-for="d in details" :key="d.label" class="flex gap-1.5">
        <dt class="shrink-0 font-medium">{{ d.label }}:</dt>
        <dd class="min-w-0 text-muted break-words">{{ d.text }}</dd>
      </div>
    </dl>
  </div>
</template>
