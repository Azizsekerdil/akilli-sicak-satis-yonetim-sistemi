/**
 * İadeler / Returns.
 *
 * A return is captured first and posted second: posting is what actually moves
 * the goods back onto the van (or into scrap) and raises the credit note, so
 * it stays an explicit action rather than a side effect of saving.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, PackageX, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatQuantity, toNumber } from '@/lib/format'
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

const REASONS = [
  'EXPIRED', 'DAMAGED', 'WRONG_PRODUCT', 'QUALITY', 'OVERSTOCK',
  'CUSTOMER_REQUEST', 'RECALL', 'OTHER',
] as const
const DISPOSITIONS = ['RESALEABLE', 'SCRAP', 'QUARANTINE'] as const
const UOMS = ['CASE', 'PIECE', 'PACK', 'PALLET'] as const

interface ReturnListItem {
  id: number
  return_no: string
  return_date: string
  customer_id: number
  customer_name?: string | null
  sale_id?: number | null
  reason: string
  disposition: string
  is_posted: boolean
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  line_count: number
}

interface ReturnItem {
  id: number
  product_id: number
  line_no: number
  quantity: number | string
  uom: string
  unit_price: number | string
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  reason: string
  disposition: string
  expiry_date?: string | null
}

interface ReturnDetail extends ReturnListItem {
  posted_at?: string | null
  creates_credit_note: boolean
  notes?: string | null
  credit_note?: { id: number; invoice_no: string; total_amount: number | string } | null
  items: ReturnItem[]
}

interface CustomerOption {
  id: number
  code: string
  name: string
  trade_name?: string | null
}

interface ProductRef {
  id: number
  sku: string
  name: string
  sales_uom: string
}

interface SaleRef {
  id: number
  sale_no: string
  sale_date: string
  total_amount: number | string
}

interface DraftLine {
  product_id: number
  name: string
  sku: string
  quantity: number
  uom: string
  reason: string
  disposition: string
}

const SIZE = 25

export default function Returns() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [reason, setReason] = useState('')
  const [disposition, setDisposition] = useState('')
  const [postedOnly, setPostedOnly] = useState(false)
  const [creating, setCreating] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)

  const params = {
    page,
    size: SIZE,
    search: search || undefined,
    start: start || undefined,
    end: end || undefined,
    reason: reason || undefined,
    disposition: disposition || undefined,
    is_posted: postedOnly ? true : undefined,
  }

  const list = useQuery({
    queryKey: ['returns', params],
    queryFn: () => api.get<Paged<ReturnListItem>>('/sales/returns', params),
  })

  const detail = useQuery({
    queryKey: ['return', detailId],
    queryFn: () => api.get<ReturnDetail>(`/sales/returns/${detailId}`),
    enabled: detailId !== null,
  })

  const products = useQuery({
    queryKey: ['product-names'],
    queryFn: () => api.get<Paged<ProductRef>>('/products', { size: 500 }),
    enabled: detailId !== null && can('stock.products', 'VIEW'),
    staleTime: 5 * 60_000,
    retry: false,
    throwOnError: false,
  })

  const productNames = useMemo(() => {
    const map = new Map<number, string>()
    for (const p of products.data?.items ?? []) map.set(p.id, `${p.name} · ${p.sku}`)
    return map
  }, [products.data])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['returns'] })
    void qc.invalidateQueries({ queryKey: ['return'] })
  }

  const post = useMutation({
    mutationFn: (id: number) => api.post<ReturnDetail>(`/sales/returns/${id}/post`),
    onSuccess: () => { push('success', t('returns.postedOk')); invalidate() },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const canPost = can('sales.returns', 'APPROVE') || can('sales.returns', 'CREATE')
  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('returns.title')}
        icon={<PackageX className="h-5 w-5" />}
        actions={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => void list.refetch()}>
              <RefreshCw className="h-3.5 w-3.5" />
              {t('common.refresh')}
            </button>
            {can('sales.returns', 'CREATE') && (
              <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
                <Plus className="h-3.5 w-3.5" />
                {t('returns.create')}
              </button>
            )}
          </>
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="relative min-w-[14rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('returns.searchPlaceholder')}
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
            aria-label={t('returns.reason')}
            className="input w-auto"
            value={reason}
            onChange={(e) => { setReason(e.target.value); setPage(1) }}
          >
            <option value="">{t('returns.allReasons')}</option>
            {REASONS.map((r) => (
              <option key={r} value={r}>{t(`returnReason.${r}`)}</option>
            ))}
          </select>
          <select
            aria-label={t('returns.disposition')}
            className="input w-auto"
            value={disposition}
            onChange={(e) => { setDisposition(e.target.value); setPage(1) }}
          >
            <option value="">{t('returns.allDispositions')}</option>
            {DISPOSITIONS.map((d) => (
              <option key={d} value={d}>{t(`returnDisposition.${d}`)}</option>
            ))}
          </select>
          <label className="flex items-center gap-1.5 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={postedOnly}
              onChange={(e) => { setPostedOnly(e.target.checked); setPage(1) }}
            />
            {t('returns.postedFilter')}
          </label>
          <button
            type="button"
            className="btn-ghost btn-sm"
            onClick={() => {
              setSearch(''); setStart(''); setEnd(''); setReason('')
              setDisposition(''); setPostedOnly(false); setPage(1)
            }}
          >
            {t('common.reset')}
          </button>
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
                    <th>{t('returns.returnNo')}</th>
                    <th>{t('returns.returnDate')}</th>
                    <th>{t('common.customer')}</th>
                    <th>{t('returns.reason')}</th>
                    <th>{t('returns.disposition')}</th>
                    <th>{t('common.status')}</th>
                    <th className="text-right">{t('common.total')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="font-medium text-shell-800">{r.return_no}</td>
                      <td className="tabular">{formatDate(r.return_date, { short: true })}</td>
                      <td className="max-w-[16rem] truncate">{r.customer_name ?? `#${r.customer_id}`}</td>
                      <td className="text-xs text-shell-600">{t(`returnReason.${r.reason}`, r.reason)}</td>
                      <td className="text-xs text-shell-600">
                        {t(`returnDisposition.${r.disposition}`, r.disposition)}
                      </td>
                      <td>
                        <StatusBadge
                          status={r.is_posted ? 'POSTED' : 'DRAFT'}
                          label={r.is_posted ? t('returns.posted') : t('returns.pending')}
                        />
                      </td>
                      <td className="tabular text-right font-medium">{formatMoney(r.total_amount)}</td>
                      <td className="whitespace-nowrap text-right">
                        {canPost && !r.is_posted && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            title={t('returns.post')}
                            disabled={post.isPending}
                            onClick={() => post.mutate(r.id)}
                          >
                            <Check className="h-3.5 w-3.5 text-ok-600" />
                          </button>
                        )}
                        <button type="button" className="btn-ghost btn-sm" onClick={() => setDetailId(r.id)}>
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

      <CreateModal
        open={creating}
        onClose={() => setCreating(false)}
        onSaved={() => { setCreating(false); push('success', t('returns.created')); invalidate() }}
        onError={(e) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))}
      />

      {/* Detail */}
      <Modal
        open={detailId !== null}
        onClose={() => setDetailId(null)}
        size="lg"
        title={detail.data ? `${t('returns.detail')} — ${detail.data.return_no}` : t('returns.detail')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setDetailId(null)}>
              {t('common.close')}
            </button>
            {canPost && detail.data && !detail.data.is_posted && (
              <button
                type="button"
                className="btn-primary"
                disabled={post.isPending}
                onClick={() => post.mutate(detail.data!.id)}
              >
                {post.isPending ? <Spinner /> : <Check className="h-4 w-4" />}
                {t('returns.post')}
              </button>
            )}
          </>
        }
      >
        {detail.isLoading ? (
          <SkeletonRows rows={5} cols={5} />
        ) : detail.isError ? (
          <ErrorState error={detail.error} />
        ) : !detail.data ? (
          <EmptyState />
        ) : (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <Info label={t('common.customer')} value={detail.data.customer_name ?? `#${detail.data.customer_id}`} />
              <Info label={t('returns.reason')} value={t(`returnReason.${detail.data.reason}`, detail.data.reason)} />
              <Info
                label={t('returns.disposition')}
                value={t(`returnDisposition.${detail.data.disposition}`, detail.data.disposition)}
              />
              <Info label={t('returns.returnDate')} value={formatDate(detail.data.return_date)} />
              <Info
                label={t('returns.sourceSale')}
                value={detail.data.sale_id ? `#${detail.data.sale_id}` : t('returns.noSourceSale')}
              />
              <Info
                label={t('returns.creditNote')}
                value={detail.data.credit_note?.invoice_no ?? '—'}
              />
            </dl>

            {detail.data.items.length === 0 ? (
              <EmptyState title={t('returns.noLines')} />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-8">#</th>
                      <th>{t('common.product')}</th>
                      <th className="text-right">{t('common.quantity')}</th>
                      <th>{t('returns.reason')}</th>
                      <th>{t('returns.disposition')}</th>
                      <th className="text-right">{t('common.total')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.items.map((it) => (
                      <tr key={it.id}>
                        <td className="tabular text-shell-400">{it.line_no}</td>
                        <td className="max-w-[16rem] truncate">
                          {productNames.get(it.product_id) ?? `#${it.product_id}`}
                        </td>
                        <td className="tabular text-right">{formatQuantity(it.quantity, it.uom)}</td>
                        <td className="text-xs">{t(`returnReason.${it.reason}`, it.reason)}</td>
                        <td className="text-xs">{t(`returnDisposition.${it.disposition}`, it.disposition)}</td>
                        <td className="tabular text-right font-medium">{formatMoney(it.total_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <dl className="ml-auto max-w-xs space-y-1 text-sm">
              <div className="flex justify-between text-shell-500">
                <dt>{t('common.net')}</dt>
                <dd className="tabular">{formatMoney(detail.data.net_amount)}</dd>
              </div>
              <div className="flex justify-between text-shell-500">
                <dt>{t('common.vat')}</dt>
                <dd className="tabular">{formatMoney(detail.data.vat_amount)}</dd>
              </div>
              <div className="flex justify-between border-t border-shell-200 pt-1.5 font-semibold">
                <dt>{t('common.total')}</dt>
                <dd className="tabular">{formatMoney(detail.data.total_amount)}</dd>
              </div>
            </dl>
          </div>
        )}
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
function CreateModal({
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
  const [saleId, setSaleId] = useState('')
  const [reason, setReason] = useState<string>('DAMAGED')
  const [disposition, setDisposition] = useState<string>('RESALEABLE')
  const [creditNote, setCreditNote] = useState(true)
  const [postNow, setPostNow] = useState(false)
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<DraftLine[]>([])
  const [term, setTerm] = useState('')

  const sales = useQuery({
    queryKey: ['returns-sales', customer?.id],
    queryFn: () => api.get<Paged<SaleRef>>('/sales', { customer_id: customer!.id, size: 20 }),
    enabled: open && !!customer,
    retry: false,
    throwOnError: false,
  })

  const search = useQuery({
    queryKey: ['returns-products', term],
    queryFn: () =>
      api.get<Paged<ProductRef>>('/products', { q: term, only_sellable: true, size: 10 }),
    enabled: open && term.trim().length >= 2,
    retry: false,
    throwOnError: false,
  })

  const addLine = (p: ProductRef) => {
    setLines((prev) =>
      prev.some((l) => l.product_id === p.id)
        ? prev
        : [
            ...prev,
            {
              product_id: p.id,
              name: p.name,
              sku: p.sku,
              quantity: 1,
              uom: p.sales_uom || 'CASE',
              reason,
              disposition,
            },
          ],
    )
    setTerm('')
  }

  const patch = (idx: number, values: Partial<DraftLine>) =>
    setLines((prev) => prev.map((l, i) => (i === idx ? { ...l, ...values } : l)))

  const save = useMutation({
    mutationFn: () =>
      api.post('/sales/returns', {
        customer_id: customer!.id,
        sale_id: saleId ? Number(saleId) : null,
        reason,
        disposition,
        creates_credit_note: creditNote,
        post_now: postNow,
        notes: notes || null,
        lines: lines.map((l) => ({
          product_id: l.product_id,
          quantity: l.quantity,
          uom: l.uom,
          reason: l.reason,
          disposition: l.disposition,
        })),
      }),
    onSuccess: () => {
      setCustomer(null); setSaleId(''); setLines([]); setNotes('')
      setReason('DAMAGED'); setDisposition('RESALEABLE'); setCreditNote(true); setPostNow(false)
      onSaved()
    },
    onError,
  })

  const valid = !!customer && lines.length > 0 && lines.every((l) => toNumber(l.quantity) > 0)

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title={t('returns.create')}
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
            <CustomerPicker value={customer} onChange={(c) => { setCustomer(c); setSaleId('') }} />
          </Field>
          <Field label={t('returns.sourceSale')}>
            <select
              className="input"
              value={saleId}
              disabled={!customer}
              onChange={(e) => setSaleId(e.target.value)}
            >
              <option value="">{t('returns.noSourceSale')}</option>
              {(sales.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.sale_no} · {formatDate(s.sale_date, { short: true })} · {formatMoney(s.total_amount)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('returns.reason')} required>
            <select className="input" value={reason} onChange={(e) => setReason(e.target.value)}>
              {REASONS.map((r) => (
                <option key={r} value={r}>{t(`returnReason.${r}`)}</option>
              ))}
            </select>
          </Field>
          <Field label={t('returns.disposition')} required>
            <select
              className="input"
              value={disposition}
              onChange={(e) => setDisposition(e.target.value)}
            >
              {DISPOSITIONS.map((d) => (
                <option key={d} value={d}>{t(`returnDisposition.${d}`)}</option>
              ))}
            </select>
          </Field>
        </div>

        {/* Lines */}
        <div>
          <div className="relative mb-2">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('returns.searchProduct')}
              value={term}
              onChange={(e) => setTerm(e.target.value)}
            />
            {(search.data?.items ?? []).length > 0 && (
              <ul className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-shell-200 bg-white py-1 shadow-pop">
                {(search.data?.items ?? []).map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      className="block w-full px-3 py-1.5 text-left text-sm hover:bg-shell-50"
                      onClick={() => addLine(p)}
                    >
                      <span className="block truncate">{p.name}</span>
                      <span className="block text-2xs text-shell-400">{p.sku}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {lines.length === 0 ? (
            <p className="text-sm text-shell-400">{t('returns.noLines')}</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('common.product')}</th>
                    <th className="w-24 text-right">{t('common.quantity')}</th>
                    <th className="w-28">{t('common.uom')}</th>
                    <th className="w-40">{t('returns.lineReason')}</th>
                    <th className="w-40">{t('returns.lineDisposition')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l, i) => (
                    <tr key={l.product_id}>
                      <td>
                        <span className="block max-w-[14rem] truncate">{l.name}</span>
                        <span className="block text-2xs text-shell-400">{l.sku}</span>
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0.01}
                          step="0.01"
                          aria-label={t('common.quantity')}
                          className="input tabular w-20 py-1 text-right"
                          value={l.quantity}
                          onChange={(e) => patch(i, { quantity: Number(e.target.value) || 0 })}
                        />
                      </td>
                      <td>
                        <select
                          aria-label={t('common.uom')}
                          className="input w-24 py-1 text-xs"
                          value={l.uom}
                          onChange={(e) => patch(i, { uom: e.target.value })}
                        >
                          {UOMS.map((u) => (
                            <option key={u} value={u}>{u}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <select
                          aria-label={t('returns.lineReason')}
                          className="input w-36 py-1 text-xs"
                          value={l.reason}
                          onChange={(e) => patch(i, { reason: e.target.value })}
                        >
                          {REASONS.map((r) => (
                            <option key={r} value={r}>{t(`returnReason.${r}`)}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <select
                          aria-label={t('returns.lineDisposition')}
                          className="input w-36 py-1 text-xs"
                          value={l.disposition}
                          onChange={(e) => patch(i, { disposition: e.target.value })}
                        >
                          {DISPOSITIONS.map((d) => (
                            <option key={d} value={d}>{t(`returnDisposition.${d}`)}</option>
                          ))}
                        </select>
                      </td>
                      <td className="text-right">
                        <button
                          type="button"
                          aria-label={t('common.delete')}
                          className="text-shell-400 hover:text-danger-600"
                          onClick={() => setLines((prev) => prev.filter((_, x) => x !== i))}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm text-shell-700">
            <input
              type="checkbox"
              checked={creditNote}
              onChange={(e) => setCreditNote(e.target.checked)}
            />
            {t('returns.createsCreditNote')}
          </label>
          <label className="flex items-center gap-2 text-sm text-shell-700">
            <input type="checkbox" checked={postNow} onChange={(e) => setPostNow(e.target.checked)} />
            {t('returns.postNow')}
          </label>
        </div>

        <Field label={t('common.notes')}>
          <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Pieces                                                                     */
/* -------------------------------------------------------------------------- */
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
        className="btn-secondary w-full justify-between"
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
        className="input"
        placeholder={t('common.customer')}
        value={term}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onChange={(e) => setTerm(e.target.value)}
      />
      {open && (results.data?.items ?? []).length > 0 && (
        <ul className="absolute z-20 mt-1 max-h-60 w-full overflow-y-auto rounded-lg border border-shell-200 bg-white py-1 shadow-pop">
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
