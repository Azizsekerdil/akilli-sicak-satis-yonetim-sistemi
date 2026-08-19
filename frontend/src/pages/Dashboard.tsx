/**
 * Dashboard — KPI grid, trend charts and the "top N" league tables.
 *
 * Every figure comes from the backend's own aggregation; nothing is computed
 * here beyond formatting, so the dashboard and the reports can never disagree.
 *
 * The range selector maps onto what the API actually accepts: the KPI and
 * chart endpoints take a reference date (plus a window length for the charts),
 * while the performance tables take an explicit start/end pair.
 */
import { useQuery } from '@tanstack/react-query'
import { LayoutDashboard, RefreshCw } from 'lucide-react'
import { useMemo, useState, type ReactElement } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatNumber, formatPercent } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  KpiTile,
  PageHeader,
  SkeletonRows,
  type KpiCardData,
} from '@/components/ui'

/** Palette for every series on this screen — brand, ok, warn, danger, info, violet. */
export const CHART_COLORS = [
  '#4f46e5', '#10b981', '#f59e0b', '#ef4444',
  '#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6',
]

/* -------------------------------------------------------------------------- */
/* Types (mirror app/schemas/common.py and app/schemas/analytics.py)          */
/* -------------------------------------------------------------------------- */
interface SeriesPoint {
  label: string
  value: number | string
  secondary?: number | string | null
  bucket_date?: string | null
}

interface ChartSeries {
  key: string
  name_tr: string
  name_en: string
  chart_type: string
  points: SeriesPoint[]
  unit?: string | null
}

interface KpiResponse {
  as_of: string
  generated_at: string
  cards: KpiCardData[]
}

interface ChartResponse {
  as_of: string
  generated_at: string
  charts: ChartSeries[]
}

interface PerformanceRow {
  rank: number
  key: string
  code?: string | null
  label: string
  group_label?: string | null
  secondary_label?: string | null
  sales_amount: number | string
  net_amount: number | string
  margin_amount: number | string
  margin_percent: number
  quantity?: number | string | null
  order_count: number
  customer_count: number
  share_percent: number
}

