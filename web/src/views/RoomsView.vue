<script setup lang="ts">
// Rooms: all eleven rooms by floor. Tapping one opens its history: a drawer on phones and
// tablets, a side panel on desktop. The open room lives in ?room= so Live can link to it.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import Icon from '@/components/Icon.vue'
import RoomCard from '@/components/rooms/RoomCard.vue'
import RoomDetail from '@/components/rooms/RoomDetail.vue'
import { groupByFloor } from '@/stores/rooms'
import { useStatus } from '@/stores/status'

const status = useStatus()
const route = useRoute()
const router = useRouter()

onMounted(() => status.start())

const data = computed(() => status.data)
const floors = computed(() => groupByFloor(data.value?.rooms ?? []))
const tz = computed(() => data.value?.tz ?? Intl.DateTimeFormat().resolvedOptions().timeZone)
const counts = computed(() => {
  const rooms = data.value?.rooms ?? []
  return {
    total: rooms.length,
    inUse: rooms.filter((r) => r.state === 'occupied' || r.state === 'asleep').length,
    stale: rooms.filter((r) => r.stale).length,
    noSensor: rooms.filter((r) => !r.has_sensor).length,
  }
})

const selectedKey = computed(() => {
  const q = route.query.room
  return typeof q === 'string' ? q : ''
})
const selected = computed(() => data.value?.rooms.find((r) => r.room_key === selectedKey.value) ?? null)

// Desktop gets the in-page side panel; anything narrower gets the modal drawer.
const wideQuery = window.matchMedia('(min-width: 1024px)')
const isWide = ref(wideQuery.matches)
const onWide = (e: MediaQueryListEvent) => (isWide.value = e.matches)
wideQuery.addEventListener('change', onWide)

const modalOpen = computed(() => !!selected.value && !isWide.value)
const dialog = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null

function select(key: string) {
  if (key === selectedKey.value) return close()
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  router.replace({ query: { ...route.query, room: key } })
}

function close() {
  const query = { ...route.query }
  delete query.room
  router.replace({ query })
  const el = returnFocus
  returnFocus = null
  nextTick(() => el?.focus())
}

function onKeydown(e: KeyboardEvent) {
  if (!selected.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
    return
  }
  // Keep Tab inside the drawer while it is modal.
  if (e.key === 'Tab' && modalOpen.value && dialog.value) {
    const items = [...dialog.value.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(
      (el) => !el.hasAttribute('disabled'),
    )
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}
document.addEventListener('keydown', onKeydown)

// No background scrolling behind the drawer.
watch(modalOpen, (open) => {
  document.body.style.overflow = open ? 'hidden' : ''
}, { immediate: true })

onBeforeUnmount(() => {
  wideQuery.removeEventListener('change', onWide)
  document.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})
</script>

<template>
  <div>
    <AsyncState :loading="!status.data && !status.error" :error="status.data ? '' : status.error"
                :empty="!!status.data && !status.data.rooms.length" empty-text="No rooms reported yet.">
      <div v-if="data" class="lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)] lg:items-start lg:gap-4">
        <div class="min-w-0 space-y-5">
          <p class="text-sm text-muted">
            <span class="num">{{ counts.total }}</span> rooms · <span class="num">{{ counts.inUse }}</span> in use
            <template v-if="counts.stale"> · <span class="num text-warn">{{ counts.stale }} stale</span></template>
            · <span class="num">{{ counts.noSensor }}</span> without a sensor
          </p>
          <p v-if="status.error" role="status" class="text-xs text-warn">Couldn't refresh: {{ status.error }}</p>

          <section v-for="f in floors" :key="f.key" :aria-labelledby="`floor-${f.key}`">
            <h2 :id="`floor-${f.key}`" class="card-title mb-2">{{ f.label }}</h2>
            <div class="grid gap-3 sm:grid-cols-2">
              <RoomCard v-for="r in f.rooms" :key="r.room_key" :room="r" :tz="tz"
                        :selected="r.room_key === selectedKey" @select="select(r.room_key)" />
            </div>
          </section>
        </div>

        <aside v-if="isWide" id="room-detail" aria-labelledby="room-detail-title"
               class="card sticky top-20 max-h-[calc(100dvh-6rem)] overflow-y-auto">
          <RoomDetail v-if="selected" :key="selected.room_key" :room="selected" :tz="tz" @close="close" />
          <div v-else class="py-10 text-center text-sm text-muted">
            <Icon name="rooms" :size="28" class="mx-auto mb-2" />
            Pick a room to see its temperature, setpoints and occupancy over time.
          </div>
        </aside>
      </div>
    </AsyncState>

    <Teleport to="body">
      <div v-if="modalOpen && selected" class="fixed inset-0 z-40">
        <div class="absolute inset-0 bg-black/40" aria-hidden="true" @click="close" />
        <div id="room-detail" ref="dialog" role="dialog" aria-modal="true" aria-labelledby="room-detail-title"
             class="absolute inset-x-0 bottom-0 max-h-[88dvh] overflow-x-hidden overflow-y-auto rounded-t-2xl border-t border-line bg-surface p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] shadow-xl md:inset-y-0 md:right-0 md:left-auto md:max-h-none md:w-[28rem] md:rounded-none md:rounded-l-2xl md:border-t-0 md:border-l">
          <div class="mx-auto mb-3 h-1 w-10 rounded-full bg-line md:hidden" aria-hidden="true" />
          <RoomDetail :key="selected.room_key" :room="selected" :tz="tz" autofocus @close="close" />
        </div>
      </div>
    </Teleport>
  </div>
</template>
