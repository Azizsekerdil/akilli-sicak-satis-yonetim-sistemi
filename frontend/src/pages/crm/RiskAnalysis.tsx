/**
 * Risk Analizi / Risk analysis.
 *
 * Four questions on one page: how old is the receivable, who owes the most,
 * which items are already late, and which customers are quietly slipping away
 * (stopped ordering, or ordering less than they used to).
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ShieldAlert, TrendingDown, Users } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { api } from '@/lib/api'
import { formatDate, formatMoney, formatNumber, formatPercent, toNumber } from '@/lib/format'
import {
  Card, EmptyState, ErrorState, Field, PageHeader, SkeletonRows,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface AgingTotals {
  current: number | string; d1_30: number | string; d31_60: number | string
  d61_90: number | string; d90_plus: number | string
  total: number | string; overdue: number | string
}

interface AgingSummary {
  as_of: string
  totals: AgingTotals
}

interface TopDebtor {
  customer_id: number; customer_code: string; customer_name: string
  balance: number | string; overdue_balance: number | string
  credit_limit: number | string; risk_score: number
  last_payment_date?: string | null; phone?: string | null
}

interface OverdueItem {
  ledger_id: number; customer_id: number; customer_code: string; customer_name: string
  entry_type: string; due_date?: string | null; reference_no?: string | null
  open_amount: number | string; days_past_due: number; bucket: string
}

interface CustomerLite {
  id: number; code: string; name: string; trade_name?: string | null
  city?: string | null; phone?: string | null; balance: number | string
  risk_score: number
}

interface ChurnItem {
  customer: CustomerLite
  days_since_last_order: number
  last_order_date?: string | null
  total_sales_amount: number | string
  balance: number | string
}

interface DecliningItem {
  customer: CustomerLite
  current_amount: number | string
  previous_amount: number | string
  drop_percent: number
  days: number
}

const BUCKETS: { key: keyof AgingTotals; label: string }[] = [
  { key: 'current', label: 'crm.bucketCurrent' },
  { key: 'd1_30', label: 'crm.bucket1_30' },
  { key: 'd31_60', label: 'crm.bucket31_60' },
  { key: 'd61_90', label: 'crm.bucket61_90' },
  { key: 'd90_plus', label: 'crm.bucket90' },
]

const BUCKET_TONE: Record<string, string> = {
  current: 'badge-muted', d1_30: 'badge-info', d31_60: 'badge-warn',
  d61_90: 'badge-warn', d90_plus: 'badge-danger',
}

function riskTone(score: number): string {
  if (score >= 70) return 'badge-danger'
  if (score >= 50) return 'badge-warn'
  if (score >= 30) return 'badge-info'
  return 'badge-ok'
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function RiskAnalysis() {
  const { t } = useTranslation()

  const [minDays, setMinDays] = useState('1')
  const [churnDays, setChurnDays] = useState('90')
  const [declineDays, setDeclineDays] = useState('30')
  const [dropPercent, setDropPercent] = useState('20')

  const summary = useQuery({
    queryKey: ['risk-aging-summary'],
    queryFn: () => api.get<AgingSummary>('/customers/aging-summary', { limit: 1 }),
  })

  const debtors = useQuery({
    queryKey: ['top-debtors'],
    queryFn: () => api.get<TopDebtor[]>('/customers/top-debtors', { limit: 25 }),
  })

  const overdue = useQuery({
    queryKey: ['risk-overdue', minDays],
    queryFn: () =>
      api.get<OverdueItem[]>('/customers/overdue', {
        min_days: Math.max(0, Number(minDays) || 0), limit: 200,
      }),
  })

  const churn = useQuery({
    queryKey: ['churn-risk', churnDays],
    queryFn: () =>
      api.get<ChurnItem[]>('/customers/churn-risk', {
        days: Math.max(7, Number(churnDays) || 90), limit: 100,
      }),
  })

  const declining = useQuery({
    queryKey: ['declining', declineDays, dropPercent],
    queryFn: () =>
      api.get<DecliningItem[]>('/customers/declining', {
        days: Math.max(7, Number(declineDays) || 30),
        drop_percent: Math.max(1, Number(dropPercent) || 20),
        limit: 100,
      }),
  })

  const totals = summary.data?.totals

  return (
    <>
      <PageHeader
        title={t('crm.riskAnalysis')}
        subtitle={t('crm.riskSubtitle')}
        icon={<ShieldAlert className="h-5 w-5" />}
      />

      {/* ---- ageing summary ---- */}
      {summary.isLoading ? (
        <SkeletonRows rows={1} cols={6} />
      ) : summary.isError ? (
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      ) : (
        totals && (
          <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
            {BUCKETS.map((b) => (
              <div key={b.key} className="card p-4">
                <p className="text-2xs font-medium uppercase tracking-wide text-shell-500">
                  {t(b.label)}
                </p>
                <p className="tabular mt-1 text-lg font-semibold text-shell-900">
                  {formatMoney(totals[b.key])}
                </p>
              </div>
            ))}
            <div className="card border-danger-200 bg-danger-50/40 p-4">
              <p className="text-2xs font-medium uppercase tracking-wide text-danger-600">
                {t('crm.overdueBalance')}
              </p>
              <p className="tabular mt-1 text-lg font-semibold text-danger-700">
                {formatMoney(totals.overdue)}
              </p>
            </div>
          </div>
        )
      )}

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        {/* ---- top debtors ---- */}
        <Card title={t('crm.topDebtors')} bodyClassName="p-0">
          {debtors.isLoading ? (
            <SkeletonRows rows={6} cols={4} />
          ) : debtors.isError ? (
            <ErrorState error={debtors.error} onRetry={() => void debtors.refetch()} />
          ) : (debtors.data ?? []).length === 0 ? (
            <EmptyState />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('crm.name')}</th>
                    <th className="text-right">{t('crm.balance')}</th>
                    <th className="text-right">{t('crm.overdueBalance')}</th>
                    <th className="text-right">{t('crm.creditLimit')}</th>
                    <th>{t('crm.riskScore')}</th>
                    <th>{t('crm.lastPayment')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(debtors.data ?? []).map((d) => (
                    <tr key={d.customer_id}>
                      <td>
                        <Link
                          to={`/crm/customers/${d.customer_id}`}
                          className="font-medium text-brand-700 hover:underline"
                        >
                          {d.customer_name}
                        </Link>
                        <span className="tabular block text-2xs text-shell-400">{d.customer_code}</span>
                      </td>
                      <td className="tabular text-right font-medium">{formatMoney(d.balance)}</td>
                      <td className="tabular text-right text-danger-600">
                        {formatMoney(d.overdue_balance)}
                      </td>
                      <td className="tabular text-right">{formatMoney(d.credit_limit)}</td>
                      <td>
                        <span className={riskTone(d.risk_score)}>
                          {formatNumber(d.risk_score, { decimals: 1 })}
                        </span>
                      </td>
                      <td className="whitespace-nowrap text-xs">{formatDate(d.last_payment_date)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* ---- overdue items ---- */}
        <Card
          title={t('crm.overdueItems')}
          bodyClassName="p-0"
          actions={
            <input
              className="input tabular w-24"
              type="number"
              min={0}
              value={minDays}
              onChange={(e) => setMinDays(e.target.value)}
              aria-label={t('crm.minOverdueDays')}
            />
          }
        >
          {overdue.isLoading ? (
            <SkeletonRows rows={6} cols={5} />
          ) : overdue.isError ? (
            <ErrorState error={overdue.error} onRetry={() => void overdue.refetch()} />
          ) : (overdue.data ?? []).length === 0 ? (
            <EmptyState title={t('crm.noOverdue')} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('crm.name')}</th>
                    <th>{t('crm.reference')}</th>
                    <th>{t('crm.dueDate')}</th>
                    <th className="text-right">{t('crm.daysPastDue')}</th>
                    <th className="text-right">{t('crm.openAmount')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(overdue.data ?? []).map((r) => (
                    <tr key={r.ledger_id}>
                      <td>
                        <Link
                          to={`/crm/customers/${r.customer_id}`}
                          className="font-medium text-brand-700 hover:underline"
                        >
                          {r.customer_name}
                        </Link>
                        <span className="tabular block text-2xs text-shell-400">{r.customer_code}</span>
                      </td>
                      <td className="text-xs">{r.reference_no || '—'}</td>
                      <td className="whitespace-nowrap text-xs">{formatDate(r.due_date)}</td>
                      <td className="tabular text-right">
                        <span className={BUCKET_TONE[r.bucket] ?? 'badge-muted'}>
                          {formatNumber(r.days_past_due)}
                        </span>
                      </td>
                      <td className="tabular text-right font-semibold text-danger-600">
                        {formatMoney(r.open_amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {/* ---- churn risk ---- */}
      <Card
        className="mt-4"
        title={t('crm.churnRisk')}
        bodyClassName="p-0"
        actions={
          <div className="flex items-end gap-2">
            <Field label={t('crm.inactiveDays')}>
              <input
                className="input tabular w-24"
                type="number"
                min={7}
                value={churnDays}
                onChange={(e) => setChurnDays(e.target.value)}
              />
            </Field>
          </div>
        }
      >
        {churn.isLoading ? (
          <SkeletonRows rows={5} cols={5} />
        ) : churn.isError ? (
          <ErrorState error={churn.error} onRetry={() => void churn.refetch()} />
        ) : (churn.data ?? []).length === 0 ? (
          <EmptyState />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('crm.code')}</th>
                  <th>{t('crm.name')}</th>
                  <th>{t('crm.lastOrder')}</th>
                  <th className="text-right">{t('crm.totalSales')}</th>
                  <th className="text-right">{t('crm.balance')}</th>
                  <th>{t('crm.explanation')}</th>
                </tr>
              </thead>
              <tbody>
                {(churn.data ?? []).map((row) => (
                  <tr key={row.customer.id}>
                    <td className="tabular whitespace-nowrap">
                      <Link
                        to={`/crm/customers/${row.customer.id}`}
                        className="font-medium text-brand-700 hover:underline"
                      >
                        {row.customer.code}
                      </Link>
                    </td>
                    <td>
                      <span className="block max-w-[16rem] truncate">
                        {row.customer.trade_name || row.customer.name}
                      </span>
                      <span className="block text-2xs text-shell-400">{row.customer.city}</span>
                    </td>
                    <td className="whitespace-nowrap text-xs">{formatDate(row.last_order_date)}</td>
                    <td className="tabular text-right">{formatMoney(row.total_sales_amount)}</td>
                    <td
                      className={`tabular text-right ${
                        toNumber(row.balance) > 0 ? 'text-danger-600' : ''
                      }`}
                    >
                      {formatMoney(row.balance)}
                    </td>
                    <td className="text-xs text-shell-600">
                      <span className="flex items-center gap-1.5">
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warn-500" />
                        {t('crm.daysSinceLastOrder', {
                          days: formatNumber(row.days_since_last_order),
                          date: formatDate(row.last_order_date),
                        })}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* ---- declining customers ---- */}
      <Card
        className="mt-4"
        title={t('crm.declining')}
        bodyClassName="p-0"
        actions={
          <div className="flex items-end gap-2">
            <Field label={t('crm.period')}>
              <input
                className="input tabular w-24"
                type="number"
                min={7}
                value={declineDays}
                onChange={(e) => setDeclineDays(e.target.value)}
              />
            </Field>
            <Field label={t('crm.minDropPercent')}>
              <input
                className="input tabular w-24"
                type="number"
                min={1}
                max={100}
                value={dropPercent}
                onChange={(e) => setDropPercent(e.target.value)}
              />
            </Field>
          </div>
        }
      >
        {declining.isLoading ? (
          <SkeletonRows rows={5} cols={5} />
        ) : declining.isError ? (
          <ErrorState error={declining.error} onRetry={() => void declining.refetch()} />
        ) : (declining.data ?? []).length === 0 ? (
          <EmptyState />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('crm.code')}</th>
                  <th>{t('crm.name')}</th>
                  <th className="text-right">{t('crm.previousAmount')}</th>
                  <th className="text-right">{t('crm.currentAmount')}</th>
                  <th className="text-right">{t('crm.dropPercent')}</th>
                  <th>{t('crm.explanation')}</th>
                </tr>
              </thead>
              <tbody>
                {(declining.data ?? []).map((row) => (
                  <tr key={row.customer.id}>
                    <td className="tabular whitespace-nowrap">
                      <Link
                        to={`/crm/customers/${row.customer.id}`}
                        className="font-medium text-brand-700 hover:underline"
                      >
                        {row.customer.code}
                      </Link>
                    </td>
                    <td>
                      <span className="block max-w-[16rem] truncate">
                        {row.customer.trade_name || row.customer.name}
                      </span>
                      <span className="block text-2xs text-shell-400">{row.customer.city}</span>
                    </td>
                    <td className="tabular text-right">{formatMoney(row.previous_amount)}</td>
                    <td className="tabular text-right font-medium">{formatMoney(row.current_amount)}</td>
                    <td className="tabular text-right font-semibold text-danger-600">
                      {formatPercent(-Math.abs(row.drop_percent), { sign: true })}
                    </td>
                    <td className="text-xs text-shell-600">
                      <span className="flex items-center gap-1.5">
                        <TrendingDown className="h-3.5 w-3.5 shrink-0 text-danger-500" />
                        {t('crm.declineExplain', {
                          previous: formatMoney(row.previous_amount),
                          current: formatMoney(row.current_amount),
                          percent: formatNumber(row.drop_percent, { decimals: 1 }),
                          days: formatNumber(row.days),
                        })}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="mt-4 flex items-center gap-2 text-2xs text-shell-400">
        <Users className="h-3.5 w-3.5" />
        {t('crm.riskSubtitle')}
      </p>
    </>
  )
}
