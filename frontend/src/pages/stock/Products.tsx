/**
 * Ürünler / Products — the catalogue grid.
 *
 * Left column carries the category and brand trees (server-side filters, not
 * client-side folding); the main column is the paged grid.  The editor covers
 * every field the backend's ProductCreate/ProductUpdate accepts, because a
 * half-filled product breaks van loading (volume/weight) and FEFO (shelf life).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Barcode,
  ChevronDown,
  ChevronRight,
  Download,
  Package,
  Pencil,
  Plus,
  Search,
  Trash2,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatMoney, formatNumber, formatPercent } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  PageHeader,
  Pagination,
  SectionTitle,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface Ref {
  id: number
  code: string
  name: string
  name_en?: string | null
}

interface TreeNode extends Ref {
  parent_id?: number | null
  product_count: number
  is_active: boolean
  children: TreeNode[]
}

interface ProductRow {
  id: number
  sku: string
  code: string
  name: string
  name_en?: string | null
  status: string
  is_active: boolean
  is_sellable: boolean
  category?: Ref | null
  brand?: Ref | null
  base_uom: string
  sales_uom: string
  units_per_case: number | string
  sale_price: number | string
  cost_price: number | string
  currency: string
  vat_rate: number
  min_stock_level: number | string
}

interface ProductDetail extends ProductRow {
  description?: string | null
  short_name?: string | null
  category_id?: number | null
  brand_id?: number | null
  purchase_price: number | string
  recommended_retail_price: number | string
  excise_rate: number
  excise_amount: number | string
  max_discount_percent: number
  unit_volume_l?: number | null
  unit_weight_kg?: number | null
  case_volume_l?: number | null
  case_weight_kg?: number | null
  storage_condition: string
  is_lot_tracked: boolean
  is_serial_tracked: boolean
  shelf_life_days?: number | null
  min_remaining_shelf_life_days?: number | null
  max_stock_level?: number | string | null
  reorder_point?: number | string | null
  is_returnable: boolean
  tags: string[]
}

interface BarcodeHit {
  id: number
  sku: string
  name: string
  barcode?: string | null
  sale_price: number | string
  base_uom: string
  sales_uom: string
  units_per_case: number | string
}

type FormState = Record<string, string | boolean>

const UOMS = ['PIECE', 'CASE', 'PACK', 'PALLET', 'KILOGRAM', 'GRAM', 'LITRE', 'MILLILITRE']
const STATUSES = ['ACTIVE', 'PASSIVE', 'DISCONTINUED']
const STORAGE = ['AMBIENT', 'CHILLED', 'FROZEN']

const SIZE = 25

interface FieldDef {
  k: string
  label: string
  type: 'text' | 'number' | 'select' | 'check' | 'textarea'
  options?: string[]
  required?: boolean
  step?: string
  hint?: string
}

function flatten(nodes: TreeNode[], depth = 0): { node: TreeNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flatten(n.children ?? [], depth + 1)])
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Products() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()
  const lang = currentLanguage()
  const params = useParams<{ id?: string }>()

  const [page, setPage] = useState(1)
  const [term, setTerm] = useState('')
  const [categoryId, setCategoryId] = useState<number | null>(null)
  const [brandId, setBrandId] = useState<number | null>(null)
  const [status, setStatus] = useState('')
  const [onlySellable, setOnlySellable] = useState(false)
  const [orderBy, setOrderBy] = useState('name')
  const [editing, setEditing] = useState<ProductDetail | null | undefined>(undefined)
  const [barcode, setBarcode] = useState('')
  const [hit, setHit] = useState<BarcodeHit | null>(null)

  const label = (row: { name: string; name_en?: string | null }) =>
    lang === 'en' && row.name_en ? row.name_en : row.name

  const listParams = {
    page,
    size: SIZE,
    q: term || undefined,
    category_id: categoryId ?? undefined,
    brand_id: brandId ?? undefined,
    status: status || undefined,
    only_sellable: onlySellable || undefined,
    order_by: orderBy,
  }

  const list = useQuery({
    queryKey: ['products', listParams],
    queryFn: () => api.get<Paged<ProductRow>>('/products', listParams),
  })

  const categories = useQuery({
    queryKey: ['product-categories'],
    queryFn: () => api.get<TreeNode[]>('/products/categories'),
  })
  const brands = useQuery({
    queryKey: ['product-brands'],
    queryFn: () => api.get<TreeNode[]>('/products/brands'),
  })

  /* Deep link: /stock/products/:id opens the editor straight away. */
  const deepId = params.id ? Number(params.id) : null
  useEffect(() => {
    if (!deepId || Number.isNaN(deepId)) return
    void api
      .get<ProductDetail>(`/products/${deepId}`)
      .then((p) => setEditing(p))
      .catch(() => undefined)
  }, [deepId])

  const openEdit = async (id: number) => {
    try {
      setEditing(await api.get<ProductDetail>(`/products/${id}`))
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('errors.generic'))
    }
  }

  const remove = useMutation({
    mutationFn: (id: number) => api.delete<{ message: string }>(`/products/${id}`),
    onSuccess: () => {
      push('success', t('stockCommon.removed'))
      void qc.invalidateQueries({ queryKey: ['products'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const lookup = async () => {
    const code = barcode.trim()
    if (!code) return
    try {
      setHit(await api.get<BarcodeHit>(`/products/barcode/${encodeURIComponent(code)}`))
    } catch {
      setHit(null)
      push('warning', t('products.barcodeNotFound'))
    }
  }

  const exportCsv = async () => {
    try {
      const { blob, filename } = await api.download('/products/export', undefined, {
        q: term || undefined,
        category_id: categoryId ?? undefined,
        brand_id: brandId ?? undefined,
        status: status || undefined,
        only_sellable: onlySellable || undefined,
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || 'products.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('errors.generic'))
    }
  }

  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('products.title')}
        subtitle={t('products.subtitle')}
        icon={<Package className="h-5 w-5" />}
        actions={
          <>
            {can('stock.products', 'EXPORT') && (
              <button type="button" className="btn-secondary btn-sm" onClick={exportCsv}>
                <Download className="h-4 w-4" />
                {t('common.export')}
              </button>
            )}
            {can('stock.products', 'CREATE') && (
              <button type="button" className="btn-primary btn-sm" onClick={() => setEditing(null)}>
                <Plus className="h-4 w-4" />
                {t('products.new')}
              </button>
            )}
          </>
        }
      />

      <div className="grid gap-5 lg:grid-cols-[16rem_minmax(0,1fr)]">
        {/* ------------------------------ Sidebar ------------------------------ */}
        <div className="space-y-4">
          <Card title={t('products.barcodeLookup')} bodyClassName="p-3">
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Barcode className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                <input
                  className="input pl-9"
                  placeholder={t('products.barcodePlaceholder')}
                  value={barcode}
                  onChange={(e) => setBarcode(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void lookup()}
                />
              </div>
              <button type="button" className="btn-secondary btn-sm" onClick={() => void lookup()}>
                <Search className="h-4 w-4" />
              </button>
            </div>
            {hit && (
              <button
                type="button"
                className="mt-3 w-full rounded-lg border border-shell-200 p-2 text-left hover:bg-shell-50"
                onClick={() => void openEdit(hit.id)}
              >
                <p className="truncate text-sm font-medium text-shell-800">{hit.name}</p>
                <p className="text-2xs text-shell-500">{hit.sku}</p>
                <p className="tabular mt-1 text-xs font-medium">{formatMoney(hit.sale_price)}</p>
              </button>
            )}
          </Card>

          <Card title={t('products.categories')} bodyClassName="p-2">
            <TreeFilter
              nodes={categories.data ?? []}
              loading={categories.isLoading}
              selected={categoryId}
              allLabel={t('products.allCategories')}
              onSelect={(id) => {
                setCategoryId(id)
                setPage(1)
              }}
              label={label}
            />
          </Card>

          <Card title={t('products.brands')} bodyClassName="p-2">
            <TreeFilter
              nodes={brands.data ?? []}
              loading={brands.isLoading}
              selected={brandId}
              allLabel={t('products.allBrands')}
              onSelect={(id) => {
                setBrandId(id)
                setPage(1)
              }}
              label={label}
            />
          </Card>
        </div>

        {/* ------------------------------- Grid -------------------------------- */}
        <Card bodyClassName="p-0">
          <div className="flex flex-wrap items-center gap-2 border-b border-shell-200 p-3">
            <div className="relative min-w-[12rem] flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
              <input
                className="input pl-9"
                placeholder={t('products.searchPlaceholder')}
                value={term}
                onChange={(e) => {
                  setTerm(e.target.value)
                  setPage(1)
                }}
              />
            </div>
            <select
              className="input w-auto"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select
              className="input w-auto"
              value={orderBy}
              onChange={(e) => setOrderBy(e.target.value)}
              aria-label={t('products.orderBy')}
            >
              <option value="name">{t('products.orderName')}</option>
              <option value="sku">{t('products.orderSku')}</option>
              <option value="price">{t('products.orderPrice')}</option>
              <option value="created">{t('products.orderCreated')}</option>
            </select>
            <label className="flex items-center gap-1.5 text-xs text-shell-600">
              <input
                type="checkbox"
                checked={onlySellable}
                onChange={(e) => {
                  setOnlySellable(e.target.checked)
                  setPage(1)
                }}
              />
              {t('products.onlySellable')}
            </label>
          </div>

          {list.isLoading ? (
            <SkeletonRows rows={8} cols={6} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('stockCommon.sku')}</th>
                    <th>{t('common.name')}</th>
                    <th>{t('products.categories')}</th>
                    <th>{t('products.brands')}</th>
                    <th>{t('stockCommon.uom')}</th>
                    <th className="text-right">{t('products.salePrice')}</th>
                    <th className="text-right">{t('products.vatRate')}</th>
                    <th>{t('common.status')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr key={p.id}>
                      <td className="tabular whitespace-nowrap font-medium">{p.sku}</td>
                      <td className="min-w-[12rem]">{label(p)}</td>
                      <td className="text-xs text-shell-500">{p.category ? label(p.category) : '—'}</td>
                      <td className="text-xs text-shell-500">{p.brand ? label(p.brand) : '—'}</td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {p.sales_uom} / {formatNumber(p.units_per_case)} {p.base_uom}
                      </td>
                      <td className="tabular text-right">{formatMoney(p.sale_price, { currency: p.currency })}</td>
                      <td className="tabular text-right">{formatPercent(p.vat_rate, { decimals: 0 })}</td>
                      <td>
                        <StatusBadge status={p.is_active ? p.status : 'PASSIVE'} />
                      </td>
                      <td className="whitespace-nowrap text-right">
                        {can('stock.products', 'UPDATE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() => void openEdit(p.id)}
                            aria-label={t('common.edit')}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                        )}
                        {can('stock.products', 'DELETE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => {
                              if (window.confirm(t('stockCommon.confirmDelete'))) remove.mutate(p.id)
                            }}
                            aria-label={t('common.delete')}
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

          {list.data && (
            <Pagination
              page={list.data.page}
              pages={list.data.pages}
              total={list.data.total}
              size={list.data.size}
              onPage={setPage}
            />
          )}
        </Card>
      </div>

      {editing !== undefined && (
        <ProductEditor
          product={editing}
          categories={categories.data ?? []}
          brands={brands.data ?? []}
          label={label}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined)
            void qc.invalidateQueries({ queryKey: ['products'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Tree filter                                                                */
/* -------------------------------------------------------------------------- */
function TreeFilter({
  nodes,
  loading,
  selected,
  allLabel,
  onSelect,
  label,
}: {
  nodes: TreeNode[]
  loading: boolean
  selected: number | null
  allLabel: string
  onSelect: (id: number | null) => void
  label: (row: { name: string; name_en?: string | null }) => string
}) {
  const [open, setOpen] = useState<Record<number, boolean>>({})
  if (loading) return <SkeletonRows rows={4} cols={1} />

  const render = (list: TreeNode[], depth: number) =>
    list.map((n) => {
      const kids = n.children ?? []
      const isOpen = open[n.id] ?? depth === 0
      return (
        <li key={n.id}>
          <div className="flex items-center">
            {kids.length > 0 ? (
              <button
                type="button"
                className="p-1 text-shell-400 hover:text-shell-700"
                onClick={() => setOpen((o) => ({ ...o, [n.id]: !isOpen }))}
              >
                {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              </button>
            ) : (
              <span className="w-5" />
            )}
            <button
              type="button"
              className={`flex flex-1 items-center justify-between gap-2 rounded px-2 py-1 text-left text-xs ${
                selected === n.id ? 'bg-brand-50 font-medium text-brand-700' : 'hover:bg-shell-50'
              }`}
              onClick={() => onSelect(selected === n.id ? null : n.id)}
            >
              <span className="truncate">{label(n)}</span>
              <span className="tabular text-2xs text-shell-400">{n.product_count}</span>
            </button>
          </div>
          {kids.length > 0 && isOpen && <ul className="ml-3">{render(kids, depth + 1)}</ul>}
        </li>
      )
    })

  return (
    <ul className="max-h-64 overflow-y-auto">
      <li>
        <button
          type="button"
          className={`w-full rounded px-2 py-1 text-left text-xs ${
            selected === null ? 'bg-brand-50 font-medium text-brand-700' : 'hover:bg-shell-50'
          }`}
          onClick={() => onSelect(null)}
        >
          {allLabel}
        </button>
      </li>
      {render(nodes, 0)}
    </ul>
  )
}

/* -------------------------------------------------------------------------- */
/* Editor                                                                     */
/* -------------------------------------------------------------------------- */
function ProductEditor({
  product,
  categories,
  brands,
  label,
  onClose,
  onSaved,
}: {
  product: ProductDetail | null
  categories: TreeNode[]
  brands: TreeNode[]
  label: (row: { name: string; name_en?: string | null }) => string
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()

  const [form, setForm] = useState<FormState>(() => toForm(product))
  const set = (k: string, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }))
  const txt = (k: string) => String(form[k] ?? '')

  const groups: { title: string; fields: FieldDef[] }[] = [
    {
      title: t('products.identity'),
      fields: [
        { k: 'sku', label: t('stockCommon.sku'), type: 'text', required: true },
        { k: 'code', label: t('products.code'), type: 'text' },
        { k: 'name', label: t('common.name'), type: 'text', required: true },
        { k: 'name_en', label: t('products.nameEn'), type: 'text' },
        { k: 'short_name', label: t('products.shortName'), type: 'text' },
        { k: 'tags', label: t('products.tags'), type: 'text', hint: t('products.tagsHint') },
        { k: 'description', label: t('common.description'), type: 'textarea' },
      ],
    },
    {
      title: t('products.classification'),
      fields: [
        { k: 'status', label: t('common.status'), type: 'select', options: STATUSES },
        { k: 'is_active', label: t('common.active'), type: 'check' },
        { k: 'is_sellable', label: t('products.sellable'), type: 'check' },
        { k: 'is_returnable', label: t('products.returnable'), type: 'check' },
      ],
    },
    {
      title: t('products.packaging'),
      fields: [
        { k: 'base_uom', label: t('products.baseUom'), type: 'select', options: UOMS },
        { k: 'sales_uom', label: t('products.salesUom'), type: 'select', options: UOMS },
        { k: 'units_per_case', label: t('products.unitsPerCase'), type: 'number', step: '0.001' },
        { k: 'unit_volume_l', label: t('products.unitVolume'), type: 'number', step: '0.0001' },
        { k: 'unit_weight_kg', label: t('products.unitWeight'), type: 'number', step: '0.0001' },
        { k: 'case_volume_l', label: t('products.caseVolume'), type: 'number', step: '0.0001' },
        { k: 'case_weight_kg', label: t('products.caseWeight'), type: 'number', step: '0.0001' },
        { k: 'storage_condition', label: t('products.storage'), type: 'select', options: STORAGE },
      ],
    },
    {
      title: t('products.shelfLifeSection'),
      fields: [
        { k: 'is_lot_tracked', label: t('products.lotTracked'), type: 'check' },
        { k: 'is_serial_tracked', label: t('products.serialTracked'), type: 'check' },
        { k: 'shelf_life_days', label: t('products.shelfLife'), type: 'number' },
        { k: 'min_remaining_shelf_life_days', label: t('products.minShelfLife'), type: 'number' },
      ],
    },
    {
      title: t('products.pricing'),
      fields: [
        { k: 'purchase_price', label: t('products.purchasePrice'), type: 'number', step: '0.01' },
        { k: 'cost_price', label: t('products.costPrice'), type: 'number', step: '0.01' },
        { k: 'sale_price', label: t('products.salePrice'), type: 'number', step: '0.01' },
        { k: 'recommended_retail_price', label: t('products.rrp'), type: 'number', step: '0.01' },
        { k: 'currency', label: t('products.currency'), type: 'text' },
        { k: 'vat_rate', label: t('products.vatRate'), type: 'number', step: '0.1' },
        { k: 'excise_rate', label: t('products.exciseRate'), type: 'number', step: '0.1' },
        { k: 'excise_amount', label: t('products.exciseAmount'), type: 'number', step: '0.01' },
        { k: 'max_discount_percent', label: t('products.maxDiscount'), type: 'number', step: '0.1' },
      ],
    },
    {
      title: t('products.stockLevels'),
      fields: [
        { k: 'min_stock_level', label: t('products.minStock'), type: 'number', step: '0.001' },
        { k: 'max_stock_level', label: t('products.maxStock'), type: 'number', step: '0.001' },
        { k: 'reorder_point', label: t('products.reorderPoint'), type: 'number', step: '0.001' },
      ],
    },
  ]

  const save = useMutation({
    mutationFn: () => {
      const body = toPayload(form)
      return product
        ? api.put<ProductDetail>(`/products/${product.id}`, body)
        : api.post<ProductDetail>('/products', body)
    },
    onSuccess: () => {
      push('success', t('stockCommon.saved'))
      onSaved()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const catOptions = flatten(categories)
  const brandOptions = flatten(brands)

  return (
    <Modal
      open
      onClose={onClose}
      size="xl"
      title={product ? t('products.edit') : t('products.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.isPending || !txt('sku') || !txt('name')}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-6">
        <div>
          <SectionTitle>{t('products.classification')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('products.categories')}>
              <select
                className="input"
                value={txt('category_id')}
                onChange={(e) => set('category_id', e.target.value)}
              >
                <option value="">{t('common.select')}</option>
                {catOptions.map(({ node, depth }) => (
                  <option key={node.id} value={node.id}>
                    {' '.repeat(depth * 3)}
                    {label(node)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('products.brands')}>
              <select className="input" value={txt('brand_id')} onChange={(e) => set('brand_id', e.target.value)}>
                <option value="">{t('common.select')}</option>
                {brandOptions.map(({ node, depth }) => (
                  <option key={node.id} value={node.id}>
                    {' '.repeat(depth * 3)}
                    {label(node)}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>

        {groups.map((g) => (
          <div key={g.title}>
            <SectionTitle>{g.title}</SectionTitle>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {g.fields.map((f) => (
                <div key={f.k} className={f.type === 'textarea' ? 'sm:col-span-2 lg:col-span-3' : undefined}>
                  {f.type === 'check' ? (
                    <label className="flex h-full items-center gap-2 pt-5 text-sm text-shell-700">
                      <input
                        type="checkbox"
                        checked={Boolean(form[f.k])}
                        onChange={(e) => set(f.k, e.target.checked)}
                      />
                      {f.label}
                    </label>
                  ) : (
                    <Field label={f.label} required={f.required} hint={f.hint}>
                      {f.type === 'select' ? (
                        <select className="input" value={txt(f.k)} onChange={(e) => set(f.k, e.target.value)}>
                          {(f.options ?? []).map((o) => (
                            <option key={o} value={o}>
                              {o}
                            </option>
                          ))}
                        </select>
                      ) : f.type === 'textarea' ? (
                        <textarea
                          className="input"
                          rows={2}
                          value={txt(f.k)}
                          onChange={(e) => set(f.k, e.target.value)}
                        />
                      ) : (
                        <input
                          className={f.type === 'number' ? 'input tabular text-right' : 'input'}
                          type={f.type === 'number' ? 'number' : 'text'}
                          step={f.step}
                          value={txt(f.k)}
                          onChange={(e) => set(f.k, e.target.value)}
                        />
                      )}
                    </Field>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Form <-> payload                                                           */
/* -------------------------------------------------------------------------- */
const STR_KEYS = ['sku', 'code', 'name', 'name_en', 'short_name', 'description', 'status', 'base_uom', 'sales_uom', 'storage_condition', 'currency']
const NUM_KEYS = [
  'units_per_case', 'unit_volume_l', 'unit_weight_kg', 'case_volume_l', 'case_weight_kg',
  'shelf_life_days', 'min_remaining_shelf_life_days', 'purchase_price', 'cost_price', 'sale_price',
  'recommended_retail_price', 'vat_rate', 'excise_rate', 'excise_amount', 'max_discount_percent',
  'min_stock_level', 'max_stock_level', 'reorder_point',
]
const BOOL_KEYS = ['is_active', 'is_sellable', 'is_returnable', 'is_lot_tracked', 'is_serial_tracked']

function toForm(p: ProductDetail | null): FormState {
  if (!p) {
    return {
      sku: '', code: '', name: '', name_en: '', short_name: '', description: '', tags: '',
      status: 'ACTIVE', base_uom: 'PIECE', sales_uom: 'CASE', storage_condition: 'AMBIENT',
      currency: 'TRY', units_per_case: '1', vat_rate: '20', excise_rate: '0', excise_amount: '0',
      max_discount_percent: '100', purchase_price: '0', cost_price: '0', sale_price: '0',
      recommended_retail_price: '0', min_stock_level: '0', category_id: '', brand_id: '',
      is_active: true, is_sellable: true, is_returnable: true, is_lot_tracked: true,
      is_serial_tracked: false,
    }
  }
  const out: FormState = {
    category_id: p.category_id ? String(p.category_id) : '',
    brand_id: p.brand_id ? String(p.brand_id) : '',
    tags: (p.tags ?? []).join(', '),
  }
  const raw = p as unknown as Record<string, unknown>
  for (const k of STR_KEYS) out[k] = raw[k] == null ? '' : String(raw[k])
  for (const k of NUM_KEYS) out[k] = raw[k] == null ? '' : String(raw[k])
  for (const k of BOOL_KEYS) out[k] = Boolean(raw[k])
  return out
}

function toPayload(form: FormState): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  for (const k of STR_KEYS) {
    const v = String(form[k] ?? '').trim()
    if (v) body[k] = v
  }
  for (const k of NUM_KEYS) {
    const v = String(form[k] ?? '').trim()
    if (v !== '') body[k] = Number(v)
  }
  for (const k of BOOL_KEYS) body[k] = Boolean(form[k])
  const cat = String(form.category_id ?? '').trim()
  const brand = String(form.brand_id ?? '').trim()
  body.category_id = cat ? Number(cat) : null
  body.brand_id = brand ? Number(brand) : null
  body.tags = String(form.tags ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return body
}
