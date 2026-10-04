<script setup lang="ts">
// Propose a policy change. It goes through the same gates as the models' and Claude's
// proposals (backtest, shadow days, sign-off, trial) before it is active.
import { computed, ref, useId } from 'vue'
import type { SettingsOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { errorText } from '@/components/analysis/stats'
import ParamOverrides from '@/components/model/ParamOverrides.vue'
import { effectiveOverrides, validateOverrides, type ParamOverrides as Overrides } from '@/components/model/policyMeta'
import { useControl } from '@/stores/control'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()
const uid = useId()

const title = ref('')
const rationale = ref('')
const params = ref<Overrides>({})
const busy = ref(false)
const error = ref('')
const done = ref('')

const effective = computed(() => effectiveOverrides(params.value, props.settings.policy))
const problems = computed(() => {
  const out = validateOverrides(params.value)
  const t = title.value.trim()
  if (t.length < 3 || t.length > 200) out.push('Title: 3 to 200 characters.')
  if (!Object.keys(effective.value).length) out.push('Change at least one setting from its current value.')
  return out
})

async function submit() {
  if (problems.value.length || busy.value) return
  busy.value = true
  error.value = ''
  done.value = ''
  try {
    const c = await control.proposeChange({
      title: title.value.trim(),
      rationale: rationale.value.trim(),
      params: { ...effective.value },
    })
    done.value = `Proposed (#${c.id}, ${c.status.replace(/_/g, ' ')}).`
    title.value = ''
    rationale.value = ''
    params.value = {}
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Card title="Propose a policy change">
    <form class="space-y-3" novalidate @submit.prevent="submit">
      <fieldset :disabled="!isOwner" class="space-y-3">
        <legend class="sr-only">Proposal</legend>
        <div>
          <label :for="`${uid}-title`" class="text-sm font-medium">Title</label>
          <input :id="`${uid}-title`" v-model="title" class="input mt-1" maxlength="200" placeholder="Recovery lead 20 → 30 min" />
        </div>
        <div>
          <label :for="`${uid}-why`" class="text-sm font-medium">Why <span class="font-normal text-muted">(optional)</span></label>
          <textarea :id="`${uid}-why`" v-model="rationale" rows="2" maxlength="4000" class="input mt-1" />
        </div>
        <ParamOverrides v-model="params" :current="settings.policy" empty-text="Add the settings to change." />
      </fieldset>
      <ul v-if="problems.length && (title || Object.keys(params).length)" class="space-y-0.5 text-xs text-muted">
        <li v-for="p in problems" :key="p">{{ p }}</li>
      </ul>
      <button type="submit" class="btn btn-primary" :disabled="!isOwner || busy || problems.length > 0">
        {{ busy ? 'Proposing…' : 'Propose' }}
      </button>
      <p v-if="error" class="text-sm text-bad">{{ error }}</p>
      <p v-if="done" class="text-sm text-good">{{ done }}</p>
    </form>
  </Card>
</template>
