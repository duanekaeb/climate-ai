<script setup lang="ts">
// Runtime: daily runtime per unit against the weather-expected total and the outdoor mean,
// totals per unit, and a picked day's 5-minute detail.
import { computed, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { DailyRuntime } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import IntradayPanel from '@/components/runtime/IntradayPanel.vue'
import RuntimeTotals from '@/components/runtime/RuntimeTotals.vue'
import TapChart from '@/components/runtime/TapChart.vue'
import { chartTheme, dayLabel, useThemeKey } from '@/components/runtime/chartKit'
import { dailyRuntimeOption } from '@/components/runtime/runtimeCharts'
import { UNIT_COLORS, UNIT_NAMES, minutes } from '@/lib/format'
import { baselineFailNote, failingInUse, sortUnitKeys, summarizeDays, useRuntime } from '@/stores/runtime'
import { useStatus } from '@/stores/status'

const RANGES = [14, 30, 90]

const store = useRuntime()
const status = useStatus()
const themeKey = useThemeKey()

const tz = computed(() => status.data?.tz ?? Intl.DateTimeFormat().resolvedOptions().timeZone)
const rows = computed(() => store.daily ?? [])
const days = computed(() => summarizeDays(rows.value, store.failing))
// Failing baselines behind some expectation in this range: their expectations are left out.
const failingNotes = computed(() => failingInUse(rows.value, store.failing).map(baselineFailNote))
const unitKeys = computed(() => sortUnitKeys(rows.value.map((r) => r.unit_key)))
const hasOutdoor = computed(() => days.value.some((d) => d.outdoorMean !== null))

const summary = computed(() => {
  const ds = days.value
  const covered = ds.filter((d) => d.expected !== null)
  const up = rows.value.filter((r) => r.unit_key === 'up').reduce((s, r) => s + r.maxed_min, 0)
  return {
    total: ds.reduce((s, d) => s + d.total, 0),
    actualCovered: covered.reduce((s, d) => s + d.total, 0),
    expected: covered.length ? covered.reduce((s, d) => s + (d.expected ?? 0), 0) : null,
    coveredDays: covered.length,
    excludedDays: ds.filter((d) => d.expected === null && d.excluded.length).length,
    upMaxed: up,
  }
})

const option = computed(() => {
  void themeKey.value
  return dailyRuntimeOption({ days: days.value, unitKeys: unitKeys.value, selectedDate: store.date, theme: chartTheme() })
})

function setRange(n: number) {
  if (n !== store.days || !store.daily) store.loadDaily(n)
}
function pickDate(date: string) {
  if (date && date !== store.date) store.loadIntraday(date)
}
function onTap(index: number) {
  const d = days.value[index]
  if (d) pickDate(d.date)
}

// Default to the most recent day once the range has loaded.
watch(days, (ds) => {
  if (!ds.length) return
  if (!ds.some((d) => d.date === store.date)) pickDate(ds[ds.length - 1].date)
})

function refresh() {
  store.loadDaily(store.days)
  store.loadBaselines()
  if (store.date) store.loadIntraday(store.date)
}

onMounted(() => {
  status.start()
  store.loadBaselines()
  store.loadDaily(store.days)
  if (store.date) store.loadIntraday(store.date)
})

const pickerDays = computed(() => [...days.value].reverse())
// Each unit's mode on the picked day, so the 5-minute totals count what the daily chart counts.
const intradayModes = computed(() => {
  const date = store.intraday?.date
  const out: Record<string, DailyRuntime['mode']> = {}
  for (const r of rows.value) if (r.date === date) out[r.unit_key] = r.mode
  return out
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="inline-flex rounded-xl border border-line bg-surface p-0.5" role="group" aria-label="Date range">
        <button v-for="n in RANGES" :key="n" type="button" :aria-pressed="store.days === n"
                class="rounded-lg px-3 py-1.5 text-sm font-medium"
                :class="store.days === n ? 'bg-accent text-white' : 'text-muted hover:bg-surface-2'" @click="setRange(n)">
          {{ n }} days
        </button>
      </div>
      <button type="button" class="btn !px-2.5 !py-1.5" :disabled="store.dailyLoading" aria-label="Refresh runtime"
              @click="refresh">
        <Icon name="refresh" :size="16" :class="store.dailyLoading ? 'animate-spin' : ''" />
      </button>
    </div>

    <Card title="Daily runtime" subtitle="Per unit, stacked, against what the weather predicts for the whole house.">
      <AsyncState :loading="(store.dailyLoading && !store.daily) || !store.baselinesKnown" :error="store.daily ? '' : store.dailyError"
                  :empty="!!store.daily && !store.daily.length" empty-text="No runtime recorded in this range yet.">
        <div class="space-y-3">
          <dl class="grid grid-cols-3 gap-2 text-center">
            <div class="rounded-xl bg-surface-2 p-2">
              <dt class="text-[11px] text-muted">House runtime</dt>
              <dd class="num font-semibold">{{ minutes(summary.total) }}</dd>
            </div>
            <div class="rounded-xl bg-surface-2 p-2">
              <dt class="text-[11px] text-muted">Expected</dt>
              <dd class="num font-semibold">
                {{ minutes(summary.expected) }}<sup v-if="failingNotes.length" class="text-muted">†</sup>
              </dd>
              <dd v-if="summary.expected !== null && summary.coveredDays < days.length" class="num text-[11px] leading-tight text-muted">
                {{ summary.coveredDays }} of {{ days.length }} days · actual {{ minutes(summary.actualCovered) }}
              </dd>
            </div>
            <div class="rounded-xl p-2" :class="summary.upMaxed > 0 ? 'bg-bad/10 ring-1 ring-bad/40' : 'bg-surface-2'">
              <dt class="text-[11px]" :class="summary.upMaxed > 0 ? 'text-bad' : 'text-muted'">Upstairs maxed out</dt>
              <dd class="num font-semibold" :class="summary.upMaxed > 0 ? 'text-bad' : ''">{{ minutes(summary.upMaxed) }}</dd>
            </div>
          </dl>
          <p v-if="store.dailyError" role="status" class="text-xs text-warn">Couldn't refresh: {{ store.dailyError }}</p>
          <div v-if="failingNotes.length" class="flex items-start gap-1.5 text-xs text-warn">
            <Icon name="alert" :size="14" class="mt-px shrink-0" />
            <p>
              † <template v-for="(n, i) in failingNotes" :key="n">{{ i ? '; ' : '' }}{{ n }}</template>.
              Expected leaves those out, so {{ summary.excludedDays }} of {{ days.length }} days have no house
              expectation.
              <RouterLink to="/results" class="underline">Baselines</RouterLink>
            </p>
          </div>
          <p v-else-if="store.baselinesError" role="status" class="text-xs text-warn">
            Couldn't check whether the baselines pass their checks ({{ store.baselinesError }}), so Expected may rest on a
            failing one.
          </p>

          <figure class="min-w-0">
            <TapChart :option="option" height="300px" @tap="onTap" />
            <figcaption>
              <ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
                <li v-for="k in unitKeys" :key="k" class="flex items-center gap-1">
                  <span class="h-2.5 w-2.5 rounded-sm" :style="{ background: UNIT_COLORS[k] ?? 'var(--color-accent)' }" aria-hidden="true" />{{ UNIT_NAMES[k] ?? k }}
                </li>
                <li class="flex items-center gap-1"><span class="text-ink" aria-hidden="true">◆</span>Expected for the weather (house)</li>
                <li v-if="hasOutdoor" class="flex items-center gap-1"><span class="h-0.5 w-4 rounded bg-warn" aria-hidden="true" />Outdoor mean (right axis)</li>
              </ul>
            </figcaption>
          </figure>
          <OpenMeteoAttribution v-if="hasOutdoor" />
        </div>
      </AsyncState>
    </Card>

    <Card v-if="days.length" :title="store.date ? `${dayLabel(store.date, true)} · 5-minute detail` : '5-minute detail'">
      <template #actions>
        <label class="flex items-center gap-2 text-xs text-muted">
          <span class="hidden sm:inline">Day</span>
          <select class="input !w-auto !py-1 text-sm" aria-label="Pick a day for the 5-minute detail" :value="store.date"
                  @change="pickDate(($event.target as HTMLSelectElement).value)">
            <option v-for="d in pickerDays" :key="d.date" :value="d.date">{{ dayLabel(d.date, true) }}</option>
          </select>
        </label>
      </template>
      <p class="-mt-1 mb-3 text-xs text-muted">Tap a day in the chart above, or pick one here.</p>
      <AsyncState :loading="store.intradayLoading && !store.intraday" :error="store.intraday ? '' : store.intradayError"
                  :empty="!!store.intraday && !store.intraday.units.some((u) => u.points.length)"
                  empty-text="No 5-minute data for this day.">
        <IntradayPanel v-if="store.intraday" :intraday="store.intraday" :tz="tz" :modes="intradayModes" />
      </AsyncState>
    </Card>

    <Card v-if="rows.length && store.baselinesKnown" title="Totals by unit" :subtitle="`Last ${store.days} days`">
      <RuntimeTotals :rows="rows" :days="store.days" :failing="store.failing" />
    </Card>
  </div>
</template>
