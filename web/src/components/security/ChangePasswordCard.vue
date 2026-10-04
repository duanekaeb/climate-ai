<script setup lang="ts">
// Change the owner password. Needs the current one; every other signed-in device is signed
// out (this one stays signed in), and the change is written to the audit log.
import { ref } from 'vue'
import Card from '@/components/Card.vue'
import { PASSWORD_MIN_LENGTH, authErrorText } from '@/lib/device'
import { useSecurity } from '@/stores/security'

const security = useSecurity()

const current = ref('')
const next = ref('')
const again = ref('')
const busy = ref(false)
const error = ref('')
const done = ref('')

/** What is wrong with the form, or '' when it can be sent. */
function problem(): string {
  if (!current.value) return 'Enter your current password.'
  if (next.value.length < PASSWORD_MIN_LENGTH) return `Use at least ${PASSWORD_MIN_LENGTH} characters for the new password.`
  if (next.value !== again.value) return 'The two new passwords do not match.'
  if (next.value === current.value) return 'The new password is the same as the current one.'
  return ''
}

async function submit() {
  done.value = ''
  error.value = problem()
  if (error.value) return
  busy.value = true
  try {
    await security.changePassword(current.value, next.value)
    current.value = ''
    next.value = ''
    again.value = ''
    done.value = 'Password changed. Every other device was signed out; this one stays signed in.'
  } catch (e) {
    error.value = authErrorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Card title="Password" subtitle="The one login for this house, used on every device.">
    <form class="space-y-3" novalidate @submit.prevent="submit">
      <!-- Lets password managers pair the new password with this site's login. -->
      <input type="text" name="username" value="owner" autocomplete="username" class="sr-only" tabindex="-1" aria-hidden="true" readonly />
      <div class="space-y-1">
        <label for="pw-current" class="text-sm font-medium">Current password</label>
        <input id="pw-current" v-model="current" class="input" type="password" maxlength="200" autocomplete="current-password"
               aria-describedby="pw-error" />
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <div class="space-y-1">
          <label for="pw-new" class="text-sm font-medium">New password</label>
          <input id="pw-new" v-model="next" class="input" type="password" maxlength="200" :minlength="PASSWORD_MIN_LENGTH"
                 autocomplete="new-password" aria-describedby="pw-help pw-error" />
        </div>
        <div class="space-y-1">
          <label for="pw-again" class="text-sm font-medium">Type it again</label>
          <input id="pw-again" v-model="again" class="input" type="password" maxlength="200" :minlength="PASSWORD_MIN_LENGTH"
                 autocomplete="new-password" aria-describedby="pw-error" />
        </div>
      </div>
      <p id="pw-help" class="text-xs text-muted">At least {{ PASSWORD_MIN_LENGTH }} characters. Changing it signs out every other device.</p>
      <p id="pw-error" role="alert" :class="error ? 'rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad' : 'sr-only'">{{ error }}</p>
      <p v-if="done" role="status" class="rounded-xl border border-good/40 bg-good/10 p-2.5 text-sm text-good">{{ done }}</p>
      <button type="submit" class="btn btn-primary" :disabled="busy">{{ busy ? 'Saving…' : 'Change password' }}</button>
    </form>
  </Card>
</template>
