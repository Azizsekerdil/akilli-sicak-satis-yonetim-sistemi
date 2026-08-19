/**
 * İskontolar / Standing discounts.
 *
 * Unlike campaigns these are open-ended agreements — a customer's permanent 3%,
 * a category deal — so the form is deliberately flat.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BadgePercent, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatPercent } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

interface Discount {
  id: number
  code: string
  name: string
  scope: string
  scope_id?: number | null
  product_id?: number | null
  category_id?: number | null
  basis: string
  percent: number
  amount: number | string
  min_quantity: number | string
  min_amount: number | string
  valid_from?: string | null
  valid_to?: string | null
  is_active: boolean
  priority: number
}

const SCOPES = ['CUSTOMER', 'CUSTOMER_TYPE', 'CHANNEL', 'REGION', 'PRODUCT', 'CATEGORY', 'BRAND', 'ALL'] as const

export default function Discounts() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    code: '',
    name: '',
    scope: 'CUSTOMER' as (typeof SCOPES)[number],
    scope_id: '',
    product_id: '',
    category_id: '',
    basis: 'PERCENT',
    percent: '5',
    amount: '0',
    min_quantity: '0',
    min_amount: '0',
    valid_from: '',
    valid_to: '',
    priority: '100',
  })

  const list = useQuery({
    queryKey: ['discounts', page],
    queryFn: () => api.get<Paged<Discount>>('/campaigns/discounts', { page, size: 25 }),
  })

  const create = useMutation({
    mutationFn: () =>
      api.post('/campaigns/discounts', {
        code: form.code.trim(),
        name: form.name.trim(),
        scope: form.scope,
        scope_id: form.scope_id ? Number(form.scope_id) : null,
        product_id: form.product_id ? Number(form.product_id) : null,
        category_id: form.category_id ? Number(form.category_id) : null,
        basis: form.basis,
        percent: form.basis === 'PERCENT' ? Number(form.percent) : 0,
        amount: form.basis === 'AMOUNT' ? Number(form.amount) : 0,
        min_quantity: Number(form.min_quantity),
        min_amount: Number(form.min_amount),
        valid_from: form.valid_from || null,
        valid_to: form.valid_to || null,
        priority: Number(form.priority),
      }),
    onSuccess: () => {
      push('success', t('common.created'))
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['discounts'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/campaigns/discounts/${id}`),
    onSuccess: () => {
      push('success', t('common.deleted'))
      void qc.invalidateQueries({ queryKey: ['discounts'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const data = list.data

  return (
    <>
      <PageHeader
        title={t('nav.discounts')}
        subtitle={t('nav.marketing')}
        icon={<BadgePercent className="h-5 w-5" />}
        actions={
          can('marketing.discounts', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setOpen(true)}>
              <Plus className="h-3.5 w-3.5" />
              {t('common.new')}
            </button>
          )
        }
      />

      <Card bodyClassName="p-0">
        {list.isLoading ? (
          <LoadingBlock />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : (data?.items ?? []).length === 0 ? (
          <EmptyState />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('common.code')}</th>
                    <th>{t('common.name')}</th>
                    <th>Scope</th>
                    <th className="text-right">{t('common.discount')}</th>
                    <th className="text-right">Min</th>
                    <th>{t('common.dateRange')}</th>
                    <th>{t('common.status')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(data?.items ?? []).map((d) => (
                    <tr key={d.id}>
                      <td className="font-mono text-xs">{d.code}</td>
                      <td className="max-w-xs truncate">{d.name}</td>
                      <td>
                        <span className="badge-muted">{d.scope}</span>
                      </td>
                      <td className="tabular text-right">
                        {d.basis === 'PERCENT' ? formatPercent(d.percent) : formatMoney(d.amount)}
                      </td>
                      <td className="tabular text-right text-xs">
                        {Number(d.min_quantity) > 0
                          ? `${d.min_quantity}×`
                          : Number(d.min_amount) > 0
                            ? formatMoney(d.min_amount)
                            : '—'}
                      </td>
                      <td className="whitespace-nowrap text-xs">
                        {d.valid_from ? formatDate(d.valid_from, { short: true }) : '—'} –{' '}
                        {d.valid_to ? formatDate(d.valid_to, { short: true }) : '∞'}
                      </td>
                      <td>
                        <StatusBadge status={d.is_active ? 'ACTIVE' : 'PASSIVE'} />
                      </td>
                      <td className="text-right">
                        {can('marketing.discounts', 'DELETE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => remove.mutate(d.id)}
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
            <Pagination
              page={data?.page ?? 1}
              pages={data?.pages ?? 1}
              total={data?.total ?? 0}
              size={data?.size ?? 25}
              onPage={setPage}
            />
          </>
        )}
      </Card>

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
              disabled={create.isPending || !form.code || !form.name}
              onClick={() => create.mutate()}
            >
              {create.isPending && <Spinner />}
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
          <Field label="Scope">
            <select
              className="input"
              value={form.scope}
              onChange={(e) =>
                setForm({ ...form, scope: e.target.value as (typeof SCOPES)[number] })
              }
            >
              {SCOPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Scope id" hint="customer / region / brand id">
            <input
              type="number"
              className="input tabular"
              value={form.scope_id}
              onChange={(e) => setForm({ ...form, scope_id: e.target.value })}
            />
          </Field>
          <Field label="Basis">
            <select
              className="input"
              value={form.basis}
              onChange={(e) => setForm({ ...form, basis: e.target.value })}
            >
              <option value="PERCENT">PERCENT</option>
              <option value="AMOUNT">AMOUNT</option>
            </select>
          </Field>
          {form.basis === 'PERCENT' ? (
            <Field label="%" required>
              <input
                type="number"
                min={0}
                max={100}
                step="0.1"
                className="input tabular"
                value={form.percent}
                onChange={(e) => setForm({ ...form, percent: e.target.value })}
              />
            </Field>
          ) : (
            <Field label={t('common.amount')} required>
              <input
                type="number"
                min={0}
                step="0.01"
                className="input tabular"
                value={form.amount}
                onChange={(e) => setForm({ ...form, amount: e.target.value })}
              />
            </Field>
          )}
          <Field label="Min qty / Min miktar">
            <input
              type="number"
              min={0}
              className="input tabular"
              value={form.min_quantity}
              onChange={(e) => setForm({ ...form, min_quantity: e.target.value })}
            />
          </Field>
          <Field label="Min amount / Min tutar">
            <input
              type="number"
              min={0}
              step="0.01"
              className="input tabular"
              value={form.min_amount}
              onChange={(e) => setForm({ ...form, min_amount: e.target.value })}
            />
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
        </div>
      </Modal>
    </>
  )
}
