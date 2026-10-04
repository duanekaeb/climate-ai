<script setup lang="ts">
// Ask Claude a question about the house. It is queued and answered by the agent service
// (on the owner's subscription), usually within a minute or two.
import { computed, ref, useId } from 'vue'
import type { AgentRunOut } from '@/api/types'
import { errorText } from '@/components/analysis/stats'
import { useAgent } from '@/stores/agent'

defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ asked: [run: AgentRunOut] }>()
const agent = useAgent()
const uid = useId()

const EXAMPLES = [
  'Is Linked floors working, or was it just cooler?',
  'Why did the house run so much yesterday?',
  'Does the main floor affect the bed wing?',
]
const MAX = 4000

const question = ref('')
const busy = ref(false)
const error = ref('')
const valid = computed(() => question.value.trim().length >= 2 && question.value.length <= MAX)

async function send() {
  if (!valid.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const run = await agent.ask(question.value.trim())
    question.value = ''
    emit('asked', run)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="space-y-2" @submit.prevent="send">
    <label :for="`${uid}-q`" class="sr-only">Your question</label>
    <textarea
      :id="`${uid}-q`"
      v-model="question"
      rows="3"
      :maxlength="MAX"
      class="input"
      placeholder="Ask about runtime, comfort, a decision the controller made…"
      :disabled="disabled"
      @keydown.meta.enter.prevent="send"
      @keydown.ctrl.enter.prevent="send"
    />
    <div class="flex flex-wrap gap-1.5">
      <button
        v-for="q in EXAMPLES"
        :key="q"
        type="button"
        class="chip bg-surface-2 py-1 text-left text-muted hover:text-ink"
        :disabled="disabled"
        @click="question = q"
      >
        {{ q }}
      </button>
    </div>
    <div class="flex items-center justify-between gap-2">
      <span class="num text-xs text-muted">{{ question.length }} / {{ MAX }}</span>
      <button type="submit" class="btn btn-primary" :disabled="disabled || !valid || busy">
        {{ busy ? 'Sending…' : 'Ask' }}
      </button>
    </div>
    <p v-if="error" class="text-sm text-bad">{{ error }}</p>
  </form>
</template>
