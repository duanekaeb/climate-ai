<script setup lang="ts">
// One experiment: hypothesis, arms, schedule, checkpoints, analysis and the owner's actions.
import { computed } from 'vue'
import type { ExperimentDecisionBody, ExperimentDetail } from '@/api/types'
import Card from '@/components/Card.vue'
import { dateTime, dayRange, num } from '@/components/analysis/stats'
import DecisionForm from '@/components/control/DecisionForm.vue'
import { formatParam, labelFor } from '@/components/model/policyMeta'
import { useExperiments } from '@/stores/experiments'
import AnalysisPanel from './AnalysisPanel.vue'
import CheckpointsTable from './CheckpointsTable.vue'
import ScheduleCalendar from './ScheduleCalendar.vue'
import { STATUS_CHIP, STATUS_LABEL, armColor } from './arms'

const props = defineProps<{ detail: ExperimentDetail; isOwner: boolean; today: string; tz?: string }>()
const store = useExperiments()

const e = computed(() => props.detail.experiment)
type Decision = ExperimentDecisionBody['decision']
const options = computed(() => {
  const s = e.value.status
  const out: { value: Decision; label: string; tone: 'primary' | 'danger' | 'plain' }[] = []
  if (s === 'proposed') {
    out.push({ value: 'approve', label: 'Approve', tone: 'primary' })
    out.push({ value: 'reject', label: 'Reject', tone: 'plain' })
  }
  if (s === 'approved' || s === 'running') out.push({ value: 'stop', label: 'Stop the test', tone: 'danger' })
  return out
})

function decide(decision: Decision, reason: string) {
  return store.decide(e.value.id, { decision, reason })
}
</script>

<template>
  <div class="space-y-4">
    <Card>
      <div class="flex flex-wrap items-start justify-between gap-2">
        <h2 class="min-w-0 text-lg font-semibold break-words">{{ e.name }}</h2>
        <span class="chip" :class="STATUS_CHIP[e.status]">{{ STATUS_LABEL[e.status] }}</span>
      </div>
      <p class="mt-1 text-xs text-muted">
        Proposed by {{ e.proposed_by }} · {{ dateTime(e.created_at, tz) }}
        <template v-if="e.start_date"> · runs {{ dayRange(e.start_date, e.end_date) }}</template>
      </p>
      <p class="mt-3 text-sm break-words">{{ e.hypothesis }}</p>
      <dl class="num mt-3 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
        <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">Length</dt><dd>{{ e.n_days }} days</dd></div>
        <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">Switch every</dt><dd>{{ e.block_days }} {{ e.block_days === 1 ? 'day' : 'days' }}</dd></div>
        <div class="rounded-xl bg-surface-2 p-2">
          <dt class="text-[11px] text-muted">Alpha (two-sided)</dt>
          <dd>{{ num(e.alpha, 2) }}</dd>
          <dd class="text-[11px] leading-tight text-muted">false win ≤ {{ Number((e.alpha * 50).toFixed(1)) }}%</dd>
        </div>
        <div class="rounded-xl bg-surface-2 p-2"><dt class="text-[11px] text-muted">Measure</dt><dd class="truncate" :title="e.metric">{{ e.metric.replace(/_/g, ' ') }}</dd></div>
      </dl>

      <h3 class="mt-4 mb-2 text-xs font-semibold text-muted uppercase">Arms</h3>
      <ul class="space-y-2">
        <li v-for="(a, i) in e.arms" :key="a.key" class="rounded-xl border border-line p-2.5">
          <div class="flex items-center gap-2">
            <span class="h-3 w-3 shrink-0 rounded" :style="{ background: armColor(i) }" />
            <span class="font-medium break-words">{{ a.label }}</span>
            <span class="text-xs text-muted">({{ a.key }})</span>
          </div>
          <ul v-if="Object.keys(a.params).length" class="mt-1 flex flex-wrap gap-1">
            <li v-for="(v, k) in a.params" :key="k" class="chip bg-surface-2 text-ink">
              {{ labelFor(String(k)) }}: <span class="num">{{ formatParam(String(k), v) }}</span>
            </li>
          </ul>
          <p v-else class="mt-1 text-xs text-muted">The current policy, unchanged.</p>
        </li>
      </ul>
    </Card>

    <Card title="Analysis">
      <AnalysisPanel :analysis="detail.analysis" :arms="e.arms" :checkpoints="e.checkpoints" :n-days="e.n_days" />
    </Card>

    <Card title="Schedule" subtitle="Assigned at random in blocks before the start. The first hours after a switch are dropped.">
      <ScheduleCalendar v-if="detail.schedule.length" :schedule="detail.schedule" :arms="e.arms" :today="today" />
      <p v-else class="text-sm text-muted">The day-by-day schedule is drawn when the experiment is approved.</p>
    </Card>

    <Card v-if="e.checkpoints.length" title="Checkpoints">
      <CheckpointsTable :checkpoints="e.checkpoints" :days-observed="detail.analysis.days_observed" :alpha="e.alpha" />
    </Card>

    <Card v-if="isOwner && options.length" title="Your decision">
      <p v-if="e.status === 'proposed'" class="mb-2 text-sm text-muted">
        Approving starts the test tomorrow. The controller then follows each day's arm, always inside your hard limits.
      </p>
      <p v-else class="mb-2 text-sm text-muted">
        Stopping ends the test now. Stop for comfort or safety, not because of an early interval.
      </p>
      <DecisionForm :options="options" :submit="decide" />
    </Card>
  </div>
</template>
