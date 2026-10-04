<script setup lang="ts">
// Kind, author and period chips shared by the report list and the report page.
import { computed } from 'vue'
import type { ReportOut } from '@/api/types'
import { dayLabel } from '@/components/runtime/chartKit'
import { KIND_LABELS } from '@/stores/reports'

const props = defineProps<{ report: ReportOut }>()

const KIND_CLS: Record<ReportOut['kind'], string> = {
  daily: 'bg-surface-2 text-muted',
  nightly: 'bg-st-asleep/15 text-st-asleep',
  weekly: 'bg-good/15 text-good',
  anomaly: 'bg-warn/15 text-warn',
  note: 'bg-surface-2 text-muted',
}

const period = computed(() => {
  const { period_start: a, period_end: b } = props.report
  if (a && b && a !== b) return `${dayLabel(a)} – ${dayLabel(b)}`
  if (a || b) return dayLabel((a ?? b) as string)
  return ''
})
</script>

<template>
  <span class="flex flex-wrap items-center gap-1.5 text-xs">
    <span class="chip" :class="KIND_CLS[report.kind]">{{ KIND_LABELS[report.kind] }}</span>
    <span v-if="report.author === 'claude'" class="chip bg-accent/15 text-accent">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l2.4 7.6L22 12l-7.6 2.4L12 22l-2.4-7.6L2 12l7.6-2.4z" /></svg>
      Claude
    </span>
    <span v-else class="chip border border-line text-muted">System</span>
    <span v-if="period" class="num text-muted">{{ period }}</span>
  </span>
</template>
