<script setup lang="ts">
// Loading / error / empty wrapper used by every view.
defineProps<{ loading?: boolean; error?: string | null; empty?: boolean; emptyText?: string }>()
</script>

<template>
  <div v-if="error" class="rounded-xl border border-bad/40 bg-bad/10 p-3 text-sm text-bad">{{ error }}</div>
  <div v-else-if="loading" class="animate-pulse p-6 text-center text-sm text-muted">Loading…</div>
  <div v-else-if="empty" class="p-6 text-center text-sm text-muted">{{ emptyText || 'Nothing yet.' }}</div>
  <slot v-else />
</template>
