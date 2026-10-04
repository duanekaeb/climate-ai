<script setup lang="ts">
// Model: baseline and RC house-model fits, what the data can't pin down, room offsets,
// refit on demand, backtests and day simulations.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { dateTime, duration, errorText } from '@/components/analysis/stats'
import BacktestCard from '@/components/model/BacktestCard.vue'
import FitsCard from '@/components/model/FitsCard.vue'
import RoomOffsetsCard from '@/components/model/RoomOffsetsCard.vue'
import SimulateCard from '@/components/model/SimulateCard.vue'
import { useControl } from '@/stores/control'
import { useModels } from '@/stores/models'
import { useStatus } from '@/stores/status'

const models = useModels()
const control = useControl()
const status = useStatus()
const tz = computed(() => status.data?.tz)
const current = computed(() => control.settings.data?.policy ?? null)

const fits = computed(() => models.fits.data ?? [])
const offsets = computed(() =>
  fits.value
    .filter((f) => f.kind === 'room_offsets')
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] ?? null,
)

const refitError = ref('')
const job = computed(() => models.job)
const jobRunning = computed(() => job.value !== null && (job.value.status === 'queued' || job.value.status === 'running'))

async function refit() {
  refitError.value = ''
  try {
    await models.refit()
  } catch (e) {
    refitError.value = errorText(e)
  }
}

onMounted(() => {
  // The house timezone and room names come from the status store; start it if the shell hasn't.
  status.start()
  void models.loadFits()
  void control.ensureSettings()
})
onBeforeUnmount(() => models.stopPolling())
</script>

<template>
  <div class="space-y-4">
    <p class="text-sm text-muted">
      The models learn from your house every night. The RC house model runs in shadow and only drives plans once it beats
      the linked-floors rule in backtests; until then the simple rule stays in charge.
    </p>

    <Card title="Fits" subtitle="Latest per kind, unit and mode; refit nightly">
      <template #actions>
        <button type="button" class="btn !py-1.5" :disabled="jobRunning" @click="refit">
          <Icon name="refresh" :size="14" />{{ jobRunning ? 'Refitting…' : 'Refit now' }}
        </button>
      </template>
      <div v-if="job || refitError || models.jobError" class="mb-3 rounded-xl bg-surface-2 p-3 text-sm" aria-live="polite">
        <p v-if="refitError" class="text-bad">{{ refitError }}</p>
        <template v-else-if="job">
          <p>
            Refit job #{{ job.id }}:
            <span
              class="font-medium"
              :class="{ 'text-good': job.status === 'done', 'text-bad': job.status === 'failed', 'text-accent': jobRunning }"
              >{{ job.status }}</span
            >
            <span class="text-muted">
              · queued {{ dateTime(job.created_at, tz) }}<template v-if="job.finished_at">
                · took {{ duration(job.created_at, job.finished_at) }}</template
              >
            </span>
          </p>
          <p v-if="job.error" class="mt-1 text-bad break-words">{{ job.error }}</p>
          <p v-if="jobRunning" class="mt-1 text-xs text-muted">The worker refits baselines, room offsets and the house model; this page updates when it finishes.</p>
        </template>
        <p v-if="models.jobError" class="mt-1 text-xs text-warn">{{ models.jobError }}</p>
      </div>
      <AsyncState
        :loading="models.fits.loading && !models.fits.data"
        :error="models.fits.error"
        :empty="!!models.fits.data && !fits.length"
        empty-text="No fits yet. They need a few weeks of runtime; Refit now runs them early."
      >
        <FitsCard :fits="fits" :tz="tz" />
      </AsyncState>
    </Card>

    <Card title="Room offsets" subtitle="How each sensored room runs vs its thermostat">
      <AsyncState
        :loading="models.fits.loading && !models.fits.data"
        :error="models.fits.error"
        :empty="!!models.fits.data && !offsets"
        empty-text="No room offsets learned yet (they need about two weeks of sensor data)."
      >
        <RoomOffsetsCard v-if="offsets" :fit="offsets" :rooms="status.data?.rooms ?? []" :tz="tz" />
      </AsyncState>
    </Card>

    <div class="grid gap-4 lg:grid-cols-2">
      <BacktestCard :current="current" />
      <SimulateCard :current="current" :tz="tz" />
    </div>
    <p v-if="control.settings.error" class="text-xs text-warn">
      Couldn't load the current policy ({{ control.settings.error }}); overrides show no current values.
    </p>
  </div>
</template>
