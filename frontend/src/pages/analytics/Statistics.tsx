/**
 * Descriptive statistics, trend decomposition, correlations and regression.
 *
 * Every number on this page comes from `GET /analytics/statistics`, which
 * builds all six business series over one shared calendar — that is what makes
 * the correlation matrix meaningful, so nothing here is recomputed client-side.
 */
import { useQuery } from '@tanstack/react-query'
import { BarChart3 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Card, EmptyState, ErrorState, Field, LoadingBlock, PageHeader, SectionTitle } from '@/components/ui'
import { api } from '@/lib/api'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatNumber, formatPercent } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types (mirror StatisticsOut in app/schemas/analytics.py)                   */
/* -------------------------------------------------------------------------- */
interface DescriptiveStats {
  count: number; sum: number; mean: number; median: number; mode: number
  std: number; variance: number; min: number; max: number
  q1: number; q3: number; iqr: number; p90: number; p95: number; cv: number
}
interface CorrelationPair {
  subject_a: string; subject_b: string; coefficient: number
  strength: string; strength_tr: string; direction: string
  interpretation_tr: string; interpretation_en: string
}
interface StatisticsOut {
  start: string
  end: string
  days: number
  descriptive: Record<string, DescriptiveStats>
  histogram: { bins: { lower: number; upper: number; count: number; label: string; share_percent: number }[]; total: number; bin_width: number }
  trend: { slope: number; intercept: number; r_squared: number; direction: string; n: number }
  regression: { slope: number; intercept: number; r_squared: number; p_hint: number; std_error: number; n: number; is_significant: boolean }
  growth: { day_over_day: number; week_over_week: number; period_over_period: number }
  weekday_profile: { weekday: string; index: number }[]
  decomposition: Record<string, (number | null)[]>
  correlation_matrix: Record<string, Record<string, number>>
  top_correlations: CorrelationPair[]
  series: Record<string, (string | number)[]>
}

const STAT_KEYS: (keyof DescriptiveStats)[] = [
  'count', 'sum', 'mean', 'median', 'mode', 'std', 'variance',
  'min', 'q1', 'q3', 'max', 'iqr', 'p90', 'p95', 'cv',
]
const OVERLAY_SERIES = ['collections', 'visits', 'orders', 'discount', 'returns'] as const
const SERIES_COLOUR: Record<string, string> = {
  sales: '#4f46e5',
  collections: '#059669',
  visits: '#d97706',
  orders: '#0891b2',
  discount: '#c026d3',
  returns: '#dc2626',
}