type RangeKey = 'today' | 'week' | 'month' | 'year' | 'custom'

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`

function resolveRange(key: RangeKey, custom: { start: string; end: string }) {
  const today = new Date()
  switch (key) {
    case 'today':
      return { start: iso(today), end: iso(today) }
    case 'week': {
      const d = new Date(today)
      d.setDate(d.getDate() - 6)
      return { start: iso(d), end: iso(today) }
    }
    case 'month':
      return { start: iso(new Date(today.getFullYear(), today.getMonth(), 1)), end: iso(today) }
    case 'year':
      return { start: iso(new Date(today.getFullYear(), 0, 1)), end: iso(today) }
    default:
      return custom
  }
}

/** Chart window length the backend accepts (7…365 days). */
function windowDays(start: string, end: string): number {
  const a = new Date(start).getTime()
  const b = new Date(end).getTime()
  if (Number.isNaN(a) || Number.isNaN(b)) return 30
  return Math.min(365, Math.max(7, Math.round((b - a) / 864e5) + 1))
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Dashboard() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const lang = currentLanguage()

  const [range, setRange] = useState<RangeKey>('month')
  const [custom, setCustom] = useState(() => {
    const today = iso(new Date())
    return { start: today, end: today }
  })

  const params = useMemo(() => resolveRange(range, custom), [range, custom])
  const days = useMemo(() => windowDays(params.start, params.end), [params])

  const kpis = useQuery({
    queryKey: ['dashboard-kpis', params.end],
    queryFn: () => api.get<KpiResponse>('/analytics/dashboard', { on_date: params.end }),
  })

  const charts = useQuery({
    queryKey: ['dashboard-charts', params.end, days],
    queryFn: () =>
      api.get<ChartResponse>('/analytics/dashboard/charts', { on_date: params.end, days }),
  })

  const canRank = can('analytics.reports', 'VIEW')
  const rankParams = { start: params.start, end: params.end, limit: 8 }

  const topSalespersons = useQuery({
    queryKey: ['top-salespersons', rankParams],
    queryFn: () => api.get<PerformanceRow[]>('/analytics/salespersons', rankParams),
    enabled: canRank,
  })
  const topProducts = useQuery({
    queryKey: ['top-products', rankParams],
    queryFn: () => api.get<PerformanceRow[]>('/analytics/products', rankParams),
    enabled: canRank,
  })
  const topRegions = useQuery({
    queryKey: ['top-regions', rankParams],
    queryFn: () => api.get<PerformanceRow[]>('/analytics/regions', rankParams),
    enabled: canRank,
  })

  const refreshAll = () => {
    void kpis.refetch()
    void charts.refetch()
    if (canRank) {
      void topSalespersons.refetch()
      void topProducts.refetch()
      void topRegions.refetch()
    }
  }

  const RANGES: [RangeKey, string][] = [
    ['today', t('common.today')],
    ['week', t('common.thisWeek')],
    ['month', t('common.thisMonth')],
    ['year', t('common.thisYear')],
    ['custom', t('common.custom')],
  ]

  const cards = kpis.data?.cards ?? []
  const series = charts.data?.charts ?? []

  return (
    <>
      <PageHeader
        title={t('dashboard.title')}
        subtitle={
          kpis.data ? `${t('dashboard.asOf')}: ${formatDate(kpis.data.as_of)}` : t('app.name')
        }
        icon={<LayoutDashboard className="h-5 w-5" />}
        actions={
          <button type="button" className="btn-secondary btn-sm" onClick={refreshAll}>
            <RefreshCw className="h-3.5 w-3.5" />
            {t('common.refresh')}
          </button>
        }
      />

      {/* Range selector */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {RANGES.map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setRange(key)}
            className={range === key ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
          >
            {label}
          </button>
        ))}
        {range === 'custom' && (
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="date"
              aria-label={t('common.from')}
              className="input w-auto py-1.5 text-xs"
              value={custom.start}
              max={custom.end}
              onChange={(e) => setCustom((c) => ({ ...c, start: e.target.value }))}
            />
            <span className="text-shell-400">–</span>
            <input
              type="date"
              aria-label={t('common.to')}
              className="input w-auto py-1.5 text-xs"
              value={custom.end}
              min={custom.start}
              onChange={(e) => setCustom((c) => ({ ...c, end: e.target.value }))}
            />
          </div>
        )}
      </div>

      {/* KPI grid */}
      {kpis.isLoading ? (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="skeleton h-24 rounded-xl" />
          ))}
        </div>
      ) : kpis.isError ? (
        <Card className="mb-6">
          <ErrorState error={kpis.error} onRetry={() => void kpis.refetch()} />
        </Card>
      ) : cards.length === 0 ? (
        <Card className="mb-6">
          <EmptyState />
        </Card>
      ) : (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {cards.map((k) => (
            <KpiTile key={k.key} kpi={k} lang={lang} />
          ))}
        </div>
      )}

      {/* Charts */}
      {charts.isLoading ? (
        <div className="mb-6 grid gap-5 xl:grid-cols-2">
          <div className="skeleton h-72 rounded-xl" />
          <div className="skeleton h-72 rounded-xl" />
        </div>
      ) : charts.isError ? (
        <Card className="mb-6">
          <ErrorState error={charts.error} onRetry={() => void charts.refetch()} />
        </Card>
      ) : series.length === 0 ? (
        <Card className="mb-6">
          <EmptyState title={t('dashboard.noCharts')} />
        </Card>
      ) : (
        <div className="mb-6 grid gap-5 xl:grid-cols-2">
          {series.map((s) => (
            <ChartCard key={s.key} series={s} lang={lang} />
          ))}
        </div>
      )}

      {/* Top N tables */}
      {!canRank ? (
        <Card>
          <EmptyState title={t('dashboard.performanceLocked')} />
        </Card>
      ) : (
        <div className="grid gap-5 xl:grid-cols-3">
          <TopTable title={t('dashboard.topSalespersons')} query={topSalespersons} />
          <TopTable title={t('dashboard.topProducts')} query={topProducts} />
          <TopTable title={t('dashboard.topRegions')} query={topRegions} />
        </div>
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Charts                                                                     */
/* -------------------------------------------------------------------------- */
const AXIS_TICK = { fontSize: 11, fill: '#94a3b8' }
const GRID_STROKE = '#f1f5f9'

function ChartCard({ series, lang }: { series: ChartSeries; lang: string }) {
  const name = lang === 'en' ? series.name_en : series.name_tr
  const unit = (series.unit ?? '').toUpperCase()
  const isMoney = unit === 'TRY' || unit === 'MONEY'
  const isPercent = unit === '%' || unit === 'PERCENT'

  const compact = (v: number) =>
    isMoney ? formatMoney(v, { compact: true }) : formatNumber(v, { compact: true })
  // recharts hands the formatter an untyped cell value.
  const full = (v: any): string =>
    isMoney ? formatMoney(v) : isPercent ? formatPercent(v) : formatNumber(v, { decimals: 2 })

  if (!series.points || series.points.length === 0) {
    return (
      <Card title={name}>
        <EmptyState />
      </Card>
    )
  }

  const hasSecondary = series.points.some(
    (p) => p.secondary !== null && p.secondary !== undefined,
  )

  const grid = (
    <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} vertical={false} />
  )
  const xAxis = <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} />
  const yAxis = (
    <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} tickFormatter={compact} width={64} />
  )
  const tip = (
    <Tooltip
      formatter={full}
      contentStyle={{
        borderRadius: 8,
        border: '1px solid #e2e8f0',
        fontSize: 12,
        boxShadow: '0 10px 32px -8px rgb(15 23 42 / 0.22)',
      }}
    />
  )
  const margin = { top: 8, right: 8, left: 0, bottom: 0 }

  let chart: ReactElement
  if (series.chart_type === 'bar') {
    chart = (
      <BarChart data={series.points} margin={margin}>
        {grid}
        {xAxis}
        {yAxis}
        {tip}
        <Bar dataKey="value" fill={CHART_COLORS[0]} radius={[4, 4, 0, 0]} />
        {hasSecondary && <Bar dataKey="secondary" fill={CHART_COLORS[1]} radius={[4, 4, 0, 0]} />}
      </BarChart>
    )
  } else if (series.chart_type === 'pie' || series.chart_type === 'donut') {
    chart = (
      <PieChart>
        {tip}
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Pie
          data={series.points}
          dataKey="value"
          nameKey="label"
          cx="50%"
          cy="50%"
          outerRadius={86}
          innerRadius={series.chart_type === 'donut' ? 52 : 0}
          paddingAngle={2}
        >
          {series.points.map((p, i) => (
            <Cell key={p.label ?? i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
          ))}
        </Pie>
      </PieChart>
    )
  } else if (series.chart_type === 'area') {
    chart = (
      <AreaChart data={series.points} margin={margin}>
        <defs>
          <linearGradient id={`grad-${series.key}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={CHART_COLORS[0]} stopOpacity={0.32} />
            <stop offset="95%" stopColor={CHART_COLORS[0]} stopOpacity={0} />
          </linearGradient>
        </defs>
        {grid}
        {xAxis}
        {yAxis}
        {tip}
        <Area
          type="monotone"
          dataKey="value"
          stroke={CHART_COLORS[0]}
          strokeWidth={2}
          fill={`url(#grad-${series.key})`}
        />
      </AreaChart>
    )
  } else {
    chart = (
      <LineChart data={series.points} margin={margin}>
        {grid}
        {xAxis}
        {yAxis}
        {tip}
        {hasSecondary && <Legend wrapperStyle={{ fontSize: 11 }} />}
        <Line type="monotone" dataKey="value" stroke={CHART_COLORS[0]} strokeWidth={2} dot={false} />
        {hasSecondary && (
          <Line
            type="monotone"
            dataKey="secondary"
            stroke={CHART_COLORS[1]}
            strokeWidth={2}
            strokeDasharray="4 4"
            dot={false}
          />
        )}
      </LineChart>
    )
  }

  return (
    <Card title={name} bodyClassName="p-3">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          {chart}
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
/* League tables                                                              */
/* -------------------------------------------------------------------------- */
interface TopQuery {
  data?: PerformanceRow[]
  isLoading: boolean
  isError: boolean
  error: unknown
  refetch: () => unknown
}

function TopTable({ title, query }: { title: string; query: TopQuery }) {
  const { t } = useTranslation()
  const rows = query.data ?? []

  return (
    <Card title={title} bodyClassName="p-0">
      {query.isLoading ? (
        <SkeletonRows rows={5} cols={3} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="w-10">{t('common.rank')}</th>
                <th>{t('common.name')}</th>
                <th className="text-right">{t('common.amount')}</th>
                <th className="text-right">{t('common.share')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key || row.rank}>
                  <td className="tabular text-shell-400">{row.rank}</td>
                  <td>
                    <span className="block max-w-[14rem] truncate font-medium text-shell-800">
                      {row.label}
                    </span>
                    {(row.secondary_label || row.group_label) && (
                      <span className="block max-w-[14rem] truncate text-2xs text-shell-400">
                        {row.secondary_label || row.group_label}
                      </span>
                    )}
                  </td>
                  <td className="tabular text-right font-medium">
                    {formatMoney(row.sales_amount, { compact: true })}
                  </td>
                  <td className="tabular text-right text-shell-500">
                    {formatPercent(row.share_percent)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
