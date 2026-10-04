<script setup lang="ts">
// The active policy, read-only. Next to each setting: the range the API accepts and the range
// inside which Claude may sign off a model-proposed change without you.
import { computed } from 'vue'
import type { SettingsOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { POLICY_KEYS, formatParam, paramMeta } from '@/components/model/policyMeta'

const props = defineProps<{ settings: SettingsOut }>()

const rows = computed(() =>
  POLICY_KEYS.map((k) => {
    const m = paramMeta(k)
    const r = props.settings.signoff_ranges[k]
    const ownerOnly = props.settings.owner_only_params.includes(k)
    const claude =
      ownerOnly
        ? 'owner only'
        : r && typeof r[0] === 'number' && typeof r[1] === 'number'
          ? `${formatParam(k, r[0])}–${formatParam(k, r[1])}`
          : 'owner only'
    return {
      key: k,
      label: m.label,
      help: m.help,
      value: formatParam(k, props.settings.policy[k]),
      allowed: m.kind === 'boolean' || m.min === null || m.max === null ? 'on / off' : `${formatParam(k, m.min)}–${formatParam(k, m.max)}`,
      claude,
    }
  }),
)
</script>

<template>
  <Card
    title="Policy settings"
    :subtitle="`Version ${settings.policy_version_id ?? 'default'} · changes go through proposals, never edited here`"
  >
    <ul class="divide-y divide-line">
      <li v-for="r in rows" :key="r.key" class="py-2">
        <div class="flex items-baseline justify-between gap-2">
          <span class="min-w-0 text-sm font-medium">{{ r.label }}</span>
          <span class="num shrink-0 text-sm font-semibold">{{ r.value }}</span>
        </div>
        <p class="text-xs text-muted">{{ r.help }}</p>
        <p class="num mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-muted">
          <span>allowed {{ r.allowed }}</span>
          <span :class="r.claude === 'owner only' ? 'text-warn' : ''">Claude may sign off: {{ r.claude }}</span>
        </p>
      </li>
    </ul>
  </Card>
</template>
