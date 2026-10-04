<script setup lang="ts">
// One discovered HomeKit thermostat and its pairing handshake:
// Pair (alias + unit) -> requested -> awaiting_code (owner types the code shown on the
// thermostat) -> code_submitted -> paired | failed. The parent polls /setup meanwhile.
import { computed, nextTick, reactive, ref, watch } from 'vue'
import type { HomekitDeviceOut, RoomOut, UnitOut } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { timeAgo } from '@/lib/format'
import { useSetup } from '@/stores/setup'
import { useAction } from './useAction'

const props = defineProps<{
  device: HomekitDeviceOut
  units: UnitOut[]
  rooms: RoomOut[]
  takenAliases: string[]
  secretsOk: boolean
}>()
const setup = useSetup()
const { busy, error, run } = useAction()

const ALIAS_RE = /^[a-z][a-z0-9_]{1,23}$/

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, '')

function suggestAlias(name: string): string {
  let a = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
  if (!/^[a-z]/.test(a)) a = `t_${a}`
  a = a.slice(0, 24).replace(/_+$/, '')
  return a.length >= 2 ? a : 'thermostat'
}
function guessUnit(name: string): string {
  const n = norm(name)
  for (const u of props.units) {
    const room = props.rooms.find((r) => r.key === u.thermostat_room_key)
    if ((room && n.includes(norm(room.name))) || n.includes(norm(u.name))) return u.key
  }
  return ''
}

const showForm = ref(false)
const form = reactive({ alias: '', unit: '' })
const code = ref('')
const codeInput = ref<HTMLInputElement | null>(null)

function openForm() {
  form.alias = props.device.alias ?? suggestAlias(props.device.name)
  form.unit = props.device.unit_key ?? guessUnit(props.device.name)
  error.value = ''
  showForm.value = true
}

const state = computed(() => props.device.pairing_state)
const STATE: Record<string, { label: string; cls: string }> = {
  requested: { label: 'Starting…', cls: 'bg-accent/15 text-accent' },
  awaiting_code: { label: 'Waiting for the code', cls: 'bg-accent/15 text-accent' },
  code_submitted: { label: 'Checking the code…', cls: 'bg-accent/15 text-accent' },
  paired: { label: 'Paired', cls: 'bg-good/15 text-good' },
  failed: { label: 'Pairing failed', cls: 'bg-bad/15 text-bad' },
  unpair_requested: { label: 'Unpairing…', cls: 'bg-warn/15 text-warn' },
}
const chip = computed(() => {
  const known = STATE[state.value]
  if (known) return known
  return props.device.unpaired
    ? { label: 'Ready to pair', cls: 'bg-good/15 text-good' }
    : { label: 'Paired elsewhere', cls: 'bg-warn/15 text-warn' }
})
const canPair = computed(() => props.device.unpaired && (state.value === 'none' || state.value === 'failed' || !STATE[state.value]))

const aliasProblem = computed(() => {
  if (!ALIAS_RE.test(form.alias)) return 'Lowercase letters, digits and _, starting with a letter (2–24 characters).'
  if (props.takenAliases.includes(form.alias)) return 'Another device already uses this alias.'
  return ''
})

async function pair() {
  if (aliasProblem.value || !form.unit) return
  const ok = await run(() => setup.homekitPair(props.device.device_id, form.alias, form.unit))
  if (ok) showForm.value = false
}

function formatCode(raw: string): string {
  const d = raw.replace(/\D/g, '').slice(0, 8)
  if (d.length <= 3) return d
  if (d.length <= 5) return `${d.slice(0, 3)}-${d.slice(3)}`
  return `${d.slice(0, 3)}-${d.slice(3, 5)}-${d.slice(5)}`
}
function onCodeInput(e: Event) {
  const el = e.target as HTMLInputElement
  code.value = formatCode(el.value)
  el.value = code.value
}
const codeOk = computed(() => /^\d{3}-\d{2}-\d{3}$/.test(code.value))

async function submitCode() {
  if (!codeOk.value) return
  const c = code.value
  const ok = await run(() => setup.homekitCode(props.device.device_id, c))
  if (ok) code.value = ''
}

async function unpair() {
  const name = props.device.alias ?? props.device.name
  if (!window.confirm(`Unpair ${name}? Live HomeKit readings and backup holds stop for this thermostat until you pair it again.`)) return
  await run(() => setup.homekitUnpair(props.device.device_id))
}

watch(state, (s) => {
  if (s === 'awaiting_code') nextTick(() => codeInput.value?.focus())
  if (s === 'failed') showForm.value = false
})

const unitName = computed(() => props.units.find((u) => u.key === props.device.unit_key)?.name ?? props.device.unit_key)
</script>

