/**
 * Anomaly review.
 *
 * Rows are grouped by severity so the critical ones cannot be scrolled past.
 * Each card shows the observed value against the expected one — the two
 * figures the detector actually compared — plus its z-score and method.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Check, Radar } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber, formatPercent } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface AnomalyRow {
  id: number
  anomaly_type: string
  severity: string
  subject_type: string
  subject_id: number | null
  subject_label: string | null
  detected_on: string
  observed_value: number
  expected_value: number
  deviation: number
  z_score: number | null
  method: string | null
  title: string
  description: string | null
  is_resolved: boolean
  resolved_at: string | null
  resolution_note: string | null
  created_at: string | null
}

const TYPES = [
  'SALES_SPIKE', 'SALES_DROP', 'UNUSUAL_DISCOUNT', 'UNUSUAL_RETURN',
  'STOCK_VARIANCE', 'COLLECTION_ANOMALY', 'ROUTE_DEVIATION', 'PRICE_ANOMALY',
]
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
const SEVERITY_TONE: Record<string, { badge: string; bar: string; ring: string }> = {
  CRITICAL: { badge: 'badge-danger', bar: 'bg-danger-600', ring: 'border-l-danger-600' },
  HIGH: { badge: 'badge-danger', bar: 'bg-danger-500', ring: 'border-l-danger-500' },
  MEDIUM: { badge: 'badge-warn', bar: 'bg-warn-500', ring: 'border-l-warn-500' },
  LOW: { badge: 'badge-info', bar: 'bg-info-500', ring: 'border-l-info-500' },
  INFO: { badge: 'badge-muted', bar: 'bg-shell-400', ring: 'border-l-shell-300' },
}
const PAGE_SIZE = 50

function isoDate(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Anomalies() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [start, setStart] = useState(isoDate(-29))
  const [end, setEnd] = useState(isoDate())
  const [type, setType] = useState('')
  const [severity, setSeverity] = useState('')
  const [resolved, setResolved] = useState('false')
  const [page, setPage] = useState(1)
  const [resolving, setResolving] = useState<AnomalyRow | null>(null)
  const [note, setNote] = useState('')

  const mayUpdate = can('analytics.anomalies', 'UPDATE')

  const listParams = {
    page,
    size: PAGE_SIZE,
    start: start || undefined,
    end: end || undefined,
    anomaly_type: type || undefined,
    severity: severity || undefined,
    is_resolved: resolved === '' ? undefined : resolved,
  }

  const list = useQuery({
    queryKey: ['anomalies', listParams],
    queryFn: () => api.get<Paged<AnomalyRow>>('/analytics/anomalies', listParams),
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['anomalies'] })
  const fail = (e: unknown) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))

  const detect = useMutation({
    mutationFn: () => api.post<AnomalyRow[]>('/analytics/anomalies/detect', { start, end }),
    onSuccess: (rows) => { push('success', t('anomalies.detected', { count: rows.length })); invalidate() },
    onError: fail,
  })

  const resolve = useMutation({
    mutationFn: () =>
      api.put<AnomalyRow>(`/analytics/anomalies/${resolving!.id}/resolve`, { note: note || null }),
    onSuccess: () => {
      setResolving(null)
      setNote('')
      push('success', t('anomalies.resolved'))
      invalidate()
    },
    onError: fail,
  })

  const rows = list.data?.items ?? []
  const grouped = useMemo(() => {
    const map = new Map<string, AnomalyRow[]>()
    for (const s of SEVERITIES) map.set(s, [])
    for (const r of rows) {
      if (!map.has(r.severity)) map.set(r.severity, [])
      map.get(r.severity)!.push(r)
    }
    return [...map.entries()].filter(([, items]) => items.length > 0)
  }, [rows])

  return (
    <>
      <PageHeader
        title={t('anomalies.title')}
        icon={<Activity className="h-5 w-5" />}
        actions={
          mayUpdate && (
            <button type="button" className="btn-primary btn-sm"
              disabled={detect.isPending} onClick={() => detect.mutate()}>
              {detect.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Radar className="h-3.5 w-3.5" />}
              {t('anomalies.detect')}
            </button>
          )
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t('common.from')}>
            <input type="date" className="input" value={start}
              onChange={(e) => { setStart(e.target.value); setPage(1) }} />
          </Field>
          <Field label={t('common.to')}>
            <input type="date" className="input" value={end}
              onChange={(e) => { setEnd(e.target.value); setPage(1) }} />
          </Field>
          <Field label={t('anomalies.type')}>
            <select className="input" value={type} onChange={(e) => { setType(e.target.value); setPage(1) }}>
              <option value="">{t('anomalies.allTypes')}</option>
              {TYPES.map((x) => <option key={x} value={x}>{t(`anomalies.types.${x}`)}</option>)}
            </select>
          </Field>
          <Field label={t('anomalies.severity')}>
            <select className="input" value={severity} onChange={(e) => { setSeverity(e.target.value); setPage(1) }}>
              <option value="">{t('anomalies.allSeverities')}</option>
              {SEVERITIES.map((x) => <option key={x} value={x}>{t(`anomalies.severities.${x}`)}</option>)}
            </select>
          </Field>
          <Field label={t('anomalies.statusFilter')}>
            <select className="input" value={resolved} onChange={(e) => { setResolved(e.target.value); setPage(1) }}>
              <option value="">{t('common.all')}</option>
              <option value="false">{t('anomalies.open')}</option>
              <option value="true">{t('anomalies.closed')}</option>
            </select>
          </Field>
        </div>
      </Card>

      {list.isLoading ? (
        <Card bodyClassName="p-0"><SkeletonRows rows={6} cols={4} /></Card>
      ) : list.isError ? (
        <Card><ErrorState error={list.error} onRetry={() => void list.refetch()} /></Card>
      ) : rows.length === 0 ? (
        <Card><EmptyState /></Card>
      ) : (
        <div className="space-y-5">
          {grouped.map(([sev, items]) => (
            <section key={sev}>
              <div className="mb-2 flex items-center gap-2">
                <span className={SEVERITY_TONE[sev]?.badge ?? 'badge-muted'}>
                  {t(`anomalies.severities.${sev}`, { defaultValue: sev })}
                </span>
                <span className="tabular text-xs text-shell-500">{formatNumber(items.length)}</span>
              </div>
              <div className="grid gap-3 lg:grid-cols-2">
                {items.map((a) => (
                  <AnomalyCard
                    key={a.id}
                    anomaly={a}
                    canResolve={mayUpdate}
                    onResolve={() => { setResolving(a); setNote('') }}
                  />
                ))}
              </div>
            </section>
          ))}

          <Card bodyClassName="p-0">
            <Pagination
              page={list.data?.page ?? 1}
              pages={list.data?.pages ?? 1}
              total={list.data?.total ?? 0}
              size={list.data?.size ?? PAGE_SIZE}
              onPage={setPage}
            />
          </Card>
        </div>
      )}

      <Modal
        open={resolving !== null}
        onClose={() => setResolving(null)}
        title={t('anomalies.resolveTitle')}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => setResolving(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn-primary btn-sm"
              disabled={resolve.isPending} onClick={() => resolve.mutate()}>
              {resolve.isPending && <Spinner className="h-3.5 w-3.5" />}
              {t('anomalies.resolve')}
            </button>
          </>
        }
      >
        {resolving && (
          <div className="space-y-3">
            <p className="text-sm font-medium text-shell-800">{resolving.title}</p>
            <Field label={t('anomalies.resolutionNote')} hint={t('anomalies.noteHint')}>
              <textarea className="input min-h-[5rem]" maxLength={2000} value={note}
                onChange={(e) => setNote(e.target.value)} />
            </Field>
          </div>
        )}
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Card                                                                       */
/* -------------------------------------------------------------------------- */
function AnomalyCard({
  anomaly,
  canResolve,
  onResolve,
}: {
  anomaly: AnomalyRow
  canResolve: boolean
  onResolve: () => void
}) {
  const { t } = useTranslation()
  const tone = SEVERITY_TONE[anomaly.severity] ?? SEVERITY_TONE.INFO

  return (
    <article className={`card border-l-4 p-4 ${tone.ring}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-shell-900">{anomaly.title}</p>
          <p className="text-2xs text-shell-500">
            {t(`anomalies.types.${anomaly.anomaly_type}`, { defaultValue: anomaly.anomaly_type })} ·{' '}
            {anomaly.subject_label ?? `${anomaly.subject_type}${anomaly.subject_id ? ` #${anomaly.subject_id}` : ''}`} ·{' '}
            {formatDate(anomaly.detected_on, { short: true })}
          </p>
        </div>
        {anomaly.is_resolved ? (
          <span className="badge-ok shrink-0">{t('anomalies.closed')}</span>
        ) : canResolve ? (
          <button type="button" className="btn-secondary btn-sm shrink-0" onClick={onResolve}>
            <Check className="h-3.5 w-3.5" />
            {t('anomalies.resolve')}
          </button>
        ) : null}
      </div>

      {anomaly.description && (
        <p className="mt-2 text-xs leading-relaxed text-shell-600">{anomaly.description}</p>
      )}

      <ObservedVsExpected observed={anomaly.observed_value} expected={anomaly.expected_value} tone={tone.bar} />

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-2xs sm:grid-cols-4">
        <Stat label={t('anomalies.observed')} value={formatNumber(anomaly.observed_value, { decimals: 2, compact: Math.abs(anomaly.observed_value) >= 1e6 })} />
        <Stat label={t('anomalies.expected')} value={formatNumber(anomaly.expected_value, { decimals: 2, compact: Math.abs(anomaly.expected_value) >= 1e6 })} />
        <Stat label={t('anomalies.deviation')} value={formatPercent(anomaly.deviation, { sign: true })} />
        <Stat
          label={t('anomalies.zScore')}
          value={anomaly.z_score === null ? '—' : formatNumber(anomaly.z_score, { decimals: 2 })}
        />
      </dl>

      {(anomaly.method || anomaly.resolution_note) && (
        <p className="mt-2 truncate text-2xs text-shell-400">
          {anomaly.method && `${t('anomalies.method')}: ${anomaly.method}`}
          {anomaly.resolution_note && ` · ${anomaly.resolution_note}`}
        </p>
      )}
    </article>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="truncate uppercase tracking-wide text-shell-400">{label}</dt>
      <dd className="tabular font-semibold text-shell-800">{value}</dd>
    </div>
  )
}

/**
 * Two-bar comparison of what was seen against what was expected.
 *
 * The API returns the pair of scalars the detector compared — not the series
 * behind them — so this is the honest chart for the data we actually have.
 */
function ObservedVsExpected({
  observed,
  expected,
  tone,
}: {
  observed: number
  expected: number
  tone: string
}) {
  const { t } = useTranslation()
  const scale = Math.max(Math.abs(observed), Math.abs(expected))
  if (scale <= 0) return null
  const pct = (v: number) => `${Math.min(100, (Math.abs(v) / scale) * 100)}%`
  return (
    <div className="mt-3 space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-2xs text-shell-500">{t('anomalies.observed')}</span>
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-shell-100">
          <div className={`h-full rounded-full ${tone}`} style={{ width: pct(observed) }} />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-2xs text-shell-500">{t('anomalies.expected')}</span>
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-shell-100">
          <div className="h-full rounded-full bg-shell-400" style={{ width: pct(expected) }} />
        </div>
      </div>
    </div>
  )
}
