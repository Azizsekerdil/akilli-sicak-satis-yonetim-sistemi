/**
 * Siparişler / Orders — the pre-sale worklist.
 *
 * Lists what has been promised but not yet handed over, and turns a confirmed
 * order into a delivered sale (stock movements + invoice) in one call.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ClipboardList, RefreshCw, Search, Truck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, formatQuantity } from '@/lib/format'
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

const ORDER_STATUSES = [
  'DRAFT', 'CONFIRMED', 'PARTIALLY_DELIVERED', 'DELIVERED', 'INVOICED', 'CANCELLED',
] as const
const ORDER_TYPES = ['HOT_SALE', 'PRE_SALE', 'TELESALES', 'SELF_SERVICE'] as const
const DELIVERABLE = ['DRAFT', 'CONFIRMED', 'PARTIALLY_DELIVERED']

interface OrderListItem {
  id: number
  order_no: string
  order_type: string
  status: string
  order_date: string
  delivery_date?: string | null
  customer_id: number
  customer_name?: string | null
  salesperson_id?: number | null
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  line_count: number
  payment_method: string
}

interface OrderItem {
  id: number
  product_id: number
  line_no: number
  quantity: number | string
  uom: string
  unit_price: number | string
  discount_amount: number | string
  campaign_discount_amount: number | string
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  is_free_goods: boolean
  notes?: string | null
}

interface OrderDetail extends OrderListItem {
  gross_amount: number | string
  header_discount_amount: number | string
  notes?: string | null
  cancel_reason?: string | null
  items: OrderItem[]
}

interface Salesperson {
  id: number
  code: string
  full_name: string
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
}

const SIZE = 25

export default function Orders() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [status, setStatus] = useState('')
  const [orderType, setOrderType] = useState('')
  const [salespersonId, setSalespersonId] = useState('')
  const [customer, setCustomer] = useState<CustomerOption | null>(null)
  const [openOnly, setOpenOnly] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)

  const params = {
    page,
    size: SIZE,
    search: search || undefined,
    start: start || undefined,
    end: end || undefined,
    status: status || undefined,
    order_type: orderType || undefined,
    salesperson_id: salespersonId || undefined,
    customer_id: customer?.id,
    open_only: openOnly || undefined,
  }

  const list = useQuery({
    queryKey: ['orders', params],
    queryFn: () => api.get<Paged<OrderListItem>>('/sales/orders', params),
  })

  const salespersons = useQuery({
    queryKey: ['orders-salespersons'],
    queryFn: () => api.get<Paged<Salesperson>>('/vehicles/salespersons', { size: 200 }),
    enabled: can('field.salespersons', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const detail = useQuery({
    queryKey: ['order', detailId],
    queryFn: () => api.get<OrderDetail>(`/sales/orders/${detailId}`),
    enabled: detailId !== null,
  })

  // Order lines carry only product ids; one cached catalogue page resolves the
  // names for the modal instead of one request per line.
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

  const resetFilters = () => {
    setSearch(''); setStart(''); setEnd(''); setStatus(''); setOrderType('')
    setSalespersonId(''); setCustomer(null); setOpenOnly(false); setPage(1)
  }

  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('orders.title')}
        icon={<ClipboardList className="h-5 w-5" />}
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
              placeholder={t('orders.searchPlaceholder')}
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
            <option value="">{t('orders.allStatuses')}</option>
            {ORDER_STATUSES.map((s) => (
              <option key={s} value={s}>{t(`status.${s}`)}</option>
            ))}
          </select>
          <select
            aria-label={t('common.type')}
            className="input w-auto"
            value={orderType}
            onChange={(e) => { setOrderType(e.target.value); setPage(1) }}
          >
            <option value="">{t('orders.allTypes')}</option>
            {ORDER_TYPES.map((s) => (
              <option key={s} value={s}>{t(`orderType.${s}`)}</option>
            ))}
          </select>
          {(salespersons.data?.items ?? []).length > 0 && (
            <select
              aria-label={t('common.salesperson')}
              className="input w-auto"
              value={salespersonId}
              onChange={(e) => { setSalespersonId(e.target.value); setPage(1) }}
            >
              <option value="">{t('common.salesperson')}</option>
              {(salespersons.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>{s.full_name}</option>
              ))}
            </select>
          )}
          <CustomerPicker value={customer} onChange={(c) => { setCustomer(c); setPage(1) }} />
          <label className="flex items-center gap-1.5 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={openOnly}
              onChange={(e) => { setOpenOnly(e.target.checked); setPage(1) }}
            />
            {t('orders.openOnly')}
          </label>
          <button type="button" className="btn-ghost btn-sm" onClick={resetFilters}>
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
                    <th>{t('orders.orderNo')}</th>
                    <th>{t('orders.orderDate')}</th>
                    <th>{t('common.customer')}</th>
                    <th>{t('common.type')}</th>
                    <th>{t('common.status')}</th>
                    <th className="text-right">{t('common.lineCount')}</th>
                    <th className="text-right">{t('common.total')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((o) => (
                    <tr key={o.id}>
                      <td className="font-medium text-shell-800">{o.order_no}</td>
                      <td className="tabular">{formatDate(o.order_date, { short: true })}</td>
                      <td className="max-w-[16rem] truncate">{o.customer_name ?? `#${o.customer_id}`}</td>
                      <td className="text-xs text-shell-500">{t(`orderType.${o.order_type}`, o.order_type)}</td>
                      <td><StatusBadge status={o.status} label={t(`status.${o.status}`, o.status)} /></td>
                      <td className="tabular text-right">{formatNumber(o.line_count)}</td>
                      <td className="tabular text-right font-medium">{formatMoney(o.total_amount)}</td>
                      <td className="text-right">
                        <button type="button" className="btn-ghost btn-sm" onClick={() => setDetailId(o.id)}>
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

      <OrderModal
        open={detailId !== null}
        onClose={() => setDetailId(null)}
        order={detail.data}
        isLoading={detail.isLoading}
        error={detail.isError ? detail.error : null}
        productNames={productNames}
        canDeliver={can('sales.sales', 'CREATE')}
        onDelivered={() => {
          push('success', t('orders.delivered'))
          setDetailId(null)
          void qc.invalidateQueries({ queryKey: ['orders'] })
        }}
        onError={(e) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))}
      />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Customer filter                                                            */
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

