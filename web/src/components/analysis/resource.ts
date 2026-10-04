// A loadable value for the stores: data + loading + error, where only the newest request
// may write (a slow response for an old period never overwrites the current one).
import { shallowReactive } from 'vue'
import { errorText } from './stats'

export interface Resource<T> {
  data: T | null
  loading: boolean
  error: string
  /** Identifies what `data` was loaded for (e.g. "2026-09-20..2026-10-03"). */
  key: string | null
  seq: number
}

export function resource<T>(): Resource<T> {
  return shallowReactive<Resource<T>>({ data: null, loading: false, error: '', key: null, seq: 0 })
}

/** Run `fn` into `r`. Resolves to the data, or null on error (the error is stored on `r`). */
export async function loadInto<T>(r: Resource<T>, key: string, fn: () => Promise<T>): Promise<T | null> {
  const seq = ++r.seq
  r.loading = true
  r.error = ''
  if (r.key !== key) r.data = null
  r.key = key
  try {
    const data = await fn()
    if (seq === r.seq) r.data = data
    return data
  } catch (e) {
    if (seq === r.seq) r.error = errorText(e)
    return null
  } finally {
    if (seq === r.seq) r.loading = false
  }
}
