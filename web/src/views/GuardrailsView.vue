<script setup lang="ts">
// Guardrails: controller mode, the current plan and manual holds; the change gates and the
// policy with Claude's sign-off ranges; the owner's hard limits, comfort bands, sleep windows
// and schedule; and the action log with read-back.
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onEvent } from '@/api/ws'
import AsyncState from '@/components/AsyncState.vue'
import Segmented from '@/components/analysis/Segmented.vue'
import ActionLog from '@/components/control/ActionLog.vue'
import ChangesCard from '@/components/control/ChangesCard.vue'
import ComfortForm from '@/components/control/ComfortForm.vue'
import LimitsForm from '@/components/control/LimitsForm.vue'
import ManualHoldCard from '@/components/control/ManualHoldCard.vue'
import ModeCard from '@/components/control/ModeCard.vue'
import PlanCard from '@/components/control/PlanCard.vue'
import PolicyParamsCard from '@/components/control/PolicyParamsCard.vue'
import ProposeChangeForm from '@/components/control/ProposeChangeForm.vue'
import ScheduleForm from '@/components/control/ScheduleForm.vue'
import SleepWindowsForm from '@/components/control/SleepWindowsForm.vue'
import { unitKeys } from '@/components/control/units'
import { useAuth } from '@/stores/auth'
import { useControl } from '@/stores/control'
import { useStatus } from '@/stores/status'

type Tab = 'controller' | 'changes' | 'limits' | 'log'
const TABS: { value: Tab; label: string }[] = [
  { value: 'controller', label: 'Controller' },
  { value: 'changes', label: 'Changes' },
  { value: 'limits', label: 'Limits' },
  { value: 'log', label: 'Log' },
]

const route = useRoute()
const router = useRouter()
const control = useControl()
const status = useStatus()
const auth = useAuth()

const isOwner = computed(() => auth.state?.role === 'owner')
const tz = computed(() => status.data?.tz)
const settings = computed(() => control.settings.data)
const units = computed(() => unitKeys(settings.value))
const rooms = computed(() => status.data?.rooms ?? [])

const tab = computed<Tab>({
  get: () => {
    const t = route.query.tab
    return TABS.some((x) => x.value === t) ? (t as Tab) : 'controller'
  },
  set: (t) => {
    void router.replace({ query: { ...route.query, tab: t } })
  },
})
const pendingForOwner = computed(
  () => (control.changes.data ?? []).filter((c) => c.needs === 'owner' && !['active', 'rejected'].includes(c.status)).length,
)
const tabs = computed(() =>
  TABS.map((t) => (t.value === 'changes' && pendingForOwner.value ? { ...t, label: `Changes (${pendingForOwner.value})` } : t)),
)

let unsubscribe: (() => void) | null = null
onMounted(() => {
  // The house timezone and room names come from the status store; start it if the shell hasn't.
  status.start()
  void control.loadSettings()
  void control.loadPlan()
  void control.loadChanges()
  unsubscribe = onEvent((e) => {
    if (e.type === 'change') void control.loadChanges()
    if (e.type === 'action') {
      void control.loadActions(control.actions.key && control.actions.key !== 'all' ? control.actions.key : null)
      void control.loadPlan()
    }
    if (e.type === 'status' && tab.value === 'controller') void control.loadPlan()
  })
})
onBeforeUnmount(() => unsubscribe?.())
</script>

<template>
  <div class="space-y-4">
    <Segmented v-model="tab" :options="tabs" label="Guardrails section" />

    <AsyncState :loading="control.settings.loading && !settings" :error="control.settings.error">
      <template v-if="settings">
        <!-- Controller: mode, plan, manual hold -->
        <div v-if="tab === 'controller'" class="grid gap-4 lg:grid-cols-2">
          <div class="space-y-4">
            <ModeCard :settings="settings" :is-owner="isOwner" />
            <ManualHoldCard :settings="settings" :units="status.data?.units ?? []" :is-owner="isOwner" />
          </div>
          <AsyncState :loading="control.plan.loading && !control.plan.data" :error="control.plan.error">
            <PlanCard
              v-if="control.plan.data"
              :plan="control.plan.data"
              :loading="control.plan.loading"
              :rooms="rooms"
              @refresh="control.loadPlan()"
            />
          </AsyncState>
        </div>

        <!-- Changes: gates, proposals, the policy -->
        <div v-else-if="tab === 'changes'" class="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <ChangesCard :settings="settings" :is-owner="isOwner" :tz="tz" />
          <div class="space-y-4">
            <PolicyParamsCard :settings="settings" />
            <ProposeChangeForm v-if="isOwner" :settings="settings" :is-owner="isOwner" />
          </div>
        </div>

        <!-- Limits: everything the owner sets -->
        <div v-else-if="tab === 'limits'" class="space-y-4">
          <p v-if="!isOwner" class="text-sm text-muted">Read-only: only the owner can change these.</p>
          <div class="grid gap-4 lg:grid-cols-2">
            <LimitsForm :settings="settings" :is-owner="isOwner" />
            <ScheduleForm :settings="settings" :is-owner="isOwner" />
          </div>
          <ComfortForm :settings="settings" :is-owner="isOwner" />
          <SleepWindowsForm :settings="settings" :rooms="rooms" :is-owner="isOwner" />
        </div>

        <!-- Log -->
        <ActionLog v-else :units="units" :tz="tz" />
      </template>
    </AsyncState>
  </div>
</template>