/* -------------------------------------------------------------------------- */
/* Detail + deliver                                                           */
/* -------------------------------------------------------------------------- */
function OrderModal({
  open,
  onClose,
  order,
  isLoading,
  error,
  productNames,
  canDeliver,
  onDelivered,
  onError,
}: {
  open: boolean
  onClose: () => void
  order?: OrderDetail
  isLoading: boolean
  error: unknown
  productNames: Map<number, string>
  canDeliver: boolean
  onDelivered: () => void
  onError: (e: unknown) => void
}) {
  const { t } = useTranslation()
  const [createInvoice, setCreateInvoice] = useState(true)

  const deliver = useMutation({
    mutationFn: () =>
      api.post(`/sales/orders/${order!.id}/deliver`, { create_invoice: createInvoice }),
    onSuccess: onDelivered,
    onError,
  })

  const deliverable = !!order && DELIVERABLE.includes(order.status)

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title={order ? `${t('orders.detail')} — ${order.order_no}` : t('orders.detail')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.close')}
          </button>
          {canDeliver && deliverable && (
            <button
              type="button"
              className="btn-primary"
              disabled={deliver.isPending}
              onClick={() => deliver.mutate()}
            >
              {deliver.isPending ? <Spinner /> : <Truck className="h-4 w-4" />}
              {t('orders.deliver')}
            </button>
          )}
        </>
      }
    >
      {isLoading ? (
        <SkeletonRows rows={6} cols={4} />
      ) : error ? (
        <ErrorState error={error} />
      ) : !order ? (
        <EmptyState />
      ) : (
        <div className="space-y-4">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
            <Info label={t('common.customer')} value={order.customer_name ?? `#${order.customer_id}`} />
            <Info label={t('common.status')} value={t(`status.${order.status}`, order.status)} />
            <Info label={t('common.type')} value={t(`orderType.${order.order_type}`, order.order_type)} />
            <Info label={t('orders.orderDate')} value={formatDate(order.order_date)} />
            <Info label={t('orders.deliveryDate')} value={formatDate(order.delivery_date)} />
            <Info label={t('common.method')} value={t(`payment.${order.payment_method}`, order.payment_method)} />
          </dl>

          {order.items.length === 0 ? (
            <EmptyState title={t('orders.noLines')} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th className="w-8">#</th>
                    <th>{t('common.product')}</th>
                    <th className="text-right">{t('common.quantity')}</th>
                    <th className="text-right">{t('common.unitPrice')}</th>
                    <th className="text-right">{t('common.discount')}</th>
                    <th className="text-right">{t('common.total')}</th>
                  </tr>
                </thead>
                <tbody>
                  {order.items.map((it) => (
                    <tr key={it.id}>
                      <td className="tabular text-shell-400">{it.line_no}</td>
                      <td>
                        <span className="block max-w-[18rem] truncate">
                          {productNames.get(it.product_id) ?? `#${it.product_id}`}
                        </span>
                        {it.is_free_goods && (
                          <span className="badge-ok mt-0.5">{t('hotSale.freeGoods')}</span>
                        )}
                      </td>
                      <td className="tabular text-right">{formatQuantity(it.quantity, it.uom)}</td>
                      <td className="tabular text-right">{formatMoney(it.unit_price)}</td>
                      <td className="tabular text-right">
                        {formatMoney(Number(it.discount_amount) + Number(it.campaign_discount_amount))}
                      </td>
                      <td className="tabular text-right font-medium">{formatMoney(it.total_amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <dl className="ml-auto max-w-xs space-y-1 text-sm">
            <Total label={t('common.net')} value={order.net_amount} />
            <Total label={t('common.vat')} value={order.vat_amount} />
            <div className="flex justify-between border-t border-shell-200 pt-1.5 font-semibold">
              <dt>{t('common.total')}</dt>
              <dd className="tabular">{formatMoney(order.total_amount)}</dd>
            </div>
          </dl>

          {canDeliver && deliverable && (
            <div className="rounded-lg border border-shell-200 bg-shell-50 p-3">
              <p className="mb-2 text-xs text-shell-600">{t('orders.deliverHint')}</p>
              <Field label={t('orders.createInvoice')}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={createInvoice}
                    onChange={(e) => setCreateInvoice(e.target.checked)}
                  />
                  {t('common.yes')}
                </label>
              </Field>
            </div>
          )}
        </div>
      )}
    </Modal>
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
