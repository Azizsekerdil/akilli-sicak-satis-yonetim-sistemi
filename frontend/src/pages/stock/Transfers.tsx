/**
 * Transferler / Stock transfers.
 *
 * DRAFT → IN_TRANSIT (ship) → RECEIVED (receive).  Receiving takes a quantity
 * per line rather than a blanket confirmation, because short deliveries are the
 * normal case and the difference has to land in the ledger, not in a note.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeftRight, Ban, Check, ChevronDown, ChevronRight, Plus, Send, Trash2 } from 'lucide-react'
import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface WarehouseRow {
  id: number
  code: string
  name: string
  warehouse_type: string
}

interface ProductLite {
  id: number
  sku: string
  name: string
  base_uom: string
  sales_uom: string
}

interface TransferItem {
  id?: number | null
  product_id: number
  lot_id?: number | null
  quantity: number | string
  received_quantity: number | string
  uom?: string | null
  unit_cost?: number | string | null
  product_sku?: string | null
  product_name?: string | null
  base_quantity?: number | string | null
}

interface TransferRow {
  id: number
  document_no: string
  source_warehouse_id: number
  target_warehouse_id: number
  status: string
  transfer_date: string
  shipped_at?: string | null
  received_at?: string | null
  vehicle_id?: number | null
  notes?: string | null
  source_warehouse_name?: string | null
  target_warehouse_name?: string | null
  total_quantity: number | string
  total_cost: number | string
  items: TransferItem[]
}

interface DraftLine {
  product_id: number
  sku: string
  name: string
  uom: string
  quantity: string
}

const STATUSES = ['DRAFT', 'IN_TRANSIT', 'RECEIVED', 'CANCELLED']
const UOMS = ['PIECE', 'CASE', 'PACK', 'PALLET', 'KILOGRAM', 'GRAM', 'LITRE', 'MILLILITRE']
const SIZE = 20
const today = () => new Date().toISOString().slice(0, 10)
const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v ?? 0)
  return Number.isFinite(n) ? n : 0
}

export default function Transfers() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [receiving, setReceiving] = useState<TransferRow | null>(null)

  const warehouses = useQuery({
    queryKey: ['warehouses', 'picker'],
    queryFn: () => api.get<Paged<WarehouseRow>>('/warehouses', { size: 200, is_active: true }),
  })

  const listParams = {
    page,
    size: SIZE,
    status: status || undefined,
    source_warehouse_id: sourceId || undefined,
    target_warehouse_id: targetId || undefined,
  }
  const list = useQuery({
    queryKey: ['transfers', listParams],
    queryFn: () => api.get<Paged<TransferRow>>('/warehouses/transfers', listParams),
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['transfers'] })
  const fail = (e: unknown) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))

  const ship = useMutation({
    mutationFn: (id: number) => api.post<TransferRow>(`/warehouses/transfers/${id}/ship`, {}),
    onSuccess: () => {
      push('success', t('transfers.shipped'))
      invalidate()
    },
    onError: fail,
  })

  const cancel = useMutation({
    mutationFn: (id: number) => api.post<TransferRow>(`/warehouses/transfers/${id}/cancel`, {}),
    onSuccess: () => {
      push('success', t('transfers.cancelled'))
      invalidate()
    },
    onError: fail,
  })

  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('transfers.title')}
        subtitle={t('transfers.subtitle')}
        icon={<ArrowLeftRight className="h-5 w-5" />}
        actions={
          can('stock.transfers', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              {t('transfers.new')}
            </button>
          )
        }
      />

      <Card bodyClassName="p-0">
        <div className="flex flex-wrap gap-2 border-b border-shell-200 p-3">
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
            value={sourceId}
            onChange={(e) => {
              setSourceId(e.target.value)
              setPage(1)
            }}
          >
            <option value="">{t('warehouses.title')} —</option>
            {(warehouses.data?.items ?? []).map((w) => (
              <option key={w.id} value={w.id}>
                {w.code}
              </option>
            ))}
          </select>
          <select
            className="input w-auto"
            value={targetId}
            onChange={(e) => {
              setTargetId(e.target.value)
              setPage(1)
            }}
          >
            <option value="">— {t('warehouses.title')}</option>
            {(warehouses.data?.items ?? []).map((w) => (
              <option key={w.id} value={w.id}>
                {w.code}
              </option>
            ))}
          </select>
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
                  <th />
                  <th>{t('stockCommon.documentNo')}</th>
                  <th>{t('transfers.transferDate')}</th>
                  <th>{t('warehouses.title')}</th>
                  <th className="text-right">{t('stockCommon.quantity')}</th>
                  <th className="text-right">{t('common.total')}</th>
                  <th>{t('common.status')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((tr) => (
                  <Fragment key={tr.id}>
                    <tr>
                      <td className="w-8">
                        <button
                          type="button"
                          className="text-shell-400 hover:text-shell-700"
                          onClick={() => setExpanded(expanded === tr.id ? null : tr.id)}
                          aria-label={t('common.details')}
                        >
                          {expanded === tr.id ? (
                            <ChevronDown className="h-4 w-4" />
                          ) : (
                            <ChevronRight className="h-4 w-4" />
                          )}
                        </button>
                      </td>
                      <td className="tabular whitespace-nowrap font-medium">{tr.document_no}</td>
                      <td className="whitespace-nowrap text-xs">{formatDate(tr.transfer_date, { short: true })}</td>
                      <td className="text-xs">
                        {tr.source_warehouse_name ?? tr.source_warehouse_id} →{' '}
                        {tr.target_warehouse_name ?? tr.target_warehouse_id}
                      </td>
                      <td className="tabular text-right">{formatNumber(tr.total_quantity, { decimals: 2 })}</td>
                      <td className="tabular text-right">{formatMoney(tr.total_cost)}</td>
                      <td>
                        <StatusBadge status={tr.status} />
                      </td>
                      <td className="whitespace-nowrap text-right">
                        {tr.status === 'DRAFT' && can('stock.transfers', 'UPDATE') && (
                          <button
                            type="button"
                            className="btn-secondary btn-sm"
                            disabled={ship.isPending}
                            onClick={() => ship.mutate(tr.id)}
                          >
                            <Send className="h-3.5 w-3.5" />
                            {t('transfers.ship')}
                          </button>
                        )}
                        {tr.status === 'IN_TRANSIT' && can('stock.transfers', 'APPROVE') && (
                          <button
                            type="button"
                            className="btn-primary btn-sm"
                            onClick={() => setReceiving(tr)}
                          >
                            <Check className="h-3.5 w-3.5" />
                            {t('transfers.receive')}
                          </button>
                        )}
                        {tr.status === 'DRAFT' && can('stock.transfers', 'DELETE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => {
                              if (window.confirm(t('stockCommon.confirmDelete'))) cancel.mutate(tr.id)
                            }}
                            aria-label={t('transfers.cancel')}
                          >
                            <Ban className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </td>
                    </tr>
                    {expanded === tr.id && (
                      <tr>
                        <td colSpan={8} className="bg-shell-50/60">
                          <table className="table">
                            <thead>
                              <tr>
                                <th>{t('stockCommon.sku')}</th>
                                <th>{t('stockCommon.product')}</th>
                                <th className="text-right">{t('stockCommon.quantity')}</th>
                                <th>{t('stockCommon.uom')}</th>
                                <th className="text-right">{t('transfers.receivedQuantity')}</th>
                                <th className="text-right">{t('stockCommon.unitCost')}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {tr.items.map((it, i) => (
                                <tr key={it.id ?? `${tr.id}-${i}`}>
                                  <td className="tabular">{it.product_sku ?? '—'}</td>
                                  <td>{it.product_name ?? `#${it.product_id}`}</td>
                                  <td className="tabular text-right">{formatNumber(it.quantity, { decimals: 2 })}</td>
                                  <td className="text-xs">{it.uom ?? '—'}</td>
                                  <td className="tabular text-right">
                                    {formatNumber(it.received_quantity, { decimals: 2 })}
                                  </td>
                                  <td className="tabular text-right">{formatMoney(it.unit_cost ?? 0)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          {tr.notes && <p className="px-4 py-2 text-xs text-shell-500">{tr.notes}</p>}
                        </td>
                      </tr>
                    )}
                  </Fragment>
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

      {creating && (
        <TransferCreator
          warehouses={warehouses.data?.items ?? []}
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false)
            invalidate()
          }}
        />
      )}

      {receiving && (
        <ReceiveModal
          transfer={receiving}
          onClose={() => setReceiving(null)}
          onSaved={() => {
            setReceiving(null)
            invalidate()
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
function TransferCreator({
  warehouses,
  onClose,
  onSaved,
}: {
  warehouses: WarehouseRow[]
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const [date, setDate] = useState(today())
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<DraftLine[]>([])
  const [term, setTerm] = useState('')

  const products = useQuery({
    queryKey: ['transfer-products', term],
    queryFn: () => api.get<Paged<ProductLite>>('/products', { q: term, size: 8 }),
    enabled: term.trim().length >= 2,
  })

  const save = useMutation({
    mutationFn: () =>
      api.post<TransferRow>('/warehouses/transfers', {
        source_warehouse_id: Number(source),
        target_warehouse_id: Number(target),
        transfer_date: date,
        notes: notes || null,
        items: lines
          .filter((l) => num(l.quantity) > 0)
          .map((l) => ({ product_id: l.product_id, quantity: num(l.quantity), uom: l.uom })),
      }),
    onSuccess: () => {
      push('success', t('stockCommon.saved'))
      onSaved()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const valid =
    source && target && source !== target && lines.some((l) => num(l.quantity) > 0)

  return (
    <Modal
      open
      size="lg"
      onClose={onClose}
      title={t('transfers.new')}
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
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t('warehouses.title')} required>
            <select className="input" value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="">{t('stockCommon.selectWarehouse')}</option>
              {warehouses.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.code} — {w.name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label={t('warehouses.title')}
            required
            error={source && target && source === target ? t('transfers.sameWarehouse') : undefined}
          >
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">{t('stockCommon.selectWarehouse')}</option>
              {warehouses.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.code} — {w.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('transfers.transferDate')} required>
            <input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
        </div>

        <div>
          <div className="relative">
            <Plus className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
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
                      setTerm('')
                      setLines((prev) =>
                        prev.some((l) => l.product_id === p.id)
                          ? prev
                          : [
                              ...prev,
                              {
                                product_id: p.id,
                                sku: p.sku,
                                name: p.name,
                                uom: p.sales_uom || p.base_uom,
                                quantity: '1',
                              },
                            ],
                      )
                    }}
                  >
                    <span className="truncate">{p.name}</span>
                    <span className="tabular text-2xs text-shell-400">{p.sku}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {lines.length === 0 ? (
          <EmptyState title={t('stockCommon.noLines')} />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('stockCommon.sku')}</th>
                  <th>{t('stockCommon.product')}</th>
                  <th className="text-right">{t('stockCommon.quantity')}</th>
                  <th>{t('stockCommon.uom')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {lines.map((l, i) => (
                  <tr key={l.product_id}>
                    <td className="tabular">{l.sku}</td>
                    <td>{l.name}</td>
                    <td className="text-right">
                      <input
                        type="number"
                        min={0}
                        step="0.001"
                        className="input tabular w-24 py-1 text-right"
                        value={l.quantity}
                        onChange={(e) =>
                          setLines((prev) =>
                            prev.map((x, xi) => (xi === i ? { ...x, quantity: e.target.value } : x)),
                          )
                        }
                      />
                    </td>
                    <td>
                      <select
                        className="input w-28 py-1"
                        value={l.uom}
                        onChange={(e) =>
                          setLines((prev) => prev.map((x, xi) => (xi === i ? { ...x, uom: e.target.value } : x)))
                        }
                      >
                        {UOMS.map((u) => (
                          <option key={u} value={u}>
                            {u}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="text-right">
                      <button
                        type="button"
                        className="btn-ghost btn-sm text-danger-600"
                        onClick={() => setLines((prev) => prev.filter((_, xi) => xi !== i))}
                        aria-label={t('common.delete')}
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

        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Receive                                                                    */
