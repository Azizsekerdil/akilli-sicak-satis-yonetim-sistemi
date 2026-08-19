/**
 * Sayımlar / Stock counts.
 *
 * A count sheet is opened against a warehouse (optionally prefilled with the
 * system quantities), counted line by line, then approved — which posts one
 * COUNT_ADJUSTMENT movement per variance.  Variance is counted − system, so a
 * negative number means stock is missing.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ClipboardCheck, Plus, Save, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'
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

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface WarehouseRow {
  id: number
  code: string
  name: string
}

interface CountItem {
  id?: number | null
  product_id: number
  lot_id?: number | null
  system_quantity: number | string
  counted_quantity: number | string
  variance_quantity: number | string
  unit_cost: number | string
  variance_value: number | string
  reason?: string | null
  product_sku?: string | null
  product_name?: string | null
  uom?: string | null
  lot_number?: string | null
  expiry_date?: string | null
}

interface CountRow {
  id: number
  document_no: string
  warehouse_id: number
  status: string
  count_date: string
  approved_at?: string | null
  is_van_end_of_day: boolean
  total_variance_qty: number | string
  total_variance_value: number | string
  notes?: string | null
  warehouse_code?: string | null
  warehouse_name?: string | null
  items: CountItem[]
}

const STATUSES = ['DRAFT', 'IN_PROGRESS', 'COUNTED', 'APPROVED', 'CANCELLED']
const SIZE = 20
const today = () => new Date().toISOString().slice(0, 10)
const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v ?? 0)
  return Number.isFinite(n) ? n : 0
}

export default function Counts() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [warehouseId, setWarehouseId] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [counted, setCounted] = useState<Record<string, string>>({})
  const [term, setTerm] = useState('')

  const warehouses = useQuery({
    queryKey: ['warehouses', 'picker'],
    queryFn: () => api.get<Paged<WarehouseRow>>('/warehouses', { size: 200, is_active: true }),
  })

  const listParams = {
    page,
    size: SIZE,
    status: status || undefined,
    warehouse_id: warehouseId || undefined,
  }
  const list = useQuery({
    queryKey: ['counts', listParams],
    queryFn: () => api.get<Paged<CountRow>>('/warehouses/counts', listParams),
  })

  const detail = useQuery({
    queryKey: ['count', selectedId],
    queryFn: () => api.get<CountRow>(`/warehouses/counts/${selectedId}`),
    enabled: selectedId !== null,
  })

  /* Seed the editable column from whatever the sheet already holds. */
  useEffect(() => {
    if (!detail.data) return
    const seed: Record<string, string> = {}
    for (const it of detail.data.items) seed[lineKey(it)] = String(num(it.counted_quantity))
    setCounted(seed)
  }, [detail.data])

  const saveLines = useMutation({
    mutationFn: () =>
      api.put<CountRow>(`/warehouses/counts/${selectedId}/lines`, {
        lines: (detail.data?.items ?? []).map((it) => ({
          product_id: it.product_id,
          lot_id: it.lot_id ?? null,
          counted_quantity: num(counted[lineKey(it)]),
          reason: it.reason ?? null,
        })),
      }),
    onSuccess: () => {
      push('success', t('counts.linesSaved'))
      void detail.refetch()
      void qc.invalidateQueries({ queryKey: ['counts'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const approve = useMutation({
    mutationFn: () => api.post<CountRow>(`/warehouses/counts/${selectedId}/approve`, {}),
    onSuccess: () => {
      push('success', t('counts.approved'))
      void detail.refetch()
      void qc.invalidateQueries({ queryKey: ['counts'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []
  const sheet = detail.data
  const locked = sheet ? sheet.status === 'APPROVED' || sheet.status === 'CANCELLED' : true
  const items = (sheet?.items ?? []).filter((it) => {
    const q = term.trim().toLocaleLowerCase('tr-TR')
    if (!q) return true
    return (
      (it.product_name ?? '').toLocaleLowerCase('tr-TR').includes(q) ||
      (it.product_sku ?? '').toLocaleLowerCase('tr-TR').includes(q)
    )
  })

  return (
    <>
      <PageHeader
        title={t('counts.title')}
        subtitle={t('counts.subtitle')}
        icon={<ClipboardCheck className="h-5 w-5" />}
        actions={
          can('stock.counts', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              {t('counts.new')}
            </button>
          )
        }
      />

      <div className="grid gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
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
              value={warehouseId}
              onChange={(e) => {
                setWarehouseId(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('warehouses.title')}</option>
              {(warehouses.data?.items ?? []).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.code}
                </option>
              ))}
            </select>
          </div>

          {list.isLoading ? (
            <SkeletonRows rows={6} cols={2} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <ul className="divide-y divide-shell-100">
              {rows.map((c) => (
                <li key={c.id} className={selectedId === c.id ? 'bg-brand-50/60' : 'hover:bg-shell-50'}>
                  <button
                    type="button"
                    className="w-full px-3 py-2.5 text-left"
                    onClick={() => setSelectedId(c.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="tabular truncate text-sm font-medium text-shell-800">
                        {c.document_no}
                      </span>
                      <StatusBadge status={c.status} />
                    </div>
                    <p className="mt-0.5 text-2xs text-shell-500">
                      {formatDate(c.count_date, { short: true })} · {c.warehouse_code ?? c.warehouse_id}
                      {c.is_van_end_of_day ? ` · ${t('counts.vanEndOfDay')}` : ''}
                    </p>
                    {num(c.total_variance_qty) !== 0 && (
                      <p className="tabular mt-0.5 text-2xs text-danger-600">
                        {t('counts.totalVariance')}: {formatNumber(c.total_variance_qty, { decimals: 2 })}
                      </p>
                    )}
                  </button>
                </li>
              ))}
            </ul>
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

        {/* ------------------------------ Detail ------------------------------ */}
        <div>
          {!selectedId ? (
            <Card>
              <EmptyState title={t('counts.detail')} />
            </Card>
          ) : detail.isLoading ? (
            <Card>
              <SkeletonRows rows={8} cols={5} />
            </Card>
          ) : detail.isError ? (
            <Card>
              <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
            </Card>
          ) : sheet ? (
            <Card
              title={`${sheet.document_no} — ${sheet.warehouse_name ?? sheet.warehouse_id}`}
              bodyClassName="p-0"
              actions={
                <div className="flex items-center gap-2">
                  <StatusBadge status={sheet.status} />
                  {!locked && can('stock.counts', 'UPDATE') && (
                    <button
                      type="button"
                      className="btn-secondary btn-sm"
                      disabled={saveLines.isPending || sheet.items.length === 0}
                      onClick={() => saveLines.mutate()}
                    >
                      {saveLines.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
                      {t('counts.saveLines')}
                    </button>
                  )}
                  {!locked && can('stock.counts', 'APPROVE') && (
                    <button
                      type="button"
                      className="btn-primary btn-sm"
                      disabled={approve.isPending}
                      onClick={() => {
                        if (window.confirm(t('counts.confirmApprove'))) approve.mutate()
                      }}
                    >
                      {approve.isPending ? <Spinner /> : <ClipboardCheck className="h-3.5 w-3.5" />}
                      {t('counts.approve')}
                    </button>
                  )}
                </div>
              }
            >
              <div className="flex flex-wrap items-center gap-4 border-b border-shell-200 p-3">
                <div className="relative min-w-[10rem] flex-1">
                  <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                  <input
                    className="input pl-9"
                    placeholder={t('common.search')}
                    value={term}
                    onChange={(e) => setTerm(e.target.value)}
                  />
                </div>
                <p className="text-xs text-shell-500">
                  {t('counts.totalVariance')}:{' '}
                  <span className="tabular font-medium">
                    {formatNumber(sheet.total_variance_qty, { decimals: 2 })}
                  </span>
                  {' · '}
                  {t('counts.varianceValue')}:{' '}
                  <span className="tabular font-medium">{formatMoney(sheet.total_variance_value)}</span>
                </p>
              </div>

              {items.length === 0 ? (
                <EmptyState />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('stockCommon.sku')}</th>
                        <th>{t('stockCommon.product')}</th>
                        <th>{t('stockCommon.lot')}</th>
                        <th className="text-right">{t('stockCommon.systemQty')}</th>
                        <th className="text-right">{t('stockCommon.countedQty')}</th>
                        <th className="text-right">{t('stockCommon.variance')}</th>
                        <th className="text-right">{t('counts.varianceValue')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((it) => {
                        const key = lineKey(it)
                        const diff = num(counted[key]) - num(it.system_quantity)
                        return (
                          <tr key={key}>
                            <td className="tabular whitespace-nowrap">{it.product_sku ?? '—'}</td>
                            <td className="min-w-[10rem]">{it.product_name ?? `#${it.product_id}`}</td>
                            <td className="whitespace-nowrap text-xs">
                              {it.lot_number ?? '—'}
                              {it.expiry_date && (
                                <span className="ml-1 text-shell-400">
                                  {formatDate(it.expiry_date, { short: true })}
                                </span>
                              )}
                            </td>
                            <td className="tabular text-right">
                              {formatNumber(it.system_quantity, { decimals: 2 })} {it.uom ?? ''}
                            </td>
                            <td className="text-right">
                              <input
                                type="number"
                                min={0}
                                step="0.001"
                                disabled={locked}
                                className="input tabular w-24 py-1 text-right"
                                value={counted[key] ?? ''}
                                onChange={(e) => setCounted((p) => ({ ...p, [key]: e.target.value }))}
                              />
                            </td>
                            <td
                              className={`tabular text-right font-medium ${
                                diff < 0 ? 'text-danger-600' : diff > 0 ? 'text-warn-600' : 'text-shell-400'
                              }`}
                            >
                              {formatNumber(diff, { decimals: 2 })}
                            </td>
                            <td className="tabular text-right text-xs text-shell-500">
                              {formatMoney(num(it.unit_cost) * diff)}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          ) : null}
        </div>
      </div>

      {creating && (
        <CountCreator
          warehouses={warehouses.data?.items ?? []}
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false)
            setSelectedId(id)
            void qc.invalidateQueries({ queryKey: ['counts'] })
          }}
        />
      )}
    </>
  )
}

function lineKey(it: CountItem): string {
  return `${it.product_id}:${it.lot_id ?? 0}`
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
function CountCreator({
  warehouses,
  onClose,
  onCreated,
}: {
  warehouses: WarehouseRow[]
  onClose: () => void
  onCreated: (id: number) => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [warehouseId, setWarehouseId] = useState('')
  const [date, setDate] = useState(today())
  const [prefill, setPrefill] = useState(true)
  const [vanEod, setVanEod] = useState(false)
  const [notes, setNotes] = useState('')

  const save = useMutation({
    mutationFn: () =>
      api.post<CountRow>('/warehouses/counts', {
        warehouse_id: Number(warehouseId),
        count_date: date,
        prefill,
        is_van_end_of_day: vanEod,
        notes: notes || null,
      }),
    onSuccess: (c) => {
      push('success', t('stockCommon.saved'))
      onCreated(c.id)
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={t('counts.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!warehouseId || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('warehouses.title')} required>
          <select className="input" value={warehouseId} onChange={(e) => setWarehouseId(e.target.value)}>
            <option value="">{t('stockCommon.selectWarehouse')}</option>
            {warehouses.map((w) => (
              <option key={w.id} value={w.id}>
                {w.code} — {w.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('counts.countDate')} required>
          <input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
          <input type="checkbox" checked={prefill} onChange={(e) => setPrefill(e.target.checked)} />
          {t('counts.prefill')}
        </label>
        <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
          <input type="checkbox" checked={vanEod} onChange={(e) => setVanEod(e.target.checked)} />
          {t('counts.vanEndOfDay')}
        </label>
        <div className="sm:col-span-2">
          <Field label={t('common.notes')}>
            <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>
      </div>
    </Modal>
  )
}
