/**
 * Fiyat Listeleri / Price lists.
 *
 * A list plus its item grid.  Items are edited inline because setting prices is
 * a bulk activity — opening a modal per SKU would make the screen unusable.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Save, Tags, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

interface PriceList {
  id: number
  code: string
  name: string
  currency: string
  valid_from?: string | null
  valid_to?: string | null
  channel?: string | null
  customer_type?: string | null
  is_default: boolean
  is_active: boolean
  priority: number
  item_count?: number
}

interface PriceListItem {
  id?: number
  product_id: number
  product_sku?: string | null
  product_name?: string | null
  uom: string
  price: number | string
  min_quantity: number | string
  discount_percent: number
}

interface ProductRow {
  id: number
  sku: string
  name: string
  sales_uom: string
  sale_price: number | string
}

export default function PriceLists() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [selected, setSelected] = useState<PriceList | null>(null)
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState<PriceListItem[]>([])
  const [productTerm, setProductTerm] = useState('')

  const [form, setForm] = useState({
    code: '',
    name: '',
    currency: 'TRY',
    channel: '',
    customer_type: '',
    valid_from: '',
    valid_to: '',
    priority: '100',
    is_default: false,
  })

  const lists = useQuery({
    queryKey: ['price-lists'],
    queryFn: () => api.get<PriceList[]>('/products/price-lists'),
  })

  const items = useQuery({
    queryKey: ['price-list-items', selected?.id],
    queryFn: () => api.get<PriceListItem[]>(`/products/price-lists/${selected!.id}/items`),
    enabled: !!selected,
  })

  useEffect(() => {
    if (items.data) setRows(items.data)
  }, [items.data])

  const products = useQuery({
    queryKey: ['pl-products', productTerm],
    queryFn: () => api.get<Paged<ProductRow>>('/products', { term: productTerm, size: 10 }),
    enabled: productTerm.trim().length >= 2,
  })

  const createList = useMutation({
    mutationFn: () =>
      api.post('/products/price-lists', {
        code: form.code.trim(),
        name: form.name.trim(),
        currency: form.currency,
        channel: form.channel || null,
        customer_type: form.customer_type || null,
        valid_from: form.valid_from || null,
        valid_to: form.valid_to || null,
        priority: Number(form.priority),
        is_default: form.is_default,
      }),
    onSuccess: () => {
      push('success', t('common.created'))
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['price-lists'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const saveItems = useMutation({
    mutationFn: () =>
      api.put(`/products/price-lists/${selected!.id}/items`, {
        items: rows.map((r) => ({
          product_id: r.product_id,
          uom: r.uom,
          price: Number(r.price),
          min_quantity: Number(r.min_quantity),
          discount_percent: Number(r.discount_percent),
        })),
      }),
    onSuccess: () => {
      push('success', t('common.saved'))
      void qc.invalidateQueries({ queryKey: ['price-list-items', selected?.id] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const patch = (i: number, p: Partial<PriceListItem>) =>
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...p } : r)))

  const addProduct = (p: ProductRow) => {
    if (rows.some((r) => r.product_id === p.id)) return
    setRows((prev) => [
      ...prev,
      {
        product_id: p.id,
        product_sku: p.sku,
        product_name: p.name,
        uom: p.sales_uom || 'CASE',
        price: Number(p.sale_price) || 0,
        min_quantity: 0,
        discount_percent: 0,
      },
    ])
    setProductTerm('')
  }

  return (
    <>
      <PageHeader
        title={t('nav.priceLists')}
        subtitle={t('nav.marketing')}
        icon={<Tags className="h-5 w-5" />}
        actions={
          can('marketing.price_lists', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setOpen(true)}>
              <Plus className="h-3.5 w-3.5" />
              {t('common.new')}
            </button>
          )
        }
      />

      <div className="grid gap-5 lg:grid-cols-[20rem_1fr]">
        <Card title={t('nav.priceLists')} bodyClassName="p-0">
          {lists.isLoading ? (
            <LoadingBlock />
          ) : lists.isError ? (
            <ErrorState error={lists.error} onRetry={() => void lists.refetch()} />
          ) : (lists.data ?? []).length === 0 ? (
            <EmptyState />
          ) : (
            <ul className="divide-y divide-shell-100">
              {(lists.data ?? []).map((pl) => (
                <li key={pl.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(pl)}
                    className={`w-full px-4 py-3 text-left hover:bg-shell-50 ${
                      selected?.id === pl.id ? 'bg-brand-50' : ''
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{pl.name}</span>
                      {pl.is_default && <span className="badge-info">default</span>}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-2xs text-shell-500">
                      <span className="font-mono">{pl.code}</span>
                      <span>{pl.currency}</span>
                      {pl.valid_to && <span>→ {formatDate(pl.valid_to, { short: true })}</span>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title={selected ? selected.name : t('common.select')}
          bodyClassName="p-0"
          actions={
            selected &&
            can('marketing.price_lists', 'UPDATE') && (
              <button
                type="button"
                className="btn-primary btn-sm"
                disabled={saveItems.isPending}
                onClick={() => saveItems.mutate()}
              >
                {saveItems.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
                {t('common.save')}
              </button>
            )
          }
        >
          {!selected ? (
            <EmptyState title={t('common.select')} />
          ) : (
            <>
              {can('marketing.price_lists', 'UPDATE') && (
                <div className="relative border-b border-shell-200 p-3">
                  <input
                    className="input"
                    placeholder={t('common.search')}
                    value={productTerm}
                    onChange={(e) => setProductTerm(e.target.value)}
                  />
                  {(products.data?.items ?? []).length > 0 && (
                    <ul className="absolute left-3 right-3 top-full z-20 max-h-64 overflow-y-auto rounded-lg border border-shell-200 bg-white shadow-pop">
                      {(products.data?.items ?? []).map((p) => (
                        <li key={p.id}>
                          <button
                            type="button"
                            className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-shell-50"
                            onClick={() => addProduct(p)}
                          >
                            <span className="truncate">{p.name}</span>
                            <span className="text-2xs text-shell-400">{p.sku}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {items.isLoading ? (
                <LoadingBlock />
              ) : rows.length === 0 ? (
                <EmptyState />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>SKU</th>
                        <th>{t('common.name')}</th>
                        <th>{t('common.quantity')}</th>
                        <th className="text-right">{t('common.price')}</th>
                        <th className="text-right">Min</th>
                        <th className="text-right">%</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r, i) => (
                        <tr key={`${r.product_id}-${r.uom}`}>
                          <td className="font-mono text-xs">{r.product_sku}</td>
                          <td className="max-w-xs truncate">{r.product_name}</td>
                          <td>
                            <select
                              className="input py-1 text-xs"
                              value={r.uom}
                              onChange={(e) => patch(i, { uom: e.target.value })}
                            >
                              {['CASE', 'PIECE', 'PACK', 'PALLET'].map((u) => (
                                <option key={u} value={u}>
                                  {u}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td className="text-right">
                            <input
                              type="number"
                              step="0.01"
                              className="input tabular w-28 py-1 text-right text-xs"
                              value={String(r.price)}
                              onChange={(e) => patch(i, { price: e.target.value })}
                            />
                          </td>
                          <td className="text-right">
                            <input
                              type="number"
                              className="input tabular w-20 py-1 text-right text-xs"
                              value={String(r.min_quantity)}
                              onChange={(e) => patch(i, { min_quantity: e.target.value })}
                            />
                          </td>
                          <td className="text-right">
                            <input
                              type="number"
                              step="0.1"
                              className="input tabular w-16 py-1 text-right text-xs"
                              value={String(r.discount_percent)}
                              onChange={(e) =>
                                patch(i, { discount_percent: Number(e.target.value) })
                              }
                            />
                          </td>
                          <td className="text-right">
                            <button
                              type="button"
                              className="btn-ghost btn-sm text-danger-600"
                              onClick={() => setRows((p) => p.filter((_, idx) => idx !== i))}
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
            </>
          )}
        </Card>
      </div>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={t('common.new')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={createList.isPending || !form.code || !form.name}
              onClick={() => createList.mutate()}
            >
              {createList.isPending && <Spinner />}
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('common.code')} required>
            <input
              className="input"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
            />
          </Field>
          <Field label={t('common.name')} required>
            <input
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </Field>
          <Field label="Currency">
            <input
              className="input"
              value={form.currency}
              onChange={(e) => setForm({ ...form, currency: e.target.value.toUpperCase() })}
            />
          </Field>
          <Field label="Priority" hint="0 = highest">
            <input
              type="number"
              className="input tabular"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
            />
          </Field>
          <Field label="Channel">
            <select
              className="input"
              value={form.channel}
              onChange={(e) => setForm({ ...form, channel: e.target.value })}
            >
              <option value="">{t('common.all')}</option>
              {['TRADITIONAL', 'MODERN', 'HORECA', 'WHOLESALE', 'INSTITUTIONAL'].map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('common.from')}>
            <input
              type="date"
              className="input"
              value={form.valid_from}
              onChange={(e) => setForm({ ...form, valid_from: e.target.value })}
            />
          </Field>
          <Field label={t('common.to')}>
            <input
              type="date"
              className="input"
              value={form.valid_to}
              onChange={(e) => setForm({ ...form, valid_to: e.target.value })}
            />
          </Field>
          <label className="flex items-center gap-2 self-end pb-2 text-sm">
            <input
              type="checkbox"
              checked={form.is_default}
              onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
            />
            Default
          </label>
        </div>
      </Modal>
    </>
  )
}
