<script setup lang="ts">
// Live (home screen): the whole house right now, refreshed by websocket events and every 60 s.
// Utility events are refetched whenever the status is (the same websocket events and poll).
import { computed, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { UtilityEventOut } from '@/api/types'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import AlertList from '@/components/live/AlertList.vue'
import LiveHeader from '@/components/live/LiveHeader.vue'
import RoomChips from '@/components/live/RoomChips.vue'
import UnitCard from '@/components/live/UnitCard.vue'
import UtilityEventCard from '@/components/live/UtilityEventCard.vue'
import { groupEvents } from '@/components/live/events'
import { localTime } from '@/lib/format'
import { useAuth } from '@/stores/auth'
import { useControl } from '@/stores/control'
import { sortUnitKeys } from '@/stores/runtime'
import { useStatus } from '@/stores/status'
import { useUtilityEvents } from '@/stores/utilityEvents'

const status = useStatus()
const auth = useAuth()
const control = useControl()
const utilityEvents = useUtilityEvents()

onMounted(() => {
  status.start()
  // "Resume schedule" says how long the ecobee schedule runs first (control.resume_backoff_hours).
  void control.ensureSettings()
})
// Each status refresh (websocket status / action / alert events, the 60 s poll) reloads the events.
watch(
  () => status.data?.now,
  (now) => {
    if (now) void utilityEvents.load()
  },
  { immediate: true },
)

const data = computed(() => status.data)
const units = computed(() => {
  const list = data.value?.units ?? []
  const order = sortUnitKeys(list.map((u) => u.unit_key))
  return [...list].sort((a, b) => order.indexOf(a.unit_key) - order.indexOf(b.unit_key))
})
const openAlerts = computed(() => (data.value?.alerts ?? []).filter((a) => a.resolved_at === null).length)
const isOwner = computed(() => auth.state?.role === 'owner')
const backoffHours = computed(() => control.settings.data?.control.resume_backoff_hours ?? null)
const eventGroups = computed(() =>
  groupEvents(
    units.value.map((u) => u.utility_event).filter((e): e is UtilityEventOut => !!e),
    utilityEvents.list.data ?? [],
    units.value.map((u) => u.unit_key),
  ),
)
</script>

<template>
  <div>
    <AsyncState :loading="!status.data && !status.error" :error="status.data ? '' : status.error">
      <div v-if="data" class="space-y-4">
        <div v-if="data.source.kind === 'simulator'" role="status"
             class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-accent/30 bg-accent/10 p-3 text-sm">
          <span class="min-w-0 flex-1 font-medium">Simulated house — connect your ecobees in Setup.</span>
          <RouterLink to="/setup" class="font-medium text-accent underline">Open Setup</RouterLink>
        </div>
        <div v-if="!data.location_confirmed" role="status"
             class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm">
          <span class="min-w-0 flex-1">
            <span class="font-medium">The house location isn't confirmed.</span>
            Weather and schedules use it; confirm it in Setup.
          </span>
          <RouterLink to="/setup" class="font-medium text-warn underline">Confirm location</RouterLink>
        </div>
        <p v-if="status.error" role="status" class="text-xs text-warn">
          Couldn't refresh ({{ status.error }}). Showing the reading from {{ localTime(data.now, data.tz) }}.
        </p>

        <LiveHeader :status="data" />

        <section v-if="eventGroups.length" aria-label="Utility events" class="grid gap-4 lg:grid-cols-2">
          <UtilityEventCard v-for="g in eventGroups" :key="g.key" :group="g" :tz="data.tz" :now="data.now"
                            :mode="data.controller.mode" :is-owner="isOwner" @changed="status.load()" />
        </section>

        <section aria-label="Thermostats">
          <div v-if="units.length" class="grid gap-4 lg:grid-cols-3">
            <UnitCard v-for="u in units" :key="u.unit_key" :unit="u" :rooms="data.rooms" :mode="data.controller.mode" :tz="data.tz"
                      :now="data.now" :is-owner="isOwner" :resume-backoff-hours="backoffHours" />
          </div>
          <p v-else class="card text-sm text-muted">No thermostat has reported yet.</p>
        </section>

        <div class="grid gap-4 lg:grid-cols-2">
          <Card title="Rooms" subtitle="Tap a room for its last 24 hours.">
            <template #actions>
              <RouterLink to="/rooms" class="text-sm font-medium text-accent">All rooms</RouterLink>
            </template>
            <RoomChips v-if="data.rooms.length" :rooms="data.rooms" />
            <p v-else class="text-sm text-muted">No rooms reported yet.</p>
          </Card>
          <Card title="Open alerts" :subtitle="openAlerts ? `${openAlerts} open` : undefined">
            <AlertList :alerts="data.alerts" :can-resolve="isOwner" @resolved="status.load()" />
          </Card>
        </div>
      </div>
    </AsyncState>
    <div v-if="!status.data && status.error" class="mt-3 text-center">
      <button type="button" class="btn" :disabled="status.loading" @click="status.load()">
        <Icon name="refresh" :size="16" />Try again
      </button>
    </div>
  </div>
</template>
