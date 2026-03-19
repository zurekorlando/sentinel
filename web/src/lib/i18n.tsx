/**
 * Lightweight i18n — zero external dependencies.
 * Provides LanguageProvider, useLanguage(), and useT() (translation hook).
 *
 * Usage:
 *   const { t, interp } = useT()
 *   t.nav.dashboard                           // simple string
 *   interp(t.jobs.confirmDelete, { id: 'x' }) // string with {{id}} placeholder
 */

import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import en, { type Translations } from '../locales/en'
import es from '../locales/es'

// ── Types ─────────────────────────────────────────────────────────────────────

export type Locale = 'en' | 'es'

interface LanguageContextValue {
  locale: Locale
  setLocale: (l: Locale) => void
}

// ── Context ───────────────────────────────────────────────────────────────────

const LanguageContext = createContext<LanguageContextValue>({
  locale: 'en',
  setLocale: () => undefined,
})

// ── Provider ──────────────────────────────────────────────────────────────────

const STORAGE_KEY = 'sentinel-locale'

const LOCALES: Record<Locale, Translations> = { en, es }

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'es' ? 'es' : 'en'
  })

  function setLocale(l: Locale) {
    localStorage.setItem(STORAGE_KEY, l)
    setLocaleState(l)
  }

  // Sync <html lang> attribute
  useEffect(() => {
    document.documentElement.setAttribute('lang', locale)
  }, [locale])

  return (
    <LanguageContext.Provider value={{ locale, setLocale }}>
      {children}
    </LanguageContext.Provider>
  )
}

// ── Hooks ─────────────────────────────────────────────────────────────────────

/** Returns the current locale and a setter. */
export function useLanguage() {
  return useContext(LanguageContext)
}

/**
 * Returns the translation dictionary for the active locale plus an
 * interpolation helper.
 *
 * @example
 *   const { t, interp } = useT()
 *   t.nav.jobs                                      // "Jobs" | "Trabajos"
 *   interp(t.jobs.confirmDelete, { id: 'nightly' }) // replaces {{id}}
 */
export function useT() {
  const { locale } = useContext(LanguageContext)
  const t = LOCALES[locale]

  function interp(template: string, vars: Record<string, string | number> = {}): string {
    return Object.entries(vars).reduce<string>(
      (s, [k, v]) => s.replaceAll(`{{${k}}}`, String(v)),
      template,
    )
  }

  return { t, interp, locale }
}
