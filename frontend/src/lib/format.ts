/**
 * Locale-aware formatting.
 *
 * Turkish uses "." as the thousands separator and "," as the decimal mark —
 * the exact opposite of English — so every number the user sees goes through
 * here rather than through `toFixed()`.
 */
import { langStore } from './api'

const localeOf = (lang?: string) => (lang ?? langStore.get()) === 'en' ? 'en-GB' : 'tr-TR'

export function formatMoney(
  value: number | string | null | undefined,
  opts: { currency?: string; lang?: string; decimals?: number; compact?: boolean } = {},
): string {
  const n = toNumber(value)
  const { currency = 'TRY', lang, decimals = 2, compact = false } = opts
  return new Intl.NumberFormat(localeOf(lang), {
    style: 'currency',
    currency,
    minimumFractionDigits: compact ? 0 : decimals,
    maximumFractionDigits: compact ? 1 : decimals,
    notation: compact ? 'compact' : 'standard',
  }).format(n)
}

export function formatNumber(
  value: number | string | null | undefined,
  opts: { decimals?: number; lang?: string; compact?: boolean } = {},
): string {
  const n = toNumber(value)
  const { decimals = 0, lang, compact = false } = opts
  return new Intl.NumberFormat(localeOf(lang), {
    minimumFractionDigits: compact ? 0 : decimals,
    maximumFractionDigits: compact ? 1 : decimals,
    notation: compact ? 'compact' : 'standard',
  }).format(n)
}

export function formatPercent(
  value: number | string | null | undefined,
  opts: { decimals?: number; lang?: string; sign?: boolean } = {},
): string {
  const n = toNumber(value)
  const { decimals = 1, lang, sign = false } = opts
  const s = new Intl.NumberFormat(localeOf(lang), {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    signDisplay: sign ? 'exceptZero' : 'auto',
  }).format(n)
  return `${s}%`
}

export function formatQuantity(
  value: number | string | null | undefined,
  uom?: string,
  lang?: string,
): string {
  const n = toNumber(value)
  const s = new Intl.NumberFormat(localeOf(lang), {
    maximumFractionDigits: Number.isInteger(n) ? 0 : 2,
  }).format(n)
  return uom ? `${s} ${uomLabel(uom, lang)}` : s
}

const UOM_LABELS: Record<string, [string, string]> = {
  PIECE: ['adet', 'pcs'],
  CASE: ['koli', 'case'],
  PACK: ['paket', 'pack'],
  PALLET: ['palet', 'pallet'],
  KILOGRAM: ['kg', 'kg'],
  GRAM: ['g', 'g'],
  LITRE: ['lt', 'L'],
  MILLILITRE: ['ml', 'ml'],
}

export function uomLabel(uom: string, lang?: string): string {
  const pair = UOM_LABELS[uom]
  if (!pair) return uom
  return (lang ?? langStore.get()) === 'en' ? pair[1] : pair[0]
}

export function formatDate(
  value: string | Date | null | undefined,
  opts: { lang?: string; withTime?: boolean; short?: boolean } = {},
): string {
  if (!value) return '—'
  const d = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const { lang, withTime = false, short = false } = opts
  return new Intl.DateTimeFormat(localeOf(lang), {
    day: '2-digit',
    month: short ? '2-digit' : 'short',
    year: 'numeric',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(d)
}

export function formatRelative(value: string | Date | null | undefined, lang?: string): string {
  if (!value) return '—'
  const d = value instanceof Date ? value : new Date(value)
  const diffMs = d.getTime() - Date.now()
  const abs = Math.abs(diffMs)
  const rtf = new Intl.RelativeTimeFormat(localeOf(lang), { numeric: 'auto' })
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['year', 31536e6], ['month', 2592e6], ['day', 864e5],
    ['hour', 36e5], ['minute', 6e4], ['second', 1000],
  ]
  for (const [unit, ms] of units) {
    if (abs >= ms || unit === 'second') return rtf.format(Math.round(diffMs / ms), unit)
  }
  return '—'
}

/** Days until a date; negative when already past. Used for expiry badges. */
export function daysUntil(value: string | Date | null | undefined): number | null {
  if (!value) return null
  const d = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(d.getTime())) return null
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  d.setHours(0, 0, 0, 0)
  return Math.round((d.getTime() - today.getTime()) / 864e5)
}

export function toNumber(value: number | string | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : 0
}

/** Trend direction for a KPI delta. */
export function trendOf(change: number | null | undefined): 'up' | 'down' | 'flat' {
  if (change === null || change === undefined) return 'flat'
  if (change > 0.5) return 'up'
  if (change < -0.5) return 'down'
  return 'flat'
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toLocaleUpperCase('tr-TR') ?? '')
    .join('')
}
