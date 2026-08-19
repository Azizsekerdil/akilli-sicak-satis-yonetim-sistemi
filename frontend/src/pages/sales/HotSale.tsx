/**
 * Sıcak Satış / Hot Sale — the flagship field screen.
 *
 * Pick customer → see van stock → build a basket priced live by the campaign
 * engine → take payment → post.  The whole document chain (order, delivery,
 * invoice, collection) is created by one backend transaction, and the basket
 * carries a client-generated UUID so a retry after a dropped connection can
 * never post the same sale twice.
 */
import clsx from 'clsx'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Download,
  Gift,
  Minus,
  Package,
  Plus,
  Search,
  Sparkles,
  Store,
  Trash2,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { daysUntil, formatDate, formatMoney, formatNumber, formatQuantity, toNumber } from '@/lib/format'
import {
  Card,
  EmptyState,
  ExpiryBadge,
  Field,
  LoadingBlock,
  PageHeader,
  SectionTitle,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types — mirror the backend schemas                                         */
/* -------------------------------------------------------------------------- */
interface Customer {
  id: number
  code: string
  name: string
  trade_name?: string | null
  status: string
  city?: string | null
  phone?: string | null
  balance: number | string
  overdue_balance: number | string
  credit_limit: number | string
  risk_score: number
  last_order_date?: string | null
}

interface VanStockRow {
  product_id: number
  sku?: string | null
  product_name?: string | null
  base_uom?: string | null
  units_per_case: number | string
  base_quantity: number | string
  available_quantity: number | string
}

interface ProductRow {
  id: number
  sku: string
  name: string
  base_uom: string
  sales_uom: string
  units_per_case: number | string
}

interface ExpiryRow {
  product_id: number
  lot_id: number
  expiry_date?: string | null
  days_to_expiry?: number | null
}

interface SalesHistoryItem {
  id: number
  sale_no: string
  sale_date: string
  total_amount: number | string
  payment_method: string
  line_count: number
}

interface QuoteLine {
  line_no: number
  product_id: number
  sku: string
  product_name: string
  quantity: number | string
  uom: string
  unit_price: number | string
  discount_amount: number | string
  campaign_discount_amount: number | string
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  is_free_goods: boolean
  campaign_id?: number | null
}

interface AppliedCampaign {
  campaign_id: number
  code: string
  name: string
  campaign_type: string
  times_applied: number
  discount_amount: number | string
  free_goods_quantity: number | string
  explanation?: string | null
}

interface PriceQuote {
  lines: QuoteLine[]
  gross_amount: number | string
  line_discount_amount: number | string
  campaign_discount_amount: number | string
  header_discount_amount: number | string
  net_amount: number | string
  vat_amount: number | string
  total_amount: number | string
  applied_campaigns: AppliedCampaign[]
}

interface SuggestionOut {
  payload: Record<string, unknown>
  explanation: string
  confidence: number
  degraded: boolean
  error_key?: string | null
}

interface SuggestionLine {
  product_id: number
  sku?: string
  name?: string
  product?: string
  uom?: string
  suggested_quantity?: number | string
  suggested_cases?: number
  reason?: string
  reason_tr?: string
  reason_en?: string
}

interface HotSaleResult {
  sale: { id: number; sale_no: string; total_amount: number | string }
  invoice?: { id: number; invoice_no: string } | null
  payment?: { id: number; payment_no: string; amount: number | string } | null
  stock_movements: number
}

interface StockItem {
  product_id: number
  sku: string
  name: string
  uom: string
  cases: number
  available: number
  expiryDays: number | null
}

interface BasketLine {
  product_id: number
  name: string
  sku: string
  quantity: number
  uom: string
  discount_percent: number
}

const PAYMENT_METHODS = ['CASH', 'CREDIT_CARD', 'BANK_TRANSFER', 'CHEQUE', 'OPEN_ACCOUNT'] as const
type PaymentMethod = (typeof PAYMENT_METHODS)[number]
const UOMS = ['CASE', 'PIECE', 'PACK', 'PALLET'] as const

const fold = (s: string) => s.toLocaleLowerCase('tr-TR')

/** Debounce so a held-down quantity button does not fire a quote per tick. */
function useDebounced<T>(value: T, ms = 350): T {
  const [out, setOut] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setOut(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return out
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function HotSale() {
  const { t } = useTranslation()
  const { session, can } = useAuth()
  const { push } = useToast()
  const lang = currentLanguage()

  const salespersonId = session?.salesperson_id ?? null

  const [step, setStep] = useState(0)
  const [customer, setCustomer] = useState<Customer | null>(null)
  const [term, setTerm] = useState('')
  const [productTerm, setProductTerm] = useState('')
  const [basket, setBasket] = useState<BasketLine[]>([])
  const [method, setMethod] = useState<PaymentMethod>('CASH')
  const [amount, setAmount] = useState('')
  const [instrument, setInstrument] = useState({
    bank_name: '',
    document_number: '',
    maturity_date: '',
    drawer_name: '',
  })
  const [notes, setNotes] = useState('')
  const [clientUid, setClientUid] = useState(() => crypto.randomUUID())
  const [done, setDone] = useState<HotSaleResult | null>(null)

  /* ---- which van am I selling from? -------------------------------------- */
  const daySession = useQuery({
    queryKey: ['hs-session', salespersonId],
    queryFn: () =>
      api.get<Paged<{ vehicle_id: number; warehouse_id: number | null }>>(
        '/vehicles/day-sessions',
        { salesperson_id: salespersonId, status: 'OPEN', size: 1 },
      ),
    enabled: !!salespersonId && can('field.day_session', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const vehicles = useQuery({
    queryKey: ['hs-vehicles'],
    queryFn: () =>
      api.get<Paged<{ id: number; plate_number: string; warehouse_id: number | null; default_salesperson_id: number | null }>>(
        '/vehicles',
        { is_active: true, size: 200 },
      ),
    enabled: can('field.vehicles', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const van = useMemo(() => {
    const open = daySession.data?.items?.[0]
    const list = vehicles.data?.items ?? []
    const byId = open ? list.find((v) => v.id === open.vehicle_id) : undefined
    const mine = list.find((v) => v.default_salesperson_id === salespersonId)
    const chosen = byId ?? mine ?? null
    const vehicleId = open?.vehicle_id ?? chosen?.id ?? null
    return {
      vehicleId,
      warehouseId: open?.warehouse_id ?? chosen?.warehouse_id ?? null,
      plate: chosen?.plate_number ?? null,
    }
  }, [daySession.data, vehicles.data, salespersonId])

  /* ---- customers ---------------------------------------------------------- */
  const debouncedTerm = useDebounced(term)
  const debouncedProductTerm = useDebounced(productTerm)
  const customers = useQuery({
    queryKey: ['hs-customers', debouncedTerm],
    queryFn: () => api.get<Paged<Customer>>('/customers', { term: debouncedTerm, size: 15 }),
    enabled: !customer,
  })

  const history = useQuery({
    queryKey: ['hs-history', customer?.id],
    queryFn: () =>
      api.get<SalesHistoryItem[]>(`/customers/${customer!.id}/sales-history`, { limit: 5 }),
    enabled: !!customer,
    retry: false,
    throwOnError: false,
  })

  /* ---- what is on the van ------------------------------------------------- */
  const vanStock = useQuery({
    queryKey: ['hs-van-stock', van.vehicleId],
    queryFn: () => api.get<VanStockRow[]>(`/vehicles/${van.vehicleId}/stock`),
    enabled: !!van.vehicleId,
  })

  // Fallback when no van could be resolved: sell straight from the catalogue
  // (the backend still resolves the vehicle from the salesperson's profile).
  const catalogue = useQuery({
    queryKey: ['hs-catalogue', debouncedProductTerm],
    queryFn: () =>
      api.get<Paged<ProductRow>>('/products', {
        q: debouncedProductTerm,
        only_sellable: true,
        is_active: true,
        size: 40,
      }),
    enabled:
      !van.vehicleId &&
      !daySession.isLoading &&
      !vehicles.isLoading &&
      can('stock.products', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const expiry = useQuery({
    queryKey: ['hs-expiry', van.warehouseId],
    queryFn: () =>
      api.get<ExpiryRow[]>('/warehouses/stock/expiring', {
        warehouse_id: van.warehouseId,
        days: 120,
      }),
    enabled: !!van.warehouseId && can('stock.lots', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const expiryByProduct = useMemo(() => {
    const map = new Map<number, number>()
    for (const row of expiry.data ?? []) {
      const days = row.days_to_expiry ?? daysUntil(row.expiry_date ?? null)
      if (days === null || days === undefined) continue
      const prev = map.get(row.product_id)
      if (prev === undefined || days < prev) map.set(row.product_id, days)
    }
    return map
  }, [expiry.data])

  const stockItems: StockItem[] = useMemo(() => {
    if (van.vehicleId) {
      return (vanStock.data ?? []).map((r) => {
        const perCase = Math.max(1, toNumber(r.units_per_case))
        const available = toNumber(r.available_quantity)
        return {
          product_id: r.product_id,
          sku: r.sku ?? '',
          name: r.product_name ?? String(r.product_id),
          uom: 'CASE',
          cases: available / perCase,
          available,
          expiryDays: expiryByProduct.get(r.product_id) ?? null,
        }
      })
    }
    return (catalogue.data?.items ?? []).map((p) => ({
      product_id: p.id,
      sku: p.sku,
      name: p.name,
      uom: p.sales_uom || 'CASE',
      cases: 0,
      available: 0,
      expiryDays: null,
    }))
  }, [van.vehicleId, vanStock.data, catalogue.data, expiryByProduct])

  const visibleStock = useMemo(() => {
    const q = fold(productTerm.trim())
    if (!q || !van.vehicleId) return stockItems
    return stockItems.filter((r) => fold(r.name).includes(q) || fold(r.sku).includes(q))
  }, [stockItems, productTerm, van.vehicleId])

  /* ---- AI suggestions (never allowed to block the sale) ------------------- */
  const ai = useQuery({
    queryKey: ['hs-ai', customer?.id],
    queryFn: () => api.post<SuggestionOut>(`/ai/assistant/customer/${customer!.id}`),
    enabled: !!customer && can('ai.assistant', 'EXECUTE'),
    retry: false,
    throwOnError: false,
  })

  const suggestions: SuggestionLine[] = useMemo(() => {
    const payload = ai.data?.payload as Record<string, unknown> | undefined
    if (!payload || ai.isError) return []
    const raw = (payload.lines ?? payload.items ?? []) as SuggestionLine[]
    return Array.isArray(raw) ? raw.filter((r) => r && r.product_id) : []
  }, [ai.data, ai.isError])

  /* ---- live pricing ------------------------------------------------------- */
  const debouncedBasket = useDebounced(basket, 400)
  const quote = useQuery({
    queryKey: ['hs-quote', customer?.id, debouncedBasket],
    queryFn: () =>
      api.post<PriceQuote>('/campaigns/quote', {
        customer_id: customer!.id,
        salesperson_id: salespersonId ?? undefined,
        lines: debouncedBasket.map((l) => ({
          product_id: l.product_id,
          quantity: l.quantity,
          uom: l.uom,
          discount_percent: l.discount_percent,
        })),
      }),
    enabled: !!customer && debouncedBasket.length > 0,
    retry: false,
    throwOnError: false,
  })

  const totals = quote.data
  const grandTotal = toNumber(totals?.total_amount)
  const freeLines = (totals?.lines ?? []).filter((l) => l.is_free_goods)

  useEffect(() => {
    setAmount(method === 'OPEN_ACCOUNT' ? '0' : grandTotal > 0 ? String(grandTotal) : '')
  }, [grandTotal, method])

  const remainingCredit =
    toNumber(customer?.credit_limit) - toNumber(customer?.balance) - grandTotal
  const overCredit = !!customer && toNumber(customer.credit_limit) > 0 && remainingCredit < 0

  /* ---- basket ------------------------------------------------------------- */
  const addLine = useCallback((item: { product_id: number; name: string; sku: string; uom: string }, qty = 1) => {
    setBasket((prev) => {
      const i = prev.findIndex((l) => l.product_id === item.product_id && l.uom === item.uom)
      if (i >= 0) {
        const next = [...prev]
        next[i] = { ...next[i], quantity: next[i].quantity + qty }
        return next
      }
      return [...prev, { ...item, quantity: qty, discount_percent: 0 }]
    })
  }, [])

  const patchLine = (idx: number, patch: Partial<BasketLine>) =>
    setBasket((prev) => prev.map((l, i) => (i === idx ? { ...l, ...patch } : l)))

  const dropLine = (idx: number) => setBasket((prev) => prev.filter((_, i) => i !== idx))

  const startNewSale = () => {
    setBasket([])
    setCustomer(null)
    setTerm('')
    setProductTerm('')
    setNotes('')
    setAmount('')
    setMethod('CASH')
    setInstrument({ bank_name: '', document_number: '', maturity_date: '', drawer_name: '' })
    setClientUid(crypto.randomUUID())
    setDone(null)
    setStep(0)
  }

  /* ---- post --------------------------------------------------------------- */
  const complete = useMutation({
    mutationFn: () =>
      api.post<HotSaleResult>('/sales/hot-sale', {
        client_uid: clientUid,
        customer_id: customer!.id,
        salesperson_id: salespersonId ?? undefined,
        vehicle_id: van.vehicleId ?? undefined,
        notes: notes || null,
        lines: basket.map((l) => ({
          product_id: l.product_id,
          quantity: l.quantity,
          uom: l.uom,
          discount_percent: l.discount_percent,
        })),
        payment:
          method === 'OPEN_ACCOUNT' || toNumber(amount) <= 0
            ? null
            : {
                method,
                amount: toNumber(amount),
                bank_name: instrument.bank_name || null,
                document_number: instrument.document_number || null,
                maturity_date: instrument.maturity_date || null,
                drawer_name: instrument.drawer_name || null,
              },
      }),
    onSuccess: (data) => {
      setDone(data)
      push('success', t('hotSale.completed'))
      void vanStock.refetch()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const downloadInvoice = async (invoiceId: number) => {
    try {
      const { blob, filename } = await api.download(`/sales/invoices/${invoiceId}/pdf`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('invoices.pdfFailed'))
    }
  }

  /* ---- confirmation ------------------------------------------------------- */
  if (done) {
    return (
      <>
        <PageHeader title={t('hotSale.title')} icon={<Zap className="h-5 w-5" />} />
        <Card className="mx-auto max-w-lg">
          <div className="flex flex-col items-center gap-4 py-4 text-center">
            <div className="rounded-full bg-ok-50 p-4 text-ok-600">
              <Check className="h-8 w-8" />
            </div>
            <h2 className="text-lg font-semibold text-shell-900">{t('hotSale.completed')}</h2>
            <dl className="w-full space-y-2 text-sm">
              <Row label={t('hotSale.saleNo')} value={done.sale.sale_no} />
              {done.invoice && <Row label={t('hotSale.invoiceNo')} value={done.invoice.invoice_no} />}
              {done.payment && <Row label={t('hotSale.paymentNo')} value={done.payment.payment_no} />}
              <div className="flex justify-between pt-1">
                <dt className="text-shell-500">{t('common.total')}</dt>
                <dd className="tabular text-lg font-semibold">
                  {formatMoney(done.sale.total_amount)}
                </dd>
              </div>
            </dl>
            <div className="flex w-full flex-col gap-2 sm:flex-row">
              {done.invoice && (
                <button
                  type="button"
                  className="btn-secondary flex-1 justify-center"
                  onClick={() => void downloadInvoice(done.invoice!.id)}
                >
                  <Download className="h-4 w-4" />
                  {t('hotSale.printInvoice')}
                </button>
              )}
              <button type="button" className="btn-primary flex-1 justify-center" onClick={startNewSale}>
                <Plus className="h-4 w-4" />
                {t('hotSale.newSale')}
              </button>
            </div>
          </div>
        </Card>
      </>
    )
  }

  /* ---- main --------------------------------------------------------------- */
  const STEPS = [t('hotSale.stepCustomer'), t('hotSale.stepProducts'), t('hotSale.stepBasket')]

  return (
    <>
      <PageHeader
        title={t('hotSale.title')}
        subtitle={van.plate ? `${t('hotSale.vehicle')}: ${van.plate}` : undefined}
        icon={<Zap className="h-5 w-5" />}
      />

      {!van.vehicleId && !daySession.isLoading && !vehicles.isLoading && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-warn-200 bg-warn-50 p-3 text-sm text-warn-700">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{t('hotSale.noVehicle')}</span>
        </div>
      )}

      {/* Mobile stepper */}
      <div className="mb-4 flex gap-1 xl:hidden">
        {STEPS.map((label, i) => (
          <button
            key={label}
            type="button"
            onClick={() => setStep(i)}
            className={clsx(
              'flex-1 rounded-lg px-2 py-2 text-xs font-medium',
              step === i ? 'bg-brand-600 text-white' : 'bg-shell-100 text-shell-600',
            )}
          >
            {i + 1}. {label}
            {i === 2 && basket.length > 0 && ` (${basket.length})`}
          </button>
        ))}
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        {/* 1 — customer, history, AI */}
        <div className={clsx('space-y-5', step !== 0 && 'hidden xl:block')}>
          <Card title={t('hotSale.selectCustomer')} bodyClassName="p-4">
            {customer ? (
              <CustomerCard customer={customer} history={history.data ?? []} onChange={() => setCustomer(null)} />
            ) : (
              <>
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                  <input
                    className="input pl-9"
                    placeholder={t('hotSale.searchCustomer')}
                    value={term}
                    onChange={(e) => setTerm(e.target.value)}
                  />
                </div>
                {customers.isLoading ? (
                  <LoadingBlock />
                ) : (customers.data?.items ?? []).length === 0 ? (
                  <EmptyState />
                ) : (
                  <ul className="mt-3 max-h-80 space-y-1 overflow-y-auto">
                    {(customers.data?.items ?? []).map((c) => (
                      <li key={c.id}>
                        <button
                          type="button"
                          className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm hover:bg-shell-50"
                          onClick={() => {
                            setCustomer(c)
                            setStep(1)
                          }}
                        >
                          <Store className="h-4 w-4 shrink-0 text-shell-400" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate">{c.trade_name || c.name}</span>
                            <span className="block text-2xs text-shell-400">
                              {c.code}
                              {c.city ? ` · ${c.city}` : ''}
                            </span>
                          </span>
                          <ChevronRight className="h-4 w-4 shrink-0 text-shell-300" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </Card>

          {customer && suggestions.length > 0 && (
            <Card
              title={t('hotSale.aiSuggestions')}
              actions={<Sparkles className="h-4 w-4 text-brand-500" />}
              bodyClassName="p-4"
            >
              {ai.data?.explanation && (
                <p className="mb-3 rounded-lg bg-brand-50 p-2.5 text-xs text-brand-800">
                  {ai.data.explanation}
                </p>
              )}
              <ul className="space-y-2">
                {suggestions.slice(0, 6).map((s) => {
                  const qty = Math.max(1, Math.round(toNumber(s.suggested_quantity ?? s.suggested_cases ?? 1)))
                  const label = s.name ?? s.product ?? s.sku ?? String(s.product_id)
                  const reason = (lang === 'en' ? s.reason_en : s.reason_tr) || s.reason || ''
                  return (
                    <li key={s.product_id} className="rounded-lg border border-shell-200 p-2.5">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">{label}</p>
                          {reason && <p className="text-2xs text-shell-500">{reason}</p>}
                        </div>
                        <button
                          type="button"
                          className="btn-secondary btn-sm shrink-0"
                          title={t('hotSale.addToBasket')}
                          onClick={() =>
                            addLine(
                              {
                                product_id: s.product_id,
                                name: label,
                                sku: s.sku ?? '',
                                uom: s.uom ?? 'CASE',
                              },
                              qty,
                            )
                          }
                        >
                          <Plus className="h-3.5 w-3.5" />
                          {formatNumber(qty)}
                        </button>
                      </div>
                    </li>
                  )
                })}
              </ul>
            </Card>
          )}
        </div>

        {/* 2 — stock */}
        <Card
          title={van.vehicleId ? t('hotSale.vanStock') : t('nav.products')}
          bodyClassName="p-0"
          className={clsx(step !== 1 && 'hidden xl:block')}
          actions={<span className="text-2xs text-shell-400">{visibleStock.length}</span>}
        >
          <div className="border-b border-shell-200 p-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
              <input
                className="input pl-9"
                placeholder={t('hotSale.searchProduct')}
                value={productTerm}
                onChange={(e) => setProductTerm(e.target.value)}
              />
            </div>
            {!customer && (
              <p className="mt-2 text-2xs text-shell-400">{t('hotSale.selectCustomerFirst')}</p>
            )}
          </div>
          {vanStock.isLoading || catalogue.isLoading ? (
            <LoadingBlock />
          ) : visibleStock.length === 0 ? (
            <EmptyState />
          ) : (
            <ul className="max-h-[34rem] divide-y divide-shell-100 overflow-y-auto">
              {visibleStock.map((row) => (
                <li key={row.product_id}>
                  <button
                    type="button"
                    disabled={!customer}
                    className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-shell-50 disabled:opacity-40"
                    onClick={() => {
                      addLine(row)
                      setStep(2)
                    }}
                  >
                    <Package className="h-4 w-4 shrink-0 text-shell-400" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm">{row.name}</span>
                      <span className="block text-2xs text-shell-400">{row.sku}</span>
                    </span>
                    {row.expiryDays !== null && <ExpiryBadge days={row.expiryDays} />}
                    {van.vehicleId && (
                      <span className="tabular w-20 shrink-0 text-right text-sm font-medium">
                        {formatNumber(row.cases, { decimals: row.cases % 1 === 0 ? 0 : 1 })}{' '}
                        <span className="text-2xs font-normal text-shell-400">
                          {t('hotSale.cases')}
                        </span>
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* 3 — basket, campaigns, totals, payment */}
        <Card
          title={t('hotSale.basket')}
          bodyClassName="p-0"
          className={clsx(step !== 2 && 'hidden xl:block')}
          actions={
            basket.length > 0 ? (
              <button type="button" className="btn-ghost btn-sm" onClick={() => setBasket([])}>
                {t('hotSale.clearBasket')}
              </button>
            ) : undefined
          }
        >
          {basket.length === 0 ? (
            <EmptyState title={t('hotSale.emptyBasket')} />
          ) : (
            <>
              <ul className="max-h-72 divide-y divide-shell-100 overflow-y-auto">
                {basket.map((line, i) => (
                  <li key={`${line.product_id}-${line.uom}-${i}`} className="p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">{line.name}</p>
                        <p className="text-2xs text-shell-400">{line.sku}</p>
                      </div>
                      <button
                        type="button"
                        aria-label={t('hotSale.removeLine')}
                        className="text-shell-400 hover:text-danger-600"
                        onClick={() => dropLine(i)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <div className="flex items-center rounded-lg border border-shell-300">
                        <button
                          type="button"
                          className="px-2 py-1 text-shell-500 hover:text-shell-900"
                          onClick={() => patchLine(i, { quantity: Math.max(1, line.quantity - 1) })}
                        >
                          <Minus className="h-3.5 w-3.5" />
                        </button>
                        <input
                          type="number"
                          min={1}
                          step="1"
                          aria-label={t('common.quantity')}
                          className="tabular w-14 border-0 bg-transparent p-0 text-center text-sm outline-none"
                          value={line.quantity}
                          onChange={(e) =>
                            patchLine(i, { quantity: Math.max(1, Number(e.target.value) || 1) })
                          }
                        />
                        <button
                          type="button"
                          className="px-2 py-1 text-shell-500 hover:text-shell-900"
                          onClick={() => patchLine(i, { quantity: line.quantity + 1 })}
                        >
                          <Plus className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      <select
                        aria-label={t('common.uom')}
                        className="input w-auto py-1 text-xs"
                        value={line.uom}
                        onChange={(e) => patchLine(i, { uom: e.target.value })}
                      >
                        {UOMS.map((u) => (
                          <option key={u} value={u}>
                            {u}
                          </option>
                        ))}
                      </select>
                      <div className="ml-auto flex items-center gap-1">
                        <input
                          type="number"
                          min={0}
                          max={100}
                          aria-label={t('common.discount')}
                          className="tabular w-14 rounded border border-shell-300 px-1.5 py-1 text-right text-xs"
                          value={line.discount_percent}
                          onChange={(e) =>
                            patchLine(i, {
                              discount_percent: Math.min(100, Math.max(0, Number(e.target.value) || 0)),
                            })
                          }
                        />
                        <span className="text-2xs text-shell-400">%</span>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>

              {freeLines.length > 0 && (
                <div className="border-t border-shell-200 bg-ok-50/60 px-4 py-2.5">
                  <SectionTitle>{t('hotSale.freeGoods')}</SectionTitle>
                  <ul className="space-y-1">
                    {freeLines.map((l) => (
                      <li key={`free-${l.line_no}`} className="flex items-center gap-2 text-xs text-ok-700">
                        <Gift className="h-3.5 w-3.5 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{l.product_name}</span>
                        <span className="tabular">{formatQuantity(l.quantity, l.uom)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(totals?.applied_campaigns ?? []).length > 0 && (
                <div className="border-t border-shell-200 bg-brand-50/60 px-4 py-2.5">
                  <SectionTitle>{t('hotSale.campaigns')}</SectionTitle>
                  <ul className="space-y-1">
                    {(totals?.applied_campaigns ?? []).map((c) => (
                      <li key={c.campaign_id} className="flex justify-between gap-2 text-xs text-brand-800">
                        <span className="min-w-0 truncate">{c.name || c.code}</span>
                        <span className="tabular shrink-0">-{formatMoney(c.discount_amount)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="space-y-1.5 border-t border-shell-200 px-4 py-3 text-sm">
                {quote.isFetching && (
                  <div className="flex justify-center py-1">
                    <Spinner className="h-3.5 w-3.5 text-shell-400" />
                  </div>
                )}
                {quote.isError && (
                  <p className="mb-1 text-2xs text-danger-600">{t('hotSale.pricingFailed')}</p>
                )}
                <Amount label={t('common.gross')} value={totals?.gross_amount} />
                <Amount label={t('hotSale.lineDiscount')} value={totals?.line_discount_amount} negative />
                <Amount
                  label={t('hotSale.campaignDiscount')}
                  value={totals?.campaign_discount_amount}
                  negative
                />
                <Amount label={t('common.net')} value={totals?.net_amount} />
                <Amount label={t('common.vat')} value={totals?.vat_amount} />
                <div className="flex justify-between border-t border-shell-200 pt-2 text-base font-semibold text-shell-900">
                  <span>{t('common.total')}</span>
                  <span className="tabular">{formatMoney(grandTotal)}</span>
                </div>
              </div>

              <div className="space-y-3 border-t border-shell-200 p-4">
                <SectionTitle>{t('hotSale.payment')}</SectionTitle>
                {overCredit && (
                  <p className="flex items-start gap-1.5 rounded-lg bg-warn-50 p-2 text-2xs text-warn-700">
                    <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
                    {t('hotSale.creditWarning')}
                  </p>
                )}
                <div className="grid grid-cols-2 gap-1.5">
                  {PAYMENT_METHODS.map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setMethod(m)}
                      className={clsx(
                        'btn-sm justify-center',
                        method === m ? 'btn-primary' : 'btn-secondary',
                      )}
                    >
                      {t(`payment.${m}`)}
                    </button>
                  ))}
                </div>
                <Field label={t('hotSale.amountReceived')}>
                  <input
                    type="number"
                    step="0.01"
                    min={0}
                    className="input tabular text-right"
                    value={amount}
                    onChange={(e) => setAmount(e.target.value)}
                    disabled={method === 'OPEN_ACCOUNT'}
                  />
                </Field>
                {method === 'CHEQUE' && (
                  <div className="grid gap-2 sm:grid-cols-2">
                    <Field label={t('hotSale.bankName')}>
                      <input
                        className="input"
                        value={instrument.bank_name}
                        onChange={(e) => setInstrument((s) => ({ ...s, bank_name: e.target.value }))}
                      />
                    </Field>
                    <Field label={t('hotSale.documentNumber')}>
                      <input
                        className="input"
                        value={instrument.document_number}
                        onChange={(e) =>
                          setInstrument((s) => ({ ...s, document_number: e.target.value }))
                        }
                      />
                    </Field>
                    <Field label={t('hotSale.maturityDate')}>
                      <input
                        type="date"
                        className="input"
                        value={instrument.maturity_date}
                        onChange={(e) =>
                          setInstrument((s) => ({ ...s, maturity_date: e.target.value }))
                        }
                      />
                    </Field>
                    <Field label={t('hotSale.drawerName')}>
                      <input
                        className="input"
                        value={instrument.drawer_name}
                        onChange={(e) => setInstrument((s) => ({ ...s, drawer_name: e.target.value }))}
                      />
                    </Field>
                  </div>
                )}
                <Field label={t('common.notes')}>
                  <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
                </Field>
                {can('sales.hot_sale', 'CREATE') && (
                  <button
                    type="button"
                    className="btn-primary w-full justify-center"
                    disabled={!customer || basket.length === 0 || complete.isPending}
                    onClick={() => complete.mutate()}
                  >
                    {complete.isPending ? <Spinner /> : <Check className="h-4 w-4" />}
                    {t('hotSale.complete')}
                  </button>
                )}
              </div>
            </>
          )}
        </Card>
      </div>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Small pieces                                                               */
/* -------------------------------------------------------------------------- */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-shell-100 pb-2">
      <dt className="text-shell-500">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  )
}

function Amount({
  label,
  value,
  negative,
}: {
  label: string
  value?: number | string
  negative?: boolean
}) {
  const n = toNumber(value)
  return (
    <div className="flex justify-between text-shell-500">
      <span>{label}</span>
      <span className="tabular">
        {negative && n > 0 ? '-' : ''}
        {formatMoney(n)}
      </span>
    </div>
  )
}

function CustomerCard({
  customer,
  history,
  onChange,
}: {
  customer: Customer
  history: SalesHistoryItem[]
  onChange: () => void
}) {
  const { t } = useTranslation()
  const risk = customer.risk_score ?? 0
  return (
    <div>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-medium text-shell-900">
            {customer.trade_name || customer.name}
          </p>
          <p className="text-xs text-shell-500">{customer.code}</p>
        </div>
        <button type="button" className="btn-ghost btn-sm shrink-0" onClick={onChange}>
          {t('hotSale.change')}
        </button>
      </div>

      <dl className="mt-3 space-y-1.5 text-xs">
        <div className="flex justify-between">
          <dt className="text-shell-500">{t('hotSale.balance')}</dt>
          <dd className={clsx('tabular', toNumber(customer.balance) > 0 && 'font-medium text-danger-600')}>
            {formatMoney(customer.balance)}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-shell-500">{t('hotSale.creditLimit')}</dt>
          <dd className="tabular">{formatMoney(customer.credit_limit)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-shell-500">{t('hotSale.overdueBalance')}</dt>
          <dd className={clsx('tabular', toNumber(customer.overdue_balance) > 0 && 'text-danger-600')}>
            {formatMoney(customer.overdue_balance)}
          </dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-shell-500">{t('hotSale.riskScore')}</dt>
          <dd>
            <span className={risk >= 70 ? 'badge-danger' : risk >= 40 ? 'badge-warn' : 'badge-ok'}>
              {formatNumber(risk)}
            </span>
          </dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-shell-500">{t('hotSale.lastOrder')}</dt>
          <dd>{formatDate(customer.last_order_date)}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-shell-500">{t('common.status')}</dt>
          <dd>
            <StatusBadge status={customer.status} label={t(`status.${customer.status}`, customer.status)} />
          </dd>
        </div>
      </dl>

      <div className="mt-4">
        <SectionTitle>{t('hotSale.history')}</SectionTitle>
        {history.length === 0 ? (
          <p className="text-xs text-shell-400">{t('hotSale.noHistory')}</p>
        ) : (
          <ul className="space-y-1 text-xs">
            {history.slice(0, 5).map((h) => (
              <li key={h.id} className="flex justify-between gap-2 text-shell-600">
                <span className="min-w-0 truncate">
                  {formatDate(h.sale_date, { short: true })} · {h.sale_no}
                </span>
                <span className="tabular shrink-0">{formatMoney(h.total_amount)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
