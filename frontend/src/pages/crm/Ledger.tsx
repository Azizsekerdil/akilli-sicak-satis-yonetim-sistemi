/**
 * Cari Hesap / Company-wide receivables.
 *
 * The collection desk's working screen: ageing totals across the caller's data
 * scope, every open item with the bucket it falls into, and a CSV of exactly
 * what is on screen after filtering.
 */
import { useQuery } from '@tanstack/react-query'
import { Download, Search, Wallet } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, toNumber } from '@/lib/format'
import {
  Card, EmptyState, ErrorState, Field, PageHeader, SkeletonRows, useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface AgingTotals {
  current: number | string; d1_30: number | string; d31_60: number | string
  d61_90: number | string; d90_plus: number | string
  total: number | string; overdue: number | string
}

interface AgingCustomerRow extends AgingTotals {
  customer_id: number; customer_code: string; customer_name: string
}

interface AgingSummary {
  as_of: string
  totals: AgingTotals
  customers: AgingCustomerRow[]
}

interface OverdueItem {
  ledger_id: number
  customer_id: number
  customer_code: string
  customer_name: string
  phone?: string | null
  salesperson_id?: number | null
  entry_type: string
  entry_date: string
  due_date?: string | null
  reference_no?: string | null
  open_amount: number | string
  days_past_due: number
  bucket: string
}

interface SalespersonRow {
  id: number
  code?: string | null
  full_name?: string | null
}

interface CustomerLite {
  id: number
}

const BUCKETS: { key: keyof AgingTotals; label: string }[] = [
  { key: 'current', label: 'crm.bucketCurrent' },
  { key: 'd1_30', label: 'crm.bucket1_30' },
  { key: 'd31_60', label: 'crm.bucket31_60' },
  { key: 'd61_90', label: 'crm.bucket61_90' },
  { key: 'd90_plus', label: 'crm.bucket90' },
]

const BUCKET_TONE: Record<string, string> = {
  current: 'badge-muted',
  d1_30: 'badge-info',
  d31_60: 'badge-warn',
  d61_90: 'badge-warn',
  d90_plus: 'badge-danger',
}

const BUCKET_LABEL: Record<string, string> = {
  current: 'crm.bucketCurrent',
  d1_30: 'crm.bucket1_30',
  d31_60: 'crm.bucket31_60',
  d61_90: 'crm.bucket61_90',
  d90_plus: 'crm.bucket90',
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Ledger() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()

  const [asOf, setAsOf] = useState('')
  const [minDays, setMinDays] = useState('1')
  const [salespersonId, setSalespersonId] = useState('')
  const [regionId, setRegionId] = useState('')
  const [term, setTerm] = useState('')

  const summary = useQuery({
    queryKey: ['aging-summary', asOf],
    queryFn: () =>
      api.get<AgingSummary>('/customers/aging-summary', {
        as_of: asOf || undefined, limit: 300,
      }),
  })

  const overdue = useQuery({
    queryKey: ['overdue-items', minDays],
    queryFn: () =>
      api.get<OverdueItem[]>('/customers/overdue', {
        min_days: Math.max(0, Number(minDays) || 0), limit: 1000,
      }),
  })

  const salespersons = useQuery({
    queryKey: ['salespersons-lookup'],
    queryFn: () =>
      api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons'),
    retry: false,
    throwOnError: false,
  })

  /* A region filter needs the customer ids in that region — the receivable rows
     themselves only carry the salesperson. */
  const regionCustomers = useQuery({
    queryKey: ['region-customers', regionId],
    queryFn: () =>
      api.get<Paged<CustomerLite>>('/customers', { region_id: regionId, size: 500 }),
    enabled: regionId.trim() !== '' && can('crm.customers'),
    retry: false,
    throwOnError: false,
  })

  const regionIds = useMemo(() => {
    if (regionId.trim() === '' || !regionCustomers.data) return null
    return new Set(regionCustomers.data.items.map((c) => c.id))
  }, [regionId, regionCustomers.data])

  const rows = useMemo(() => {
    const q = term.trim().toLocaleLowerCase('tr-TR')
    return (overdue.data ?? []).filter((r) => {
      if (salespersonId && String(r.salesperson_id ?? '') !== salespersonId) return false
      if (regionIds && !regionIds.has(r.customer_id)) return false
      if (q) {
        const hay = `${r.customer_code} ${r.customer_name} ${r.reference_no ?? ''}`
          .toLocaleLowerCase('tr-TR')
        if (!hay.includes(q)) return false
      }
      return true
    })
  }, [overdue.data, salespersonId, regionIds, term])

  const totalOpen = useMemo(
    () => rows.reduce((sum, r) => sum + toNumber(r.open_amount), 0),
    [rows],
  )

  const exportCsv = () => {
    if (rows.length === 0) {
      push('warning', t('common.noData'))
      return
    }
    const head = [
      t('crm.code'), t('crm.name'), t('crm.phone'), t('crm.entryType'),
      t('crm.entryDate'), t('crm.dueDate'), t('crm.reference'),
      t('crm.openAmount'), t('crm.daysPastDue'), t('crm.bucket'),
    ]
    const body = rows.map((r) => [
      r.customer_code, r.customer_name, r.phone ?? '', r.entry_type,
      r.entry_date, r.due_date ?? '', r.reference_no ?? '',
      String(toNumber(r.open_amount)), String(r.days_past_due), r.bucket,
    ])
    const csv = [head, ...body]
      .map((line) => line.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(';'))
      .join('\r\n')
    const blob = new Blob([`﻿${csv}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `receivables-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const totals = summary.data?.totals

  return (
    <>
      <PageHeader
        title={t('crm.receivables')}
        subtitle={t('crm.receivablesSubtitle')}
        icon={<Wallet className="h-5 w-5" />}
        actions={
          <button type="button" className="btn-secondary" onClick={exportCsv}>
            <Download className="h-4 w-4" />
            {t('common.export')}
          </button>
        }
      />

      {/* ---- ageing totals ---- */}
      {summary.isLoading ? (
        <SkeletonRows rows={1} cols={5} />
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

      {/* ---- filters ---- */}
      <Card className="mt-4" bodyClassName="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="relative min-w-[13rem] flex-1">
            <Search className="absolute left-2.5 top-8 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <Field label={t('common.search')}>
              <input
                className="input pl-9"
                value={term}
                onChange={(e) => setTerm(e.target.value)}
              />
            </Field>
          </div>
          <Field label={t('crm.minOverdueDays')}>
            <input
              className="input tabular w-32"
              type="number"
              min={0}
              value={minDays}
              onChange={(e) => setMinDays(e.target.value)}
            />
          </Field>
          <Field label={t('crm.salesperson')}>
            <select
              className="input w-48"
              value={salespersonId}
              onChange={(e) => setSalespersonId(e.target.value)}
            >
              <option value="">{t('common.all')}</option>
              {(salespersons.data?.items ?? []).map((s) => (
                <option key={s.id} value={String(s.id)}>
                  {s.full_name || s.code || s.id}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('crm.regionId')}>
            <input
              className="input tabular w-28"
              type="number"
              value={regionId}
              onChange={(e) => setRegionId(e.target.value)}
            />
          </Field>
          <Field label={t('crm.asOf')}>
            <input
              className="input w-44"
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </Field>
        </div>
      </Card>

      {/* ---- open items ---- */}
      <Card className="mt-4" title={t('crm.overdueItems')} bodyClassName="p-0">
        {overdue.isLoading ? (
          <SkeletonRows rows={8} cols={8} />
        ) : overdue.isError ? (
          <ErrorState error={overdue.error} onRetry={() => void overdue.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState title={t('crm.noOverdue')} />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('crm.code')}</th>
                  <th>{t('crm.name')}</th>
                  <th>{t('crm.entryType')}</th>
                  <th>{t('crm.reference')}</th>
                  <th>{t('crm.dueDate')}</th>
                  <th className="text-right">{t('crm.daysPastDue')}</th>
                  <th>{t('crm.bucket')}</th>
                  <th className="text-right">{t('crm.openAmount')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.ledger_id}>
                    <td className="tabular whitespace-nowrap">
                      <Link
                        to={`/crm/customers/${r.customer_id}`}
                        className="font-medium text-brand-700 hover:underline"
                      >
                        {r.customer_code}
                      </Link>
                    </td>
                    <td>
                      <span className="block max-w-[18rem] truncate">{r.customer_name}</span>
                      {r.phone && <span className="tabular block text-2xs text-shell-400">{r.phone}</span>}
                    </td>
                    <td className="text-xs">
                      {t(`crm.ledgerTypes.${r.entry_type}`, { defaultValue: r.entry_type })}
                    </td>
                    <td className="text-xs">{r.reference_no || '—'}</td>
                    <td className="whitespace-nowrap">{formatDate(r.due_date)}</td>
                    <td className="tabular text-right font-medium">
                      {formatNumber(r.days_past_due)}
                    </td>
                    <td>
                      <span className={BUCKET_TONE[r.bucket] ?? 'badge-muted'}>
                        {t(BUCKET_LABEL[r.bucket] ?? 'crm.bucketCurrent')}
                      </span>
                    </td>
                    <td className="tabular text-right font-semibold text-danger-600">
                      {formatMoney(r.open_amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="bg-shell-50 font-semibold">
                  <td colSpan={7}>
                    {t('crm.totalsRow')} · {formatNumber(rows.length)}
                  </td>
                  <td className="tabular text-right">{formatMoney(totalOpen)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </Card>

      {/* ---- per-customer ageing ---- */}
      <Card className="mt-4" title={t('crm.ageing')} bodyClassName="p-0">
        {summary.isLoading ? (
          <SkeletonRows rows={6} cols={7} />
        ) : (summary.data?.customers ?? []).length === 0 ? (
          <EmptyState />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('crm.code')}</th>
                  <th>{t('crm.name')}</th>
                  {BUCKETS.map((b) => (
                    <th key={b.key} className="text-right">{t(b.label)}</th>
                  ))}
                  <th className="text-right">{t('common.total')}</th>
                </tr>
              </thead>
              <tbody>
                {(summary.data?.customers ?? [])
                  .filter((r) => !regionIds || regionIds.has(r.customer_id))
                  .map((r) => (
                    <tr key={r.customer_id}>
                      <td className="tabular whitespace-nowrap">
                        <Link
                          to={`/crm/customers/${r.customer_id}`}
                          className="font-medium text-brand-700 hover:underline"
                        >
                          {r.customer_code}
                        </Link>
                      </td>
                      <td>
                        <span className="block max-w-[18rem] truncate">{r.customer_name}</span>
                      </td>
                      {BUCKETS.map((b) => (
                        <td key={b.key} className="tabular text-right">
                          {formatMoney(r[b.key])}
                        </td>
                      ))}
                      <td className="tabular text-right font-semibold">{formatMoney(r.total)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  )
}
