<script setup lang="ts">
// One agent run: what was asked or why it ran, its status, and Claude's answer (markdown).
import { computed, ref, watch } from 'vue'
import type { AgentRunOut } from '@/api/types'
import Markdown from '@/components/Markdown.vue'
import { dateTime, duration } from '@/components/analysis/stats'

const props = defineProps<{ run: AgentRunOut; tz?: string; open?: boolean }>()

const expanded = ref(props.open ?? false)
watch(
  () => props.open,
  (v) => {
    if (v) expanded.value = true
  },
)

const STATUS: Record<AgentRunOut['status'], string> = {
  queued: 'bg-surface-2 text-muted',
  running: 'bg-accent/15 text-accent animate-pulse',
  completed: 'bg-good/15 text-good',
  failed: 'bg-bad/15 text-bad',
  deferred: 'bg-warn/15 text-warn',
  cancelled: 'bg-surface-2 text-muted',
}
const KIND: Record<AgentRunOut['kind'], string> = {
  nightly: 'Nightly review',
  weekly: 'Weekly report',
  triggered: 'Triggered check',
  chat: 'Question',
  signin_check: 'Sign-in check',
}

const triggerText = computed(() => {
  const t = props.run.trigger
  if (!t) return ''
  const reason = t.reason ?? t.kind ?? t.type
  return typeof reason === 'string' ? reason : ''
})
const finishedBadly = computed(
  () => props.run.status === 'completed' && props.run.terminal_reason && props.run.terminal_reason !== 'completed',
)
</script>

<template>
  <article class="rounded-xl border border-line p-3">
    <header class="flex flex-wrap items-center gap-2">
      <span class="text-sm font-medium">{{ KIND[run.kind] }}</span>
      <span class="chip" :class="STATUS[run.status]">{{ run.status }}</span>
      <span class="ml-auto text-xs text-muted">{{ dateTime(run.created_at, tz) }}</span>
    </header>

    <p v-if="run.kind === 'chat' && run.prompt" class="mt-2 border-l-2 border-line pl-2 text-sm break-words">
      {{ run.prompt }}
    </p>
    <p v-else-if="triggerText" class="mt-1 text-xs text-muted break-words">Why: {{ triggerText }}</p>

    <p class="num mt-1 text-xs text-muted">
      by {{ run.requested_by }}
      <template v-if="run.started_at"> · started {{ dateTime(run.started_at, tz) }}</template>
      <template v-if="run.finished_at"> · took {{ duration(run.started_at ?? run.created_at, run.finished_at) }}</template>
      <template v-if="run.model"> · {{ run.model }}</template>
      <template v-if="run.num_turns !== null"> · {{ run.num_turns }} turns</template>
    </p>

    <p v-if="run.status === 'queued'" class="mt-2 text-xs text-muted">Waiting for the agent service to pick it up.</p>
    <p v-if="run.status === 'deferred'" class="mt-2 text-xs text-warn">
      Deferred (usage limit reached)<template v-if="run.not_before">; retries after {{ dateTime(run.not_before, tz) }}</template>.
    </p>
    <p v-if="run.error" class="mt-2 rounded-lg bg-bad/10 p-2 text-xs text-bad break-words">{{ run.error }}</p>
    <p v-if="finishedBadly" class="mt-2 text-xs text-warn">
      Ended early ({{ run.terminal_reason }}); the answer may be incomplete.
    </p>

    <div v-if="run.result_text" class="mt-2">
      <button type="button" class="text-xs font-medium text-accent" :aria-expanded="expanded" @click="expanded = !expanded">
        {{ expanded ? 'Hide answer' : 'Show answer' }}
      </button>
      <Markdown v-if="expanded" :source="run.result_text" class="mt-1 break-words" />
    </div>
  </article>
</template>
