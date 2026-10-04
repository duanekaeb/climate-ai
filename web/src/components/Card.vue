<script setup lang="ts">
defineProps<{ title?: string; subtitle?: string }>()
</script>

<template>
  <section class="card min-w-0">
    <header v-if="title || $slots.actions" class="mb-3 flex items-start justify-between gap-3">
      <div class="min-w-0">
        <h2 v-if="title" class="card-title">{{ title }}</h2>
        <p v-if="subtitle" class="mt-0.5 text-sm text-muted">{{ subtitle }}</p>
      </div>
      <div class="shrink-0"><slot name="actions" /></div>
    </header>
    <slot />
  </section>
</template>
