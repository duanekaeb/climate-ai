// Busy / error / done state for one owner action in Setup.
import { ref } from 'vue'
import { ApiError } from '@/api/client'

export function errorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) return 'Only the owner can change setup.'
  return e instanceof Error ? e.message : String(e)
}

export function useAction() {
  const busy = ref(false)
  const error = ref('')
  const done = ref('')

  async function run(fn: () => Promise<unknown>, okText = ''): Promise<boolean> {
    busy.value = true
    error.value = ''
    done.value = ''
    try {
      await fn()
      done.value = okText
      return true
    } catch (e) {
      error.value = errorText(e)
      return false
    } finally {
      busy.value = false
    }
  }

  return { busy, error, done, run }
}
