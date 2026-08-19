/**
 * Token / Maliyet — AI usage and budget dashboard.
 *
 * Two questions this screen has to answer without scrolling: "what has AI cost
 * us this month?" and "are we about to blow the budget?".  Everything else —
 * the breakdowns by provider, model, user and agent — is there to explain the
 * answer.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Coins } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Card, EmptyState, ErrorState, LoadingBlock, PageHeader } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDate, formatMoney, formatNumber, toNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface UsageRow {
  key: string
  label: string
  bucket_date?: string | null
  requests: number
  errors: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cost: number | string
  avg_latency_ms: number
}

interface UsageSummary {
  start: string
  end: string
  group_by: string
  rows: UsageRow[]
  total_requests: number
  total_errors: number
  total_tokens: number
  total_cost: number | string
  currency: string
}

interface Budget {
  spent_this_month: number | string
  budget: number | string
  percent: number
  warn: boolean
  exceeded: boolean
  currency: string
  period_start?: string | null
}

const GROUPS = [
  { value: 'provider', labelKey: 'aiUsage.byProvider' },
  { value: 'model', labelKey: 'aiUsage.byModel' },
  { value: 'user', labelKey: 'aiUsage.byUser' },
  { value: 'agent', labelKey: 'aiUsage.byAgent' },
] as const

const iso = (d: Date) => d.toISOString().slice(0, 10)

function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'warn' | 'danger'
}) {
  return (
    <div
      className={`card p-4 ${
        tone === 'danger'
          ? 'ring-1 ring-danger-500/30'
          : tone === 'warn'
            ? 'ring-1 ring-warn-500/30'
            : ''
      }`}
    >
      <p className="truncate text-xs font-medium uppercase tracking-wide text-shell-500">{label}</p>
      <p className="tabular mt-1.5 text-2xl font-semibold text-shell-900">{value}</p>
      {hint && <p className="tabular mt-1 text-2xs text-shell-400">{hint}</p>}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AIUsage() {
  const { t } = useTranslation()

  const today = iso(new Date())
  const monthStart = useMemo(() => {
    const d = new Date()
    d.setDate(1)
    return iso(d)
  }, [])

  const [start, setStart] = useState(monthStart)
  const [end, setEnd] = useState(today)
  const [groupBy, setGroupBy] = useState<string>('provider')

  const todayQuery = useQuery({
    queryKey: ['ai', 'usage', 'today', today],
    queryFn: () => api.get<UsageSummary>('/ai/usage', { start: today, end: today, group_by: 'day' }),
  })

  const dailyQuery = useQuery({
    queryKey: ['ai', 'usage', 'day', start, end],
    queryFn: () => api.get<UsageSummary>('/ai/usage', { start, end, group_by: 'day' }),
  })

  const groupQuery = useQuery({
    queryKey: ['ai', 'usage', groupBy, start, end],
    queryFn: () => api.get<UsageSummary>('/ai/usage', { start, end, group_by: groupBy }),
  })

  const budgetQuery = useQuery({
    queryKey: ['ai', 'budget'],
    queryFn: () => api.get<Budget>('/ai/budget'),
  })

  const daily = dailyQuery.data
  const currency = daily?.currency ?? 'USD'

  const dayChart = useMemo(
    () =>
      (daily?.rows ?? []).map((row) => ({
        label: formatDate(row.bucket_date ?? row.key, { short: true }),
        input: row.input_tokens,
        output: row.output_tokens,
        cost: toNumber(row.cost),
      })),
    [daily?.rows],
  )

  const groupChart = useMemo(
    () =>
      (groupQuery.data?.rows ?? []).map((row) => ({
        label: row.label || row.key,
        tokens: row.total_tokens,
        cost: toNumber(row.cost),
        requests: row.requests,
      })),
    [groupQuery.data?.rows],
  )

  const budget = budgetQuery.data
  const percent = budget ? Math.min(100, Math.max(0, budget.percent)) : 0
  const inputTokens = (daily?.rows ?? []).reduce((sum, r) => sum + r.input_tokens, 0)
  const outputTokens = (daily?.rows ?? []).reduce((sum, r) => sum + r.output_tokens, 0)

  return (
    <div>
      <PageHeader
        title={t('aiUsage.title')}
        subtitle={t('aiUsage.subtitle')}
        icon={<Coins className="h-5 w-5" />}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="date"
              className="input w-auto"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
            <input
              type="date"
              className="input w-auto"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        }
      />

      {/* Totals */}
      <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Tile
          label={t('aiUsage.today')}
          value={formatNumber(todayQuery.data?.total_tokens ?? 0)}
          hint={`${formatNumber(todayQuery.data?.total_requests ?? 0)} ${t('aiUsage.requests')} · ${formatMoney(
            todayQuery.data?.total_cost ?? 0,
            { currency: todayQuery.data?.currency ?? currency, decimals: 4 },
          )}`}
        />
        <Tile
          label={t('aiUsage.thisMonth')}
          value={formatNumber(daily?.total_tokens ?? 0)}
          hint={`${formatNumber(daily?.total_requests ?? 0)} ${t('aiUsage.requests')} · ${formatNumber(
            daily?.total_errors ?? 0,
          )} ${t('aiUsage.errors')}`}
        />
        <Tile
          label={`${t('aiUsage.inputTokens')} / ${t('aiUsage.outputTokens')}`}
          value={`${formatNumber(inputTokens, { compact: true })} / ${formatNumber(outputTokens, { compact: true })}`}
          hint={`${t('aiUsage.totalTokens')}: ${formatNumber(inputTokens + outputTokens)}`}
        />
        <Tile
          label={t('aiUsage.estimated')}
          value={formatMoney(daily?.total_cost ?? 0, { currency, decimals: 4 })}
          tone={budget?.exceeded ? 'danger' : budget?.warn ? 'warn' : undefined}
        />
      </div>

      {/* Budget */}
      <Card title={t('aiUsage.budget')} className="mb-4">
        {budgetQuery.isLoading ? (
          <LoadingBlock />
        ) : budgetQuery.isError ? (
          <ErrorState error={budgetQuery.error} onRetry={() => void budgetQuery.refetch()} />
        ) : budget ? (
          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="tabular text-sm text-shell-700">
                {t('aiUsage.budgetSpent', {
                  spent: formatMoney(budget.spent_this_month, {
                    currency: budget.currency,
                    decimals: 2,
                  }),
                  budget: formatMoney(budget.budget, { currency: budget.currency, decimals: 2 }),
                })}
              </p>
              <span
                className={
                  budget.exceeded ? 'badge-danger' : budget.warn ? 'badge-warn' : 'badge-ok'
                }
              >
                {formatNumber(budget.percent, { decimals: 1 })}%
              </span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-shell-100">
              <div
                className={`h-full rounded-full ${
                  budget.exceeded ? 'bg-danger-500' : budget.warn ? 'bg-warn-500' : 'bg-ok-500'
                }`}
                style={{ width: `${percent}%` }}
              />
            </div>
            {(budget.warn || budget.exceeded) && (
              <p
                className={`mt-2 flex items-center gap-2 text-xs ${
                  budget.exceeded ? 'text-danger-700' : 'text-warn-700'
                }`}
              >
                <AlertTriangle className="h-4 w-4" />
                {budget.exceeded ? t('aiUsage.budgetExceeded') : t('aiUsage.budgetWarn')}
              </p>
            )}
            {budget.period_start && (
              <p className="tabular mt-2 text-2xs text-shell-400">
                {formatDate(budget.period_start)}
              </p>
            )}
          </div>
        ) : (
          <EmptyState title={t('aiUsage.noUsage')} />
        )}
      </Card>

      {/* Charts */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title={t('aiUsage.byDay')}>
          {dailyQuery.isLoading ? (
            <LoadingBlock />
          ) : dailyQuery.isError ? (
            <ErrorState error={dailyQuery.error} onRetry={() => void dailyQuery.refetch()} />
          ) : dayChart.length === 0 ? (
            <EmptyState title={t('aiUsage.noUsage')} />
          ) : (
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={dayChart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} width={56} />
                  <Tooltip formatter={(v: number | string) => formatNumber(v)} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Area
                    type="monotone"
                    dataKey="input"
                    name={t('aiUsage.inputTokens')}
                    stackId="1"
                    stroke="#2563eb"
                    fill="#bfdbfe"
                  />
                  <Area
                    type="monotone"
                    dataKey="output"
                    name={t('aiUsage.outputTokens')}
                    stackId="1"
                    stroke="#7c3aed"
                    fill="#ddd6fe"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card
          title={t(GROUPS.find((g) => g.value === groupBy)?.labelKey ?? 'aiUsage.byProvider')}
          actions={
            <select
              className="input w-auto py-1 text-xs"
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value)}
            >
              {GROUPS.map((g) => (
                <option key={g.value} value={g.value}>
                  {t(g.labelKey)}
                </option>
              ))}
            </select>
          }
        >
          {groupQuery.isLoading ? (
            <LoadingBlock />
          ) : groupQuery.isError ? (
            <ErrorState error={groupQuery.error} onRetry={() => void groupQuery.refetch()} />
          ) : groupChart.length === 0 ? (
            <EmptyState title={t('aiUsage.noUsage')} />
          ) : (
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={groupChart} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} width={56} />
                  <Tooltip formatter={(v: number | string) => formatNumber(v)} />
                  <Bar dataKey="tokens" name={t('aiUsage.totalTokens')} fill="#2563eb" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
      </div>

      {/* Detail table */}
      <Card
        title={t(GROUPS.find((g) => g.value === groupBy)?.labelKey ?? 'aiUsage.byProvider')}
        className="mt-4"
        bodyClassName="p-0"
      >
        {groupQuery.isLoading ? (
          <LoadingBlock />
        ) : (groupQuery.data?.rows ?? []).length === 0 ? (
          <EmptyState title={t('aiUsage.noUsage')} />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('common.name')}</th>
                  <th className="text-right">{t('aiUsage.requests')}</th>
                  <th className="text-right">{t('aiUsage.errors')}</th>
                  <th className="text-right">{t('aiUsage.inputTokens')}</th>
                  <th className="text-right">{t('aiUsage.outputTokens')}</th>
                  <th className="text-right">{t('aiUsage.totalTokens')}</th>
                  <th className="text-right">{t('aiUsage.avgLatency')}</th>
                  <th className="text-right">{t('aiUsage.cost')}</th>
                </tr>
              </thead>
              <tbody>
                {(groupQuery.data?.rows ?? []).map((row) => (
                  <tr key={row.key}>
                    <td className="font-medium text-shell-800">{row.label || row.key}</td>
                    <td className="tabular text-right">{formatNumber(row.requests)}</td>
                    <td className="tabular text-right">{formatNumber(row.errors)}</td>
                    <td className="tabular text-right">{formatNumber(row.input_tokens)}</td>
                    <td className="tabular text-right">{formatNumber(row.output_tokens)}</td>
                    <td className="tabular text-right">{formatNumber(row.total_tokens)}</td>
                    <td className="tabular text-right">
                      {formatNumber(row.avg_latency_ms, { decimals: 0 })} ms
                    </td>
                    <td className="tabular text-right">
                      {formatMoney(row.cost, { currency, decimals: 4 })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
