/**
 * Sales targets.
 *
 * Actuals, projections and the risk score are computed server-side; the
 * "refresh" action re-runs that calculation for a window rather than doing any
 * arithmetic here.  Posting the same (subject, metric, period) revises the
 * existing target — the endpoint is an upsert.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, RefreshCw, Target as TargetIcon, Trash2 } from 'lucide-react'
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
import { formatDate, formatMoney, formatNumber, formatPercent, toNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface TargetRow {
  id: number
  subject_type: string
  subject_id: number
  metric: string
  period: string
  period_start: string
  period_end: string
  target_value: number | string
  actual_value: number | string
  projected_value: number | string | null
  currency: string
  risk_score: number
  achievement_percent: number
  last_calculated_at: string | null
  notes: string | null
}
interface RefreshResult { refreshed: number; period_start: string; period_end: string }
interface SalespersonRow { id: number; full_name: string }

const SUBJECTS = ['COMPANY', 'REGION', 'ROUTE', 'SALESPERSON', 'PRODUCT', 'CATEGORY', 'BRAND', 'CUSTOMER']
const METRICS = ['REVENUE', 'VOLUME', 'MARGIN', 'COLLECTION', 'VISITS', 'NEW_CUSTOMERS']
const PERIODS = ['DAILY', 'WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY']
const MONEY_METRICS = new Set(['REVENUE', 'MARGIN', 'COLLECTION'])
const PAGE_SIZE = 25

function isoDate(d: Date = new Date()): string {
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}
function monthStart(): string {
  const d = new Date()
  return isoDate(new Date(d.getFullYear(), d.getMonth(), 1))
}
function monthEnd(): string {
  const d = new Date()
  return isoDate(new Date(d.getFullYear(), d.getMonth() + 1, 0))
}

interface FormState {
  id: number | null
  subject_type: string
  subject_id: string
  metric: string
  period: string
  period_start: string
  period_end: string
  target_value: string
  notes: string
}
const emptyForm = (): FormState => ({
  id: null,
  subject_type: 'SALESPERSON',
  subject_id: '',
  metric: 'REVENUE',
  period: 'MONTHLY',
  period_start: monthStart(),
  period_end: monthEnd(),
  target_value: '',
  notes: '',
})

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Targets() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [subjectType, setSubjectType] = useState('')
  const [metric, setMetric] = useState('')
  const [period, setPeriod] = useState('')
  const [start, setStart] = useState(monthStart())
  const [end, setEnd] = useState(monthEnd())
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)

  const mayCreate = can('analytics.targets', 'CREATE')
  const mayUpdate = can('analytics.targets', 'UPDATE')
  const mayDelete = can('analytics.targets', 'DELETE')

  const people = useQuery({
    queryKey: ['target-salespersons'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons'),
    throwOnError: false,
  })
  const personName = useMemo(() => {
    const m = new Map<number, string>()
    for (const p of people.data?.items ?? []) m.set(p.id, p.full_name)
    return m
  }, [people.data])

  const listParams = {
    page,
    size: PAGE_SIZE,
    subject_type: subjectType || undefined,
    metric: metric || undefined,
    period: period || undefined,
    start: start || undefined,
    end: end || undefined,
  }

  const list = useQuery({
    queryKey: ['targets', listParams],
    queryFn: () => api.get<Paged<TargetRow>>('/analytics/targets', listParams),
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['targets'] })
  const fail = (e: unknown) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))

  const refresh = useMutation({
    mutationFn: () =>
      api.post<RefreshResult>('/analytics/targets/refresh', undefined, {
        period_start: start || undefined,
        period_end: end || undefined,
      }),
    onSuccess: (r) => { push('success', t('targets.refreshed', { count: r.refreshed })); invalidate() },
    onError: fail,
  })

  const save = useMutation({
    mutationFn: () => {
      const f = form!
      if (f.id) {
        return api.put<TargetRow>(`/analytics/targets/${f.id}`, {
          target_value: toNumber(f.target_value),
          period_end: f.period_end,
          notes: f.notes || null,
        })
      }
      return api.post<TargetRow>('/analytics/targets', {
        subject_type: f.subject_type,
        subject_id: Number(f.subject_id) || 0,
        metric: f.metric,
        period: f.period,
        period_start: f.period_start,
        period_end: f.period_end,
        target_value: toNumber(f.target_value),
        notes: f.notes || null,
      })
    },
    onSuccess: () => { setForm(null); push('success', t('targets.saved')); invalidate() },
    onError: fail,
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/analytics/targets/${id}`),
    onSuccess: () => { push('success', t('targets.deleted')); invalidate() },
    onError: fail,
  })

  const rows = list.data?.items ?? []

  const subjectLabel = (r: TargetRow) => {
    const type = t(`targets.subjects.${r.subject_type}`, { defaultValue: r.subject_type })
    if (r.subject_type === 'COMPANY') return type
    if (r.subject_type === 'SALESPERSON' && personName.has(r.subject_id)) {
      return `${type} · ${personName.get(r.subject_id)}`
    }
    return `${type} #${r.subject_id}`
  }

  return (
    <>
      <PageHeader
        title={t('targets.title')}
        icon={<TargetIcon className="h-5 w-5" />}
        actions={
          <>
            {mayUpdate && (
              <button type="button" className="btn-secondary btn-sm"
                disabled={refresh.isPending} onClick={() => refresh.mutate()}>
                {refresh.isPending ? <Spinner className="h-3.5 w-3.5" /> : <RefreshCw className="h-3.5 w-3.5" />}
                {t('targets.refresh')}
              </button>
            )}
            {mayCreate && (
              <button type="button" className="btn-primary btn-sm" onClick={() => setForm(emptyForm())}>
                <Plus className="h-3.5 w-3.5" />
                {t('targets.newTarget')}
              </button>
            )}
          </>
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t('targets.subjectType')}>
            <select className="input" value={subjectType}
              onChange={(e) => { setSubjectType(e.target.value); setPage(1) }}>
              <option value="">{t('targets.allSubjects')}</option>
              {SUBJECTS.map((s) => <option key={s} value={s}>{t(`targets.subjects.${s}`)}</option>)}
            </select>
          </Field>
          <Field label={t('targets.metric')}>
            <select className="input" value={metric} onChange={(e) => { setMetric(e.target.value); setPage(1) }}>
              <option value="">{t('targets.allMetrics')}</option>
              {METRICS.map((m) => <option key={m} value={m}>{t(`targets.metrics.${m}`)}</option>)}
            </select>
          </Field>
          <Field label={t('targets.period')}>
            <select className="input" value={period} onChange={(e) => { setPeriod(e.target.value); setPage(1) }}>
              <option value="">{t('targets.allPeriods')}</option>
              {PERIODS.map((p) => <option key={p} value={p}>{t(`targets.periods.${p}`)}</option>)}
            </select>
          </Field>
          <Field label={t('common.from')}>
            <input type="date" className="input" value={start}
              onChange={(e) => { setStart(e.target.value); setPage(1) }} />
          </Field>
          <Field label={t('common.to')}>
            <input type="date" className="input" value={end}
              onChange={(e) => { setEnd(e.target.value); setPage(1) }} />
          </Field>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {list.isLoading ? (
          <SkeletonRows rows={6} cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('targets.subjectType')}</th>
                    <th>{t('targets.metric')}</th>
                    <th>{t('targets.period')}</th>
                    <th className="text-right">{t('targets.targetValue')}</th>
                    <th className="text-right">{t('targets.actualValue')}</th>
                    <th className="w-56">{t('targets.achievement')}</th>
                    <th className="text-right">{t('targets.projectedValue')}</th>
                    <th className="text-right">{t('targets.risk')}</th>
                    {(mayUpdate || mayDelete) && <th className="text-right">{t('common.actions')}</th>}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const money = MONEY_METRICS.has(r.metric)
                    const fmt = (v: number | string | null) =>
                      v === null ? '—' : money ? formatMoney(v) : formatNumber(v, { decimals: 0 })
                    return (
                      <tr key={r.id}>
                        <td>
                          <span className="block truncate font-medium text-shell-900">{subjectLabel(r)}</span>
                          {r.notes && <span className="block truncate text-2xs text-shell-400">{r.notes}</span>}
                        </td>
                        <td>{t(`targets.metrics.${r.metric}`, { defaultValue: r.metric })}</td>
                        <td className="whitespace-nowrap text-xs">
                          {t(`targets.periods.${r.period}`, { defaultValue: r.period })}
                          <span className="block text-2xs text-shell-400">
                            {formatDate(r.period_start, { short: true })} — {formatDate(r.period_end, { short: true })}
                          </span>
                        </td>
                        <td className="tabular text-right">{fmt(r.target_value)}</td>
                        <td className="tabular text-right">{fmt(r.actual_value)}</td>
                        <td>
                          <ProgressBar percent={r.achievement_percent} risk={r.risk_score} />
                        </td>
                        <td className="tabular text-right">{fmt(r.projected_value)}</td>
                        <td className="tabular text-right">
                          <RiskBadge score={r.risk_score} />
                        </td>
                        {(mayUpdate || mayDelete) && (
                          <td className="text-right">
                            <div className="flex justify-end gap-1">
                              {mayUpdate && (
                                <button type="button" className="btn-ghost btn-sm" title={t('common.edit')}
                                  onClick={() => setForm({
                                    id: r.id,
                                    subject_type: r.subject_type,
                                    subject_id: String(r.subject_id),
                                    metric: r.metric,
                                    period: r.period,
                                    period_start: r.period_start,
                                    period_end: r.period_end,
                                    target_value: String(r.target_value),
                                    notes: r.notes ?? '',
                                  })}>
                                  <Pencil className="h-3.5 w-3.5" />
                                </button>
                              )}
                              {mayDelete && (
                                <button type="button" className="btn-ghost btn-sm text-danger-600"
                                  title={t('common.delete')}
                                  onClick={() => { if (window.confirm(t('targets.deleteConfirm'))) remove.mutate(r.id) }}>
                                  <Trash2 className="h-3.5 w-3.5" />
                                </button>
                              )}
                            </div>
                          </td>
                        )}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <Pagination
              page={list.data?.page ?? 1}
              pages={list.data?.pages ?? 1}
              total={list.data?.total ?? 0}
              size={list.data?.size ?? PAGE_SIZE}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      {/* ------------------------- Create / edit ------------------------- */}
      <Modal
        open={form !== null}
        onClose={() => setForm(null)}
        title={form?.id ? t('targets.editTarget') : t('targets.newTarget')}
        footer={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => setForm(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn-primary btn-sm"
              disabled={save.isPending || !form?.target_value} onClick={() => save.mutate()}>
              {save.isPending && <Spinner className="h-3.5 w-3.5" />}
              {t('common.save')}
            </button>
          </>
        }
      >
        {form && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('targets.subjectType')} required>
              <select className="input" value={form.subject_type} disabled={form.id !== null}
                onChange={(e) => setForm({ ...form, subject_type: e.target.value })}>
                {SUBJECTS.map((s) => <option key={s} value={s}>{t(`targets.subjects.${s}`)}</option>)}
              </select>
            </Field>
            {form.subject_type === 'SALESPERSON' && (people.data?.items ?? []).length > 0 ? (
              <Field label={t('routes.salesperson')} required>
                <select className="input" value={form.subject_id} disabled={form.id !== null}
                  onChange={(e) => setForm({ ...form, subject_id: e.target.value })}>
                  <option value="">{t('common.select')}</option>
                  {(people.data?.items ?? []).map((p) => (
                    <option key={p.id} value={p.id}>{p.full_name}</option>
                  ))}
                </select>
              </Field>
            ) : (
              <Field label={t('targets.subjectId')} hint={form.subject_type === 'COMPANY' ? '0' : undefined}>
                <input type="number" className="input tabular" value={form.subject_id} disabled={form.id !== null}
                  onChange={(e) => setForm({ ...form, subject_id: e.target.value })} />
              </Field>
            )}
            <Field label={t('targets.metric')} required>
              <select className="input" value={form.metric} disabled={form.id !== null}
                onChange={(e) => setForm({ ...form, metric: e.target.value })}>
                {METRICS.map((m) => <option key={m} value={m}>{t(`targets.metrics.${m}`)}</option>)}
              </select>
            </Field>
            <Field label={t('targets.period')} required>
              <select className="input" value={form.period} disabled={form.id !== null}
                onChange={(e) => setForm({ ...form, period: e.target.value })}>
                {PERIODS.map((p) => <option key={p} value={p}>{t(`targets.periods.${p}`)}</option>)}
              </select>
            </Field>
            <Field label={t('targets.periodStart')} required>
              <input type="date" className="input" value={form.period_start} disabled={form.id !== null}
                onChange={(e) => setForm({ ...form, period_start: e.target.value })} />
            </Field>
            <Field label={t('targets.periodEnd')} required>
              <input type="date" className="input" value={form.period_end}
                onChange={(e) => setForm({ ...form, period_end: e.target.value })} />
            </Field>
            <Field label={t('targets.targetValue')} required>
              <input type="number" min={0} step="0.01" className="input tabular text-right"
                value={form.target_value} onChange={(e) => setForm({ ...form, target_value: e.target.value })} />
            </Field>
            <Field label={t('common.notes')}>
              <input className="input" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
            </Field>
          </div>
        )}
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Sub-components                                                             */
/* -------------------------------------------------------------------------- */
function riskTone(score: number): { bar: string; badge: string; key: string } {
  if (score >= 67) return { bar: 'bg-danger-500', badge: 'badge-danger', key: 'targets.riskHigh' }
  if (score >= 34) return { bar: 'bg-warn-500', badge: 'badge-warn', key: 'targets.riskMedium' }
  return { bar: 'bg-ok-500', badge: 'badge-ok', key: 'targets.riskLow' }
}

function ProgressBar({ percent, risk }: { percent: number; risk: number }) {
  const tone = riskTone(risk)
  return (
    <div className="min-w-[8rem]">
      <div className="h-2 w-full overflow-hidden rounded-full bg-shell-100">
        <div className={`h-full rounded-full ${tone.bar}`} style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
      </div>
      <p className="tabular mt-1 text-2xs text-shell-500">{formatPercent(percent)}</p>
    </div>
  )
}

function RiskBadge({ score }: { score: number }) {
  const { t } = useTranslation()
  const tone = riskTone(score)
  return (
    <span className={tone.badge}>
      {t(tone.key)} {formatNumber(score, { decimals: 0 })}
    </span>
  )
}
