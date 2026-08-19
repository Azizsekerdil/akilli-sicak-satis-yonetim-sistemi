/**
 * Tahsilatlar / Collections.
 *
 * Records money coming in and allocates it to open invoices.  Cheques and
 * promissory notes stay PENDING until they clear, so the two instrument
 * actions (clear / bounce) live on the list itself.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Banknote, Check, Plus, RefreshCw, Search, XCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, toNumber } from '@/lib/format'
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
  StatusBadge,
  useToast,
} from '@/components/ui'

const METHODS = ['CASH', 'CREDIT_CARD', 'BANK_TRANSFER', 'CHEQUE', 'PROMISSORY_NOTE'] as const
const STATUSES = ['PENDING', 'CLEARED', 'BOUNCED', 'CANCELLED'] as const
const INSTRUMENTS = ['CHEQUE', 'PROMISSORY_NOTE']

interface Allocation {
  id: number
  invoice_id: number
  amount: number | string
  invoice_no?: string | null
  invoice_date?: string | null
}

interface Payment {
  id: number
  payment_no: string
  customer_id: number
  customer_name?: string | null
  payment_date: string
  payment_method: string
  status: string
  amount: number | string
  allocated_amount: number | string
  unallocated_amount: number | string
  bank_name?: string | null
  document_number?: string | null
  maturity_date?: string | null
  drawer_name?: string | null
  reference?: string | null
  notes?: string | null
  allocations: Allocation[]
}

interface MethodTotal {
  payment_method: string
  status?: string | null
  count: number
  amount: number | string
}

interface CollectionsSummary {
  start: string
  end: string
  total_amount: number | string
  cleared_amount: number | string
  pending_amount: number | string
  bounced_amount: number | string
  count: number
  by_method: MethodTotal[]
}

interface OpenInvoice {
  invoice_id: number
  invoice_no: string
  invoice_date: string
  due_date?: string | null
  open_amount: number | string
  days_overdue: number
  status: string
}

interface CustomerOption {
  id: number
  code: string
  name: string
  trade_name?: string | null
}

const SIZE = 25
const monthStart = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`
}
const today = () => new Date().toISOString().slice(0, 10)

export default function Payments() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [start, setStart] = useState(monthStart)
  const [end, setEnd] = useState(today)
  const [method, setMethod] = useState('')
  const [status, setStatus] = useState('')
  const [customer, setCustomer] = useState<CustomerOption | null>(null)
  const [creating, setCreating] = useState(false)
  const [detail, setDetail] = useState<Payment | null>(null)
  const [bounceTarget, setBounceTarget] = useState<Payment | null>(null)
  const [bounceReason, setBounceReason] = useState('')

  const params = {
    page,
    size: SIZE,
    search: search || undefined,
    start: start || undefined,
    end: end || undefined,
    payment_method: method || undefined,
    status: status || undefined,
    customer_id: customer?.id,
  }

  const list = useQuery({
    queryKey: ['payments', params],
    queryFn: () => api.get<Paged<Payment>>('/sales/payments', params),
  })

  const summary = useQuery({
    queryKey: ['payments-summary', start, end],
    queryFn: () =>
      api.get<CollectionsSummary>('/sales/payments-summary', {
        start: start || monthStart(),
        end: end || today(),
      }),
    retry: false,
    throwOnError: false,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['payments'] })
    void qc.invalidateQueries({ queryKey: ['payments-summary'] })
  }

  const clear = useMutation({
    mutationFn: (id: number) => api.post<Payment>(`/sales/payments/${id}/clear`),
    onSuccess: () => { push('success', t('payments.cleared')); invalidate() },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const bounce = useMutation({
    mutationFn: () =>
      api.post<Payment>(`/sales/payments/${bounceTarget!.id}/bounce`, { reason: bounceReason }),
    onSuccess: () => {
      push('success', t('payments.bounced'))
      setBounceTarget(null)
      setBounceReason('')
      invalidate()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []
  const canUpdate = can('sales.payments', 'UPDATE')

  return (
    <>
      <PageHeader
        title={t('payments.title')}
        icon={<Banknote className="h-5 w-5" />}
        actions={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => void list.refetch()}>
              <RefreshCw className="h-3.5 w-3.5" />
              {t('common.refresh')}
            </button>
            {can('sales.payments', 'CREATE') && (
              <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
                <Plus className="h-3.5 w-3.5" />
                {t('payments.record')}
              </button>
            )}
          </>
        }
      />

      {/* Summary strip */}
      {summary.data && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile label={t('payments.totalCollected')} value={summary.data.total_amount} tone="brand" />
          <Tile label={t('payments.clearedAmount')} value={summary.data.cleared_amount} tone="ok" />
          <Tile label={t('payments.pendingAmount')} value={summary.data.pending_amount} tone="warn" />
          <Tile label={t('payments.bouncedAmount')} value={summary.data.bounced_amount} tone="danger" />
          {summary.data.by_method.length > 0 && (
            <div className="col-span-2 flex flex-wrap gap-2 md:col-span-4">
              {summary.data.by_method.map((m, i) => (
                <span
                  key={`${m.payment_method}-${m.status ?? i}`}
                  className="badge-muted"
                >
                  {t(`payment.${m.payment_method}`, m.payment_method)} · {formatMoney(m.amount)} (
                  {formatNumber(m.count)})
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="relative min-w-[14rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('payments.searchPlaceholder')}
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(1) }}
            />
          </div>
          <input
            type="date"
            aria-label={t('common.from')}
            className="input w-auto"
            value={start}
            onChange={(e) => { setStart(e.target.value); setPage(1) }}
          />
          <input
            type="date"
            aria-label={t('common.to')}
            className="input w-auto"
            value={end}
            onChange={(e) => { setEnd(e.target.value); setPage(1) }}
          />
          <select
            aria-label={t('common.method')}
            className="input w-auto"
            value={method}
            onChange={(e) => { setMethod(e.target.value); setPage(1) }}
          >
            <option value="">{t('payments.allMethods')}</option>
            {METHODS.map((m) => (
              <option key={m} value={m}>{t(`payment.${m}`)}</option>
            ))}
          </select>
          <select
            aria-label={t('common.status')}
            className="input w-auto"
            value={status}
            onChange={(e) => { setStatus(e.target.value); setPage(1) }}
          >
            <option value="">{t('payments.allStatuses')}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>{t(`status.${s}`)}</option>
            ))}
          </select>
          <CustomerPicker value={customer} onChange={(c) => { setCustomer(c); setPage(1) }} />
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {list.isLoading ? (
          <SkeletonRows rows={8} cols={6} />
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
                    <th>{t('payments.paymentNo')}</th>
                    <th>{t('common.date')}</th>
                    <th>{t('common.customer')}</th>
                    <th>{t('common.method')}</th>
                    <th>{t('common.status')}</th>
                    <th className="text-right">{t('common.amount')}</th>
                    <th className="text-right">{t('payments.unallocated')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr key={p.id}>
                      <td className="font-medium text-shell-800">{p.payment_no}</td>
                      <td className="tabular">{formatDate(p.payment_date, { short: true })}</td>
                      <td className="max-w-[16rem] truncate">{p.customer_name ?? `#${p.customer_id}`}</td>
                      <td className="text-xs text-shell-600">
                        {t(`payment.${p.payment_method}`, p.payment_method)}
                        {p.document_number && (
                          <span className="block text-2xs text-shell-400">{p.document_number}</span>
                        )}
                      </td>
                      <td><StatusBadge status={p.status} label={t(`status.${p.status}`, p.status)} /></td>
                      <td className="tabular text-right font-medium">{formatMoney(p.amount)}</td>
                      <td className="tabular text-right">{formatMoney(p.unallocated_amount)}</td>
                      <td className="whitespace-nowrap text-right">
                        {canUpdate && p.status === 'PENDING' && INSTRUMENTS.includes(p.payment_method) && (
                          <>
                            <button
                              type="button"
                              className="btn-ghost btn-sm"
                              title={t('payments.clear')}
                              disabled={clear.isPending}
                              onClick={() => clear.mutate(p.id)}
                            >
                              <Check className="h-3.5 w-3.5 text-ok-600" />
                            </button>
                            <button
                              type="button"
                              className="btn-ghost btn-sm"
                              title={t('payments.bounce')}
                              onClick={() => setBounceTarget(p)}
                            >
                              <XCircle className="h-3.5 w-3.5 text-danger-600" />
                            </button>
                          </>
                        )}
                        <button type="button" className="btn-ghost btn-sm" onClick={() => setDetail(p)}>
                          {t('common.details')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={list.data?.page ?? page}
              pages={list.data?.pages ?? 1}
              total={list.data?.total ?? 0}
              size={list.data?.size ?? SIZE}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      <RecordModal
        open={creating}
        onClose={() => setCreating(false)}
        onSaved={() => { setCreating(false); push('success', t('payments.recorded')); invalidate() }}
        onError={(e) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))}
      />

      {/* Allocations */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        size="md"
        title={detail ? `${t('payments.paymentNo')} ${detail.payment_no}` : ''}
        footer={
          <button type="button" className="btn-secondary" onClick={() => setDetail(null)}>
            {t('common.close')}
          </button>
        }
      >
        {detail && (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <Info label={t('common.customer')} value={detail.customer_name ?? `#${detail.customer_id}`} />
              <Info label={t('common.method')} value={t(`payment.${detail.payment_method}`, detail.payment_method)} />
              <Info label={t('common.amount')} value={formatMoney(detail.amount)} />
              {detail.bank_name && <Info label={t('payments.bankName')} value={detail.bank_name} />}
              {detail.document_number && (
                <Info label={t('payments.documentNumber')} value={detail.document_number} />
              )}
              {detail.maturity_date && (
                <Info label={t('payments.maturityDate')} value={formatDate(detail.maturity_date)} />
              )}
              {detail.drawer_name && <Info label={t('payments.drawerName')} value={detail.drawer_name} />}
              {detail.reference && <Info label={t('payments.reference')} value={detail.reference} />}
            </dl>

            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
                {t('payments.allocations')}
              </p>
              {detail.allocations.length === 0 ? (
                <EmptyState />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('invoices.invoiceNo')}</th>
                        <th>{t('common.date')}</th>
                        <th className="text-right">{t('common.amount')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.allocations.map((a) => (
                        <tr key={a.id}>
                          <td>{a.invoice_no ?? `#${a.invoice_id}`}</td>
                          <td className="tabular">{formatDate(a.invoice_date, { short: true })}</td>
                          <td className="tabular text-right">{formatMoney(a.amount)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>

      {/* Bounce */}
      <Modal
        open={bounceTarget !== null}
        onClose={() => setBounceTarget(null)}
        size="sm"
        title={t('payments.bounceTitle')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setBounceTarget(null)}>
              {t('common.close')}
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={bounceReason.trim().length < 3 || bounce.isPending}
              onClick={() => bounce.mutate()}
            >
              {bounce.isPending ? <Spinner /> : <XCircle className="h-4 w-4" />}
              {t('common.confirm')}
            </button>
          </>
        }
      >
        <Field label={t('payments.bounceReason')} required>
          <textarea
            className="input min-h-[5rem]"
            value={bounceReason}
            onChange={(e) => setBounceReason(e.target.value)}
          />
        </Field>
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Record a collection                                                        */
/* -------------------------------------------------------------------------- */
function RecordModal({
  open,
  onClose,
  onSaved,
  onError,
}: {
  open: boolean
  onClose: () => void
  onSaved: () => void
  onError: (e: unknown) => void
}) {
  const { t } = useTranslation()
  const [customer, setCustomer] = useState<CustomerOption | null>(null)
  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState<string>('CASH')
  const [paymentDate, setPaymentDate] = useState(today)
  const [instrument, setInstrument] = useState({
    bank_name: '', document_number: '', maturity_date: '', drawer_name: '',
  })
  const [reference, setReference] = useState('')
  const [notes, setNotes] = useState('')
  const [selected, setSelected] = useState<number[]>([])

  const openInvoices = useQuery({
    queryKey: ['open-invoices', customer?.id],
    queryFn: () => api.get<OpenInvoice[]>('/sales/open-invoices', { customer_id: customer!.id }),
    enabled: open && !!customer,
    retry: false,
    throwOnError: false,
  })

  const selectedTotal = useMemo(
    () =>
      (openInvoices.data ?? [])
        .filter((i) => selected.includes(i.invoice_id))
        .reduce((sum, i) => sum + toNumber(i.open_amount), 0),
    [openInvoices.data, selected],
  )

  const save = useMutation({
    mutationFn: () =>
      api.post('/sales/payments', {
        customer_id: customer!.id,
        amount: toNumber(amount),
        payment_method: method,
        payment_date: paymentDate || null,
        invoice_ids: selected.length > 0 ? selected : null,
        bank_name: instrument.bank_name || null,
        document_number: instrument.document_number || null,
        maturity_date: instrument.maturity_date || null,
        drawer_name: instrument.drawer_name || null,
        reference: reference || null,
        notes: notes || null,
      }),
    onSuccess: () => {
      setCustomer(null); setAmount(''); setMethod('CASH'); setSelected([])
      setInstrument({ bank_name: '', document_number: '', maturity_date: '', drawer_name: '' })
      setReference(''); setNotes('')
      onSaved()
    },
    onError,
  })

  const isInstrument = INSTRUMENTS.includes(method)
  const valid = !!customer && toNumber(amount) > 0

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title={t('payments.record')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!valid || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? <Spinner /> : <Check className="h-4 w-4" />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('common.customer')} required>
            <CustomerPicker value={customer} onChange={(c) => { setCustomer(c); setSelected([]) }} wide />
          </Field>
          <Field label={t('common.amount')} required>
            <input
              type="number"
              step="0.01"
              min={0}
              className="input tabular text-right"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </Field>
          <Field label={t('common.method')} required>
            <select className="input" value={method} onChange={(e) => setMethod(e.target.value)}>
              {METHODS.map((m) => (
                <option key={m} value={m}>{t(`payment.${m}`)}</option>
              ))}
            </select>
          </Field>
          <Field label={t('common.date')}>
            <input
              type="date"
              className="input"
              value={paymentDate}
              onChange={(e) => setPaymentDate(e.target.value)}
            />
          </Field>
        </div>

        {isInstrument && (
          <div className="rounded-lg border border-shell-200 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
              {t('payments.instrumentDetails')}
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t('payments.bankName')}>
                <input
                  className="input"
                  value={instrument.bank_name}
                  onChange={(e) => setInstrument((s) => ({ ...s, bank_name: e.target.value }))}
                />
              </Field>
              <Field label={t('payments.documentNumber')}>
                <input
                  className="input"
                  value={instrument.document_number}
                  onChange={(e) => setInstrument((s) => ({ ...s, document_number: e.target.value }))}
                />
              </Field>
              <Field label={t('payments.maturityDate')}>
                <input
                  type="date"
                  className="input"
                  value={instrument.maturity_date}
                  onChange={(e) => setInstrument((s) => ({ ...s, maturity_date: e.target.value }))}
                />
              </Field>
              <Field label={t('payments.drawerName')}>
                <input
                  className="input"
                  value={instrument.drawer_name}
                  onChange={(e) => setInstrument((s) => ({ ...s, drawer_name: e.target.value }))}
                />
              </Field>
            </div>
          </div>
        )}

        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-shell-500">
            {t('payments.openInvoices')}
          </p>
          <p className="mb-2 text-2xs text-shell-400">{t('payments.allocateHint')}</p>
          {!customer ? (
            <p className="text-sm text-shell-400">{t('payments.selectCustomerFirst')}</p>
          ) : openInvoices.isLoading ? (
            <SkeletonRows rows={3} cols={3} />
          ) : (openInvoices.data ?? []).length === 0 ? (
            <p className="text-sm text-shell-400">{t('payments.noOpenInvoices')}</p>
          ) : (
            <>
              <div className="table-wrap max-h-56 overflow-y-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-8" />
                      <th>{t('invoices.invoiceNo')}</th>
                      <th>{t('invoices.dueDate')}</th>
                      <th className="text-right">{t('invoices.openAmount')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(openInvoices.data ?? []).map((inv) => (
                      <tr key={inv.invoice_id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={inv.invoice_no}
                            checked={selected.includes(inv.invoice_id)}
                            onChange={(e) =>
                              setSelected((prev) =>
                                e.target.checked
                                  ? [...prev, inv.invoice_id]
                                  : prev.filter((x) => x !== inv.invoice_id),
                              )
                            }
                          />
                        </td>
                        <td>{inv.invoice_no}</td>
                        <td className={`tabular ${inv.days_overdue > 0 ? 'text-danger-600' : ''}`}>
                          {formatDate(inv.due_date, { short: true })}
                        </td>
                        <td className="tabular text-right">{formatMoney(inv.open_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {selected.length > 0 && (
                <p className="mt-2 text-right text-xs text-shell-600">
                  {t('common.total')}: <span className="tabular">{formatMoney(selectedTotal)}</span>
                </p>
              )}
            </>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('payments.reference')}>
            <input className="input" value={reference} onChange={(e) => setReference(e.target.value)} />
          </Field>
          <Field label={t('common.notes')}>
            <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Pieces                                                                     */
/* -------------------------------------------------------------------------- */
function Tile({
  label,
  value,
  tone,
}: {
  label: string
  value: number | string
  tone: 'brand' | 'ok' | 'warn' | 'danger'
}) {
  const colour = {
    brand: 'text-brand-700', ok: 'text-ok-700', warn: 'text-warn-700', danger: 'text-danger-700',
  }[tone]
  return (
    <div className="card p-4">
      <p className="truncate text-xs font-medium uppercase tracking-wide text-shell-500">{label}</p>
      <p className={`tabular mt-1.5 text-xl font-semibold ${colour}`}>{formatMoney(value)}</p>
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
      <dd className="text-shell-800">{value}</dd>
    </div>
  )
}

function CustomerPicker({
  value,
  onChange,
  wide,
}: {
  value: CustomerOption | null
  onChange: (c: CustomerOption | null) => void
  wide?: boolean
}) {
  const { t } = useTranslation()
  const [term, setTerm] = useState('')
  const [open, setOpen] = useState(false)

  const results = useQuery({
    queryKey: ['customer-lookup', term],
    queryFn: () => api.get<Paged<CustomerOption>>('/customers', { term, size: 8 }),
    enabled: open && term.trim().length >= 2,
    retry: false,
    throwOnError: false,
  })

  if (value) {
    return (
      <button
        type="button"
        className={`btn-secondary btn-sm ${wide ? 'w-full justify-between' : 'max-w-[14rem]'}`}
        onClick={() => { onChange(null); setTerm('') }}
      >
        <span className="truncate">{value.trade_name || value.name}</span>
        <span className="text-shell-400">×</span>
      </button>
    )
  }

  return (
    <div className="relative">
      <input
        className={wide ? 'input' : 'input w-44'}
        placeholder={t('common.customer')}
        value={term}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onChange={(e) => setTerm(e.target.value)}
      />
      {open && (results.data?.items ?? []).length > 0 && (
        <ul className="absolute z-20 mt-1 max-h-60 w-64 overflow-y-auto rounded-lg border border-shell-200 bg-white py-1 shadow-pop">
          {(results.data?.items ?? []).map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className="block w-full px-3 py-1.5 text-left text-sm hover:bg-shell-50"
                onMouseDown={() => { onChange(c); setTerm(''); setOpen(false) }}
              >
                <span className="block truncate">{c.trade_name || c.name}</span>
                <span className="block text-2xs text-shell-400">{c.code}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
