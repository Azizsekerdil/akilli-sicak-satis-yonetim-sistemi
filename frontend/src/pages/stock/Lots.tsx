/**
 * Lot / SKT — batch tracking and expiry.
 *
 * FEFO allocation lives or dies on this screen: every lot carries an expiry, a
 * remaining-days badge, and a blocking switch that takes stock out of
 * allocation without deleting it (the ledger keeps referencing it forever).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, CalendarClock, CheckCircle2, Plus, Search } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  ExpiryBadge,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface LotRow {
  id: number
  product_id: number
  lot_number: string
  batch_number?: string | null
  serial_number?: string | null
  production_date?: string | null
  expiry_date?: string | null
  received_date?: string | null
  supplier_name?: string | null
  unit_cost: number | string
  is_blocked: boolean
  block_reason?: string | null
  notes?: string | null
  product_sku?: string | null
  product_name?: string | null
  days_to_expiry?: number | null
  on_hand?: number | string | null
}

interface ProductLite {
  id: number
  sku: string
  name: string
}

const SIZE = 25
const today = () => new Date().toISOString().slice(0, 10)
const plusDays = (days: number) => {
  const d = new Date()
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

export default function Lots() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [expiry, setExpiry] = useState<'all' | 'expired' | '7' | '30' | '90'>('all')
  const [onlyBlocked, setOnlyBlocked] = useState(false)
  const [creating, setCreating] = useState(false)
  const [blocking, setBlocking] = useState<LotRow | null>(null)

  const expiresBefore =
    expiry === 'all' ? undefined : expiry === 'expired' ? today() : plusDays(Number(expiry))

  const listParams = {
    page,
    size: SIZE,
    search: search || undefined,
    is_blocked: onlyBlocked ? true : undefined,
    expires_before: expiresBefore,
  }
  const list = useQuery({
    queryKey: ['lots', listParams],
    queryFn: () => api.get<Paged<LotRow>>('/warehouses/lots', listParams),
  })

  const block = useMutation({
    mutationFn: (payload: { id: number; blocked: boolean; reason: string }) =>
      api.post<LotRow>(`/warehouses/lots/${payload.id}/block`, {
        blocked: payload.blocked,
        reason: payload.reason || null,
      }),
    onSuccess: (_d, v) => {
      push('success', v.blocked ? t('lots.blockedOk') : t('lots.unblockedOk'))
      setBlocking(null)
      void qc.invalidateQueries({ queryKey: ['lots'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('lots.title')}
        subtitle={t('lots.subtitle')}
        icon={<CalendarClock className="h-5 w-5" />}
        actions={
          can('stock.lots', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              {t('lots.new')}
            </button>
          )
        }
      />

      <Card bodyClassName="p-0">
        <div className="flex flex-wrap items-center gap-3 border-b border-shell-200 p-3">
          <div className="relative min-w-[12rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('lots.lotNumber')}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(1)
              }}
            />
          </div>
          <select
            className="input w-auto"
            value={expiry}
            onChange={(e) => {
              setExpiry(e.target.value as typeof expiry)
              setPage(1)
            }}
            aria-label={t('lots.expiryFilter')}
          >
            <option value="all">{t('lots.allLots')}</option>
            <option value="expired">{t('lots.expired')}</option>
            <option value="7">{t('lots.expiringIn', { days: 7 })}</option>
            <option value="30">{t('lots.expiringIn', { days: 30 })}</option>
            <option value="90">{t('lots.expiringIn', { days: 90 })}</option>
          </select>
          <label className="flex items-center gap-1.5 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={onlyBlocked}
              onChange={(e) => {
                setOnlyBlocked(e.target.checked)
                setPage(1)
              }}
            />
            {t('lots.onlyBlocked')}
          </label>
        </div>

        {list.isLoading ? (
          <SkeletonRows rows={8} cols={7} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('lots.lotNumber')}</th>
                  <th>{t('stockCommon.sku')}</th>
                  <th>{t('stockCommon.product')}</th>
                  <th>{t('lots.expiryDate')}</th>
                  <th className="text-right">{t('lots.daysToExpiry')}</th>
                  <th>{t('lots.supplier')}</th>
                  <th className="text-right">{t('stockCommon.unitCost')}</th>
                  <th>{t('common.status')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((l) => (
                  <tr key={l.id}>
                    <td className="tabular whitespace-nowrap font-medium">{l.lot_number}</td>
                    <td className="tabular whitespace-nowrap text-xs">{l.product_sku ?? '—'}</td>
                    <td className="min-w-[10rem]">{l.product_name ?? `#${l.product_id}`}</td>
                    <td className="whitespace-nowrap text-xs">
                      {l.expiry_date ? formatDate(l.expiry_date, { short: true }) : '—'}
                    </td>
                    <td className="text-right">
                      <ExpiryBadge days={l.days_to_expiry ?? null} />
                    </td>
                    <td className="text-xs text-shell-500">{l.supplier_name ?? '—'}</td>
                    <td className="tabular text-right">{formatMoney(l.unit_cost)}</td>
                    <td>
                      {l.is_blocked ? (
                        <span className="badge-danger" title={l.block_reason ?? undefined}>
                          {t('lots.blocked')}
                        </span>
                      ) : (
                        <span className="badge-ok">{t('common.active')}</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap text-right">
                      {can('stock.lots', 'UPDATE') &&
                        (l.is_blocked ? (
                          <button
                            type="button"
                            className="btn-secondary btn-sm"
                            disabled={block.isPending}
                            onClick={() => block.mutate({ id: l.id, blocked: false, reason: '' })}
                          >
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            {t('lots.unblock')}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => setBlocking(l)}
                          >
                            <Ban className="h-3.5 w-3.5" />
                            {t('lots.block')}
                          </button>
                        ))}
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

      {blocking && (
        <BlockModal
          lot={blocking}
          pending={block.isPending}
          onClose={() => setBlocking(null)}
          onConfirm={(reason) => block.mutate({ id: blocking.id, blocked: true, reason })}
        />
      )}

      {creating && (
        <LotCreator
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false)
            void qc.invalidateQueries({ queryKey: ['lots'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Block                                                                      */
