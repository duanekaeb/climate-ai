<script setup lang="ts">
// Open alerts on Live. The owner can resolve one; the list then refreshes from /status.
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { AlertOut } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { timeAgo } from '@/lib/format'

const props = defineProps<{ alerts: AlertOut[]; canResolve: boolean }>()
const emit = defineEmits<{ resolved: [] }>()

const LEVEL: Record<AlertOut['level'], { cls: string; label: string }> = {
  error: { cls: 'border-bad/40 bg-bad/10 text-bad', label: 'Problem' },
  warn: { cls: 'border-warn/40 bg-warn/10 text-warn', label: 'Warning' },
  info: { cls: 'border-accent/30 bg-accent/10 text-accent', label: 'Info' },
}
const RANK: Record<AlertOut['level'], number> = { error: 0, warn: 1, info: 2 }

const open = computed(() =>
  props.alerts
    .filter((a) => a.resolved_at === null)
    .sort((a, b) => RANK[a.level] - RANK[b.level] || b.ts.localeCompare(a.ts)),
)

const busy = ref<number | null>(null)
const error = ref('')

async function resolve(a: AlertOut) {
  busy.value = a.id
  error.value = ''
  try {
    await api.post<AlertOut>(`/alerts/${a.id}/resolve`)
    emit('resolved')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <div>
    <p v-if="error" role="alert" class="mb-2 rounded-xl border border-bad/40 bg-bad/10 p-2 text-sm text-bad">{{ error }}</p>
    <p v-if="!open.length" class="text-sm text-muted">No open alerts.</p>
    <ul v-else class="space-y-2">
      <li v-for="a in open" :key="a.id" class="rounded-xl border p-3" :class="LEVEL[a.level].cls">
        <div class="flex items-start gap-2">
          <Icon name="alert" :size="18" class="mt-0.5 shrink-0" />
          <div class="min-w-0 flex-1">
            <p class="font-medium break-words">
              <span class="sr-only">{{ LEVEL[a.level].label }}: </span>{{ a.title }}
            </p>
            <p v-if="a.body" class="mt-0.5 text-sm break-words whitespace-pre-line text-ink/80">{{ a.body }}</p>
            <p class="mt-1 text-xs text-muted">{{ timeAgo(a.ts) }}</p>
          </div>
          <button v-if="canResolve" type="button" class="btn shrink-0 !px-2 !py-1 text-xs" :disabled="busy === a.id"
                  :aria-label="`Resolve alert: ${a.title}`" @click="resolve(a)">
            <Icon name="check" :size="14" />{{ busy === a.id ? 'Resolving…' : 'Resolve' }}
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>
