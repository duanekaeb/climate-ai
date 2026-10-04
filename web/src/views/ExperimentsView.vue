<script setup lang="ts">
// Experiments: randomized switchbacks on total-house runtime. List, detail (schedule,
// checkpoints, analysis), the owner's approve / reject / stop, proposals and test sizing.
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onEvent } from '@/api/ws'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { dayRange, todayIn } from '@/components/analysis/stats'
import ExperimentDetailCard from '@/components/experiments/ExperimentDetailCard.vue'
import PowerCalculator from '@/components/experiments/PowerCalculator.vue'
import ProposeExperimentForm from '@/components/experiments/ProposeExperimentForm.vue'
import { STATUS_CHIP } from '@/components/experiments/arms'
import { useAuth } from '@/stores/auth'
import { useControl } from '@/stores/control'
import { useExperiments } from '@/stores/experiments'
import { useStatus } from '@/stores/status'

const route = useRoute()
const router = useRouter()
const store = useExperiments()
const control = useControl()
const status = useStatus()
const auth = useAuth()

const isOwner = computed(() => auth.state?.role === 'owner')
const tz = computed(() => status.data?.tz)
const today = computed(() => todayIn(tz.value))

const selectedId = computed(() => {
  const v = Number(route.query.id)
  return Number.isInteger(v) && v > 0 ? v : null
})
const proposing = computed(() => route.query.new === '1')
const focused = computed(() => selectedId.value !== null || proposing.value)

const ORDER: Record<string, number> = { running: 0, proposed: 1, approved: 2, completed: 3, stopped: 4, rejected: 5 }
const experiments = computed(() =>
  [...(store.list.data ?? [])].sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || b.id - a.id),
)
const detail = computed(() =>
  store.detail.data && store.detail.data.experiment.id === selectedId.value ? store.detail.data : null,
)

function open(id: number) {
  void router.replace({ query: { id: String(id) } })
}
function propose() {
  void router.replace({ query: { new: '1' } })
}
function back() {
  void router.replace({ query: {} })
}

watch(
  selectedId,
  (id) => {
    if (id !== null) void store.loadDetail(id)
  },
  { immediate: true },
)

let unsubscribe: (() => void) | null = null
onMounted(() => {
  // The house timezone and room names come from the status store; start it if the shell hasn't.
  status.start()
  void store.loadList()
  void control.ensureSettings()
  unsubscribe = onEvent((e) => {
    if (e.type !== 'change') return
    void store.loadList()
    if (selectedId.value !== null) void store.loadDetail(selectedId.value)
  })
})
onBeforeUnmount(() => unsubscribe?.())
</script>

<template>
  <div class="grid gap-4 md:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
    <!-- list + sizing (hidden on phones while looking at one experiment) -->
    <div class="space-y-4" :class="focused && 'hidden md:block'">
      <Card title="Experiments">
        <template #actions>
          <button type="button" class="btn btn-primary !py-1.5" @click="propose">+ Propose</button>
        </template>
        <AsyncState
          :loading="store.list.loading && !store.list.data"
          :error="store.list.error"
          :empty="!!store.list.data && !experiments.length"
          empty-text="No experiments yet. Propose one, or wait for Claude's weekly review to suggest one."
        >
          <ul class="-mx-1 space-y-1">
            <li v-for="e in experiments" :key="e.id">
              <button
                type="button"
                class="w-full rounded-xl px-2 py-2 text-left hover:bg-surface-2"
                :class="selectedId === e.id && 'bg-surface-2'"
                :aria-current="selectedId === e.id ? 'true' : undefined"
                @click="open(e.id)"
              >
                <div class="flex items-start justify-between gap-2">
                  <span class="min-w-0 text-sm font-medium break-words">{{ e.name }}</span>
                  <span class="chip shrink-0" :class="STATUS_CHIP[e.status]">{{ e.status }}</span>
                </div>
                <p class="mt-0.5 text-xs text-muted">
                  {{ e.arms.length }} arms · {{ e.n_days }} days
                  <template v-if="e.start_date"> · {{ dayRange(e.start_date, e.end_date) }}</template>
                  · by {{ e.proposed_by }}
                </p>
              </button>
            </li>
          </ul>
        </AsyncState>
      </Card>
      <PowerCalculator />
    </div>

    <!-- detail / proposal -->
    <div class="min-w-0 space-y-4" :class="!focused && 'hidden md:block'">
      <button v-if="focused" type="button" class="btn !py-1.5 md:hidden" @click="back">
        <span aria-hidden="true">‹</span> All experiments
      </button>

      <Card v-if="proposing" title="Propose an experiment">
        <ProposeExperimentForm :current="control.settings.data?.policy ?? null" @created="open" @cancel="back" />
      </Card>

      <template v-else-if="selectedId !== null">
        <AsyncState :loading="store.detail.loading && !detail" :error="store.detail.error">
          <ExperimentDetailCard v-if="detail" :detail="detail" :is-owner="isOwner" :today="today" :tz="tz" />
        </AsyncState>
      </template>

      <Card v-else>
        <div class="flex items-start gap-3 text-sm text-muted">
          <Icon name="experiments" :size="22" class="shrink-0" />
          <p>
            Pick an experiment to see its schedule, checkpoints and analysis. Each test randomizes days between options,
            measures weather-normalized total-house runtime, and decides only at checkpoints fixed in advance.
          </p>
        </div>
      </Card>
    </div>
  </div>
</template>