/* -------------------------------------------------------------------------- */
function BlockModal({
  lot,
  pending,
  onClose,
  onConfirm,
}: {
  lot: LotRow
  pending: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const { t } = useTranslation()
  const [reason, setReason] = useState('')
  return (
    <Modal
      open
      size="sm"
      onClose={onClose}
      title={`${t('lots.block')} — ${lot.lot_number}`}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn-danger" disabled={pending} onClick={() => onConfirm(reason)}>
            {pending && <Spinner />}
            {t('lots.block')}
          </button>
        </>
      }
    >
      <Field label={t('lots.blockReason')}>
        <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      </Field>
      <p className="mt-3 text-xs text-shell-500">
        {lot.product_name} · {t('lots.onHand')}: {formatNumber(lot.on_hand ?? 0, { decimals: 2 })}
      </p>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
function LotCreator({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [product, setProduct] = useState<ProductLite | null>(null)
  const [term, setTerm] = useState('')
  const [f, setF] = useState({
    lot_number: '',
    batch_number: '',
    serial_number: '',
    production_date: '',
    expiry_date: '',
    received_date: today(),
    supplier_name: '',
    unit_cost: '0',
    notes: '',
  })
  const set = (k: keyof typeof f, v: string) => setF((p) => ({ ...p, [k]: v }))

  const products = useQuery({
    queryKey: ['lot-products', term],
    queryFn: () => api.get<Paged<ProductLite>>('/products', { q: term, size: 8 }),
    enabled: term.trim().length >= 2 && !product,
  })

  const save = useMutation({
    mutationFn: () =>
      api.post<LotRow>('/warehouses/lots', {
        product_id: product!.id,
        lot_number: f.lot_number.trim(),
        batch_number: f.batch_number.trim() || null,
        serial_number: f.serial_number.trim() || null,
        production_date: f.production_date || null,
        expiry_date: f.expiry_date || null,
        received_date: f.received_date || null,
        supplier_name: f.supplier_name.trim() || null,
        unit_cost: Number(f.unit_cost || 0),
        notes: f.notes.trim() || null,
      }),
    onSuccess: () => {
      push('success', t('lots.created'))
      onSaved()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={t('lots.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!product || !f.lot_number.trim() || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t('stockCommon.product')} required>
          {product ? (
            <div className="flex items-center justify-between rounded-lg border border-shell-200 px-3 py-2">
              <span className="truncate text-sm">
                {product.name} <span className="tabular text-2xs text-shell-400">{product.sku}</span>
              </span>
              <button type="button" className="btn-ghost btn-sm" onClick={() => setProduct(null)}>
                {t('common.edit')}
              </button>
            </div>
          ) : (
            <>
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                <input
                  className="input pl-9"
                  placeholder={t('stockCommon.selectProduct')}
                  value={term}
                  onChange={(e) => setTerm(e.target.value)}
                />
              </div>
              {term.trim().length >= 2 && (
                <ul className="mt-2 max-h-40 divide-y divide-shell-100 overflow-y-auto rounded-lg border border-shell-200">
                  {products.isLoading && <LoadingBlock />}
                  {(products.data?.items ?? []).map((p) => (
                    <li key={p.id}>
                      <button
                        type="button"
                        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-shell-50"
                        onClick={() => {
                          setProduct(p)
                          setTerm('')
                        }}
                      >
                        <span className="truncate">{p.name}</span>
                        <span className="tabular text-2xs text-shell-400">{p.sku}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('lots.lotNumber')} required>
            <input className="input" value={f.lot_number} onChange={(e) => set('lot_number', e.target.value)} />
          </Field>
          <Field label={t('lots.batchNumber')}>
            <input className="input" value={f.batch_number} onChange={(e) => set('batch_number', e.target.value)} />
          </Field>
          <Field label={t('lots.serialNumber')}>
            <input className="input" value={f.serial_number} onChange={(e) => set('serial_number', e.target.value)} />
          </Field>
          <Field label={t('lots.supplier')}>
            <input className="input" value={f.supplier_name} onChange={(e) => set('supplier_name', e.target.value)} />
          </Field>
          <Field label={t('lots.productionDate')}>
            <input
              type="date"
              className="input"
              value={f.production_date}
              onChange={(e) => set('production_date', e.target.value)}
            />
          </Field>
          <Field label={t('lots.expiryDate')}>
            <input
              type="date"
              className="input"
              value={f.expiry_date}
              onChange={(e) => set('expiry_date', e.target.value)}
            />
          </Field>
          <Field label={t('lots.receivedDate')}>
            <input
              type="date"
              className="input"
              value={f.received_date}
              onChange={(e) => set('received_date', e.target.value)}
            />
          </Field>
          <Field label={t('stockCommon.unitCost')}>
            <input
              type="number"
              step="0.01"
              className="input tabular text-right"
              value={f.unit_cost}
              onChange={(e) => set('unit_cost', e.target.value)}
            />
          </Field>
        </div>

        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={f.notes} onChange={(e) => set('notes', e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}