/* -------------------------------------------------------------------------- */
function ReceiveModal({
  transfer,
  onClose,
  onSaved,
}: {
  transfer: TransferRow
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [received, setReceived] = useState<Record<number, string>>(() =>
    Object.fromEntries(
      transfer.items.filter((i) => i.id != null).map((i) => [i.id as number, String(num(i.quantity))]),
    ),
  )
  const [notes, setNotes] = useState('')

  const save = useMutation({
    mutationFn: () =>
      api.post<TransferRow>(`/warehouses/transfers/${transfer.id}/receive`, {
        lines: Object.entries(received).map(([itemId, qty]) => ({
          item_id: Number(itemId),
          received_quantity: num(qty),
        })),
        notes: notes || null,
      }),
    onSuccess: () => {
      push('success', t('transfers.received'))
      onSaved()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      size="lg"
      onClose={onClose}
      title={`${t('transfers.receiveTitle')} — ${transfer.document_no}`}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending && <Spinner />}
            {t('transfers.receive')}
          </button>
        </>
      }
    >
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>{t('stockCommon.sku')}</th>
              <th>{t('stockCommon.product')}</th>
              <th className="text-right">{t('stockCommon.quantity')}</th>
              <th className="text-right">{t('transfers.receivedQuantity')}</th>
              <th className="text-right">{t('stockCommon.variance')}</th>
            </tr>
          </thead>
          <tbody>
            {transfer.items.map((it, i) => {
              const id = it.id ?? -i
              const diff = num(received[id]) - num(it.quantity)
              return (
                <tr key={id}>
                  <td className="tabular">{it.product_sku ?? '—'}</td>
                  <td>{it.product_name ?? `#${it.product_id}`}</td>
                  <td className="tabular text-right">
                    {formatNumber(it.quantity, { decimals: 2 })} {it.uom ?? ''}
                  </td>
                  <td className="text-right">
                    <input
                      type="number"
                      min={0}
                      step="0.001"
                      className="input tabular w-24 py-1 text-right"
                      value={received[id] ?? ''}
                      onChange={(e) => setReceived((p) => ({ ...p, [id]: e.target.value }))}
                    />
                  </td>
                  <td
                    className={`tabular text-right ${
                      diff < 0 ? 'text-danger-600' : diff > 0 ? 'text-warn-600' : 'text-shell-400'
                    }`}
                  >
                    {formatNumber(diff, { decimals: 2 })}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-4">
        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}
