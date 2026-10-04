<script setup lang="ts">
// Save / Undo row for a settings section, with the result of the last save.
defineProps<{ dirty: boolean; valid: boolean; busy: boolean; error: string; saved: boolean; disabled?: boolean }>()
const emit = defineEmits<{ save: []; reset: [] }>()
</script>

<template>
  <div class="mt-3 flex flex-wrap items-center gap-2">
    <button type="button" class="btn btn-primary" :disabled="disabled || !dirty || !valid || busy" @click="emit('save')">
      {{ busy ? 'Saving…' : 'Save' }}
    </button>
    <button v-if="dirty" type="button" class="btn" :disabled="busy" @click="emit('reset')">Undo changes</button>
    <span v-if="error" class="text-sm text-bad">{{ error }}</span>
    <span v-else-if="saved && !dirty" class="text-sm text-good">Saved.</span>
    <span v-else-if="dirty" class="text-xs text-muted">Unsaved changes</span>
  </div>
</template>
