/**
 * Shared UI primitives.
 *
 * Deliberately small and unopinionated — enough structure that every screen
 * looks like part of one product, without a component library dependency.
 */
import clsx from 'clsx'
import {
  AlertCircle,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Info,
  Loader2,
  Minus,
  X,
} from 'lucide-react'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { useTranslation } from 'react-i18next'
import { formatMoney, formatNumber, formatPercent, trendOf } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Page scaffolding                                                           */
/* -------------------------------------------------------------------------- */
export function PageHeader({
  title,
  subtitle,
  actions,
  icon,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="flex items-start gap-3">
        {icon && (
          <div className="mt-0.5 rounded-lg bg-brand-50 p-2 text-brand-600">{icon}</div>
        )}
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-shell-900">{title}</h1>
          {subtitle && <p className="mt-0.5 text-sm text-shell-500">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Card({
  title,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={clsx('card', className)}>
      {(title || actions) && (
        <header className="card-header">
          {title && <h2 className="card-title">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={clsx('p-5', bodyClassName)}>{children}</div>
    </section>
  )
}

/* -------------------------------------------------------------------------- */
/* States                                                                     */
/* -------------------------------------------------------------------------- */
export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx('animate-spin', className ?? 'h-4 w-4')} />
}

export function LoadingBlock({ label }: { label?: string }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-14 text-shell-400">
      <Spinner className="h-6 w-6" />
      <span className="text-sm">{label ?? t('common.loading')}</span>
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title?: string
  description?: string
  action?: ReactNode
  icon?: ReactNode
}) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-14 text-center">
      <div className="rounded-full bg-shell-100 p-3 text-shell-400">
        {icon ?? <Inbox className="h-6 w-6" />}
      </div>
      <p className="text-sm font-medium text-shell-700">{title ?? t('common.noData')}</p>
      {description && <p className="max-w-sm text-sm text-shell-500">{description}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const { t } = useTranslation()
  const message =
    error && typeof error === 'object' && 'message' in error
      ? String((error as Error).message)
      : t('errors.generic')
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-14 text-center">
      <div className="rounded-full bg-danger-50 p-3 text-danger-600">
        <AlertCircle className="h-6 w-6" />
      </div>
      <p className="max-w-md text-sm text-shell-700">{message}</p>
      {onRetry && (
        <button type="button" className="btn-secondary btn-sm" onClick={onRetry}>
          {t('common.retry')}
        </button>
      )}
    </div>
  )
}

export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-2 p-2">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }).map((__, c) => (
            <div key={c} className="skeleton h-8 flex-1" />
          ))}
        </div>
      ))}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* KPI                                                                        */
/* -------------------------------------------------------------------------- */
export interface KpiCardData {
  key: string
  label_tr: string
  label_en: string
  value: number | string
  previous_value?: number | string | null
  change_percent?: number | null
  unit?: string | null
  format?: string
  trend?: string | null
  severity?: string | null
  icon?: string | null
}

