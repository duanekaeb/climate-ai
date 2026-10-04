// Light / dark theme. It follows the device setting live (public/theme.js applies it before
// the first paint) unless the owner picks one with the sun / moon button; that choice is
// remembered on this device until "Use device setting" clears it.
import { reactive } from 'vue'

const KEY = 'climate.theme'
export type ThemeChoice = 'light' | 'dark' | null

const media: MediaQueryList | null =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null

function readChoice(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'dark' || v === 'light' ? v : null
  } catch {
    return null // private mode / blocked storage
  }
}

export const theme = reactive({
  /** Dark mode is on right now. */
  dark: false,
  /** The owner's explicit pick, or null when following the device. */
  choice: readChoice() as ThemeChoice,
})

function apply() {
  const dark = theme.choice ? theme.choice === 'dark' : (media?.matches ?? false)
  theme.dark = dark
  document.documentElement.classList.toggle('dark', dark)
}

/** Pick light or dark explicitly (remembered), or null to follow the device again. */
export function setTheme(choice: ThemeChoice): void {
  theme.choice = choice
  try {
    if (choice) localStorage.setItem(KEY, choice)
    else localStorage.removeItem(KEY)
  } catch {
    /* private mode: the choice lasts for this visit */
  }
  apply()
}

export function toggleTheme(): void {
  setTheme(theme.dark ? 'light' : 'dark')
}

if (typeof document !== 'undefined') {
  apply()
  media?.addEventListener('change', () => {
    if (!theme.choice) apply()
  })
}
