<script setup lang="ts">
// The strip at the top of Live: house occupancy, controller mode, data source, Claude, weather.
import { computed } from 'vue'
import type { AgentRunOut, HouseStatus } from '@/api/types'
import Icon from '@/components/Icon.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import { pct, temp, timeAgo } from '@/lib/format'
import { MODE_INFO } from './labels'

const props = defineProps<{ status: HouseStatus }>()

const mode = computed(() => MODE_INFO[props.status.controller.mode])
const source = computed(() => props.status.source)
const weather = computed(() => props.status.weather)
const agent = computed(() => props.status.agent)
const policy = computed(() => props.status.controller.policy)

const RUN_KIND: Record<AgentRunOut['kind'], string> = {
  nightly: 'Nightly review',
  weekly: 'Weekly review',
  triggered: 'Triggered check',
  chat: 'Question',
  signin_check: 'Sign-in check',
}

const agentSignIn = computed(() => {
  const s = agent.value.signed_in
  if (s === true) return { label: 'Signed in', cls: 'bg-good' }
  if (s === false) return { label: 'Signed out', cls: 'bg-bad' }
  return { label: 'Sign-in unknown', cls: 'bg-st-unknown' }
})

const lastRun = computed(() => {
  const r = agent.value.last_run
  if (!r) return 'No runs yet'
  const when = r.finished_at ?? r.started_at ?? r.created_at
  return `${RUN_KIND[r.kind]} ${r.status} · ${timeAgo(when)}`
})

const agentQuiet = computed(() => {
  const beat = agent.value.last_beat_at
  if (!beat) return 'The agent has not checked in yet.'
  return Date.now() - new Date(beat).getTime() > 15 * 60_000 ? `Last check-in ${timeAgo(beat)}.` : ''
})

const isOpenMeteo = computed(() => /open-meteo/i.test(weather.value?.attribution ?? ''))
</script>

<template>
  <div class="grid grid-cols-2 gap-3 lg:grid-cols-5">
    <!-- house occupancy -->
    <section class="card !p-3" aria-labelledby="lh-house">
      <h2 id="lh-house" class="card-title !text-xs">House</h2>
      <p class="mt-1 flex items-center gap-1.5 font-semibold">
        <span class="h-2.5 w-2.5 shrink-0 rounded-full" aria-hidden="true"
              :class="status.house_empty ? 'bg-st-empty' : 'bg-st-occupied'" />
        {{ status.house_empty ? 'House empty' : "Someone's home" }}
      </p>
      <p v-if="status.house_empty_reason" class="mt-1 text-xs text-muted">{{ status.house_empty_reason }}</p>
    </section>

    <!-- controller -->
    <section class="card !p-3" aria-labelledby="lh-ctl">
      <h2 id="lh-ctl" class="card-title !text-xs">Controller</h2>
      <p class="mt-1"><span class="chip" :class="mode.cls">{{ mode.label }} mode</span></p>
      <p class="mt-1 text-xs text-muted">{{ mode.hint }}</p>
      <p class="mt-0.5 text-xs text-muted">
        Linked floors {{ policy.linked_floors_enabled ? 'on' : 'off' }} · planned
        {{ timeAgo(status.controller.last_tick_at) }}
      </p>
    </section>

    <!-- data source -->
    <section class="card !p-3" aria-labelledby="lh-src">
      <h2 id="lh-src" class="card-title !text-xs">Data</h2>
      <p class="mt-1 flex items-center gap-1.5 font-semibold">
        <span class="h-2.5 w-2.5 shrink-0 rounded-full" aria-hidden="true" :class="source.ok ? 'bg-good' : 'bg-bad'" />
        {{ source.kind === 'simulator' ? 'Simulator' : 'ecobee cloud' }}
        <span class="sr-only">{{ source.ok ? 'healthy' : 'has a problem' }}</span>
      </p>
      <p v-if="source.kind === 'ecobee' && source.signed_in === false" class="mt-1 text-xs font-medium text-bad">
        Signed out of ecobee
      </p>
      <p v-if="source.detail" class="mt-1 line-clamp-2 text-xs text-muted">{{ source.detail }}</p>
      <p class="mt-0.5 text-xs text-muted">
        {{ source.last_success_at ? `Last read ${timeAgo(source.last_success_at)}` : 'Waiting for the first read' }}
      </p>
      <p v-if="status.homekit.enabled" class="mt-0.5 text-xs" :class="status.homekit.online ? 'text-muted' : 'text-warn'">
        HomeKit {{ status.homekit.online ? `online · ${status.homekit.paired} paired` : 'offline' }}
      </p>
    </section>

    <!-- Claude -->
    <section class="card !p-3" aria-labelledby="lh-agent">
      <h2 id="lh-agent" class="card-title !text-xs">Claude</h2>
      <p class="mt-1 flex items-center gap-1.5 font-semibold">
        <span class="h-2.5 w-2.5 shrink-0 rounded-full" aria-hidden="true" :class="agentSignIn.cls" />
        {{ agent.enabled ? agentSignIn.label : 'Paused' }}
      </p>
      <p class="mt-1 text-xs text-muted">{{ lastRun }}</p>
      <p v-if="agentQuiet" class="mt-0.5 text-xs text-muted">{{ agentQuiet }}</p>
      <p v-if="agent.token_warning" class="mt-1 flex items-start gap-1 text-xs font-medium text-warn">
        <Icon name="alert" :size="14" class="mt-px shrink-0" />{{ agent.token_warning }}
      </p>
    </section>

    <!-- weather now -->
    <section class="card col-span-2 !p-3 lg:col-span-1" aria-labelledby="lh-wx">
      <h2 id="lh-wx" class="card-title !text-xs">Outdoors</h2>
      <template v-if="weather">
        <div class="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span class="num text-2xl font-semibold">{{ temp(weather.temp_f, 0) }}<span class="text-sm font-normal text-muted">F</span></span>
          <span class="num text-xs text-muted">
            <template v-if="weather.forecast_high_f !== null || weather.forecast_low_f !== null">
              High {{ temp(weather.forecast_high_f, 0) }} · Low {{ temp(weather.forecast_low_f, 0) }}
            </template>
          </span>
        </div>
        <p class="num mt-0.5 text-xs text-muted">
          <span v-if="weather.rh !== null">{{ pct(weather.rh) }} humidity</span>
          <span v-if="weather.rh !== null && weather.cloud_cover !== null"> · </span>
          <span v-if="weather.cloud_cover !== null">{{ pct(weather.cloud_cover) }} cloud</span>
        </p>
        <div class="mt-1">
          <OpenMeteoAttribution v-if="isOpenMeteo" />
          <p v-else-if="weather.attribution" class="text-xs text-muted">{{ weather.attribution }}</p>
        </div>
      </template>
      <p v-else class="mt-1 text-sm text-muted">No weather yet.</p>
    </section>
  </div>
</template>
