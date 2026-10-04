<script setup lang="ts">
// Reports: the daily / nightly / weekly / anomaly write-ups. List and detail side by side on
// wide screens; one at a time on a phone. ?kind= and ?id= keep the place for deep links.
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onEvent } from '@/api/ws'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import Markdown from '@/components/Markdown.vue'
import ReportMeta from '@/components/reports/ReportMeta.vue'
import { localDate, localTime, timeAgo } from '@/lib/format'
import { REPORT_FILTERS, type ReportFilter, isReportFilter, useReports } from '@/stores/reports'

const store = useReports()
const route = useRoute()
const router = useRouter()

const kind = computed<ReportFilter>(() => (isReportFilter(route.query.kind) ? route.query.kind : ''))
const selectedId = computed(() => {
  const q = route.query.id
  const n = typeof q === 'string' ? Number(q) : NaN
  return Number.isInteger(n) && n > 0 ? n : null
})

watch(kind, (k) => store.load(k), { immediate: true })
watch(
  [selectedId, () => store.items],
  ([id]) => {
    if (id === null) store.close()
    else if (store.detail?.id !== id || store.detailError) store.open(id)
  },
  { immediate: true },
)

function setKind(k: ReportFilter) {
  const query = { ...route.query }
  if (k) query.kind = k
  else delete query.kind
  router.replace({ query })
}
function openReport(id: number) {
  router.replace({ query: { ...route.query, id: String(id) } })
}
function back() {
  const query = { ...route.query }
  delete query.id
  router.replace({ query })
}

let off: (() => void) | undefined
onMounted(() => {
  off = onEvent((e) => {
    if (e.type === 'report') store.load(kind.value, { quiet: true })
  })
})
onBeforeUnmount(() => off?.())

const emptyText = computed(() =>
  kind.value
    ? `No ${REPORT_FILTERS.find((f) => f.key === kind.value)?.label.toLowerCase()} reports yet.`
    : 'No reports yet. The first daily report arrives after a full day of data.',
)
const detail = computed(() => (selectedId.value !== null && store.detail?.id === selectedId.value ? store.detail : null))
</script>

<template>
  <div class="space-y-4">
    <div role="group" aria-label="Report kind" class="flex flex-wrap gap-1.5">
      <button v-for="f in REPORT_FILTERS" :key="f.key || 'all'" type="button" :aria-pressed="kind === f.key"
              class="chip border !px-3 !py-1.5 text-sm"
              :class="kind === f.key ? 'border-accent bg-accent text-white' : 'border-line bg-surface text-muted hover:bg-surface-2'"
              @click="setKind(f.key)">
        {{ f.label }}
      </button>
    </div>

    <div class="md:grid md:grid-cols-[minmax(0,20rem)_minmax(0,1fr)] md:items-start md:gap-4">
      <!-- list (hidden on phones while a report is open) -->
      <section aria-label="Reports" :class="selectedId !== null ? 'hidden md:block' : ''">
        <AsyncState :loading="store.loading && !store.items" :error="store.items ? '' : store.error"
                    :empty="!!store.items && !store.items.length" :empty-text="emptyText">
          <ul class="space-y-2">
            <li v-for="r in store.items ?? []" :key="r.id">
              <button type="button" :aria-current="r.id === selectedId ? 'true' : undefined"
                      class="card block w-full !p-3 text-left hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                      :class="r.id === selectedId ? '!border-accent ring-1 ring-accent' : ''" @click="openReport(r.id)">
                <p class="line-clamp-2 font-medium">{{ r.title }}</p>
                <div class="mt-1.5"><ReportMeta :report="r" /></div>
                <p class="mt-1 text-xs text-muted">{{ timeAgo(r.created_at) }}</p>
              </button>
            </li>
          </ul>
          <p v-if="store.error" role="status" class="mt-2 text-xs text-warn">Couldn't refresh: {{ store.error }}</p>
        </AsyncState>
      </section>

      <!-- detail -->
      <section aria-label="Report" :class="selectedId === null ? 'hidden md:block' : ''">
        <button v-if="selectedId !== null" type="button" class="btn mb-3 !py-1.5 md:hidden" @click="back">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
          All reports
        </button>
        <Card v-if="selectedId !== null">
          <AsyncState :loading="store.detailLoading && !detail" :error="detail ? '' : store.detailError">
            <article v-if="detail" class="min-w-0">
              <h2 class="text-lg font-semibold">{{ detail.title }}</h2>
              <div class="mt-2"><ReportMeta :report="detail" /></div>
              <p class="mt-1 text-xs text-muted">
                Published {{ localDate(detail.created_at) }}, {{ localTime(detail.created_at) }}
                <template v-if="detail.author === 'claude'"> · written by Claude</template>
              </p>
              <hr class="my-3 border-line" />
              <Markdown :source="detail.body_md" class="min-w-0 break-words" />
            </article>
          </AsyncState>
        </Card>
        <div v-else class="card hidden py-12 text-center text-sm text-muted md:block">
          <Icon name="reports" :size="28" class="mx-auto mb-2" />
          Pick a report to read it.
        </div>
      </section>
    </div>
  </div>
</template>
