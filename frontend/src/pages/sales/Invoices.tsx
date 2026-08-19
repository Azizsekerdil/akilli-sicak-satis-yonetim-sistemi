/**
 * Faturalar / Invoices.
 *
 * The receivable side of a sale: what was billed, what is still open and what
 * is already past its due date.  The PDF is rendered by the backend's own
 * exporter so the printout at the door matches the ledger exactly.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Download, FileText, RefreshCw, Search } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatQuantity } from '@/lib/format'
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

const INVOICE_STATUSES = [
  'DRAFT', 'ISSUED', 'PARTIALLY_PAID', 'PAID', 'OVERDUE', 'CANCELLED',
] as const
const DOCUMENT_TYPES = ['INVOICE', 'WAYBILL', 'CREDIT_NOTE', 'RECEIPT', 'PROFORMA'] as const

interface InvoiceListItem {
  id: number
  invoice_no: string
  document_type: string
  status: string
  invoice_date: string
  due_date?: string | null
  customer_id: number
  customer_name?: string | null
  sale_id?: number | null
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  paid_amount: number | string
  open_amount: number | string
  is_overdue: boolean
}

interface InvoiceItem {
  id: number
  product_id: number
  line_no: number
  description?: string | null
  quantity: number | string
  uom: string
  unit_price: number | string
  discount_amount: number | string
  net_amount: number | string
  vat_rate: number
  vat_amount: number | string
  total_amount: number | string
}

interface InvoiceDetail extends InvoiceListItem {
  serial?: string | null
  discount_amount: number | string
  issued_at?: string | null
  cancelled_at?: string | null
  cancel_reason?: string | null
  notes?: string | null
  items: InvoiceItem[]
}

interface CustomerOption {
  id: number
  code: string
  name: string
  trade_name?: string | null
}

const SIZE = 25

export default function Invoices() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [status, setStatus] = useState('')
  const [documentType, setDocumentType] = useState('')
  const [onlyOpen, setOnlyOpen] = useState(false)
  const [customer, setCustomer] = useState<CustomerOption | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [cancelId, setCancelId] = useState<number | null>(null)
  const [reason, setReason] = useState('')
  const [downloading, setDownloading] = useState<number | null>(null)

  const params = {
    page,
    size: SIZE,
    search: search || undefined,
    start: start || undefined,
    end: end || undefined,
    status: status || undefined,
    document_type: documentType || undefined,
    only_open: onlyOpen || undefined,
    customer_id: customer?.id,
  }

  const list = useQuery({
    queryKey: ['invoices', params],
    queryFn: () => api.get<Paged<InvoiceListItem>>('/sales/invoices', params),
  })

  const detail = useQuery({
    queryKey: ['invoice', detailId],
    queryFn: () => api.get<InvoiceDetail>(`/sales/invoices/${detailId}`),
    enabled: detailId !== null,
  })

  const cancel = useMutation({
    mutationFn: () => api.post(`/sales/invoices/${cancelId}/cancel`, { reason }),
    onSuccess: () => {
      push('success', t('invoices.cancelled'))
      setCancelId(null)
      setReason('')
      setDetailId(null)
      void qc.invalidateQueries({ queryKey: ['invoices'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const downloadPdf = async (id: number) => {
    setDownloading(id)
    try {
      const { blob, filename } = await api.download(`/sales/invoices/${id}/pdf`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('invoices.pdfFailed'))
    } finally {
      setDownloading(null)
    }
  }

  const rows = list.data?.items ?? []
  const canCancel = can('sales.invoices', 'UPDATE')

  return (
    <>
      <PageHeader
        title={t('invoices.title')}
        icon={<FileText className="h-5 w-5" />}
        actions={
          <button type="button" className="btn-secondary btn-sm" onClick={() => void list.refetch()}>
            <RefreshCw className="h-3.5 w-3.5" />
            {t('common.refresh')}
          </button>
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="relative min-w-[14rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('invoices.searchPlaceholder')}
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
            aria-label={t('common.status')}
            className="input w-auto"
            value={status}
            onChange={(e) => { setStatus(e.target.value); setPage(1) }}
          >
            <option value="">{t('invoices.allStatuses')}</option>
            {INVOICE_STATUSES.map((s) => (
              <option key={s} value={s}>{t(`status.${s}`)}</option>
            ))}
          </select>
          <select
            aria-label={t('common.type')}
            className="input w-auto"
            value={documentType}
            onChange={(e) => { setDocumentType(e.target.value); setPage(1) }}
          >
            <option value="">{t('invoices.allTypes')}</option>
            {DOCUMENT_TYPES.map((s) => (
              <option key={s} value={s}>{t(`documentType.${s}`)}</option>
            ))}
          </select>
          <CustomerPicker value={customer} onChange={(c) => { setCustomer(c); setPage(1) }} />
          <label className="flex items-center gap-1.5 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={onlyOpen}
              onChange={(e) => { setOnlyOpen(e.target.checked); setPage(1) }}
            />
            {t('invoices.openOnly')}
          </label>
          <button
            type="button"
            className="btn-ghost btn-sm"
            onClick={() => {
              setSearch(''); setStart(''); setEnd(''); setStatus('')
              setDocumentType(''); setOnlyOpen(false); setCustomer(null); setPage(1)
            }}
          >
            {t('common.reset')}
          </button>
        </div>
      </Card>

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
                    <th>{t('invoices.invoiceNo')}</th>
                    <th>{t('invoices.invoiceDate')}</th>
                    <th>{t('common.customer')}</th>
                    <th>{t('common.status')}</th>
                    <th>{t('invoices.dueDate')}</th>
                    <th className="text-right">{t('common.total')}</th>
                    <th className="text-right">{t('invoices.openAmount')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((inv) => (
                    <tr key={inv.id}>
                      <td className="font-medium text-shell-800">{inv.invoice_no}</td>
                      <td className="tabular">{formatDate(inv.invoice_date, { short: true })}</td>
                      <td className="max-w-[16rem] truncate">
                        {inv.customer_name ?? `#${inv.customer_id}`}
                      </td>
                      <td>
                        <StatusBadge
                          status={inv.is_overdue ? 'OVERDUE' : inv.status}
                          label={t(`status.${inv.is_overdue ? 'OVERDUE' : inv.status}`, inv.status)}
                        />
                      </td>
                      <td className={`tabular ${inv.is_overdue ? 'text-danger-600' : ''}`}>
                        {formatDate(inv.due_date, { short: true })}
                      </td>
                      <td className="tabular text-right font-medium">{formatMoney(inv.total_amount)}</td>
                      <td className="tabular text-right">{formatMoney(inv.open_amount)}</td>
                      <td className="whitespace-nowrap text-right">
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          disabled={downloading === inv.id}
                          onClick={() => void downloadPdf(inv.id)}
                          aria-label={t('invoices.downloadPdf')}
                          title={t('invoices.downloadPdf')}
                        >
                          {downloading === inv.id ? <Spinner /> : <Download className="h-3.5 w-3.5" />}
                        </button>
                        <button type="button" className="btn-ghost btn-sm" onClick={() => setDetailId(inv.id)}>
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

      {/* Detail */}
      <Modal
        open={detailId !== null}
        onClose={() => setDetailId(null)}
        size="lg"
        title={detail.data ? `${t('invoices.detail')} — ${detail.data.invoice_no}` : t('invoices.detail')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setDetailId(null)}>
              {t('common.close')}
            </button>
            {detail.data && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => void downloadPdf(detail.data!.id)}
              >
                <Download className="h-4 w-4" />
                {t('invoices.downloadPdf')}
              </button>
            )}
            {canCancel && detail.data && detail.data.status !== 'CANCELLED' && (
              <button
                type="button"
                className="btn-danger"
                onClick={() => setCancelId(detail.data!.id)}
              >
                <Ban className="h-4 w-4" />
                {t('common.cancel')}
              </button>
            )}
          </>
        }
      >
        {detail.isLoading ? (
          <SkeletonRows rows={6} cols={5} />
        ) : detail.isError ? (
          <ErrorState error={detail.error} />
        ) : !detail.data ? (
          <EmptyState />
        ) : (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <Info label={t('common.customer')} value={detail.data.customer_name ?? `#${detail.data.customer_id}`} />
              <Info label={t('common.type')} value={t(`documentType.${detail.data.document_type}`, detail.data.document_type)} />
              <Info label={t('common.status')} value={t(`status.${detail.data.status}`, detail.data.status)} />
              <Info label={t('invoices.invoiceDate')} value={formatDate(detail.data.invoice_date)} />
              <Info label={t('invoices.dueDate')} value={formatDate(detail.data.due_date)} />
              <Info label={t('invoices.paidAmount')} value={formatMoney(detail.data.paid_amount)} />
            </dl>

            {detail.data.cancel_reason && (
              <p className="rounded-lg bg-danger-50 p-2.5 text-xs text-danger-700">
                {detail.data.cancel_reason}
              </p>
            )}

            {detail.data.items.length === 0 ? (
              <EmptyState title={t('invoices.noLines')} />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-8">#</th>
                      <th>{t('common.description')}</th>
                      <th className="text-right">{t('common.quantity')}</th>
                      <th className="text-right">{t('common.unitPrice')}</th>
                      <th className="text-right">{t('common.vat')}</th>
                      <th className="text-right">{t('common.total')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.items.map((it) => (
                      <tr key={it.id}>
                        <td className="tabular text-shell-400">{it.line_no}</td>
                        <td className="max-w-[18rem] truncate">
                          {it.description ?? `#${it.product_id}`}
                        </td>
                        <td className="tabular text-right">{formatQuantity(it.quantity, it.uom)}</td>
                        <td className="tabular text-right">{formatMoney(it.unit_price)}</td>
                        <td className="tabular text-right">{formatMoney(it.vat_amount)}</td>
                        <td className="tabular text-right font-medium">{formatMoney(it.total_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <dl className="ml-auto max-w-xs space-y-1 text-sm">
              <Total label={t('common.net')} value={detail.data.net_amount} />
              <Total label={t('common.vat')} value={detail.data.vat_amount} />
              <Total label={t('invoices.paidAmount')} value={detail.data.paid_amount} />
              <div className="flex justify-between border-t border-shell-200 pt-1.5 font-semibold">
                <dt>{t('invoices.openAmount')}</dt>
                <dd className="tabular">{formatMoney(detail.data.open_amount)}</dd>
              </div>
            </dl>
          </div>
        )}
      </Modal>

      {/* Cancel */}
      <Modal
        open={cancelId !== null}
        onClose={() => setCancelId(null)}
        size="sm"
        title={t('invoices.cancelTitle')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setCancelId(null)}>
              {t('common.close')}
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={reason.trim().length < 3 || cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              {cancel.isPending ? <Spinner /> : <Ban className="h-4 w-4" />}
              {t('common.confirm')}
            </button>
          </>
        }
      >
        <Field label={t('invoices.cancelReason')} required>
          <textarea
            className="input min-h-[5rem]"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Pieces                                                                     */
/* -------------------------------------------------------------------------- */
function CustomerPicker({
  value,
  onChange,
}: {
  value: CustomerOption | null
  onChange: (c: CustomerOption | null) => void
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
        className="btn-secondary btn-sm max-w-[14rem]"
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
        className="input w-44"
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

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
      <dd className="text-shell-800">{value}</dd>
    </div>
  )
}

function Total({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex justify-between text-shell-500">
      <dt>{label}</dt>
      <dd className="tabular">{formatMoney(value)}</dd>
    </div>
  )
}
