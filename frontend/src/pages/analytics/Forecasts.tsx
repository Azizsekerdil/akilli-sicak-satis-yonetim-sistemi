/**
 * Demand forecasting.
 *
 * `POST /analytics/forecast` back-tests several methods on the subject's own
 * history and returns the winner with a confidence band.  The band is plotted
 * in quantity (the unit the API predicts in); the historic line — when the
 * report engine can supply one for this subject — is money, on its own axis.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { LineChart as LineChartIcon, Play } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  PageHeader,
  SectionTitle,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatNumber, formatPercent, toNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface ForecastPoint {
  bucket_date: string
  label: string
  value: number | string
  lower: number | string
  upper: number | string
  amount: number | string
}
interface ForecastCandidate {
  method: string
  mae: number
  mape: number
  rmse: number
  bias: number
}
interface ForecastOut {
  run_id: string
  subject_type: string
  subject_id: number
  granularity: string
  horizon_days: number
  method: string
  confidence: number
  mae: number
  history_points: number
  total_forecast_quantity: number | string
  total_forecast_amount: number | string
  points: ForecastPoint[]
  candidates: ForecastCandidate[]
  explanation_tr: string
  explanation_en: string
  generated_at: string
}
interface ReportRow { [key: string]: unknown }
interface ReportResult { rows: ReportRow[] }

interface ProductRow { id: number; sku: string; name: string }
interface CustomerRow { id: number; code: string; name: string }
interface SalespersonRow { id: number; full_name: string }

interface Subject {
  product_id: string
  customer_id: string
  salesperson_id: string
}

const GRANULARITIES = ['DAILY', 'WEEKLY', 'MONTHLY', 'QUARTERLY', 'YEARLY']
const HISTORY_DAYS = 90

function isoDate(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Forecasts() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const lang = currentLanguage()

  const [form, setForm] = useState<Subject>({ product_id: '', customer_id: '', salesperson_id: '' })
  const [horizon, setHorizon] = useState('14')
  const [granularity, setGranularity] = useState('DAILY')
  const [persist, setPersist] = useState(true)
  const [productTerm, setProductTerm] = useState('')
  const [customerTerm, setCustomerTerm] = useState('')
  const [submitted, setSubmitted] = useState<Subject | null>(null)
  const [result, setResult] = useState<ForecastOut | null>(null)

  const products = useQuery({
    queryKey: ['fc-products', productTerm],
    queryFn: () => api.get<Paged<ProductRow>>('/products', { q: productTerm, size: 25 }),
    enabled: can('stock.products') && productTerm.trim().length >= 2,
    throwOnError: false,
  })
  const customers = useQuery({
    queryKey: ['fc-customers', customerTerm],
    queryFn: () => api.get<Paged<CustomerRow>>('/customers', { term: customerTerm, size: 25 }),
    enabled: can('crm.customers') && customerTerm.trim().length >= 2,
    throwOnError: false,
  })
  const people = useQuery({
    queryKey: ['fc-salespersons'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons'),
    throwOnError: false,
  })

  const run = useMutation({
    mutationFn: () =>
      api.post<ForecastOut>('/analytics/forecast', {
        product_id: form.product_id ? Number(form.product_id) : null,
        customer_id: form.customer_id ? Number(form.customer_id) : null,
        salesperson_id: form.salesperson_id ? Number(form.salesperson_id) : null,
        horizon_days: Number(horizon) || 14,
        granularity,
        persist,
      }),
    onSuccess: (r) => {
      setResult(r)
      setSubmitted({ ...form })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  /* The report engine can bucket sales by day for a customer / salesperson /
     the whole company, but not for a single product — so history is only
     fetched when no product was selected. */
  const historyAvailable = Boolean(submitted && !submitted.product_id && can('analytics.reports'))

  const history = useQuery({
    queryKey: ['fc-history', submitted?.customer_id, submitted?.salesperson_id],
    queryFn: () =>
      api.post<ReportResult>('/reports/sales_daily/run', {
        params: {
          start: isoDate(-HISTORY_DAYS),
          end: isoDate(),
          customer_id: submitted?.customer_id || undefined,
          salesperson_id: submitted?.salesperson_id || undefined,
        },
      }),
    enabled: historyAvailable,
    throwOnError: false,
  })

  const chartRows = useMemo(() => {
    const rows: {
      date: string
      history: number | null
      band: [number, number] | null
      predicted: number | null
      amount: number | null
    }[] = []

    for (const r of history.data?.rows ?? []) {
      const d = r.bucket_date
      if (typeof d !== 'string') continue
      rows.push({ date: d, history: toNumber(r.total_amount as number), band: null, predicted: null, amount: null })
    }
    for (const p of result?.points ?? []) {
      rows.push({
        date: p.bucket_date,
        history: null,
        band: [toNumber(p.lower), toNumber(p.upper)],
        predicted: toNumber(p.value),
        amount: toNumber(p.amount),
      })
    }
    return rows.sort((a, b) => a.date.localeCompare(b.date))
  }, [history.data, result])

  const subjectChosen = form.product_id || form.customer_id || form.salesperson_id

  return (
    <>
      <PageHeader
        title={t('forecasts.title')}
        subtitle={result ? `${result.subject_type} · ${result.run_id}` : undefined}
        icon={<LineChartIcon className="h-5 w-5" />}
      />

      <div className="grid gap-5 xl:grid-cols-4">
        {/* ------------------------- Form ------------------------- */}
        <Card className="xl:col-span-1" title={t('forecasts.subject')} bodyClassName="p-4">
          <div className="space-y-3">
            {can('stock.products') && (
              <>
                <Field label={t('forecasts.product')} hint={t('common.search')}>
                  <input className="input" value={productTerm} placeholder={t('common.search')}
                    onChange={(e) => { setProductTerm(e.target.value); setForm((f) => ({ ...f, product_id: '' })) }} />
                </Field>
                {(products.data?.items ?? []).length > 0 && (
                  <select className="input" value={form.product_id}
                    onChange={(e) => setForm((f) => ({ ...f, product_id: e.target.value }))}>
                    <option value="">{t('common.select')}</option>
                    {(products.data?.items ?? []).map((p) => (
                      <option key={p.id} value={p.id}>{p.sku} — {p.name}</option>
                    ))}
                  </select>
                )}
              </>
            )}

            {can('crm.customers') && (
              <>
                <Field label={t('forecasts.customer')} hint={t('common.search')}>
                  <input className="input" value={customerTerm} placeholder={t('common.search')}
                    onChange={(e) => { setCustomerTerm(e.target.value); setForm((f) => ({ ...f, customer_id: '' })) }} />
                </Field>
                {(customers.data?.items ?? []).length > 0 && (
                  <select className="input" value={form.customer_id}
                    onChange={(e) => setForm((f) => ({ ...f, customer_id: e.target.value }))}>
                    <option value="">{t('common.select')}</option>
                    {(customers.data?.items ?? []).map((c) => (
                      <option key={c.id} value={c.id}>{c.code} — {c.name}</option>
                    ))}
                  </select>
                )}
              </>
            )}

            {(people.data?.items ?? []).length > 0 && (
              <Field label={t('forecasts.salesperson')}>
                <select className="input" value={form.salesperson_id}
                  onChange={(e) => setForm((f) => ({ ...f, salesperson_id: e.target.value }))}>
                  <option value="">{t('forecasts.company')}</option>
                  {(people.data?.items ?? []).map((p) => (
                    <option key={p.id} value={p.id}>{p.full_name}</option>
                  ))}
                </select>
              </Field>
            )}

            <Field label={t('forecasts.horizon')} required>
              <input type="number" min={1} max={365} className="input tabular" value={horizon}
                onChange={(e) => setHorizon(e.target.value)} />
            </Field>

            <Field label={t('forecasts.granularity')}>
              <select className="input" value={granularity} onChange={(e) => setGranularity(e.target.value)}>
                {GRANULARITIES.map((g) => (
                  <option key={g} value={g}>{t(`forecasts.granularities.${g}`)}</option>
                ))}
              </select>
            </Field>

            <label className="flex cursor-pointer items-center gap-2 text-xs text-shell-700">
              <input type="checkbox" checked={persist} onChange={() => setPersist((v) => !v)}
                className="h-3.5 w-3.5 rounded border-shell-300 text-brand-600" />
              {t('forecasts.persist')}
            </label>

            <button type="button" className="btn-primary w-full"
              disabled={run.isPending || !can('analytics.forecasts', 'EXECUTE')}
              onClick={() => run.mutate()}>
              {run.isPending ? <Spinner className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              {t('forecasts.run')}
            </button>
            {!subjectChosen && (
              <p className="text-2xs text-shell-400">{t('forecasts.company')}</p>
            )}
          </div>
        </Card>

        {/* ------------------------- Result ------------------------- */}
        <div className="space-y-5 xl:col-span-3">
          {run.isPending ? (
            <Card bodyClassName="p-0"><SkeletonRows rows={6} cols={4} /></Card>
          ) : run.isError ? (
            <Card><ErrorState error={run.error} onRetry={() => run.mutate()} /></Card>
          ) : !result ? (
            <Card><EmptyState title={t('forecasts.noResult')} /></Card>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {[
                  [t('forecasts.method'), result.method],
                  [t('forecasts.confidence'), formatPercent(result.confidence * 100)],
                  [t('forecasts.mae'), formatNumber(result.mae, { decimals: 2 })],
                  [t('forecasts.historyPoints'), formatNumber(result.history_points)],
                  [t('forecasts.totalQuantity'), formatNumber(result.total_forecast_quantity, { decimals: 1 })],
                  [t('forecasts.totalAmount'), formatMoney(result.total_forecast_amount, { compact: true })],
                ].map(([label, value]) => (
                  <div key={String(label)} className="card p-3">
                    <p className="truncate text-2xs font-medium uppercase tracking-wide text-shell-500">{label}</p>
                    <p className="tabular mt-0.5 truncate text-sm font-semibold text-shell-900">{value}</p>
                  </div>
                ))}
              </div>

              <Card title={t('forecasts.predicted')} bodyClassName="p-4">
                {chartRows.length === 0 ? (
                  <EmptyState />
                ) : (
                  <>
                    <div className="h-80 w-full">
                      <ResponsiveContainer width="100%" height="100%">
                        <ComposedChart data={chartRows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                          <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={28}
                            tickFormatter={(v: string) => formatDate(v, { short: true })} />
                          <YAxis yAxisId="qty" tick={{ fontSize: 10 }}
                            tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                          {historyAvailable && (
                            <YAxis yAxisId="amt" orientation="right" tick={{ fontSize: 10 }}
                              tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                          )}
                          <Tooltip
                            labelFormatter={(v) => formatDate(String(v))}
                            formatter={(value: number | string | (number | string)[], name: string) =>
                              Array.isArray(value)
                                ? [`${formatNumber(value[0], { decimals: 1 })} – ${formatNumber(value[1], { decimals: 1 })}`, name]
                                : [formatNumber(value, { decimals: 2 }), name]
                            }
                          />
                          <Legend wrapperStyle={{ fontSize: 11 }} />
                          <Area yAxisId="qty" type="monotone" dataKey="band" name={t('forecasts.band')}
                            stroke="none" fill="#4f46e5" fillOpacity={0.16} connectNulls={false} />
                          <Line yAxisId="qty" type="monotone" dataKey="predicted" name={t('forecasts.predicted')}
                            stroke="#4f46e5" strokeWidth={2} dot={false} connectNulls={false} />
                          {historyAvailable && (
                            <Line yAxisId="amt" type="monotone" dataKey="history" name={t('forecasts.history')}
                              stroke="#0f172a" strokeWidth={1.75} dot={false} connectNulls={false} />
                          )}
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                    {!historyAvailable && (
                      <p className="mt-2 text-2xs text-shell-400">{t('forecasts.historyUnavailable')}</p>
                    )}
                  </>
                )}
              </Card>

              <div className="grid gap-5 lg:grid-cols-2">
                <Card title={t('forecasts.explanation')} bodyClassName="p-4">
                  <p className="whitespace-pre-line text-sm leading-relaxed text-shell-700">
                    {lang === 'en' ? result.explanation_en : result.explanation_tr}
                  </p>
                </Card>

                <Card title={t('forecasts.candidates')} bodyClassName="p-0">
                  {result.candidates.length === 0 ? (
                    <EmptyState />
                  ) : (
                    <div className="table-wrap">
                      <table className="table">
                        <thead>
                          <tr>
                            <th>{t('forecasts.method')}</th>
                            <th className="text-right">{t('forecasts.mae')}</th>
                            <th className="text-right">{t('forecasts.mape')}</th>
                            <th className="text-right">{t('forecasts.rmse')}</th>
                            <th className="text-right">{t('forecasts.bias')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {result.candidates.map((c) => (
                            <tr key={c.method} className={c.method === result.method ? 'bg-brand-50' : ''}>
                              <td className="font-medium text-shell-800">{c.method}</td>
                              <td className="tabular text-right">{formatNumber(c.mae, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatPercent(c.mape)}</td>
                              <td className="tabular text-right">{formatNumber(c.rmse, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(c.bias, { decimals: 2 })}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>
              </div>

              <Card bodyClassName="p-0">
                <div className="border-b border-shell-200 px-5 py-3.5">
                  <SectionTitle>{t('forecasts.predicted')}</SectionTitle>
                </div>
                <div className="table-wrap max-h-80 overflow-y-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('common.date')}</th>
                        <th className="text-right">{t('forecasts.predicted')}</th>
                        <th className="text-right">{t('forecasts.lower')}</th>
                        <th className="text-right">{t('forecasts.upper')}</th>
                        <th className="text-right">{t('common.amount')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.points.map((p) => (
                        <tr key={p.bucket_date}>
                          <td>{p.label || formatDate(p.bucket_date, { short: true })}</td>
                          <td className="tabular text-right font-medium">{formatNumber(p.value, { decimals: 1 })}</td>
                          <td className="tabular text-right text-shell-500">{formatNumber(p.lower, { decimals: 1 })}</td>
                          <td className="tabular text-right text-shell-500">{formatNumber(p.upper, { decimals: 1 })}</td>
                          <td className="tabular text-right">{formatMoney(p.amount)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </>
          )}
        </div>
      </div>
    </>
  )
}
