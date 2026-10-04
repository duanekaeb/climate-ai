// Editable copy of one settings section. The draft follows the server value until the owner
// edits it; after a save, `reset()` adopts what the server stored.
import { computed, getCurrentScope, onScopeDispose, ref, watch, type ComputedRef, type Ref } from 'vue'
import { errorText } from '@/components/analysis/stats'

function clone<T>(v: T): T {
  return v === undefined ? v : (JSON.parse(JSON.stringify(v)) as T)
}

export interface Draft<T> {
  draft: Ref<T>
  dirty: ComputedRef<boolean>
  reset: () => void
}

// The dirty flags of every mounted draft, so a page can tell whether the owner is editing
// anything before it refreshes the settings underneath.
const mounted = new Set<ComputedRef<boolean>>()

/** True while any mounted settings form has unsaved edits. */
export function anyDraftDirty(): boolean {
  for (const dirty of mounted) if (dirty.value) return true
  return false
}

export function useDraft<T>(source: () => T): Draft<T> {
  const draft = ref(clone(source())) as Ref<T>
  const snapshot = ref(JSON.stringify(source()))
  const dirty = computed(() => JSON.stringify(draft.value) !== snapshot.value)
  if (getCurrentScope()) {
    mounted.add(dirty)
    onScopeDispose(() => mounted.delete(dirty))
  }

  watch(
    source,
    (v) => {
      if (!dirty.value) draft.value = clone(v)
      snapshot.value = JSON.stringify(v)
    },
    { deep: true },
  )

  function reset() {
    draft.value = clone(source())
    snapshot.value = JSON.stringify(source())
  }

  return { draft, dirty, reset }
}

/** busy / error / saved state around one save call. */
export function useSaver() {
  const busy = ref(false)
  const error = ref('')
  const saved = ref(false)

  async function run(fn: () => Promise<unknown>): Promise<boolean> {
    if (busy.value) return false
    busy.value = true
    error.value = ''
    saved.value = false
    try {
      await fn()
      saved.value = true
      return true
    } catch (e) {
      error.value = errorText(e)
      return false
    } finally {
      busy.value = false
    }
  }

  return { busy, error, saved, run }
}