function isoDate(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

/** −1 → red, 0 → neutral, +1 → indigo. */
function heatColour(v: number): string {
  const a = Math.min(1, Math.abs(v))
  return v >= 0 ? `rgba(79, 70, 229, ${a * 0.75})` : `rgba(220, 38, 38, ${a * 0.75})`
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Statistics() {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const [start, setStart] = useState(isoDate(-89))
  const [end, setEnd] = useState(isoDate())
  const [metric, setMetric] = useState('sales')
  const [overlay, setOverlay] = useState<string>('')

  const stats = useQuery({
    queryKey: ['statistics', start, end],
    queryFn: () => api.get<StatisticsOut>('/analytics/statistics', { start, end }),
  })

  const data = stats.data

  const chartRows = useMemo(() => {
    if (!data) return []
    const labels = (data.series.labels ?? []) as string[]
    const sales = (data.series.sales ?? []) as number[]
    const ma = data.decomposition.trend ?? []
    const extra = overlay ? ((data.series[overlay] ?? []) as number[]) : []
    return labels.map((label, i) => ({
      label,
      sales: sales[i] ?? 0,
      ma: ma[i] ?? null,
      overlay: overlay ? (extra[i] ?? 0) : null,
    }))
  }, [data, overlay])

  const matrixKeys = useMemo(() => Object.keys(data?.correlation_matrix ?? {}), [data])
  const selected = data?.descriptive[metric]

  return (
    <>
      <PageHeader
        title={t('statistics.title')}
        subtitle={data ? `${formatDate(data.start, { short: true })} — ${formatDate(data.end, { short: true })} · ${formatNumber(data.days)} ${t('common.date').toLowerCase()}` : undefined}
        icon={<BarChart3 className="h-5 w-5" />}
        actions={
          <div className="flex flex-wrap items-end gap-2">
            <Field label={t('common.from')}>
              <input type="date" className="input" value={start} onChange={(e) => setStart(e.target.value)} />
            </Field>
            <Field label={t('common.to')}>
              <input type="date" className="input" value={end} onChange={(e) => setEnd(e.target.value)} />
            </Field>
          </div>
        }
      />

      {stats.isLoading ? (
        <Card><LoadingBlock /></Card>
      ) : stats.isError ? (
        <Card><ErrorState error={stats.error} onRetry={() => void stats.refetch()} /></Card>
      ) : !data ? (
        <Card><EmptyState /></Card>
      ) : (
        <div className="space-y-5">
          {/* ---------------- Growth ---------------- */}
          <div className="grid gap-3 sm:grid-cols-3">
            {[
              [t('statistics.dod'), data.growth.day_over_day],
              [t('statistics.wow'), data.growth.week_over_week],
              [t('statistics.pop'), data.growth.period_over_period],
            ].map(([label, value]) => (
              <div key={String(label)} className="card p-4">
                <p className="text-2xs font-medium uppercase tracking-wide text-shell-500">{label}</p>
                <p className={`tabular mt-1 text-2xl font-semibold ${
                  Number(value) > 0.5 ? 'text-ok-600' : Number(value) < -0.5 ? 'text-danger-600' : 'text-shell-800'
                }`}>
                  {formatPercent(Number(value), { sign: true })}
                </p>
              </div>
            ))}
          </div>

          {/* ---------------- Descriptive ---------------- */}
          <Card
            title={t('statistics.descriptive')}
            bodyClassName="p-4"
            actions={
              <select className="input py-1 text-xs" value={metric} onChange={(e) => setMetric(e.target.value)}>
                {Object.keys(data.descriptive).map((k) => (
                  <option key={k} value={k}>{t(`statistics.metrics.${k}`, { defaultValue: k })}</option>
                ))}
              </select>
            }
          >
            {!selected ? (
              <EmptyState />
            ) : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
                {STAT_KEYS.map((k) => (
                  <div key={k} className="rounded-lg border border-shell-200 p-2.5">
                    <p className="truncate text-2xs uppercase tracking-wide text-shell-500">
                      {t(`statistics.${k}`, { defaultValue: k })}
                    </p>
                    <p className="tabular mt-0.5 text-sm font-semibold text-shell-900">
                      {k === 'cv'
                        ? formatPercent(selected[k])
                        : formatNumber(selected[k], { decimals: k === 'count' ? 0 : 2, compact: Math.abs(selected[k]) >= 1e6 })}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* ---------------- Time series ---------------- */}
          <Card
            title={t('statistics.timeSeries')}
            bodyClassName="p-4"
            actions={
              <select className="input py-1 text-xs" value={overlay} onChange={(e) => setOverlay(e.target.value)}>
                <option value="">{t('common.none')}</option>
                {OVERLAY_SERIES.map((s) => (
                  <option key={s} value={s}>{t(`statistics.metrics.${s}`, { defaultValue: s })}</option>
                ))}
              </select>
            }
          >
            {chartRows.length === 0 ? (
              <EmptyState />
            ) : (
              <div className="h-72 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={chartRows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} minTickGap={28}
                      tickFormatter={(v: string) => formatDate(v, { short: true })} />
                    <YAxis yAxisId="left" tick={{ fontSize: 10 }}
                      tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                    {overlay && (
                      <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10 }}
                        tickFormatter={(v: number) => formatNumber(v, { compact: true })} />
                    )}
                    <Tooltip
                      labelFormatter={(v) => formatDate(String(v))}
                      formatter={(value: number | string, name: string) => [formatNumber(value, { decimals: 2 }), name]}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line yAxisId="left" type="monotone" dataKey="sales" name={t('statistics.metrics.sales')}
                      stroke={SERIES_COLOUR.sales} strokeWidth={2} dot={false} />
                    <Line yAxisId="left" type="monotone" dataKey="ma" name={t('statistics.movingAverage')}
                      stroke="#0f172a" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls />
                    {overlay && (
                      <Line yAxisId="right" type="monotone" dataKey="overlay"
                        name={t(`statistics.metrics.${overlay}`, { defaultValue: overlay })}
                        stroke={SERIES_COLOUR[overlay] ?? '#64748b'} strokeWidth={1.75} dot={false} />
                    )}
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>

          <div className="grid gap-5 lg:grid-cols-2">
            {/* ---------------- Correlation heatmap ---------------- */}
            <Card title={t('statistics.correlation')} bodyClassName="p-0">
              {matrixKeys.length === 0 ? (
                <EmptyState />
              ) : (
                <>
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th />
                          {matrixKeys.map((k) => (
                            <th key={k} className="text-center text-2xs">{k.split(' / ')[lang === 'en' ? 1 : 0] ?? k}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {matrixKeys.map((row) => (
                          <tr key={row}>
                            <td className="whitespace-nowrap text-xs font-medium text-shell-700">
                              {row.split(' / ')[lang === 'en' ? 1 : 0] ?? row}
                            </td>
                            {matrixKeys.map((col) => {
                              const v = data.correlation_matrix[row]?.[col] ?? 0
                              return (
                                <td key={col} className="tabular p-0 text-center">
                                  <span
                                    className="flex h-9 items-center justify-center text-2xs font-medium"
                                    style={{
                                      background: heatColour(v),
                                      color: Math.abs(v) > 0.6 ? '#ffffff' : '#1e293b',
                                    }}
                                  >
                                    {formatNumber(v, { decimals: 2 })}
                                  </span>
                                </td>
                              )
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {data.top_correlations.length > 0 && (
                    <div className="border-t border-shell-200 p-4">
                      <SectionTitle>{t('statistics.topCorrelations')}</SectionTitle>
                      <ul className="space-y-1.5 text-xs">
                        {data.top_correlations.slice(0, 5).map((c, i) => (
                          <li key={i} className="flex items-start justify-between gap-3">
                            <span className="min-w-0 flex-1 text-shell-600">
                              {lang === 'en' ? c.interpretation_en : c.interpretation_tr}
                            </span>
                            <span className="tabular shrink-0 font-semibold text-shell-800">
                              {formatNumber(c.coefficient, { decimals: 2 })}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </Card>

            {/* ---------------- Regression ---------------- */}
            <div className="space-y-5">
              <Card title={t('statistics.regression')} bodyClassName="p-4">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {[
                    [t('statistics.slope'), formatNumber(data.regression.slope, { decimals: 3 })],
                    [t('statistics.intercept'), formatNumber(data.regression.intercept, { decimals: 2 })],
                    [t('statistics.rSquared'), formatNumber(data.regression.r_squared, { decimals: 3 })],
                    [t('statistics.stdError'), formatNumber(data.regression.std_error, { decimals: 3 })],
                    [t('statistics.observations'), formatNumber(data.regression.n)],
                    [t('statistics.trendDirection'), data.trend.direction],
                  ].map(([label, value]) => (
                    <div key={String(label)} className="rounded-lg border border-shell-200 p-2.5">
                      <p className="truncate text-2xs uppercase tracking-wide text-shell-500">{label}</p>
                      <p className="tabular mt-0.5 text-sm font-semibold text-shell-900">{value}</p>
                    </div>
                  ))}
                </div>
                <p className={`mt-3 text-xs font-medium ${data.regression.is_significant ? 'text-ok-700' : 'text-shell-500'}`}>
                  {data.regression.is_significant ? t('statistics.significant') : t('statistics.notSignificant')}
                  {' · p ≈ '}
                  {formatNumber(data.regression.p_hint, { decimals: 3 })}
                </p>
              </Card>

              <Card title={t('statistics.weekdayProfile')} bodyClassName="p-4">
                {data.weekday_profile.length === 0 ? (
                  <EmptyState />
                ) : (
                  <div className="h-48 w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data.weekday_profile} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                        <XAxis dataKey="weekday" tick={{ fontSize: 10 }} />
                        <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => formatNumber(v, { decimals: 1 })} />
                        <Tooltip formatter={(v: number | string) => formatNumber(v, { decimals: 2 })} />
                        <Bar dataKey="index" name={t('statistics.index')} fill="#4f46e5" radius={[3, 3, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </Card>
            </div>
          </div>

          {/* ---------------- Histogram ---------------- */}
          <Card title={t('statistics.histogram')} bodyClassName="p-4">
            {data.histogram.bins.length === 0 ? (
              <EmptyState />
            ) : (
              <div className="h-56 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.histogram.bins} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 9 }} interval={0} angle={-20} textAnchor="end" height={50} />
                    <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
                    <Tooltip formatter={(v: number | string) => formatNumber(v)} />
                    <Bar dataKey="count" name={t('statistics.count')} fill="#0891b2" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>
        </div>
      )}
    </>
  )
}
