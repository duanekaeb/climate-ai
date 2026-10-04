<script setup lang="ts" generic="D extends string">
// Owner decision with a required reason (experiments, gated changes). The reason is stored
// with the decision and shown to Claude in its next review.
import { computed, ref, useId } from 'vue'
import { errorText } from '@/components/analysis/stats'

const props = defineProps<{
  options: { value: D; label: string; tone: 'primary' | 'danger' | 'plain' }[]
  submit: (decision: D, reason: string) => Promise<unknown>
  placeholder?: string
}>()

const uid = useId()
const reason = ref('')
const busy = ref<D | null>(null)
const error = ref('')
const done = ref('')
const valid = computed(() => reason.value.trim().length >= 3)

async function go(d: D, label: string) {
  if (!valid.value || busy.value) return
  busy.value = d
  error.value = ''
  done.value = ''
  try {
    await props.submit(d, reason.value.trim())
    done.value = `${label}: recorded.`
    reason.value = ''
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = null
  }
}

const TONE = { primary: 'btn btn-primary', danger: 'btn btn-danger', plain: 'btn' } as const
</script>

<template>
  <div class="space-y-2">
    <label :for="`${uid}-reason`" class="text-xs text-muted">Reason (required, kept with the decision)</label>
    <textarea
      :id="`${uid}-reason`"
      v-model="reason"
      rows="2"
      maxlength="2000"
      class="input"
      :placeholder="placeholder ?? 'Why?'"
    />
    <div class="flex flex-wrap gap-2">
      <button
        v-for="o in options"
        :key="o.value"
        type="button"
        :class="TONE[o.tone]"
        :disabled="!valid || busy !== null"
        @click="go(o.value, o.label)"
      >
        {{ busy === o.value ? 'Saving…' : o.label }}
      </button>
    </div>
    <p v-if="!valid && reason.length > 0" class="text-xs text-muted">At least 3 characters.</p>
    <p v-if="error" class="text-xs text-bad">{{ error }}</p>
    <p v-if="done" class="text-xs text-good">{{ done }}</p>
  </div>
</template>
