<script setup lang="ts">
// The latest fit per kind / unit / mode: weather baselines and the RC house model, which runs
// in shadow until it beats persistence and the linked-floors rule.
import { computed } from 'vue'
import type { ModelFitOut } from '@/api/types'
import { UNIT_NAMES } from '@/lib/format'
import { dateTime, dayRange, num, numberAt } from '@/components/analysis/stats'
import { fitChip, plainParam, rcScores } from './fitText'

const props = defineProps<{ fits: ModelFitOut[]; tz?: string }>()

const order = Object.keys(UNIT_NAMES)
const byUnit = (a: ModelFitOut, b: ModelFitOut) =>
  order.indexOf(a.unit_key ?? '') - order.indexOf(b.unit_key ?? '') || (a.mode ?? '').localeCompare(b.mode ?? '')
const baselines = computed(() => props.fits.filter((f) => f.kind === 'baseline').sort(byUnit))
const rc = computed(() =>
  props.fits
    .filter((f) => f.kind === 'rc')
    .sort(byUnit)
    .map((f) => {
      const s = rcScores(f)
      return {
        fit: f,
        unidentified: s.unidentified,
        horizons: [
          { label: '1 h ahead', model: s.rmse1h, persistence: s.persist1h },
          { label: '24 h ahead', model: s.rmse24h, persistence: s.persist24h },
        ],
      }
    }),
)

function cvText(f: ModelFitOut): string {
  const cv = numberAt(f.metrics, 'cvrmse')
  return cv === null ? '—' : num(cv * 100, 1, '%')
}

function unitName(f: ModelFitOut): string {
  return f.unit_key ? (UNIT_NAMES[f.unit_key] ?? f.unit_key) : 'Whole house'
}

/** "beats" when the model's error is below persistence ("tomorrow = today"). */
function beats(model: number | null, persistence: number | null): boolean | null {
  return model === null || persistence === null ? null : model < persistence
}
</script>

<template>
  <div class="space-y-5">
    <section>
      <h3 class="mb-2 text-xs font-semibold text-muted uppercase">Weather baselines</h3>
      <p v-if="!baselines.length" class="text-sm text-muted">No baseline fits yet (they need 21+ days in a mode).</p>
      <ul v-else class="grid gap-2 sm:grid-cols-2">
        <li v-for="f in baselines" :key="f.id" class="rounded-xl border border-line p-3">
          <div class="flex items-center justify-between gap-2">
            <span class="inline-flex min-w-0 items-center gap-1.5 text-sm font-medium">
              <span class="h-2.5 w-2.5 shrink-0 rounded-full" :style="{ background: `var(--color-unit-${f.unit_key})` }" />
              <span class="truncate">{{ unitName(f) }} · {{ f.mode ?? '—' }}</span>
            </span>
            <span class="chip" :class="fitChip(f.status)">{{ f.status }}</span>
          </div>
          <p class="num mt-1 text-xs text-muted">
            CV(RMSE) {{ cvText(f) }} · R²
            {{ num(numberAt(f.metrics, 'r2'), 2) }} · trained {{ dayRange(f.train_start, f.train_end) }}
          </p>
        </li>
      </ul>
    </section>

    <section>
      <h3 class="mb-1 text-xs font-semibold text-muted uppercase">House model (RC)</h3>
      <p class="mb-2 text-xs text-muted">
        Scored on a held-out week against persistence (assuming the temperature stays where it is). Typical ecobee
        house models miss by about 0.6°F an hour ahead and 2.5°F a day ahead.
      </p>
      <p v-if="!rc.length" class="text-sm text-muted">
        No house model fitted yet. Until one passes its checks, backtests use a rule of thumb from the baselines.
      </p>
      <ul v-else class="space-y-2">
        <li v-for="{ fit: f, horizons, unidentified } in rc" :key="f.id" class="rounded-xl border border-line p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <span class="text-sm font-medium">{{ unitName(f) }}<template v-if="f.mode"> · {{ f.mode }}</template></span>
            <span class="chip" :class="fitChip(f.status)">{{ f.status }}</span>
          </div>
          <p class="mt-0.5 text-xs text-muted">
            Trained {{ dayRange(f.train_start, f.train_end) }} · fitted {{ dateTime(f.created_at, tz) }}
          </p>
          <div class="num mt-2 grid grid-cols-2 gap-2 text-sm">
            <div v-for="h in horizons" :key="h.label" class="rounded-xl bg-surface-2 p-2">
              <p class="text-[11px] text-muted">{{ h.label }}, typical error</p>
              <p>
                <span class="font-semibold">{{ num(h.model, 2, '°F') }}</span>
                <span class="text-xs text-muted"> vs {{ num(h.persistence, 2, '°F') }} persistence</span>
              </p>
              <p
                v-if="beats(h.model, h.persistence) !== null"
                class="text-xs"
                :class="beats(h.model, h.persistence) ? 'text-good' : 'text-bad'"
              >
                {{ beats(h.model, h.persistence) ? 'beats persistence' : 'no better than persistence' }}
              </p>
            </div>
          </div>
          <div v-if="unidentified.length" class="mt-2">
            <p class="text-xs font-medium">The data can't pin these down yet:</p>
            <ul class="mt-0.5 list-disc space-y-0.5 pl-5 text-xs text-muted">
              <li v-for="name in unidentified" :key="name">
                {{ plainParam(name) }} <span class="font-mono text-[10px]">({{ name }})</span>
              </li>
            </ul>
          </div>
          <p v-else class="mt-2 text-xs text-muted">Every parameter is pinned down by the data.</p>
          <p v-if="f.notes" class="mt-2 text-xs break-words">{{ f.notes }}</p>
        </li>
      </ul>
    </section>
  </div>
</template>
