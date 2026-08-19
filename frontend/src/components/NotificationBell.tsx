/** Notification bell with unread count and a dropdown feed. */
import clsx from 'clsx'
import { Bell, CheckCheck } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api, type Paged } from '@/lib/api'
import { currentLanguage } from '@/lib/i18n'
import { formatRelative } from '@/lib/format'

interface Notification {
  id: number
  notification_type: string
  severity: string
  title_tr: string
  title_en?: string | null
  body_tr?: string | null
  body_en?: string | null
  action_url?: string | null
  is_read: boolean
  created_at: string
}

const TONE: Record<string, string> = {
  INFO: 'bg-info-500',
  WARNING: 'bg-warn-500',
  ERROR: 'bg-danger-500',
  CRITICAL: 'bg-danger-600',
}

export function NotificationBell() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const qc = useQueryClient()
  const lang = currentLanguage()

  const { data } = useQuery({
    queryKey: ['notifications'],
    queryFn: () =>
      api.get<Paged<Notification>>('/system/notifications', { size: 20 }),
    refetchInterval: 60_000,
    retry: false,
    // The bell is decorative when the module is unavailable — never blow up.
    throwOnError: false,
  })

  const items = data?.items ?? []
  const unread = items.filter((n) => !n.is_read).length

  const markAll = useMutation({
    mutationFn: () => api.post('/system/notifications/read-all'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notifications'] }),
  })

  return (
    <div className="relative">
      <button
        type="button"
        className="btn-ghost btn-sm relative"
        onClick={() => setOpen((v) => !v)}
        aria-label={t('nav.notifications')}
      >
        <Bell className="h-4 w-4" />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger-500 px-1 text-2xs font-semibold text-white">
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-1 w-80 animate-slide-up rounded-lg border border-shell-200 bg-white shadow-pop">
            <div className="flex items-center justify-between border-b border-shell-100 px-3 py-2">
              <p className="text-sm font-semibold text-shell-800">
                {t('nav.notifications')}
              </p>
              {unread > 0 && (
                <button
                  type="button"
                  className="flex items-center gap-1 text-2xs text-brand-600 hover:underline"
                  onClick={() => markAll.mutate()}
                >
                  <CheckCheck className="h-3 w-3" />
                  {t('common.all')}
                </button>
              )}
            </div>

            <ul className="max-h-96 overflow-y-auto">
              {items.length === 0 && (
                <li className="px-3 py-8 text-center text-sm text-shell-400">
                  {t('common.noData')}
                </li>
              )}
              {items.map((n) => {
                const title = lang === 'en' && n.title_en ? n.title_en : n.title_tr
                const body = lang === 'en' && n.body_en ? n.body_en : n.body_tr
                const inner = (
                  <div className="flex gap-2.5 px-3 py-2.5">
                    <span
                      className={clsx(
                        'mt-1.5 h-2 w-2 shrink-0 rounded-full',
                        TONE[n.severity] ?? 'bg-shell-400',
                      )}
                    />
                    <div className="min-w-0 flex-1">
                      <p
                        className={clsx(
                          'truncate text-sm',
                          n.is_read ? 'text-shell-600' : 'font-medium text-shell-900',
                        )}
                      >
                        {title}
                      </p>
                      {body && <p className="line-clamp-2 text-xs text-shell-500">{body}</p>}
                      <p className="mt-0.5 text-2xs text-shell-400">
                        {formatRelative(n.created_at)}
                      </p>
                    </div>
                  </div>
                )
                return (
                  <li key={n.id} className="border-b border-shell-50 hover:bg-shell-50">
                    {n.action_url ? (
                      <Link to={n.action_url} onClick={() => setOpen(false)}>
                        {inner}
                      </Link>
                    ) : (
                      inner
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        </>
      )}
    </div>
  )
}
