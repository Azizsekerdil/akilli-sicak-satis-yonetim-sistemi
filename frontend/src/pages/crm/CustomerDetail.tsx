/**
 * Müşteri Kartı / Customer detail.
 *
 * One customer, nine tabs: overview KPIs, current-account movements, a printable
 * statement for a date range, receivable ageing, sales history, the SKU basket,
 * contacts, notes and the explainable collection-risk score.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Pin, Plus, RefreshCw, Store, Trash2,
} from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import {
  Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, formatPercent, toNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'
import {
  Card, EmptyState, ErrorState, Field, KpiTile, LoadingBlock, PageHeader,
  Pagination, SkeletonRows, Spinner, StatusBadge, useToast,
  type KpiCardData,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface CustomerCard {
  id: number; code: string; name: string; trade_name?: string | null
  customer_type: string; channel: string; status: string
  city?: string | null; district?: string | null; address?: string | null
  phone?: string | null; mobile?: string | null; email?: string | null
  balance: number | string; overdue_balance: number | string; credit_limit: number | string
  risk_limit: number | string; total_sales_amount: number | string
  average_order_value: number | string; order_count: number
  last_order_date?: string | null; last_visit_date?: string | null
  last_payment_date?: string | null; risk_score: number
  payment_method: string; payment_term_days: number; visit_days?: string | null
  visit_frequency: string; price_list_id?: number | null
}

interface LedgerRow {
  id: number; entry_type: string; entry_date: string; due_date?: string | null
  debit: number | string; credit: number | string; balance_after: number | string
  open_amount: number | string; is_settled: boolean
  reference_no?: string | null; description?: string | null
}

interface StatementRow {
  id: number; entry_date: string; due_date?: string | null; entry_type: string
  reference_no?: string | null; description?: string | null
  debit: number | string; credit: number | string; balance: number | string
}

interface Statement {
  customer_code: string; customer_name: string; start: string; end: string
  opening: number | string; closing: number | string; rows: StatementRow[]
  totals: { debit: number | string; credit: number | string; movement: number | string }
}

interface Aging {
  current: number | string; d1_30: number | string; d31_60: number | string
  d61_90: number | string; d90_plus: number | string
  total: number | string; overdue: number | string
}

interface Risk {
  customer_id: number; risk_score: number; risk_band: string
  balance: number | string; overdue_balance: number | string; credit_limit: number | string
  credit_utilisation_percent: number; days_past_due: number
  bounced_payments_180d: number; average_payment_interval_days?: number | null
  last_payment_date?: string | null; aging: Aging
}

interface SaleRow {
  id: number; sale_no: string; sale_date: string; total_amount: number | string
  net_amount: number | string; paid_amount: number | string; due_amount: number | string
  payment_method: string; line_count: number; is_cancelled: boolean
}

interface ProductRow {
  product_id: number; product_code: string; product_name: string
  total_quantity: number | string; total_amount: number | string
  order_count: number; last_purchase_date?: string | null
}

interface Contact {
  id: number; name: string; title?: string | null; phone?: string | null
  email?: string | null; is_primary: boolean; notes?: string | null
}

interface Note {
  id: number; body: string; category?: string | null; is_pinned: boolean
  created_at?: string | null
}

const BUCKETS: { key: keyof Aging; label: string }[] = [
  { key: 'current', label: 'crm.bucketCurrent' },
  { key: 'd1_30', label: 'crm.bucket1_30' },
  { key: 'd31_60', label: 'crm.bucket31_60' },
  { key: 'd61_90', label: 'crm.bucket61_90' },
  { key: 'd90_plus', label: 'crm.bucket90' },
]

const today = () => new Date().toISOString().slice(0, 10)
const daysAgo = (n: number) =>
  new Date(Date.now() - n * 864e5).toISOString().slice(0, 10)

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function CustomerDetail() {
  const { t } = useTranslation()
  const { id } = useParams<{ id: string }>()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()
  const lang = currentLanguage()
  const customerId = Number(id)

  const mayLedger = can('crm.ledger')
  const mayRisk = can('crm.risk')
  const mayEdit = can('crm.customers', 'UPDATE')

  const TABS = [
    { key: 'overview', label: t('crm.overview'), on: true },
    { key: 'ledger', label: t('crm.ledger'), on: mayLedger },
    { key: 'statement', label: t('crm.statement'), on: mayLedger },
    { key: 'ageing', label: t('crm.ageing'), on: mayLedger },
    { key: 'sales', label: t('crm.salesHistory'), on: true },
    { key: 'products', label: t('crm.productsBought'), on: true },
    { key: 'contacts', label: t('crm.contacts'), on: true },
    { key: 'notes', label: t('crm.notes'), on: true },
    { key: 'risk', label: t('crm.risk'), on: mayRisk },
  ].filter((tab) => tab.on)

  const [tab, setTab] = useState('overview')
  const [ledgerPage, setLedgerPage] = useState(1)
  const [onlyOpen, setOnlyOpen] = useState(false)
  const [start, setStart] = useState(daysAgo(90))
  const [end, setEnd] = useState(today())
  const [contact, setContact] = useState({ name: '', title: '', phone: '', email: '' })
  const [note, setNote] = useState({ body: '', category: '' })

  const customer = useQuery({
    queryKey: ['customer', customerId],
    queryFn: () => api.get<CustomerCard>(`/customers/${customerId}`),
    enabled: Number.isFinite(customerId),
  })

  const ledger = useQuery({
    queryKey: ['customer-ledger', customerId, ledgerPage, onlyOpen],
    queryFn: () =>
      api.get<Paged<LedgerRow>>(`/customers/${customerId}/ledger`, {
        page: ledgerPage, size: 25, only_open: onlyOpen || undefined,
      }),
    enabled: tab === 'ledger' && mayLedger,
  })

  const statement = useQuery({
    queryKey: ['customer-statement', customerId, start, end],
    queryFn: () =>
      api.get<Statement>(`/customers/${customerId}/statement`, { start, end }),
    enabled: tab === 'statement' && mayLedger,
  })

  const aging = useQuery({
    queryKey: ['customer-aging', customerId],
    queryFn: () => api.get<Aging>(`/customers/${customerId}/aging`),
    enabled: tab === 'ageing' && mayLedger,
  })

  const sales = useQuery({
    queryKey: ['customer-sales', customerId],
    queryFn: () => api.get<SaleRow[]>(`/customers/${customerId}/sales-history`, { limit: 100 }),
    enabled: tab === 'sales',
  })

  const products = useQuery({
    queryKey: ['customer-products', customerId],
    queryFn: () => api.get<ProductRow[]>(`/customers/${customerId}/products`, { limit: 200 }),
    enabled: tab === 'products',
  })

  const contacts = useQuery({
    queryKey: ['customer-contacts', customerId],
    queryFn: () => api.get<Contact[]>(`/customers/${customerId}/contacts`),
    enabled: tab === 'contacts',
  })

  const notes = useQuery({
    queryKey: ['customer-notes', customerId],
    queryFn: () => api.get<Note[]>(`/customers/${customerId}/notes`, { limit: 100 }),
    enabled: tab === 'notes',
  })

  const risk = useQuery({
    queryKey: ['customer-risk', customerId],
    queryFn: () => api.get<Risk>(`/customers/${customerId}/risk`),
    enabled: tab === 'risk' && mayRisk,
  })

  const fail = (e: unknown) =>
    push('error', e instanceof ApiError ? e.message : t('errors.generic'))

  const addContact = useMutation({
    mutationFn: () =>
      api.post(`/customers/${customerId}/contacts`, {
        name: contact.name.trim(),
        title: contact.title.trim() || null,
        phone: contact.phone.trim() || null,
        email: contact.email.trim() || null,
        is_primary: false,
      }),
    onSuccess: () => {
      setContact({ name: '', title: '', phone: '', email: '' })
      void qc.invalidateQueries({ queryKey: ['customer-contacts', customerId] })
    },
    onError: fail,
  })

  const dropContact = useMutation({
    mutationFn: (cid: number) => api.delete(`/customers/${customerId}/contacts/${cid}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['customer-contacts', customerId] }),
    onError: fail,
  })

  const addNote = useMutation({
    mutationFn: () =>
      api.post(`/customers/${customerId}/notes`, {
        body: note.body.trim(),
        category: note.category.trim() || null,
        is_pinned: false,
      }),
    onSuccess: () => {
      setNote({ body: '', category: '' })
      void qc.invalidateQueries({ queryKey: ['customer-notes', customerId] })
    },
    onError: fail,
  })

  const dropNote = useMutation({
    mutationFn: (nid: number) => api.delete(`/customers/${customerId}/notes/${nid}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['customer-notes', customerId] }),
    onError: fail,
  })

  const refreshStats = useMutation({
    mutationFn: () => api.post(`/customers/${customerId}/refresh-stats`),
    onSuccess: () => {
      push('success', t('common.success'))
      void qc.invalidateQueries({ queryKey: ['customer', customerId] })
    },
    onError: fail,
  })

  if (customer.isLoading) return <LoadingBlock />
  if (customer.isError) {
    return <ErrorState error={customer.error} onRetry={() => void customer.refetch()} />
  }
  const c = customer.data
  if (!c) return <EmptyState title={t('crm.customerNotFound')} />

  const label = (key: string, lng: 'tr' | 'en') => String(t(key, { lng }))
  const kpis: KpiCardData[] = [
    {
      key: 'balance', label_tr: label('crm.balance', 'tr'), label_en: label('crm.balance', 'en'),
      value: toNumber(c.balance), format: 'money',
      severity: toNumber(c.overdue_balance) > 0 ? 'critical' : null,
    },
    {
      key: 'overdue', label_tr: label('crm.overdueBalance', 'tr'),
      label_en: label('crm.overdueBalance', 'en'),
      value: toNumber(c.overdue_balance), format: 'money',
      severity: toNumber(c.overdue_balance) > 0 ? 'warning' : null,
    },
    {
      key: 'avg', label_tr: label('crm.averageOrder', 'tr'),
      label_en: label('crm.averageOrder', 'en'),
      value: toNumber(c.average_order_value), format: 'money',
    },
    {
      key: 'orders', label_tr: label('crm.orderCount', 'tr'),
      label_en: label('crm.orderCount', 'en'), value: c.order_count, format: 'integer',
    },
  ]

  return (
    <>
      <PageHeader
        title={c.trade_name || c.name}
        subtitle={`${c.code} · ${t(`crm.types.${c.customer_type}`)} · ${
          [c.city, c.district].filter(Boolean).join(' / ') || '—'
        }`}
        icon={<Store className="h-5 w-5" />}
        actions={
          <>
            <Link to="/crm/customers" className="btn-secondary">
              <ArrowLeft className="h-4 w-4" />
              {t('crm.backToList')}
            </Link>
            {mayEdit && (
              <button
                type="button"
                className="btn-secondary"
                disabled={refreshStats.isPending}
                onClick={() => refreshStats.mutate()}
              >
                {refreshStats.isPending ? <Spinner /> : <RefreshCw className="h-4 w-4" />}
                {t('crm.refreshStats')}
              </button>
            )}
            <StatusBadge status={c.status} label={t(`crm.statuses.${c.status}`)} />
          </>
        }
      />

      <div className="mb-4 flex flex-wrap gap-1 border-b border-shell-200">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === item.key
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-shell-500 hover:text-shell-800'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {/* ---------------- Overview ---------------- */}
      {tab === 'overview' && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {kpis.map((k) => <KpiTile key={k.key} kpi={k} lang={lang} />)}
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title={t('crm.commercialTerms')}>
              <dl className="space-y-2 text-sm">
                {[
                  [t('crm.creditLimit'), formatMoney(c.credit_limit)],
                  [t('crm.riskLimit'), formatMoney(c.risk_limit)],
                  [t('crm.paymentMethod'), t(`crm.paymentMethods.${c.payment_method}`)],
                  [t('crm.paymentTermDays'), formatNumber(c.payment_term_days)],
                  [t('crm.totalSales'), formatMoney(c.total_sales_amount)],
                  [t('crm.riskScore'), formatNumber(c.risk_score, { decimals: 1 })],
                ].map(([k, v]) => (
                  <div key={String(k)} className="flex justify-between border-b border-shell-100 pb-1.5">
                    <dt className="text-shell-500">{k}</dt>
                    <dd className="tabular font-medium">{v}</dd>
                  </div>
                ))}
              </dl>
            </Card>
            <Card title={t('crm.visitPlan')}>
              <dl className="space-y-2 text-sm">
                {[
                  [t('crm.visitFrequency'), t(`crm.frequencies.${c.visit_frequency}`)],
                  [
                    t('crm.visitDays'),
                    (c.visit_days ?? '').split(',').filter(Boolean)
                      .map((d) => t(`crm.weekdays.${d.trim()}`)).join(', ') || '—',
                  ],
                  [t('crm.lastVisit'), formatDate(c.last_visit_date)],
                  [t('crm.lastOrder'), formatDate(c.last_order_date)],
                  [t('crm.lastPayment'), formatDate(c.last_payment_date)],
                  [t('crm.phone'), c.phone || c.mobile || '—'],
                ].map(([k, v]) => (
                  <div key={String(k)} className="flex justify-between border-b border-shell-100 pb-1.5">
                    <dt className="text-shell-500">{k}</dt>
                    <dd className="font-medium">{v}</dd>
                  </div>
                ))}
              </dl>
              {c.address && <p className="mt-3 text-xs text-shell-500">{c.address}</p>}
            </Card>
          </div>
        </div>
      )}

      {/* ---------------- Ledger ---------------- */}
      {tab === 'ledger' && (
        <Card bodyClassName="p-0">
          <div className="flex flex-wrap items-center gap-3 border-b border-shell-200 p-4">
            <label className="flex items-center gap-2 text-sm text-shell-600">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-shell-300"
                checked={onlyOpen}
                onChange={(e) => { setOnlyOpen(e.target.checked); setLedgerPage(1) }}
              />
              {t('crm.onlyOpen')}
            </label>
          </div>
          {ledger.isLoading ? (
            <SkeletonRows rows={6} cols={7} />
          ) : ledger.isError ? (
            <ErrorState error={ledger.error} onRetry={() => void ledger.refetch()} />
          ) : (ledger.data?.items ?? []).length === 0 ? (
            <EmptyState />
          ) : (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('crm.entryDate')}</th>
                      <th>{t('crm.dueDate')}</th>
                      <th>{t('crm.entryType')}</th>
                      <th>{t('crm.reference')}</th>
                      <th className="text-right">{t('crm.debit')}</th>
                      <th className="text-right">{t('crm.credit')}</th>
                      <th className="text-right">{t('crm.runningBalance')}</th>
                      <th className="text-right">{t('crm.openAmount')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(ledger.data?.items ?? []).map((r) => (
                      <tr key={r.id}>
                        <td className="whitespace-nowrap">{formatDate(r.entry_date)}</td>
                        <td className="whitespace-nowrap">{formatDate(r.due_date)}</td>
                        <td className="text-xs">{t(`crm.ledgerTypes.${r.entry_type}`, { defaultValue: r.entry_type })}</td>
                        <td className="text-xs">{r.reference_no || r.description || '—'}</td>
                        <td className="tabular text-right">{formatMoney(r.debit)}</td>
                        <td className="tabular text-right">{formatMoney(r.credit)}</td>
                        <td className="tabular text-right font-medium">{formatMoney(r.balance_after)}</td>
                        <td
                          className={`tabular text-right ${
                            toNumber(r.open_amount) > 0 ? 'text-danger-600' : 'text-shell-400'
                          }`}
                        >
                          {formatMoney(r.open_amount)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination
                page={ledger.data?.page ?? 1}
                pages={ledger.data?.pages ?? 1}
                total={ledger.data?.total ?? 0}
                size={25}
                onPage={setLedgerPage}
              />
            </>
          )}
        </Card>
      )}

      {/* ---------------- Statement ---------------- */}
      {tab === 'statement' && (
        <Card bodyClassName="p-0">
          <div className="flex flex-wrap items-end gap-3 border-b border-shell-200 p-4">
            <Field label={t('common.from')}>
              <input type="date" className="input" value={start} onChange={(e) => setStart(e.target.value)} />
            </Field>
            <Field label={t('common.to')}>
              <input type="date" className="input" value={end} onChange={(e) => setEnd(e.target.value)} />
            </Field>
          </div>
          {statement.isLoading ? (
            <SkeletonRows rows={6} cols={6} />
          ) : statement.isError ? (
            <ErrorState error={statement.error} onRetry={() => void statement.refetch()} />
          ) : !statement.data ? (
            <EmptyState />
          ) : (
            <>
              <div className="flex flex-wrap gap-6 border-b border-shell-200 px-5 py-3 text-sm">
                <span className="text-shell-500">
                  {t('crm.opening')}:{' '}
                  <span className="tabular font-medium text-shell-900">
                    {formatMoney(statement.data.opening)}
                  </span>
                </span>
                <span className="text-shell-500">
                  {t('crm.movement')}:{' '}
                  <span className="tabular font-medium text-shell-900">
                    {formatMoney(statement.data.totals.movement)}
                  </span>
                </span>
                <span className="text-shell-500">
                  {t('crm.closing')}:{' '}
                  <span className="tabular font-semibold text-shell-900">
                    {formatMoney(statement.data.closing)}
                  </span>
                </span>
              </div>
              {statement.data.rows.length === 0 ? (
                <EmptyState />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('crm.entryDate')}</th>
                        <th>{t('crm.dueDate')}</th>
                        <th>{t('crm.entryType')}</th>
                        <th>{t('crm.reference')}</th>
                        <th className="text-right">{t('crm.debit')}</th>
                        <th className="text-right">{t('crm.credit')}</th>
                        <th className="text-right">{t('crm.runningBalance')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {statement.data.rows.map((r) => (
                        <tr key={r.id}>
                          <td className="whitespace-nowrap">{formatDate(r.entry_date)}</td>
                          <td className="whitespace-nowrap">{formatDate(r.due_date)}</td>
                          <td className="text-xs">{t(`crm.ledgerTypes.${r.entry_type}`, { defaultValue: r.entry_type })}</td>
                          <td className="text-xs">{r.reference_no || r.description || '—'}</td>
                          <td className="tabular text-right">{formatMoney(r.debit)}</td>
                          <td className="tabular text-right">{formatMoney(r.credit)}</td>
                          <td className="tabular text-right font-medium">{formatMoney(r.balance)}</td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="bg-shell-50 font-semibold">
                        <td colSpan={4}>{t('crm.totalsRow')}</td>
                        <td className="tabular text-right">{formatMoney(statement.data.totals.debit)}</td>
                        <td className="tabular text-right">{formatMoney(statement.data.totals.credit)}</td>
                        <td className="tabular text-right">{formatMoney(statement.data.closing)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}
            </>
          )}
        </Card>
      )}

      {/* ---------------- Ageing ---------------- */}
      {tab === 'ageing' && (
        <AgeingPanel data={aging.data} loading={aging.isLoading} error={aging.error} />
      )}

      {/* ---------------- Sales history ---------------- */}
      {tab === 'sales' && (
        <Card bodyClassName="p-0">
          {sales.isLoading ? (
            <SkeletonRows rows={6} cols={6} />
          ) : sales.isError ? (
            <ErrorState error={sales.error} onRetry={() => void sales.refetch()} />
          ) : (sales.data ?? []).length === 0 ? (
            <EmptyState />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('crm.entryDate')}</th>
                    <th>{t('crm.reference')}</th>
                    <th>{t('crm.paymentMethod')}</th>
                    <th className="text-right">{t('common.quantity')}</th>
                    <th className="text-right">{t('common.total')}</th>
                    <th className="text-right">{t('crm.openAmount')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(sales.data ?? []).map((s) => (
                    <tr key={s.id} className={s.is_cancelled ? 'opacity-50' : ''}>
                      <td className="whitespace-nowrap">{formatDate(s.sale_date)}</td>
                      <td className="tabular">{s.sale_no}</td>
                      <td className="text-xs">{t(`crm.paymentMethods.${s.payment_method}`, { defaultValue: s.payment_method })}</td>
                      <td className="tabular text-right">{formatNumber(s.line_count)}</td>
                      <td className="tabular text-right font-medium">{formatMoney(s.total_amount)}</td>
                      <td
                        className={`tabular text-right ${
                          toNumber(s.due_amount) > 0 ? 'text-danger-600' : 'text-shell-400'
                        }`}
                      >
                        {formatMoney(s.due_amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {/* ---------------- Products bought ---------------- */}
      {tab === 'products' && (
        <Card bodyClassName="p-0">
          {products.isLoading ? (
            <SkeletonRows rows={6} cols={5} />
          ) : products.isError ? (
            <ErrorState error={products.error} onRetry={() => void products.refetch()} />
          ) : (products.data ?? []).length === 0 ? (
            <EmptyState />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('crm.code')}</th>
                    <th>{t('marketing.product')}</th>
                    <th className="text-right">{t('common.quantity')}</th>
                    <th className="text-right">{t('common.amount')}</th>
                    <th className="text-right">{t('crm.orderCount')}</th>
                    <th>{t('crm.lastOrder')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(products.data ?? []).map((p) => (
                    <tr key={p.product_id}>
                      <td className="tabular">{p.product_code}</td>
                      <td>{p.product_name}</td>
                      <td className="tabular text-right">{formatNumber(p.total_quantity, { decimals: 2 })}</td>
                      <td className="tabular text-right">{formatMoney(p.total_amount)}</td>
                      <td className="tabular text-right">{formatNumber(p.order_count)}</td>
                      <td className="whitespace-nowrap">{formatDate(p.last_purchase_date)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {/* ---------------- Contacts ---------------- */}
      {tab === 'contacts' && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2" bodyClassName="p-0">
            {contacts.isLoading ? (
              <SkeletonRows rows={4} cols={4} />
            ) : contacts.isError ? (
              <ErrorState error={contacts.error} onRetry={() => void contacts.refetch()} />
            ) : (contacts.data ?? []).length === 0 ? (
              <EmptyState />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('crm.name')}</th>
                      <th>{t('crm.contactTitle')}</th>
                      <th>{t('crm.phone')}</th>
                      <th>{t('crm.email')}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(contacts.data ?? []).map((k) => (
                      <tr key={k.id}>
                        <td className="font-medium">
                          {k.name}
                          {k.is_primary && <span className="badge-info ml-2">{t('crm.isPrimary')}</span>}
                        </td>
                        <td className="text-xs">{k.title || '—'}</td>
                        <td className="tabular text-xs">{k.phone || '—'}</td>
                        <td className="text-xs">{k.email || '—'}</td>
                        <td className="text-right">
                          {mayEdit && (
                            <button
                              type="button"
                              className="btn-ghost btn-sm text-danger-600"
                              onClick={() => dropContact.mutate(k.id)}
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
          {mayEdit && (
            <Card title={t('crm.addContact')}>
              <div className="space-y-3">
                <Field label={t('crm.name')} required>
                  <input className="input" value={contact.name}
                    onChange={(e) => setContact({ ...contact, name: e.target.value })} />
                </Field>
                <Field label={t('crm.contactTitle')}>
                  <input className="input" value={contact.title}
                    onChange={(e) => setContact({ ...contact, title: e.target.value })} />
                </Field>
                <Field label={t('crm.phone')}>
                  <input className="input" value={contact.phone}
                    onChange={(e) => setContact({ ...contact, phone: e.target.value })} />
                </Field>
                <Field label={t('crm.email')}>
                  <input className="input" type="email" value={contact.email}
                    onChange={(e) => setContact({ ...contact, email: e.target.value })} />
                </Field>
                <button
                  type="button"
                  className="btn-primary w-full"
                  disabled={addContact.isPending || contact.name.trim() === ''}
                  onClick={() => addContact.mutate()}
                >
                  {addContact.isPending ? <Spinner /> : <Plus className="h-4 w-4" />}
                  {t('common.add')}
                </button>
              </div>
            </Card>
          )}
        </div>
      )}

      {/* ---------------- Notes ---------------- */}
      {tab === 'notes' && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2" bodyClassName="p-0">
            {notes.isLoading ? (
              <SkeletonRows rows={4} cols={2} />
            ) : notes.isError ? (
              <ErrorState error={notes.error} onRetry={() => void notes.refetch()} />
            ) : (notes.data ?? []).length === 0 ? (
              <EmptyState />
            ) : (
              <ul className="divide-y divide-shell-100">
                {(notes.data ?? []).map((n) => (
                  <li key={n.id} className="flex items-start gap-3 p-4">
                    <div className="min-w-0 flex-1">
                      <p className="whitespace-pre-wrap text-sm text-shell-800">{n.body}</p>
                      <p className="mt-1 flex items-center gap-2 text-2xs text-shell-400">
                        {n.is_pinned && <Pin className="h-3 w-3" />}
                        {n.category && <span className="badge-muted">{n.category}</span>}
                        {formatDate(n.created_at, { withTime: true })}
                      </p>
                    </div>
                    {mayEdit && (
                      <button
                        type="button"
                        className="btn-ghost btn-sm text-danger-600"
                        onClick={() => dropNote.mutate(n.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>
          {mayEdit && (
            <Card title={t('crm.addNote')}>
              <div className="space-y-3">
                <Field label={t('crm.noteCategory')}>
                  <input className="input" value={note.category}
                    onChange={(e) => setNote({ ...note, category: e.target.value })} />
                </Field>
                <Field label={t('crm.noteBody')} required>
                  <textarea className="input" rows={5} value={note.body}
                    onChange={(e) => setNote({ ...note, body: e.target.value })} />
                </Field>
                <button
                  type="button"
                  className="btn-primary w-full"
                  disabled={addNote.isPending || note.body.trim() === ''}
                  onClick={() => addNote.mutate()}
                >
                  {addNote.isPending ? <Spinner /> : <Plus className="h-4 w-4" />}
                  {t('common.add')}
                </button>
              </div>
            </Card>
          )}
        </div>
      )}

      {/* ---------------- Risk ---------------- */}
      {tab === 'risk' && (
        <>
          {risk.isLoading ? (
            <LoadingBlock />
          ) : risk.isError ? (
            <ErrorState error={risk.error} onRetry={() => void risk.refetch()} />
          ) : !risk.data ? (
            <EmptyState />
          ) : (
            <div className="grid gap-4 lg:grid-cols-3">
              <Card title={t('crm.riskScore')}>
                <div className="flex flex-col items-center gap-2 py-4">
                  <span className="tabular text-4xl font-semibold text-shell-900">
                    {formatNumber(risk.data.risk_score, { decimals: 1 })}
                  </span>
                  <span
                    className={
                      risk.data.risk_band === 'CRITICAL' || risk.data.risk_band === 'HIGH'
                        ? 'badge-danger'
                        : risk.data.risk_band === 'MEDIUM'
                          ? 'badge-warn'
                          : 'badge-ok'
                    }
                  >
                    {t(`crm.riskBands.${risk.data.risk_band}`, { defaultValue: risk.data.risk_band })}
                  </span>
                </div>
              </Card>
              <Card className="lg:col-span-2" title={t('crm.riskFactors')} bodyClassName="p-0">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('crm.factor')}</th>
                        <th className="text-right">{t('crm.value')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[
                        [t('crm.balance'), formatMoney(risk.data.balance)],
                        [t('crm.overdueBalance'), formatMoney(risk.data.overdue_balance)],
                        [t('crm.creditLimit'), formatMoney(risk.data.credit_limit)],
                        [t('crm.creditUtilisation'), formatPercent(risk.data.credit_utilisation_percent)],
                        [t('crm.daysPastDue'), formatNumber(risk.data.days_past_due)],
                        [t('crm.bouncedPayments'), formatNumber(risk.data.bounced_payments_180d)],
                        [
                          t('crm.avgPaymentInterval'),
                          risk.data.average_payment_interval_days === null ||
                          risk.data.average_payment_interval_days === undefined
                            ? '—'
                            : formatNumber(risk.data.average_payment_interval_days, { decimals: 1 }),
                        ],
                        [t('crm.lastPayment'), formatDate(risk.data.last_payment_date)],
                      ].map(([k, v]) => (
                        <tr key={String(k)}>
                          <td>{k}</td>
                          <td className="tabular text-right font-medium">{v}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
              <div className="lg:col-span-3">
                <AgeingPanel data={risk.data.aging} loading={false} error={null} />
              </div>
            </div>
          )}
        </>
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Ageing buckets + chart                                                     */
/* -------------------------------------------------------------------------- */
function AgeingPanel({
  data, loading, error,
}: {
  data: Aging | undefined
  loading: boolean
  error: unknown
}) {
  const { t } = useTranslation()
  if (loading) return <LoadingBlock />
  if (error) return <ErrorState error={error} />
  if (!data) return <EmptyState />

  const chart = BUCKETS.map((b) => ({ name: t(b.label), value: toNumber(data[b.key]) }))

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-5">
        {BUCKETS.map((b) => (
          <div key={b.key} className="card p-4">
            <p className="text-2xs font-medium uppercase tracking-wide text-shell-500">
              {t(b.label)}
            </p>
            <p className="tabular mt-1 text-lg font-semibold text-shell-900">
              {formatMoney(data[b.key])}
            </p>
          </div>
        ))}
      </div>
      <Card title={t('crm.ageing')}>
        <div className="mb-4 flex flex-wrap gap-6 text-sm">
          <span className="text-shell-500">
            {t('common.total')}:{' '}
            <span className="tabular font-semibold text-shell-900">{formatMoney(data.total)}</span>
          </span>
          <span className="text-shell-500">
            {t('crm.overdueBalance')}:{' '}
            <span className="tabular font-semibold text-danger-600">{formatMoney(data.overdue)}</span>
          </span>
        </div>
        <div className="h-64 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chart} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="name" tick={{ fontSize: 11 }} />
              <YAxis
                tick={{ fontSize: 11 }}
                tickFormatter={(v: number) => formatNumber(v, { compact: true })}
              />
              <Tooltip formatter={(v: number | string) => formatMoney(v)} />
              <Bar dataKey="value" fill="#2563eb" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
    </div>
  )
}
