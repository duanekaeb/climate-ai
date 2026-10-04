<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/lib/markdown'

const props = defineProps<{ source: string | null | undefined }>()
const html = computed(() => renderMarkdown(props.source))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitized by DOMPurify -->
  <div class="md text-sm leading-relaxed" v-html="html" />
</template>

<style scoped>
.md :deep(h1),
.md :deep(h2),
.md :deep(h3) { font-weight: 600; margin: 0.9em 0 0.4em; }
.md :deep(h1) { font-size: 1.15rem; }
.md :deep(h2) { font-size: 1.05rem; }
.md :deep(p) { margin: 0.5em 0; }
.md :deep(ul) { list-style: disc; padding-left: 1.25rem; margin: 0.5em 0; }
.md :deep(ol) { list-style: decimal; padding-left: 1.25rem; margin: 0.5em 0; }
.md :deep(table) { width: 100%; border-collapse: collapse; margin: 0.6em 0; font-variant-numeric: tabular-nums; display: block; overflow-x: auto; }
.md :deep(th), .md :deep(td) { border: 1px solid var(--color-line); padding: 0.3em 0.5em; text-align: left; }
.md :deep(code) { background: var(--color-surface-2); padding: 0.1em 0.3em; border-radius: 0.3em; }
.md :deep(a) { color: var(--color-accent); text-decoration: underline; }
</style>