<template>
  <li class="rounded-xl border border-line p-3">
    <div class="flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0">
        <p class="flex items-center gap-1.5 font-medium">
          <span class="h-2 w-2 shrink-0 rounded-full" :class="device.online ? 'bg-good' : 'bg-st-unknown'" aria-hidden="true" />
          <span class="truncate">{{ device.name }}</span>
          <span class="sr-only">{{ device.online ? '(online)' : '(offline)' }}</span>
        </p>
        <p class="text-xs break-all text-muted">
          {{ device.model ?? 'HomeKit accessory' }}<template v-if="device.address"> · {{ device.address }}</template> ·
          {{ device.online ? 'online' : `last seen ${timeAgo(device.last_seen_at)}` }}
        </p>
        <p v-if="device.alias || device.unit_key" class="mt-0.5 text-xs text-muted">
          <template v-if="device.alias">Alias <span class="font-mono">{{ device.alias }}</span></template>
          <template v-if="device.alias && device.unit_key"> · </template>
          <template v-if="device.unit_key">{{ unitName }}</template>
        </p>
      </div>
      <span class="chip shrink-0" :class="chip.cls">{{ chip.label }}</span>
    </div>

    <!-- pairing in progress -->
    <div v-if="state === 'requested' || state === 'code_submitted' || state === 'unpair_requested'" role="status"
         class="mt-3 flex items-center gap-2 text-sm text-muted">
      <Icon name="refresh" :size="16" class="animate-spin" />
      {{ state === 'requested' ? 'Asking the thermostat to start pairing…' : state === 'code_submitted' ? 'Checking the code with the thermostat…' : 'Removing the pairing…' }}
    </div>

    <!-- the code step -->
    <form v-else-if="state === 'awaiting_code'" class="mt-3 space-y-2 rounded-xl border border-accent/40 bg-accent/5 p-3" novalidate
          @submit.prevent="submitCode">
      <p role="status" class="font-medium">Enter the 8-digit code now shown on the thermostat.</p>
      <div class="flex flex-wrap items-center gap-2">
        <label class="sr-only" :for="`hk-code-${device.device_id}`">HomeKit setup code</label>
        <input :id="`hk-code-${device.device_id}`" ref="codeInput" :value="code" class="input num !w-40 text-center text-lg tracking-widest"
               inputmode="numeric" autocomplete="one-time-code" placeholder="XXX-XX-XXX" maxlength="10" @input="onCodeInput" />
        <button type="submit" class="btn btn-primary" :disabled="busy || !codeOk">{{ busy ? 'Sending…' : 'Pair' }}</button>
      </div>
      <p class="text-xs text-muted">The dashes are added for you.</p>
    </form>

    <!-- failed -->
    <p v-if="state === 'failed' && device.pairing_error" class="mt-3 rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad">
      {{ device.pairing_error }}
    </p>

    <!-- pair form -->
    <form v-if="showForm" class="mt-3 space-y-2" novalidate @submit.prevent="pair">
      <div class="grid gap-2 sm:grid-cols-2">
        <label class="block space-y-1">
          <span class="text-xs text-muted">Alias</span>
          <input v-model.trim="form.alias" class="input font-mono" autocomplete="off" autocapitalize="off" spellcheck="false"
                 :aria-invalid="!!aliasProblem" :aria-describedby="`hk-alias-help-${device.device_id}`" />
        </label>
        <label class="block space-y-1">
          <span class="text-xs text-muted">Unit</span>
          <select v-model="form.unit" class="input">
            <option value="" disabled>Choose a unit</option>
            <option v-for="u in units" :key="u.key" :value="u.key">{{ u.name }}</option>
          </select>
        </label>
      </div>
      <p :id="`hk-alias-help-${device.device_id}`" class="text-xs" :class="aliasProblem ? 'text-bad' : 'text-muted'">
        {{ aliasProblem || 'A short name for this pairing, such as main_floor.' }}
      </p>
      <div class="flex flex-wrap gap-2">
        <button type="submit" class="btn btn-primary" :disabled="busy || !!aliasProblem || !form.unit">{{ busy ? 'Starting…' : 'Start pairing' }}</button>
        <button type="button" class="btn" :disabled="busy" @click="showForm = false">Cancel</button>
      </div>
    </form>

    <!-- actions -->
    <div v-else-if="state !== 'awaiting_code' && state !== 'requested' && state !== 'code_submitted' && state !== 'unpair_requested'"
         class="mt-3 flex flex-wrap items-center gap-2">
      <button v-if="canPair" type="button" class="btn" :disabled="!secretsOk" @click="openForm">
        {{ state === 'failed' ? 'Try again' : 'Pair' }}
      </button>
      <button v-if="state === 'paired'" type="button" class="btn" :disabled="busy" @click="unpair">{{ busy ? 'Unpairing…' : 'Unpair' }}</button>
      <p v-if="!device.unpaired && state !== 'paired'" class="text-xs text-muted">
        Paired with another controller. Remove it from Apple Home first (or choose Disconnect from HomeKit on the thermostat).
      </p>
    </div>

    <p v-if="error" role="alert" class="mt-2 text-sm text-bad">{{ error }}</p>
  </li>
</template>
