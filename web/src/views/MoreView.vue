<script setup lang="ts">
// More (the phone tab): every secondary screen, theme info and sign out.
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import { setTheme, theme } from '@/lib/theme'
import { routes } from '@/router'
import { useAuth } from '@/stores/auth'

const auth = useAuth()
const router = useRouter()

const links = routes.filter((r) => r.meta?.nav === 'more')

const busy = ref(false)
const error = ref('')
async function signOut() {
  busy.value = true
  error.value = ''
  try {
    await auth.logout()
    await router.replace('/login')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <nav aria-label="More screens" class="card !p-0">
      <ul class="divide-y divide-line">
        <li v-for="r in links" :key="String(r.name)">
          <RouterLink :to="r.path" class="flex items-center gap-3 px-4 py-3.5 hover:bg-surface-2">
            <span class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-surface-2 text-muted">
              <Icon :name="String(r.meta?.icon)" />
            </span>
            <span class="min-w-0 flex-1 font-medium">{{ r.meta?.title }}</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                 stroke-linecap="round" class="shrink-0 text-muted" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
          </RouterLink>
        </li>
      </ul>
    </nav>

    <Card title="Appearance">
      <p class="flex items-center gap-2 text-sm">
        <Icon :name="theme.dark ? 'moon' : 'sun'" :size="18" class="text-muted" />
        <span><span class="font-medium">{{ theme.dark ? 'Dark' : 'Light' }}</span> mode is on.</span>
      </p>
      <p class="mt-1 text-sm text-muted">
        {{ theme.choice ? 'You picked it with the sun / moon button at the top of the screen, so it stays put when your device switches.' : 'It follows your device setting and switches when your device does. The sun / moon button at the top of the screen picks one.' }}
      </p>
      <button v-if="theme.choice" type="button" class="btn mt-3" @click="setTheme(null)">Use device setting</button>
    </Card>

    <Card title="Account">
      <p class="text-sm text-muted">
        Signed in as the owner on this device. Signed-in devices, the password and API tokens
        are under <RouterLink to="/security" class="text-accent underline">Security</RouterLink>.
      </p>
      <p v-if="error" role="alert" class="mt-2 text-sm text-bad">{{ error }}</p>
      <button type="button" class="btn mt-3" :disabled="busy" @click="signOut">{{ busy ? 'Signing out…' : 'Sign out' }}</button>
    </Card>
  </div>
</template>
