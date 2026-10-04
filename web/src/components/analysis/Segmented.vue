<script setup lang="ts" generic="T extends string | number">
// A compact segmented control (period pickers, tabs inside a view). Wraps onto a second row
// rather than scrolling sideways on a narrow phone.
defineProps<{ options: { value: T; label: string }[]; label: string; disabled?: boolean }>()
const model = defineModel<T>({ required: true })
</script>

<template>
  <div role="radiogroup" :aria-label="label" class="inline-flex flex-wrap gap-1 rounded-xl bg-surface-2 p-1">
    <button
      v-for="o in options"
      :key="String(o.value)"
      type="button"
      role="radio"
      :aria-checked="model === o.value"
      :disabled="disabled"
      class="rounded-lg px-2.5 py-1 text-xs font-medium whitespace-nowrap disabled:opacity-50"
      :class="model === o.value ? 'bg-surface text-ink shadow-sm' : 'text-muted hover:text-ink'"
      @click="model = o.value"
    >
      {{ o.label }}
    </button>
  </div>
</template>
