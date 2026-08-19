/**
 * i18next setup.
 *
 * Turkish is the default.  Nothing user-facing is hard-coded in components —
 * every string comes from `src/locales/{tr,en}.json`, and the backend returns
 * its own messages already translated (it receives the language on every call).
 */
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from '@/locales/en.json'
import tr from '@/locales/tr.json'
import { langStore, type Lang } from './api'

void i18n.use(initReactI18next).init({
  resources: {
    tr: { translation: tr },
    en: { translation: en },
  },
  lng: langStore.get(),
  fallbackLng: 'tr',
  supportedLngs: ['tr', 'en'],
  interpolation: { escapeValue: false },
  returnNull: false,
})

document.documentElement.lang = langStore.get()

export function setLanguage(lang: Lang): void {
  langStore.set(lang)
  void i18n.changeLanguage(lang)
  document.documentElement.lang = lang
}

export function currentLanguage(): Lang {
  return (i18n.language === 'en' ? 'en' : 'tr') as Lang
}

/** Picks the right field from a backend row that carries both languages. */
export function bilingual<T extends Record<string, unknown>>(
  row: T,
  base: string,
  lang?: Lang,
): string {
  const l = lang ?? currentLanguage()
  const value = row[`${base}_${l}`] ?? row[`${base}_tr`] ?? row[base]
  return typeof value === 'string' ? value : ''
}

export default i18n
