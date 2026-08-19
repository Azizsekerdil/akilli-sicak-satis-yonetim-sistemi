/**
 * Visit log.
 *
 * The list comes from `GET /routes/visits`; the coverage panel is built from
 * `GET /routes/efficiency`, which is the only endpoint that knows how many
 * stops were *planned* — deriving that from the visit rows alone would only
 * ever tell us what actually happened.
 */
import { useQuery } from '@tanstack/react-query'
import { ClipboardList, MapPinOff, MapPin } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  Pagination,
  PageHeader,
  SkeletonRows,
  StatusBadge,
} from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, formatPercent } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface VisitRow {
  id: number
  visit_date: string
  customer_id: number
  customer_code: string | null
  customer_name: string | null
  salesperson_id: number | null
  route_id: number | null
  outcome: string
  started_at: string | null
  ended_at: string | null
  duration_minutes: number
  is_in_geofence: boolean | null
  is_unplanned: boolean
  sale_amount: number | string
  collected_amount: number | string
  return_amount: number | string
  lines_count: number
  notes: string | null
}

interface EfficiencyRow {
  salesperson_id: number
  salesperson_code: string | null
  salesperson_name: string | null
  routes: number
  planned_km: number
  actual_km: number
  stops_planned: number
  stops_completed: number
  visits: number
  working_hours: number
  sales_count: number
  revenue: number | string
  km_per_sale: number
  stops_per_hour: number
  drop_size: number | string
  completion_rate: number
  strike_rate: number
}

interface SalespersonRow { id: number; full_name: string }
interface CustomerRow { id: number; code: string; name: string }

const OUTCOMES = ['SALE', 'NO_SALE', 'CLOSED', 'NO_ORDER', 'PAYMENT_ONLY', 'RETURN_ONLY', 'MERCHANDISING']
const PAGE_SIZE = 25

