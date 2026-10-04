<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { routes } from '@/router'
import { useAuth } from '@/stores/auth'
import { useStatus } from '@/stores/status'
import Icon from '@/components/Icon.vue'

const route = useRoute()
const auth = useAuth()
const status = useStatus()
const dark = ref(document.documentElement.classList.contains('dark'))

const nav = routes.filter((r) => r.meta?.nav)
const primary = nav.filter((r) => r.meta?.nav === 'primary')
const showShell = computed(() => !route.meta.public)

function toggleTheme() {
  dark.value = !dark.value
  document.documentElement.classList.toggle('dark', dark.value)
  try {
    localStorage.setItem('climate.theme', dark.value ? 'dark' : 'light')
  } catch {
    /* private mode */
  }
}

// The router guard loads auth before the first page renders; start live status once signed in.
watch(
  () => auth.state?.authenticated,
  (signedIn) => {
    if (signedIn) status.start()
  },
  { immediate: true },
)
</script>

<template>
  <div v-if="!showShell" class="min-h-dvh"><RouterView /></div>
  <div v-else class="flex min-h-dvh">
    <!-- sidebar (tablet / desktop) -->
    <aside class="sticky top-0 hidden h-dvh w-56 shrink-0 flex-col border-r border-line bg-surface p-3 md:flex">
      <div class="mb-4 flex items-center gap-2 px-2 pt-1">
        <img src="/icon.svg" alt="" class="h-7 w-7" />
        <span class="font-semibold">Climate AI</span>
      </div>
      <nav class="flex flex-1 flex-col gap-0.5 overflow-y-auto">
        <RouterLink v-for="r in nav" :key="String(r.name)" :to="r.path"
          class="flex items-center gap-3 rounded-xl px-3 py-2 text-sm text-muted hover:bg-surface-2"
          active-class="!bg-surface-2 !text-ink font-medium">
          <Icon :name="String(r.meta?.icon)" />{{ r.meta?.title }}
        </RouterLink>
      </nav>
      <div class="flex items-center justify-between px-2 pt-2 text-xs text-muted">
        <button class="btn !px-2 !py-1" :title="dark ? 'Light mode' : 'Dark mode'" @click="toggleTheme">
          <Icon :name="dark ? 'sun' : 'moon'" :size="16" />
        </button>
        <button class="underline" @click="auth.logout().then(() => $router.push('/login'))">Sign out</button>
      </div>
    </aside>

    <div class="flex min-w-0 flex-1 flex-col">
      <header class="sticky top-0 z-10 flex items-center gap-3 border-b border-line bg-page/90 px-4 py-3 backdrop-blur">
        <h1 class="min-w-0 flex-1 truncate text-lg font-semibold">{{ route.meta.title }}</h1>
        <template v-if="status.data">
          <span class="chip bg-surface-2 text-muted" :title="`Data source: ${status.data.source.kind}`">
            <span class="h-2 w-2 rounded-full" :class="status.data.source.ok ? 'bg-good' : 'bg-bad'" />
            {{ status.data.source.kind === 'simulator' ? 'Simulator' : 'ecobee' }}
          </span>
          <span class="chip bg-surface-2 text-muted" title="Controller mode">{{ status.data.controller.mode }}</span>
        </template>
        <button class="btn !px-2 !py-1 md:hidden" @click="toggleTheme"><Icon :name="dark ? 'sun' : 'moon'" :size="16" /></button>
      </header>
      <main class="mx-auto w-full max-w-6xl flex-1 px-4 pt-4 pb-28 md:pb-8">
        <RouterView />
      </main>
    </div>

    <!-- bottom bar (phone) -->
    <nav class="fixed inset-x-0 bottom-0 z-20 grid grid-cols-5 border-t border-line bg-surface/95 backdrop-blur md:hidden"
         style="padding-bottom: env(safe-area-inset-bottom)">
      <RouterLink v-for="r in primary" :key="String(r.name)" :to="r.path"
        class="flex flex-col items-center gap-0.5 py-2 text-[11px] text-muted" active-class="!text-accent">
        <Icon :name="String(r.meta?.icon)" :size="22" />{{ r.meta?.title === 'Did it work?' ? 'Results' : r.meta?.title }}
      </RouterLink>
      <RouterLink to="/more" class="flex flex-col items-center gap-0.5 py-2 text-[11px] text-muted" active-class="!text-accent">
        <Icon name="more" :size="22" />More
      </RouterLink>
    </nav>
  </div>
</template>
