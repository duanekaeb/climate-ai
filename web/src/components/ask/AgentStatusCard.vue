<script setup lang="ts">
// Claude's sign-in and schedule, from the agent service's heartbeat.
import { computed } from 'vue'
import type { AgentInfo } from '@/api/types'
import Card from '@/components/Card.vue'
import { WEEKDAYS, ago, dateTime, dayLabel, daysBetween, todayIn } from '@/components/analysis/stats'

const props = defineProps<{ info: AgentInfo; tz?: string }>()

const HEARTBEAT_STALE_MS = 15 * 60 * 1000

const signIn = computed(() => {
  if (props.info.signed_in === true) return { label: 'signed in', chip: 'bg-good/15 text-good' }
  if (props.info.signed_in === false) return { label: 'signed out', chip: 'bg-bad/15 text-bad' }
  return { label: 'unknown', chip: 'bg-surface-2 text-muted' }
})
const stale = computed(
  () => !props.info.last_beat_at || Date.now() - new Date(props.info.last_beat_at).getTime() > HEARTBEAT_STALE_MS,
)
const daysLeft = computed(() =>
  props.info.token_expires_at ? daysBetween(todayIn(props.tz), props.info.token_expires_at) : null,
)
const weekly = computed(() => `${WEEKDAYS[props.info.settings.weekly_day] ?? '—'} ${props.info.settings.weekly_time}`)
const cap = computed(() => props.info.settings.max_triggered_per_day)
</script>

<template>
  <Card title="Claude">
    <template #actions>
      <span class="chip" :class="signIn.chip">{{ signIn.label }}</span>
    </template>
    <div
      v-if="info.token_warning"
      class="mb-3 rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm"
      role="alert"
    >
      {{ info.token_warning }}
    </div>
    <p v-if="!info.enabled" class="mb-3 rounded-xl bg-surface-2 p-3 text-sm text-muted">
      Claude's scheduled runs are turned off. Questions still queue, but nothing runs on its own.
    </p>
    <dl class="num grid grid-cols-2 gap-x-3 gap-y-2 text-sm sm:grid-cols-3">
      <div>
        <dt class="text-[11px] text-muted uppercase">Agent service</dt>
        <dd :class="stale && 'text-warn'">
          {{ info.last_beat_at ? `seen ${ago(info.last_beat_at)}` : 'never seen' }}
        </dd>
      </div>
      <div>
        <dt class="text-[11px] text-muted uppercase">SDK</dt>
        <dd class="break-all">{{ info.sdk_version ?? '—' }}</dd>
      </div>
      <div>
        <dt class="text-[11px] text-muted uppercase">Sign-in token</dt>
        <dd :class="daysLeft !== null && daysLeft <= 30 && 'text-warn'">
          <template v-if="info.token_expires_at">
            expires {{ dayLabel(info.token_expires_at) }}
            <span class="text-xs text-muted">({{ daysLeft !== null && daysLeft >= 0 ? `${daysLeft} d` : 'expired' }})</span>
          </template>
          <template v-else>—</template>
        </dd>
      </div>
      <div>
        <dt class="text-[11px] text-muted uppercase">Triggered today</dt>
        <dd :class="info.triggered_today >= cap && 'text-warn'">{{ info.triggered_today }} of {{ cap }}</dd>
      </div>
      <div>
        <dt class="text-[11px] text-muted uppercase">Next nightly</dt>
        <dd>{{ dateTime(info.next_nightly_at, tz) }}</dd>
      </div>
      <div>
        <dt class="text-[11px] text-muted uppercase">Weekly</dt>
        <dd>{{ weekly }}</dd>
      </div>
      <div class="col-span-2 sm:col-span-3">
        <dt class="text-[11px] text-muted uppercase">Last run</dt>
        <dd v-if="info.last_run">
          {{ info.last_run.kind }} · {{ info.last_run.status }} ·
          {{ dateTime(info.last_run.finished_at ?? info.last_run.started_at ?? info.last_run.created_at, tz) }}
        </dd>
        <dd v-else>none yet</dd>
      </div>
    </dl>
    <p v-if="stale" class="mt-3 text-xs text-warn">
      The agent service hasn't reported in the last 15 minutes. Queued questions wait until it is back; the house keeps
      running without it.
    </p>
  </Card>
</template>
