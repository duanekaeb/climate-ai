<script setup lang="ts">
// One proposed change: what it changes (vs the current policy, with Claude's sign-off range),
// where it is in the gates, who decides next, and the owner's approve / hold / reject.
import { computed } from 'vue'
import type { ChangeOut, DecisionBody, SettingsOut } from '@/api/types'
import { dateTime, summarize } from '@/components/analysis/stats'
import { formatParam, isPolicyKey, labelFor } from '@/components/model/policyMeta'
import { useControl } from '@/stores/control'
import DecisionForm from './DecisionForm.vue'
import GateTimeline from './GateTimeline.vue'

const props = defineProps<{ change: ChangeOut; settings: SettingsOut | null; isOwner: boolean; pending: boolean; tz?: string }>()
const control = useControl()

const NEEDS: Record<ChangeOut['needs'], { label: string; chip: string }> = {
  owner: { label: 'waiting on you', chip: 'bg-warn/15 text-warn' },
  claude: { label: "waiting on Claude's sign-off", chip: 'bg-accent/15 text-accent' },
  nothing: { label: 'gates running', chip: 'bg-surface-2 text-muted' },
}
const STATUS_CHIP: Record<string, string> = {
  active: 'bg-good/15 text-good',
  trial: 'bg-accent/15 text-accent',
  rejected: 'bg-bad/15 text-bad',
  held: 'bg-warn/15 text-warn',
}

/** For policy changes the payload is {params: {...}} or the params themselves. */
const params = computed<Record<string, unknown> | null>(() => {
  const p = props.change.payload
  const inner = p.params
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) return inner as Record<string, unknown>
  return props.change.kind === 'policy' ? p : null
})

function signoff(key: string, value: unknown): { text: string; inside: boolean | null } {
  const s = props.settings
  if (!s) return { text: '', inside: null }
  if (s.owner_only_params.includes(key)) return { text: 'owner only', inside: false }
  const r = s.signoff_ranges[key]
  if (!r || typeof r[0] !== 'number' || typeof r[1] !== 'number' || typeof value !== 'number')
    return { text: '', inside: null }
  const inside = value >= r[0] && value <= r[1]
  return { text: `Claude may sign off ${formatParam(key, r[0])}–${formatParam(key, r[1])}`, inside }
}

const rows = computed(() =>
  Object.entries(params.value ?? {}).map(([k, v]) => ({
    key: k,
    label: labelFor(k),
    from: isPolicyKey(k) && props.settings ? formatParam(k, props.settings.policy[k]) : '—',
    to: formatParam(k, v),
    signoff: signoff(k, v),
  })),
)

type Decision = DecisionBody['decision']
const options = computed<{ value: Decision; label: string; tone: 'primary' | 'danger' | 'plain' }[]>(() => [
  { value: 'approve', label: 'Approve', tone: 'primary' },
  { value: 'hold', label: 'Hold', tone: 'plain' },
  { value: 'reject', label: 'Reject', tone: 'danger' },
])

function decide(decision: Decision, reason: string) {
  return control.decideChange(props.change.id, { decision, reason })
}
</script>

<template>
  <article class="rounded-xl border border-line p-3">
    <header class="flex flex-wrap items-start justify-between gap-2">
      <h3 class="min-w-0 font-medium break-words">{{ change.title }}</h3>
      <span class="flex flex-wrap gap-1">
        <span class="chip bg-surface-2 text-muted">{{ change.kind }}</span>
        <span class="chip" :class="STATUS_CHIP[change.status] ?? 'bg-surface-2 text-ink'">{{ change.status.replace(/_/g, ' ') }}</span>
      </span>
    </header>
    <p class="mt-0.5 text-xs text-muted">
      Proposed by {{ change.proposed_by }} · {{ dateTime(change.created_at, tz) }}
    </p>
    <p v-if="pending" class="mt-2">
      <span class="chip" :class="NEEDS[change.needs].chip">{{ NEEDS[change.needs].label }}</span>
    </p>

    <p v-if="change.rationale" class="mt-2 text-sm whitespace-pre-line break-words">{{ change.rationale }}</p>

    <ul v-if="rows.length" class="mt-2 space-y-1">
      <li v-for="r in rows" :key="r.key" class="rounded-lg bg-surface-2 px-2 py-1.5 text-sm">
        <div class="flex flex-wrap items-baseline justify-between gap-x-2">
          <span class="min-w-0">{{ r.label }}</span>
          <span class="num whitespace-nowrap">{{ r.from }} → <span class="font-semibold">{{ r.to }}</span></span>
        </div>
        <p
          v-if="r.signoff.text"
          class="text-[11px]"
          :class="r.signoff.inside === false ? 'text-warn' : 'text-muted'"
        >
          {{ r.signoff.text }}{{ r.signoff.inside === false && r.signoff.text !== 'owner only' ? ' (outside: you decide)' : '' }}
        </p>
      </li>
    </ul>
    <p v-else-if="Object.keys(change.payload).length" class="mt-2 text-xs text-muted break-words">
      {{ summarize(change.payload, 6) }}
    </p>

    <div class="mt-3">
      <GateTimeline :change="change" :tz="tz" />
    </div>

    <p v-if="change.decided_by" class="mt-2 text-xs">
      <span class="font-medium">{{ change.decided_by }}</span>
      <span class="text-muted"> decided {{ dateTime(change.decided_at, tz) }}</span>
      <template v-if="change.decision_reason">: <span class="break-words">“{{ change.decision_reason }}”</span></template>
    </p>

    <div v-if="pending && isOwner" class="mt-3 border-t border-line pt-3">
      <p v-if="change.needs === 'claude'" class="mb-2 text-xs text-muted">
        Claude reviews this in its nightly run. Deciding now overrides that.
      </p>
      <p v-else-if="change.needs === 'nothing'" class="mb-2 text-xs text-muted">
        The gates are still running. Approving now skips the remaining gates, and that is recorded.
      </p>
      <DecisionForm :options="options" :submit="decide" />
    </div>
  </article>
</template>