export function KpiTile({ kpi, lang }: { kpi: KpiCardData; lang: string }) {
  const label = lang === 'en' ? kpi.label_en : kpi.label_tr
  const trend = kpi.trend ?? trendOf(kpi.change_percent)

  const display =
    kpi.format === 'money'
      ? formatMoney(kpi.value, { compact: Number(kpi.value) >= 1_000_000 })
      : kpi.format === 'percent'
        ? formatPercent(kpi.value)
        : formatNumber(kpi.value, { decimals: kpi.format === 'integer' ? 0 : 0 })

  const severityRing =
    kpi.severity === 'critical'
      ? 'ring-1 ring-danger-500/30'
      : kpi.severity === 'warning'
        ? 'ring-1 ring-warn-500/30'
        : ''

  return (
    <div className={clsx('card p-4', severityRing)}>
      <p className="truncate text-xs font-medium uppercase tracking-wide text-shell-500">
        {label}
      </p>
      <p className="tabular mt-1.5 text-2xl font-semibold text-shell-900">{display}</p>
      {kpi.change_percent !== null && kpi.change_percent !== undefined && (
        <p
          className={clsx(
            'mt-1 flex items-center gap-1 text-xs font-medium',
            trend === 'up' && 'text-ok-600',
            trend === 'down' && 'text-danger-600',
            trend === 'flat' && 'text-shell-400',
          )}
        >
          {trend === 'up' ? (
            <ArrowUpRight className="h-3.5 w-3.5" />
          ) : trend === 'down' ? (
            <ArrowDownRight className="h-3.5 w-3.5" />
          ) : (
            <Minus className="h-3.5 w-3.5" />
          )}
          {formatPercent(kpi.change_percent, { sign: true })}
        </p>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Badges                                                                     */
/* -------------------------------------------------------------------------- */
const STATUS_TONE: Record<string, string> = {
  ACTIVE: 'badge-ok', COMPLETED: 'badge-ok', PAID: 'badge-ok', CLEARED: 'badge-ok',
  DELIVERED: 'badge-ok', RECEIVED: 'badge-ok', APPROVED: 'badge-ok', OK: 'badge-ok',
  VERIFIED: 'badge-ok', POSTED: 'badge-ok', CLOSED: 'badge-ok',
  DRAFT: 'badge-muted', PENDING: 'badge-warn', IN_PROGRESS: 'badge-info',
  IN_TRANSIT: 'badge-info', PARTIALLY_PAID: 'badge-warn', WARNING: 'badge-warn',
  OVERDUE: 'badge-danger', CANCELLED: 'badge-danger', FAILED: 'badge-danger',
  BOUNCED: 'badge-danger', BLOCKED: 'badge-danger', ERROR: 'badge-danger',
  CORRUPT: 'badge-danger', SKIPPED: 'badge-warn', DISPUTED: 'badge-danger',
  PASSIVE: 'badge-muted', INACTIVE: 'badge-muted', UNKNOWN: 'badge-muted',
}

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={STATUS_TONE[status] ?? 'badge-muted'}>{label ?? status}</span>
}

/** Expiry badge: green > 30 days, amber ≤ 30, red once past. */
export function ExpiryBadge({ days }: { days: number | null }) {
  if (days === null) return <span className="badge-muted">—</span>
  if (days < 0) return <span className="badge-danger">{days} g</span>
  if (days <= 30) return <span className="badge-warn">{days} g</span>
  return <span className="badge-ok">{days} g</span>
}

/* -------------------------------------------------------------------------- */
/* Pagination                                                                 */
/* -------------------------------------------------------------------------- */
export function Pagination({
  page,
  pages,
  total,
  size,
  onPage,
}: {
  page: number
  pages: number
  total: number
  size: number
  onPage: (p: number) => void
}) {
  const { t } = useTranslation()
  if (total === 0) return null
  const from = (page - 1) * size + 1
  const to = Math.min(page * size, total)

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-shell-200 px-5 py-3">
      <p className="text-xs text-shell-500">
        {t('common.showing', { from, to, total })}
      </p>
      <div className="flex items-center gap-1">
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
          aria-label={t('common.previous')}
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="tabular px-2 text-xs text-shell-600">
          {page} / {Math.max(pages, 1)}
        </span>
        <button
          type="button"
          className="btn-ghost btn-sm"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
          aria-label={t('common.next')}
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Modal                                                                      */
/* -------------------------------------------------------------------------- */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  size = 'md',
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [open, onClose])

  if (!open) return null
  const width = { sm: 'max-w-md', md: 'max-w-2xl', lg: 'max-w-4xl', xl: 'max-w-6xl' }[size]

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-shell-950/40 p-4 backdrop-blur-sm">
      <div
        className={clsx(
          'my-8 w-full animate-slide-up rounded-xl bg-white shadow-pop',
          width,
        )}
        role="dialog"
        aria-modal="true"
      >
        <header className="flex items-center justify-between border-b border-shell-200 px-5 py-3.5">
          <h2 className="text-sm font-semibold text-shell-800">{title}</h2>
          <button type="button" className="btn-ghost btn-sm" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto p-5">{children}</div>
        {footer && (
          <footer className="flex justify-end gap-2 border-t border-shell-200 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Toasts                                                                     */
/* -------------------------------------------------------------------------- */
type ToastKind = 'success' | 'error' | 'warning' | 'info'
interface Toast {
  id: number
  kind: ToastKind
  message: string
}

const ToastCtx = createContext<{ push: (kind: ToastKind, message: string) => void } | null>(
  null,
)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, kind, message }])
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 5000)
  }, [])

  const icons = {
    success: <CheckCircle2 className="h-4 w-4 text-ok-600" />,
    error: <AlertCircle className="h-4 w-4 text-danger-600" />,
    warning: <AlertTriangle className="h-4 w-4 text-warn-600" />,
    info: <Info className="h-4 w-4 text-info-600" />,
  }

  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className="pointer-events-auto flex animate-slide-up items-start gap-2.5 rounded-lg border border-shell-200 bg-white px-4 py-3 shadow-pop"
          >
            {icons[t.kind]}
            <p className="flex-1 text-sm text-shell-700">{t.message}</p>
            <button
              type="button"
              onClick={() => setItems((p) => p.filter((x) => x.id !== t.id))}
              className="text-shell-400 hover:text-shell-700"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastCtx)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}

/* -------------------------------------------------------------------------- */
/* Misc                                                                       */
/* -------------------------------------------------------------------------- */
export function Field({
  label,
  required,
  error,
  hint,
  children,
}: {
  label: string
  required?: boolean
  error?: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div>
      <label className="label">
        {label}
        {required && <span className="ml-0.5 text-danger-500">*</span>}
      </label>
      {children}
      {hint && !error && <p className="mt-1 text-2xs text-shell-400">{hint}</p>}
      {error && <p className="mt-1 text-2xs text-danger-600">{error}</p>}
    </div>
  )
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-shell-500">
      {children}
    </h3>
  )
}
