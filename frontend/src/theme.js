import { useEffect, useState } from 'react'

// Theme resolution: a manual choice saved in localStorage wins; otherwise
// follow the OS via prefers-color-scheme.
const STORAGE_KEY = 'theme'

const osDark = () => window.matchMedia('(prefers-color-scheme: dark)')

export function currentTheme() {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return osDark().matches ? 'dark' : 'light'
}

export function applyTheme() {
  const theme = currentTheme()
  document.documentElement.dataset.theme = theme
  return theme
}

export function useTheme() {
  const [theme, setTheme] = useState(currentTheme)

  useEffect(() => {
    const query = osDark()
    const onChange = () => setTheme(applyTheme())
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem(STORAGE_KEY, next)
    document.documentElement.dataset.theme = next
    setTheme(next)
  }

  return [theme, toggle]
}
