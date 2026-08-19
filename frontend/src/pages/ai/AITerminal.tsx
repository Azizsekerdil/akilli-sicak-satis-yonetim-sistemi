/**
 * AI Geliştirme Terminali / AI Development Terminal.
 *
 * Every action is classified server-side against the session's permission tier
 * before anything runs.  This screen never auto-approves: when the backend
 * answers `requires_approval`, the command sits in the transcript with an
 * explicit approve/reject prompt and the token is only echoed back once the
 * operator has read the command and pressed Approve.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertOctagon,
  Check,
  CircleSlash,
  Play,
  Plus,
  ShieldAlert,
  Terminal,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
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
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface TerminalSession {
  id: number
  user_id: number
  title: string
  permission_level: string
  provider?: string | null
  model?: string | null
  is_active: boolean
  command_count: number
  created_at?: string | null
  updated_at?: string | null
}

interface CommandResult {
  id: number
  session_id: number
  action_type: string
  required_level: string
  is_allowed: boolean
  requires_approval: boolean
  approve_token?: string | null
  block_reason?: string | null
  command?: string | null
  target?: string | null
  exit_code?: number | null
  output: string
  duration_ms: number
  message_key?: string | null
  created_at?: string | null
}

interface RunRequest {
  session_id: number
  instruction: string
  requested_action: string
  target?: string | null
  command?: string | null
  approve_token?: string | null
}

interface TranscriptEntry {
  key: string
  request: RunRequest
  result: CommandResult
  rejected: boolean
}

/** READ_ONLY → SYSTEM_COMMAND, least privileged first. */
const LEVELS = [
  { value: 'READ_ONLY', labelKey: 'aiTerminal.levelReadOnly' },
  { value: 'PROJECT_WRITE', labelKey: 'aiTerminal.levelProjectWrite' },
  { value: 'RUN_TESTS', labelKey: 'aiTerminal.levelRunTests' },
  { value: 'PACKAGE_INSTALL', labelKey: 'aiTerminal.levelPackageInstall' },
  { value: 'GIT_OPERATIONS', labelKey: 'aiTerminal.levelGitOperations' },
  { value: 'SYSTEM_COMMAND', labelKey: 'aiTerminal.levelSystemCommand' },
] as const

const ACTIONS = [
  'READ_FILE',
  'LIST_DIR',
  'WRITE_FILE',
  'RUN_TESTS',
  'PACKAGE_INSTALL',
  'GIT',
  'SHELL',
] as const

/** Escalating tone: the further down the list, the louder the colour. */
const LEVEL_TONE: Record<string, string> = {
  READ_ONLY: 'border-ok-200 bg-ok-50 text-ok-700',
  PROJECT_WRITE: 'border-info-200 bg-info-50 text-info-700',
  RUN_TESTS: 'border-info-200 bg-info-50 text-info-700',
  PACKAGE_INSTALL: 'border-warn-200 bg-warn-50 text-warn-700',
  GIT_OPERATIONS: 'border-warn-300 bg-warn-50 text-warn-800',
  SYSTEM_COMMAND: 'border-danger-300 bg-danger-50 text-danger-700',
}

const LEVEL_BADGE: Record<string, string> = {
  READ_ONLY: 'badge-ok',
  PROJECT_WRITE: 'badge-info',
  RUN_TESTS: 'badge-info',
  PACKAGE_INSTALL: 'badge-warn',
  GIT_OPERATIONS: 'badge-warn',
  SYSTEM_COMMAND: 'badge-danger',
}

type EntryState = 'blocked' | 'awaiting' | 'executed' | 'allowed'

