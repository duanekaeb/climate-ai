<script setup lang="ts">
// How many test days a given effect needs, from the baselines' day-to-day noise.
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import { num } from '@/components/analysis/stats'
import { useExperiments } from '@/stores/experiments'

const store = useExperiments()
const uid = useId()
const effect = ref(10)
const alpha = ref(0.1)
const power = ref(0.8)
let timer: number | undefined

watch(
  [effect, alpha, power],
  ([e, a, p]) => {
    window.clearTimeout(timer)
    timer = window.setTimeout(() => void store.loadPower(e, a, p), 300)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.clearTimeout(timer))

const out = computed(() => store.power.data)
const weeks = computed(() => (out.value?.total_days == null ? null : out.value.total_days / 7))
</script>

<template>
  <Card title="Power calculator" subtitle="How long would a test need to see this change?">
    <div class="space-y-3">
      <div>
        <div class="flex items-baseline justify-between">
          <label :for="`${uid}-effect`" class="text-sm">Change in total-house runtime</label>
          <span class="num text-sm font-semibold">{{ effect }}%</span>
        </div>
        <input
          :id="`${uid}-effect`"
          v-model.number="effect"
          type="range"
          min="2"
          max="30"
          step="1"
          class="mt-1 w-full accent-[var(--color-accent)]"
        />
      </div>
      <div class="grid grid-cols-2 gap-2">
        <label class="text-xs text-muted">
          False-win rate
          <select v-model.number="alpha" class="input num mt-1">
            <option :value="0.05">5%</option>
            <option :value="0.1">10%</option>
            <option :value="0.2">20%</option>
          </select>
        </label>
        <label class="text-xs text-muted">
          Chance to detect
          <select v-model.number="power" class="input num mt-1">
            <option :value="0.8">80%</option>
            <option :value="0.9">90%</option>
          </select>
        </label>
      </div>
      <AsyncState :loading="store.power.loading && !out" :error="store.power.error">
        <div v-if="out" class="space-y-1" aria-live="polite">
          <template v-if="out.days_per_arm !== null">
            <p class="text-sm">
              About <span class="num text-lg font-semibold">{{ out.days_per_arm }}</span> days per arm,
              <span class="num font-medium">{{ out.total_days ?? '—' }}</span> test days in all
              <span v-if="weeks !== null" class="num text-muted">(~{{ num(weeks, 0) }} weeks)</span>.
            </p>
            <p v-if="out.resid_cv !== null" class="text-xs text-muted">
              From day-to-day noise of {{ num(out.resid_cv * 100, 0, '%') }} after the weather is taken out.
            </p>
          </template>
          <p v-else class="text-sm text-muted">Can't size a test yet.</p>
          <p v-if="out.note" class="text-xs text-muted">{{ out.note }}</p>
        </div>
      </AsyncState>
    </div>
  </Card>
</template>
