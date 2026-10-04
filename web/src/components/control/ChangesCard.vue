<script setup lang="ts">
// Changes moving through the gates (pending) and decided ones (history).
import { computed, ref } from 'vue'
import type { ChangeOut, SettingsOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import Segmented from '@/components/analysis/Segmented.vue'
import { useControl } from '@/stores/control'
import ChangeItem from './ChangeItem.vue'

defineProps<{ settings: SettingsOut | null; isOwner: boolean; tz?: string }>()
const control = useControl()

const FINAL = new Set(['active', 'rejected', 'retired', 'superseded', 'cancelled', 'expired', 'rolled_back'])
const HISTORY_LIMIT = 20

const all = computed<ChangeOut[]>(() => [...(control.changes.data ?? [])].sort((a, b) => b.id - a.id))
const pending = computed(() => all.value.filter((c) => !FINAL.has(c.status)))
const history = computed(() => all.value.filter((c) => FINAL.has(c.status)).slice(0, HISTORY_LIMIT))
const waitingOnYou = computed(() => pending.value.filter((c) => c.needs === 'owner').length)

type Tab = 'pending' | 'history'
const tab = ref<Tab>('pending')
const tabs = computed(() => [
  { value: 'pending' as const, label: `Pending (${pending.value.length})` },
  { value: 'history' as const, label: 'History' },
])
const shown = computed(() => (tab.value === 'pending' ? pending.value : history.value))
</script>

<template>
  <Card title="Changes" subtitle="Every change passes: check → backtest → shadow days → sign-off → trial">
    <template #actions>
      <button
        type="button"
        class="btn !px-2 !py-1"
        aria-label="Reload changes"
        :disabled="control.changes.loading"
        @click="control.loadChanges()"
      >
        <Icon name="refresh" :size="14" />
      </button>
    </template>
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <Segmented v-model="tab" :options="tabs" label="Changes" />
      <span v-if="waitingOnYou" class="chip bg-warn/15 text-warn">{{ waitingOnYou }} waiting on you</span>
    </div>
    <AsyncState
      :loading="control.changes.loading && !control.changes.data"
      :error="control.changes.error"
      :empty="!!control.changes.data && !shown.length"
      :empty-text="tab === 'pending' ? 'Nothing in the gates right now.' : 'No decided changes yet.'"
    >
      <div class="space-y-2">
        <ChangeItem
          v-for="c in shown"
          :key="c.id"
          :change="c"
          :settings="settings"
          :is-owner="isOwner"
          :pending="tab === 'pending'"
          :tz="tz"
        />
      </div>
    </AsyncState>
  </Card>
</template>