function entryState(entry: TranscriptEntry): EntryState {
  if (entry.result.block_reason) return 'blocked'
  if (entry.result.requires_approval && entry.result.approve_token) return 'awaiting'
  if (entry.result.is_allowed) return 'executed'
  return 'allowed'
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AITerminal() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [sessionId, setSessionId] = useState<number | null>(null)
  const [newTitle, setNewTitle] = useState('')
  const [newLevel, setNewLevel] = useState<string>('READ_ONLY')

  const [action, setAction] = useState<string>('LIST_DIR')
  const [instruction, setInstruction] = useState('')
  const [target, setTarget] = useState('')
  const [command, setCommand] = useState('')
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  const canExecute = can('ai.terminal', 'EXECUTE')

  const sessionsQuery = useQuery({
    queryKey: ['ai', 'terminal', 'sessions'],
    queryFn: () => api.get<TerminalSession[]>('/ai/terminal/sessions'),
  })

  const sessions = sessionsQuery.data ?? []
  const active = sessions.find((s) => s.id === sessionId) ?? null

  const createSession = useMutation({
    mutationFn: () =>
      api.post<TerminalSession>('/ai/terminal/sessions', {
        title: newTitle.trim(),
        permission_level: newLevel,
      }),
    onSuccess: async (session) => {
      setSessionId(session.id)
      setTranscript([])
      setNewTitle('')
      await queryClient.invalidateQueries({ queryKey: ['ai', 'terminal', 'sessions'] })
      toast.push('success', t('aiTerminal.sessionCreated'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const run = useMutation({
    mutationFn: (request: RunRequest) => api.post<CommandResult>('/ai/terminal/run', request),
    onSuccess: async (result, request) => {
      setTranscript((prev) => {
        // Re-running an approved command replaces its awaiting entry in place.
        const index = prev.findIndex(
          (e) => e.result.approve_token && request.approve_token === e.result.approve_token,
        )
        const entry: TranscriptEntry = {
          key: `${result.id}-${Date.now()}`,
          request,
          result,
          rejected: false,
        }
        if (index >= 0) {
          const next = [...prev]
          next[index] = entry
          return next
        }
        return [...prev, entry]
      })
      await queryClient.invalidateQueries({ queryKey: ['ai', 'terminal', 'sessions'] })
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [transcript.length])

  const submit = () => {
    if (sessionId === null || !instruction.trim() || !canExecute) return
    run.mutate({
      session_id: sessionId,
      instruction: instruction.trim(),
      requested_action: action,
      target: target.trim() || null,
      command: command.trim() || null,
    })
  }

  const approve = (entry: TranscriptEntry) => {
    run.mutate({ ...entry.request, approve_token: entry.result.approve_token ?? null })
  }

  const reject = (entry: TranscriptEntry) => {
    setTranscript((prev) =>
      prev.map((e) => (e.key === entry.key ? { ...e, rejected: true } : e)),
    )
    toast.push('info', t('aiTerminal.rejected'))
  }

  return (
    <div>
      <PageHeader
        title={t('aiTerminal.title')}
        subtitle={t('aiTerminal.subtitle')}
        icon={<Terminal className="h-5 w-5" />}
      />

      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        {/* Sessions + level picker */}
        <div className="space-y-4">
          <Card title={t('aiTerminal.sessions')}>
            {sessionsQuery.isLoading ? (
              <LoadingBlock />
            ) : sessionsQuery.isError ? (
              <ErrorState error={sessionsQuery.error} onRetry={() => void sessionsQuery.refetch()} />
            ) : sessions.length === 0 ? (
              <p className="py-3 text-center text-xs text-shell-400">{t('aiTerminal.noSessions')}</p>
            ) : (
              <ul className="space-y-1">
                {sessions.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSessionId(s.id)
                        setTranscript([])
                      }}
                      className={`w-full rounded-lg border px-3 py-2 text-left text-xs ${
                        sessionId === s.id
                          ? 'border-brand-300 bg-brand-50'
                          : 'border-shell-200 hover:bg-shell-50'
                      }`}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium text-shell-700">
                          {s.title || `#${s.id}`}
                        </span>
                        <span className={LEVEL_BADGE[s.permission_level] ?? 'badge-muted'}>
                          {s.permission_level}
                        </span>
                      </span>
                      <span className="tabular mt-0.5 block text-2xs text-shell-400">
                        {formatNumber(s.command_count)} {t('aiTerminal.commands')} ·{' '}
                        {formatDate(s.created_at, { short: true, withTime: true })}
                        {s.is_active ? '' : ` · ${t('aiTerminal.closed')}`}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {canExecute && (
            <Card title={t('aiTerminal.newSession')}>
              <div className="space-y-3">
                <Field label={t('aiTerminal.sessionTitle')}>
                  <input
                    className="input"
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                  />
                </Field>
                <div>
                  <p className="label">{t('ai.permissionLevel')}</p>
                  <div className="space-y-1.5">
                    {LEVELS.map((level, index) => (
                      <label
                        key={level.value}
                        className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 text-xs ${
                          newLevel === level.value
                            ? LEVEL_TONE[level.value]
                            : 'border-shell-200 bg-white text-shell-600'
                        }`}
                      >
                        <input
                          type="radio"
                          name="permission-level"
                          className="mt-0.5"
                          checked={newLevel === level.value}
                          onChange={() => setNewLevel(level.value)}
                        />
                        <span>
                          <span className="flex items-center gap-1 font-semibold">
                            {index >= 3 && <ShieldAlert className="h-3 w-3" />}
                            {level.value}
                          </span>
                          <span className="block opacity-80">{t(level.labelKey)}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                  <p className="mt-2 text-2xs text-warn-700">{t('aiTerminal.escalationNote')}</p>
                </div>
                <button
                  type="button"
                  className="btn-primary w-full"
                  disabled={createSession.isPending}
                  onClick={() => createSession.mutate()}
                >
                  {createSession.isPending ? <Spinner /> : <Plus className="h-4 w-4" />}
                  {t('aiTerminal.newSession')}
                </button>
              </div>
            </Card>
          )}
        </div>

        {/* Transcript + composer */}
        <Card
          title={t('aiTerminal.transcript')}
          actions={
            active ? (
              <span className={LEVEL_BADGE[active.permission_level] ?? 'badge-muted'}>
                {active.permission_level}
              </span>
            ) : undefined
          }
          bodyClassName="p-0"
        >
          {!active ? (
            <EmptyState title={t('aiTerminal.pickSession')} />
          ) : (
            <>
              <div className="max-h-[52vh] space-y-3 overflow-y-auto bg-shell-950 p-4 font-mono text-xs text-shell-100">
                {transcript.length === 0 ? (
                  <p className="py-8 text-center text-shell-400">
                    {t('aiTerminal.emptyTranscript')}
                  </p>
                ) : (
                  transcript.map((entry) => {
                    const state = entryState(entry)
                    return (
                      <div
                        key={entry.key}
                        className="rounded-lg border border-shell-800 bg-shell-900 p-3"
                      >
                        <div className="mb-1.5 flex flex-wrap items-center gap-2">
                          <span className="text-brand-300">$</span>
                          <span className="flex-1 break-all text-shell-100">
                            {entry.result.command ??
                              entry.result.target ??
                              entry.request.instruction}
                          </span>
                          <span className="badge-muted">{entry.result.action_type}</span>
                          <span
                            className={
                              entry.rejected
                                ? 'badge-danger'
                                : state === 'blocked'
                                  ? 'badge-danger'
                                  : state === 'awaiting'
                                    ? 'badge-warn'
                                    : state === 'executed'
                                      ? 'badge-ok'
                                      : 'badge-info'
                            }
                          >
                            {entry.rejected
                              ? t('aiTerminal.reject')
                              : state === 'blocked'
                                ? t('aiTerminal.statusBlocked')
                                : state === 'awaiting'
                                  ? t('aiTerminal.statusAwaiting')
                                  : state === 'executed'
                                    ? t('aiTerminal.statusExecuted')
                                    : t('aiTerminal.statusAllowed')}
                          </span>
                        </div>

                        {state === 'blocked' && entry.result.block_reason && (
                          <p className="mb-2 flex items-start gap-2 rounded-md border border-danger-500/40 bg-danger-500/10 p-2 text-danger-200">
                            <AlertOctagon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <span>
                              <strong className="block">{t('aiTerminal.blockReason')}</strong>
                              {entry.result.block_reason}
                            </span>
                          </p>
                        )}

                        {state === 'awaiting' && !entry.rejected && (
                          <div className="mb-2 rounded-md border border-warn-500/40 bg-warn-500/10 p-2 text-warn-100">
                            <p className="mb-2 flex items-start gap-2">
                              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                              <span>{t('aiTerminal.approvePrompt')}</span>
                            </p>
                            <div className="flex flex-wrap gap-2">
                              <button
                                type="button"
                                className="btn-danger btn-sm"
                                disabled={run.isPending}
                                onClick={() => approve(entry)}
                              >
                                <Check className="h-3.5 w-3.5" />
                                {t('aiTerminal.approveRun')}
                              </button>
                              <button
                                type="button"
                                className="btn-secondary btn-sm"
                                onClick={() => reject(entry)}
                              >
                                <X className="h-3.5 w-3.5" />
                                {t('aiTerminal.reject')}
                              </button>
                            </div>
                          </div>
                        )}

                        {entry.rejected && (
                          <p className="mb-2 flex items-center gap-2 text-shell-400">
                            <CircleSlash className="h-3.5 w-3.5" />
                            {t('aiTerminal.rejected')}
                          </p>
                        )}

                        {entry.result.output && (
                          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-shell-300">
                            {entry.result.output}
                          </pre>
                        )}

                        <p className="tabular mt-1.5 text-2xs text-shell-500">
                          {entry.result.exit_code !== null &&
                            entry.result.exit_code !== undefined &&
                            `${t('aiTerminal.exitCode')} ${entry.result.exit_code} · `}
                          {t('aiTerminal.duration')} {formatNumber(entry.result.duration_ms)} ms ·{' '}
                          {entry.result.required_level}
                        </p>
                      </div>
                    )
                  })
                )}
                <div ref={endRef} />
              </div>

              <div className="space-y-3 border-t border-shell-200 p-4">
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label={t('aiTerminal.action')}>
                    <select
                      className="input"
                      value={action}
                      onChange={(e) => setAction(e.target.value)}
                    >
                      {ACTIONS.map((a) => (
                        <option key={a} value={a}>
                          {a}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label={t('aiTerminal.target')}>
                    <input
                      className="input font-mono"
                      value={target}
                      onChange={(e) => setTarget(e.target.value)}
                    />
                  </Field>
                </div>
                <Field label={t('aiTerminal.instruction')} required>
                  <input
                    className="input"
                    value={instruction}
                    onChange={(e) => setInstruction(e.target.value)}
                  />
                </Field>
                <Field label={t('aiTerminal.command')}>
                  <textarea
                    className="input font-mono"
                    rows={3}
                    value={command}
                    onChange={(e) => setCommand(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault()
                        submit()
                      }
                    }}
                  />
                </Field>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={!canExecute || run.isPending || !instruction.trim()}
                  onClick={submit}
                >
                  {run.isPending ? <Spinner /> : <Play className="h-4 w-4" />}
                  {run.isPending ? t('aiTerminal.running') : t('aiTerminal.run')}
                </button>
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  )
}