function isoDate(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Visits() {
  const { t } = useTranslation()
  const { can } = useAuth()

  const [dateFrom, setDateFrom] = useState(isoDate(-6))
  const [dateTo, setDateTo] = useState(isoDate())
  const [salespersonId, setSalespersonId] = useState('')
  const [outcome, setOutcome] = useState('')
  const [customerTerm, setCustomerTerm] = useState('')
  const [customerId, setCustomerId] = useState('')
  const [page, setPage] = useState(1)

  const reset = () => setPage(1)

  const people = useQuery({
    queryKey: ['visit-salespersons'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons'),
    throwOnError: false,
  })

  const customers = useQuery({
    queryKey: ['visit-customers', customerTerm],
    queryFn: () => api.get<Paged<CustomerRow>>('/customers', { term: customerTerm, size: 20 }),
    enabled: can('crm.customers') && customerTerm.trim().length >= 2,
    throwOnError: false,
  })

  const listParams = {
    page,
    size: PAGE_SIZE,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    salesperson_id: salespersonId || undefined,
    customer_id: customerId || undefined,
    outcome: outcome || undefined,
  }

  const list = useQuery({
    queryKey: ['visits', listParams],
    queryFn: () => api.get<Paged<VisitRow>>('/routes/visits', listParams),
  })

  const efficiency = useQuery({
    queryKey: ['visit-efficiency', dateFrom, dateTo],
    queryFn: () => api.get<EfficiencyRow[]>('/routes/efficiency', { start: dateFrom, end: dateTo }),
    enabled: can('field.routes') && Boolean(dateFrom && dateTo),
    throwOnError: false,
  })

  const effRows = useMemo(() => {
    const rows = efficiency.data ?? []
    return salespersonId ? rows.filter((r) => String(r.salesperson_id) === salespersonId) : rows
  }, [efficiency.data, salespersonId])

  const coverage = useMemo(() => {
    const acc = effRows.reduce(
      (a, r) => ({
        planned: a.planned + r.stops_planned,
        completed: a.completed + r.stops_completed,
        visits: a.visits + r.visits,
        sales: a.sales + r.sales_count,
        hours: a.hours + r.working_hours,
      }),
      { planned: 0, completed: 0, visits: 0, sales: 0, hours: 0 },
    )
    return {
      ...acc,
      coverageRate: acc.planned > 0 ? (acc.completed / acc.planned) * 100 : 0,
      strikeRate: acc.visits > 0 ? (acc.sales / acc.visits) * 100 : 0,
    }
  }, [effRows])

  const rows = list.data?.items ?? []
  const hasEfficiency = effRows.length > 0

  return (
    <>
      <PageHeader
        title={t('visits.title')}
        subtitle={`${formatDate(dateFrom, { short: true })} — ${formatDate(dateTo, { short: true })}`}
        icon={<ClipboardList className="h-5 w-5" />}
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t('common.from')}>
            <input type="date" className="input" value={dateFrom}
              onChange={(e) => { setDateFrom(e.target.value); reset() }} />
          </Field>
          <Field label={t('common.to')}>
            <input type="date" className="input" value={dateTo}
              onChange={(e) => { setDateTo(e.target.value); reset() }} />
          </Field>
          {(people.data?.items ?? []).length > 0 && (
            <Field label={t('routes.salesperson')}>
              <select className="input" value={salespersonId}
                onChange={(e) => { setSalespersonId(e.target.value); reset() }}>
                <option value="">{t('common.all')}</option>
                {(people.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>{p.full_name}</option>
                ))}
              </select>
            </Field>
          )}
          <Field label={t('visits.outcomeLabel')}>
            <select className="input" value={outcome}
              onChange={(e) => { setOutcome(e.target.value); reset() }}>
              <option value="">{t('visits.allOutcomes')}</option>
              {OUTCOMES.map((o) => (
                <option key={o} value={o}>{t(`visits.outcomes.${o}`)}</option>
              ))}
            </select>
          </Field>
          {can('crm.customers') && (
            <>
              <Field label={t('nav.customers')} hint={t('common.search')}>
                <input className="input" value={customerTerm} placeholder={t('common.search')}
                  onChange={(e) => { setCustomerTerm(e.target.value); setCustomerId(''); reset() }} />
              </Field>
              {(customers.data?.items ?? []).length > 0 && (
                <Field label={t('common.select')}>
                  <select className="input" value={customerId}
                    onChange={(e) => { setCustomerId(e.target.value); reset() }}>
                    <option value="">{t('common.all')}</option>
                    {(customers.data?.items ?? []).map((c) => (
                      <option key={c.id} value={c.id}>{c.code} — {c.name}</option>
                    ))}
                  </select>
                </Field>
              )}
            </>
          )}
          <button type="button" className="btn-secondary btn-sm"
            onClick={() => { setSalespersonId(''); setOutcome(''); setCustomerTerm(''); setCustomerId(''); reset() }}>
            {t('common.reset')}
          </button>
        </div>
      </Card>

      {/* ------------------------- Coverage ------------------------- */}
      <Card className="mb-4" title={t('visits.coverage')} bodyClassName="p-4">
        {efficiency.isLoading ? (
          <SkeletonRows rows={2} cols={5} />
        ) : !hasEfficiency ? (
          <EmptyState title={t('common.noData')} />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {[
                [t('visits.plannedStops'), formatNumber(coverage.planned)],
                [t('visits.visitedCustomers'), formatNumber(coverage.completed)],
                [t('visits.title'), formatNumber(coverage.visits)],
                [t('visits.coverageRate'), formatPercent(coverage.coverageRate)],
                [t('visits.productiveRate'), formatPercent(coverage.strikeRate)],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border border-shell-200 p-3">
                  <p className="truncate text-2xs uppercase tracking-wide text-shell-500">{label}</p>
                  <p className="tabular mt-0.5 text-lg font-semibold text-shell-900">{value}</p>
                </div>
              ))}
            </div>

            <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-shell-100">
              <div
                className="h-full rounded-full bg-brand-600"
                style={{ width: `${Math.min(100, coverage.coverageRate)}%` }}
              />
            </div>

            <div className="table-wrap mt-4">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('routes.salesperson')}</th>
                    <th className="text-right">{t('visits.plannedStops')}</th>
                    <th className="text-right">{t('visits.visitedCustomers')}</th>
                    <th className="text-right">{t('visits.coverageRate')}</th>
                    <th className="text-right">{t('visits.productiveRate')}</th>
                    <th className="text-right">{t('common.total')}</th>
                  </tr>
                </thead>
                <tbody>
                  {effRows.map((r) => (
                    <tr key={r.salesperson_id}>
                      <td>{r.salesperson_name ?? r.salesperson_code ?? `#${r.salesperson_id}`}</td>
                      <td className="tabular text-right">{formatNumber(r.stops_planned)}</td>
                      <td className="tabular text-right">{formatNumber(r.stops_completed)}</td>
                      <td className="tabular text-right">{formatPercent(r.completion_rate)}</td>
                      <td className="tabular text-right">{formatPercent(r.strike_rate)}</td>
                      <td className="tabular text-right">{formatMoney(r.revenue)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      {/* ------------------------- Visit list ------------------------- */}
      <Card bodyClassName="p-0">
        {list.isLoading ? (
          <SkeletonRows rows={8} cols={7} />
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
                    <th>{t('common.date')}</th>
                    <th>{t('nav.customers')}</th>
                    <th>{t('visits.outcomeLabel')}</th>
                    <th className="text-right">{t('visits.duration')}</th>
                    <th>{t('visits.geofence')}</th>
                    <th className="text-right">{t('visits.saleAmount')}</th>
                    <th className="text-right">{t('visits.collectedAmount')}</th>
                    <th className="text-right">{t('visits.returnAmount')}</th>
                    <th className="text-right">{t('visits.lines')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((v) => (
                    <tr key={v.id}>
                      <td className="whitespace-nowrap">
                        {formatDate(v.visit_date, { short: true })}
                        {v.is_unplanned && <span className="badge-warn ml-1.5">{t('visits.unplanned')}</span>}
                      </td>
                      <td>
                        <span className="block truncate font-medium text-shell-900">
                          {v.customer_name ?? `#${v.customer_id}`}
                        </span>
                        <span className="block text-2xs text-shell-400">{v.customer_code}</span>
                      </td>
                      <td>
                        <StatusBadge
                          status={v.outcome === 'SALE' ? 'COMPLETED' : v.outcome === 'CLOSED' ? 'CANCELLED' : 'PENDING'}
                          label={t(`visits.outcomes.${v.outcome}`, { defaultValue: v.outcome })}
                        />
                      </td>
                      <td className="tabular text-right">
                        {formatNumber(v.duration_minutes)} {t('visits.minutes')}
                      </td>
                      <td>
                        {v.is_in_geofence === null ? (
                          <span className="text-shell-300">—</span>
                        ) : v.is_in_geofence ? (
                          <span className="inline-flex items-center gap-1 text-2xs text-ok-700">
                            <MapPin className="h-3 w-3" />{t('visits.inGeofence')}
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-2xs text-danger-700">
                            <MapPinOff className="h-3 w-3" />{t('visits.outGeofence')}
                          </span>
                        )}
                      </td>
                      <td className="tabular text-right">{formatMoney(v.sale_amount)}</td>
                      <td className="tabular text-right">{formatMoney(v.collected_amount)}</td>
                      <td className="tabular text-right">{formatMoney(v.return_amount)}</td>
                      <td className="tabular text-right">{formatNumber(v.lines_count)}</td>
                    </tr>
                  ))}
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
    </>
  )
}
