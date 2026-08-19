/**
 * Ayarlar / Settings.
 *
 * Rendered straight from GET /system/settings: the backend decides which
 * categories exist, what each value's type is, whether it is editable and
 * whether it is a secret.  This screen only chooses the right control.
 *
 * Nothing is written until the save bar is used, and only the keys the user
 * actually touched are sent — so a masked secret left alone stays untouched.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Languages, RotateCcw, Save, Settings as SettingsIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  PageHeader,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, type Lang } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage, setLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface SettingItem {
  id: number
  category: string
  key: string
  value?: string | null
  value_type: string
  default_value?: string | null
  label: string
  label_tr?: string | null
  label_en?: string | null
  description?: string | null
  is_secret: boolean
  is_editable: boolean
  requires_restart: boolean
  sort_order: number
}

interface SettingGroup {
  category: string
  label: string
  label_tr: string
  label_en: string
  items: SettingItem[]
}

const dirtyKey = (item: SettingItem) => `${item.category}::${item.key}`

/* -------------------------------------------------------------------------- */
/* One control                                                                */
/* -------------------------------------------------------------------------- */
function SettingControl({
  item,
  value,
  disabled,
  onChange,
}: {
  item: SettingItem
  value: string
  disabled: boolean
  onChange: (value: string) => void
}) {
  const { t } = useTranslation()

  if (item.is_secret) {
    return (
      <input
        type="password"
        className="input"
        autoComplete="new-password"
        value={value}
        disabled={disabled}
        placeholder={item.value ?? '••••'}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }

  switch (item.value_type) {
    case 'bool':
      return (
        <select
          className="input"
          value={value.toLowerCase() === 'true' ? 'true' : 'false'}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="true">{t('common.yes')}</option>
          <option value="false">{t('common.no')}</option>
        </select>
      )
    case 'int':
      return (
        <input
          type="number"
          step="1"
          className="input tabular"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    case 'float':
    case 'decimal':
      return (
        <input
          type="number"
          step="any"
          className="input tabular"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    case 'json':
      return (
        <textarea
          className="input font-mono text-xs"
          rows={3}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    default:
      return (
        <input
          className="input"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      )
  }
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function SettingsPage() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [draft, setDraft] = useState<Record<string, string>>({})
  const [uiLang, setUiLang] = useState<Lang>(currentLanguage())

  const settingsQuery = useQuery({
    queryKey: ['system', 'settings'],
    queryFn: () => api.get<SettingGroup[]>('/system/settings'),
  })

  const canEdit = can('system.settings', 'UPDATE')
  const groups = settingsQuery.data ?? []

  const items = useMemo(() => {
    const byKey = new Map<string, SettingItem>()
    groups.forEach((group) => group.items.forEach((item) => byKey.set(dirtyKey(item), item)))
    return byKey
  }, [groups])

  const save = useMutation({
    mutationFn: () =>
      api.put<SettingGroup[]>('/system/settings', {
        items: Object.entries(draft).map(([key, value]) => {
          const item = items.get(key)
          return {
            category: item?.category ?? key.split('::')[0],
            key: item?.key ?? key.split('::')[1],
            value,
          }
        }),
      }),
    onSuccess: async () => {
      setDraft({})
      await queryClient.invalidateQueries({ queryKey: ['system', 'settings'] })
      toast.push('success', t('sysSettings.saved'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const dirtyCount = Object.keys(draft).length

  return (
    <div className="pb-20">
      <PageHeader
        title={t('sysSettings.title')}
        subtitle={t('sysSettings.subtitle')}
        icon={<SettingsIcon className="h-5 w-5" />}
      />

      <Card title={t('sysSettings.language')} className="mb-4">
        <Field label={t('sysSettings.language')} hint={t('sysSettings.languageHint')}>
          <div className="flex items-center gap-2">
            <Languages className="h-4 w-4 text-shell-400" />
            <select
              className="input w-auto"
              value={uiLang}
              onChange={(e) => {
                const next = e.target.value as Lang
                setUiLang(next)
                setLanguage(next)
                void queryClient.invalidateQueries()
              }}
            >
              <option value="tr">Türkçe</option>
              <option value="en">English</option>
            </select>
          </div>
        </Field>
      </Card>

      {settingsQuery.isLoading ? (
        <LoadingBlock />
      ) : settingsQuery.isError ? (
        <ErrorState error={settingsQuery.error} onRetry={() => void settingsQuery.refetch()} />
      ) : groups.length === 0 ? (
        <EmptyState title={t('sysSettings.noSettings')} />
      ) : (
        <div className="space-y-4">
          {groups.map((group) => (
            <Card key={group.category} title={group.label || group.category}>
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {group.items.map((item) => {
                  const key = dirtyKey(item)
                  const current = draft[key] ?? (item.is_secret ? '' : (item.value ?? ''))
                  const isDirty = key in draft
                  const hints = [
                    item.description,
                    item.is_secret ? t('sysSettings.secretHint') : null,
                    item.requires_restart ? t('sysSettings.requiresRestart') : null,
                    !item.is_editable ? t('sysSettings.readOnly') : null,
                    item.default_value
                      ? `${t('sysSettings.default')}: ${item.default_value}`
                      : null,
                  ].filter(Boolean)
                  return (
                    <div
                      key={item.id}
                      className={isDirty ? 'rounded-lg ring-1 ring-brand-400/40' : ''}
                    >
                      <Field
                        label={item.label || item.key}
                        hint={hints.join(' · ') || undefined}
                      >
                        <SettingControl
                          item={item}
                          value={current}
                          disabled={!canEdit || !item.is_editable}
                          onChange={(value) =>
                            setDraft((prev) => ({ ...prev, [key]: value }))
                          }
                        />
                      </Field>
                      <p className="mt-1 font-mono text-2xs text-shell-300">{item.key}</p>
                    </div>
                  )
                })}
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Dirty-state save bar */}
      {dirtyCount > 0 && canEdit && (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-shell-200 bg-white/95 px-4 py-3 shadow-pop backdrop-blur">
          <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3">
            <p className="tabular text-sm font-medium text-shell-700">
              {t('sysSettings.dirty', { count: dirtyCount })}
            </p>
            <div className="flex gap-2">
              <button type="button" className="btn-secondary btn-sm" onClick={() => setDraft({})}>
                <RotateCcw className="h-3.5 w-3.5" />
                {t('sysSettings.discard')}
              </button>
              <button
                type="button"
                className="btn-primary btn-sm"
                onClick={() => save.mutate()}
                disabled={save.isPending}
              >
                {save.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
                {t('sysSettings.saveAll')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
