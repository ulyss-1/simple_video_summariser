import { useCallback, useEffect, useState } from 'react'

// Theme helper (#211). The stored choice is 'light' | 'dark'; no stored choice
// means "follow the OS". CSS does the rest through [data-theme] on <html>.
export type Theme = 'light' | 'dark'

// Keep in step with public/theme-init.js.
export const THEME_STORAGE_KEY = 'theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

export function readStoredTheme(): Theme | null {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Storage blocked: the choice still applies for this page view.
  }
}

function osMedia(): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(DARK_QUERY) : null
}

export function systemTheme(): Theme {
  return osMedia()?.matches ? 'dark' : 'light'
}

export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const [chosen, setChosen] = useState<Theme | null>(readStoredTheme)
  const [system, setSystem] = useState<Theme>(systemTheme)

  useEffect(() => {
    const mq = osMedia()
    if (!mq) return
    const onChange = () => setSystem(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const theme = chosen ?? system

  useEffect(() => {
    if (chosen) document.documentElement.setAttribute('data-theme', chosen)
    else document.documentElement.removeAttribute('data-theme')
  }, [chosen])

  const toggleTheme = useCallback(() => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    storeTheme(next)
    setChosen(next)
  }, [theme])

  return { theme, toggleTheme }
}
