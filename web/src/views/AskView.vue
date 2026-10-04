<script setup lang="ts">
// Ask Claude: sign-in status, questions, recent runs (live via the 'agent_run' websocket
// event, polled every 5 s while one is queued or running) and on-demand nightly / weekly runs.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { AgentRunOut, RunBody } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import AgentStatusCard from '@/components/ask/AgentStatusCard.vue'
import AskBox from '@/components/ask/AskBox.vue'
import RunItem from '@/components/ask/RunItem.vue'
import { errorText } from '@/components/analysis/stats'
import { isActive, useAgent } from '@/stores/agent'
import { useAuth } from '@/stores/auth'
import { useStatus } from '@/stores/status'

const agent = useAgent()
const auth = useAuth()
const status = useStatus()
const isOwner = computed(() => auth.state?.role === 'owner')
const tz = computed(() => status.data?.tz)

const runs = computed(() => agent.runs.data ?? [])
const lastAsked = ref<number | null>(null)
const runError = ref('')
const starting = ref<RunBody['kind'] | null>(null)

function pending(kind: RunBody['kind']): boolean {
  return runs.value.some((r) => r.kind === kind && isActive(r))
}

async function runNow(kind: RunBody['kind']) {
  starting.value = kind
  runError.value = ''
  try {
    const r = await agent.runNow(kind)
    lastAsked.value = r.id
  } catch (e) {
    runError.value = errorText(e)
  } finally {
    starting.value = null
  }
}

function onAsked(run: AgentRunOut) {
  lastAsked.value = run.id
}

let stop: (() => void) | null = null
onMounted(() => {
  stop = agent.follow()
})
onBeforeUnmount(() => stop?.())
</script>

<template>
  <div class="space-y-4">
    <p class="text-sm text-muted">
      Claude runs on your Claude subscription a few times a day: a nightly review, a weekly report, up to three checks
      after something unusual, and your questions. It never controls the thermostats; it can only propose changes, or
      sign off ones the models queued, inside your limits. The controller keeps the house running without it.
    </p>

    <AsyncState :loading="agent.info.loading && !agent.info.data" :error="agent.info.error">
      <AgentStatusCard v-if="agent.info.data" :info="agent.info.data" :tz="tz" />
    </AsyncState>

    <Card title="Ask about your house" subtitle="Answers use the house's own data through read-only tools">
      <AskBox :disabled="!isOwner" @asked="onAsked" />
      <p v-if="!isOwner" class="mt-2 text-xs text-muted">Only the owner can ask questions.</p>
    </Card>

    <Card title="Runs">
      <template #actions>
        <button
          type="button"
          class="btn !px-2 !py-1"
          aria-label="Reload runs"
          :disabled="agent.runs.loading"
          @click="agent.loadRuns()"
        >
          <Icon name="refresh" :size="14" />
        </button>
      </template>
      <div v-if="isOwner" class="mb-3 flex flex-wrap gap-2">
        <button
          type="button"
          class="btn"
          :disabled="starting !== null || pending('nightly')"
          @click="runNow('nightly')"
        >
          {{ starting === 'nightly' ? 'Queuing…' : pending('nightly') ? 'Nightly queued' : 'Run nightly now' }}
        </button>
        <button
          type="button"
          class="btn"
          :disabled="starting !== null || pending('weekly')"
          @click="runNow('weekly')"
        >
          {{ starting === 'weekly' ? 'Queuing…' : pending('weekly') ? 'Weekly queued' : 'Run weekly now' }}
        </button>
      </div>
      <p v-if="runError" class="mb-2 text-sm text-bad">{{ runError }}</p>
      <AsyncState
        :loading="agent.runs.loading && !agent.runs.data"
        :error="agent.runs.error"
        :empty="!!agent.runs.data && !runs.length"
        empty-text="No runs yet. The first nightly review runs after the agent service signs in."
      >
        <div class="space-y-2">
          <RunItem
            v-for="(r, i) in runs"
            :key="r.id"
            :run="r"
            :tz="tz"
            :open="r.id === lastAsked || (lastAsked === null && i === 0)"
          />
        </div>
      </AsyncState>
    </Card>
  </div>
</template>
