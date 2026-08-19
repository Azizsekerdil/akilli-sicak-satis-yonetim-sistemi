/**
 * Ctrl+K command palette.
 *
 * Two result kinds: screens the user is allowed to open, and live records
 * (customers, products) fetched from the API as they type.
 */
import clsx from 'clsx'
import { CornerDownLeft, Package, Search, Store, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { NAV_GROUPS } from './Layout'

interface Hit {
  id: string
  label: string
  sub?: string
  to: string
  kind: 'screen' | 'customer' | 'product'
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [remote, setRemote] = useState<Hit[]>([])
  const [cursor, setCursor] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      setQuery('')
      setRemote([])
      setCursor(0)
      setTimeout(() => inputRef.current?.focus(), 20)
    }
  }, [open])

  const screens = useMemo<Hit[]>(
    () =>
      NAV_GROUPS.flatMap((g) => g.items)
        .filter((i) => can(i.resource))
        .map((i) => ({
          id: `s:${i.to}`,
          label: t(i.labelKey),
          to: i.to,
          kind: 'screen' as const,
        })),
    [can, t],
  )

  // Debounced record lookup — the palette stays usable while typing.
  useEffect(() => {
    if (!open || query.trim().length < 2) {
      setRemote([])
      return
    }
    const ctrl = new AbortController()
    const timer = setTimeout(async () => {
      const hits: Hit[] = []
      try {
        if (can('crm.customers')) {
          const r = await api.get<Paged<any>>(
            '/customers',
            { term: query, size: 5 },
            ctrl.signal,
          )
          hits.push(
            ...(r.items ?? []).map((c) => ({
              id: `c:${c.id}`,
              label: c.trade_name || c.name,
              sub: c.code,
              to: `/crm/customers/${c.id}`,
              kind: 'customer' as const,
            })),
          )
        }
      } catch {
        /* palette must never block on a failing lookup */
      }
      try {
        if (can('stock.products')) {
          const r = await api.get<Paged<any>>(
            '/products',
            { term: query, size: 5 },
            ctrl.signal,
          )
          hits.push(
            ...(r.items ?? []).map((p) => ({
              id: `p:${p.id}`,
              label: p.name,
              sub: p.sku,
              to: `/stock/products/${p.id}`,
              kind: 'product' as const,
            })),
          )
        }
      } catch {
        /* ignore */
      }
      setRemote(hits)
    }, 250)

    return () => {
      ctrl.abort()
      clearTimeout(timer)
    }
  }, [query, open, can])

  const results = useMemo(() => {
    const q = query.trim().toLocaleLowerCase('tr-TR')
    const matched = q
      ? screens.filter((s) => s.label.toLocaleLowerCase('tr-TR').includes(q))
      : screens.slice(0, 8)
    return [...matched, ...remote].slice(0, 14)
  }, [query, screens, remote])

  useEffect(() => setCursor(0), [results.length])

  if (!open) return null

  const go = (hit: Hit) => {
    navigate(hit.to)
    onClose()
  }

  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center bg-shell-950/40 p-4 pt-[10vh] backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl animate-slide-up overflow-hidden rounded-xl bg-white shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 border-b border-shell-200 px-4">
          <Search className="h-4 w-4 shrink-0 text-shell-400" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setCursor((c) => Math.min(c + 1, results.length - 1))
              } else if (e.key === 'ArrowUp') {
                e.preventDefault()
                setCursor((c) => Math.max(c - 1, 0))
              } else if (e.key === 'Enter' && results[cursor]) {
                e.preventDefault()
                go(results[cursor])
              } else if (e.key === 'Escape') {
                onClose()
              }
            }}
            placeholder={t('common.searchPlaceholder')}
            className="flex-1 bg-transparent py-3.5 text-sm outline-none placeholder:text-shell-400"
          />
          <button type="button" className="text-shell-400 hover:text-shell-700" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </div>

        <ul className="max-h-80 overflow-y-auto py-1.5">
          {results.length === 0 && (
            <li className="px-4 py-8 text-center text-sm text-shell-400">
              {t('common.noResults')}
            </li>
          )}
          {results.map((hit, i) => (
            <li key={hit.id}>
              <button
                type="button"
                onMouseEnter={() => setCursor(i)}
                onClick={() => go(hit)}
                className={clsx(
                  'flex w-full items-center gap-2.5 px-4 py-2.5 text-left text-sm',
                  i === cursor ? 'bg-brand-50 text-brand-900' : 'text-shell-700',
                )}
              >
                {hit.kind === 'customer' ? (
                  <Store className="h-4 w-4 text-shell-400" />
                ) : hit.kind === 'product' ? (
                  <Package className="h-4 w-4 text-shell-400" />
                ) : (
                  <CornerDownLeft className="h-4 w-4 text-shell-400" />
                )}
                <span className="flex-1 truncate">{hit.label}</span>
                {hit.sub && <span className="text-2xs text-shell-400">{hit.sub}</span>}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
