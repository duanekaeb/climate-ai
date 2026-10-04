<script setup lang="ts">
// The one and only time a new API token is shown. The server keeps only a keyed hash of it,
// so it can never be shown again; "Done" drops it from this page's memory.
import { nextTick, onMounted, ref } from 'vue'
import type { ApiTokenCreated } from '@/api/types'
import Icon from '@/components/Icon.vue'
import { ROLE_LABEL } from './tokenRoles'

const props = defineProps<{ token: ApiTokenCreated }>()
const emit = defineEmits<{ done: [] }>()

const field = ref<HTMLInputElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const copied = ref(false)
const copyError = ref('')

function selectAll() {
  const el = field.value
  if (!el) return
  el.focus()
  el.setSelectionRange(0, el.value.length) // iOS ignores select() on read-only inputs
}

async function copy() {
  copyError.value = ''
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(props.token.token)
    } else {
      // Plain http on the home network has no Clipboard API: copy the selection instead.
      selectAll()
      if (!document.execCommand('copy')) throw new Error('copy refused')
    }
    copied.value = true
  } catch {
    selectAll()
    copyError.value = 'Copying did not work here. The token is selected: copy it by hand.'
  }
}

onMounted(() => nextTick(() => panel.value?.focus()))
</script>

<template>
  <section ref="panel" tabindex="-1" role="region" aria-labelledby="reveal-title"
           class="space-y-3 rounded-xl border border-warn/50 bg-warn/10 p-3 outline-none">
    <div class="flex items-start gap-2">
      <Icon name="key" :size="18" class="mt-0.5 shrink-0 text-warn" />
      <div class="min-w-0">
        <h3 id="reveal-title" class="font-medium">Copy “{{ token.name }}” now</h3>
        <p class="mt-0.5 text-sm">
          This is the only time the token is shown. Climate AI keeps just a fingerprint of it, so
          if you lose it, revoke it and create another. Anyone holding it can act as
          “{{ ROLE_LABEL[token.role] }}”{{ token.local_only ? ' from your home network' : ' from anywhere' }}.
        </p>
      </div>
    </div>
    <div class="flex gap-2">
      <label for="reveal-token" class="sr-only">New API token</label>
      <input id="reveal-token" ref="field" :value="token.token" readonly spellcheck="false" autocomplete="off"
             class="input min-w-0 flex-1 font-mono text-xs" @focus="selectAll" />
      <button type="button" class="btn shrink-0" @click="copy">
        <Icon :name="copied ? 'check' : 'copy'" :size="16" />{{ copied ? 'Copied' : 'Copy' }}
      </button>
    </div>
    <p v-if="copyError" role="alert" class="text-sm text-bad">{{ copyError }}</p>
    <p class="text-xs text-muted">
      Send it as <code class="font-mono">Authorization: Bearer &lt;token&gt;</code>. For the Claude agent,
      put it in the server's <code class="font-mono">.env</code> as <code class="font-mono">CLIMATE_AGENT_TOKEN</code>
      (the MCP server reads <code class="font-mono">CLIMATE_MCP_TOKEN</code>, or falls back to the agent's).
    </p>
    <div class="flex justify-end">
      <button type="button" class="btn btn-primary" @click="emit('done')">I've saved it</button>
    </div>
  </section>
</template>
