/**
 * Eğitim Merkezi / Training Centre.
 *
 * Lessons in the order the system wants them learned, each with an estimated
 * duration and a completion tick.  Opening one shows the body in both
 * languages (field staff mix the two), the numbered steps, a link straight to
 * the screen each step is about, and the button that records completion.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Circle, Clock, ExternalLink, GraduationCap } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import {
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  Modal,
  PageHeader,
  Spinner,
  useToast,
} from '@/components/ui'
import { api } from '@/lib/api'
import { formatNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface LessonStep {
  index: number
  title: string
  detail: string
  title_tr?: string | null
  title_en?: string | null
  detail_tr?: string | null
  detail_en?: string | null
  screen?: string | null
}

interface Lesson {
  id: number
  code: string
  module?: string | null
  sort_order: number
  title: string
  title_tr: string
  title_en: string
  summary: string
  body?: string | null
  body_tr?: string | null
  body_en?: string | null
  target_route?: string | null
  estimated_minutes: number
  required_role?: string | null
  step_count: number
  steps: LessonStep[]
  is_completed: boolean
  progress_percent: number
  last_step: number
  completed_at?: string | null
}

interface TrainingSummary {
  total_lessons: number
  completed_lessons: number
  in_progress_lessons: number
  not_started_lessons: number
  completion_percent: number
  total_minutes: number
  last_activity_at?: string | null
}

/* -------------------------------------------------------------------------- */
/* Lesson modal                                                               */
/* -------------------------------------------------------------------------- */
function LessonView({ lessonId, onClose }: { lessonId: number; onClose: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const lang = currentLanguage()

  const lessonQuery = useQuery({
    queryKey: ['system', 'training', 'lesson', lessonId],
    queryFn: () => api.get<Lesson>(`/system/training/lessons/${lessonId}`),
  })

  const complete = useMutation({
    mutationFn: () =>
      api.post(`/system/training/lessons/${lessonId}/progress`, {
        is_completed: true,
        progress_percent: 100,
        last_step: lessonQuery.data?.step_count ?? 0,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'training'] })
      toast.push('success', t('sysTraining.marked'))
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const lesson = lessonQuery.data
  const primaryBody = lang === 'en' ? lesson?.body_en : lesson?.body_tr
  const secondaryBody = lang === 'en' ? lesson?.body_tr : lesson?.body_en

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={lesson?.title ?? t('sysTraining.title')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.close')}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => complete.mutate()}
            disabled={complete.isPending}
          >
            {complete.isPending ? <Spinner /> : <CheckCircle2 className="h-4 w-4" />}
            {t('sysTraining.markComplete')}
          </button>
        </>
      }
    >
      {lessonQuery.isLoading ? (
        <LoadingBlock />
      ) : lessonQuery.isError ? (
        <ErrorState error={lessonQuery.error} onRetry={() => void lessonQuery.refetch()} />
      ) : lesson ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2 text-2xs">
            <span className="badge-muted">{lesson.code}</span>
            {lesson.module && <span className="badge-info">{lesson.module}</span>}
            <span className="badge-muted">
              <Clock className="h-3 w-3" />
              {t('sysTraining.minutes', { count: lesson.estimated_minutes })}
            </span>
            {lesson.is_completed && (
              <span className="badge-ok">{t('sysTraining.lessonCompleted')}</span>
            )}
          </div>

          {lesson.summary && <p className="text-sm text-shell-600">{lesson.summary}</p>}

          {(primaryBody || lesson.body) && (
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-shell-700">
              {primaryBody || lesson.body}
            </p>
          )}
          {secondaryBody && secondaryBody !== primaryBody && (
            <p className="whitespace-pre-wrap border-l-2 border-shell-200 pl-3 text-xs leading-relaxed text-shell-400">
              {secondaryBody}
            </p>
          )}

          {lesson.steps.length > 0 && (
            <div>
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
                {t('sysTraining.steps')}
              </h4>
              <ol className="space-y-2">
                {lesson.steps.map((step) => (
                  <li
                    key={step.index}
                    className="rounded-lg border border-shell-200 bg-shell-50 p-3"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <p className="text-sm font-medium text-shell-800">
                        <span className="tabular mr-2 text-shell-400">{step.index}.</span>
                        {step.title}
                      </p>
                      {step.screen && (
                        <Link
                          to={step.screen}
                          className="btn-secondary btn-sm"
                          onClick={onClose}
                        >
                          <ExternalLink className="h-3.5 w-3.5" />
                          {t('sysTraining.goToScreen')}
                        </Link>
                      )}
                    </div>
                    {step.detail && (
                      <p className="mt-1 whitespace-pre-wrap text-xs text-shell-600">
                        {step.detail}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {lesson.target_route && (
            <Link to={lesson.target_route} className="btn-secondary btn-sm" onClick={onClose}>
              <ExternalLink className="h-3.5 w-3.5" />
              {t('sysTraining.goToScreen')}
            </Link>
          )}
        </div>
      ) : null}
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Training() {
  const { t } = useTranslation()
  const [openLesson, setOpenLesson] = useState<number | null>(null)

  const lessonsQuery = useQuery({
    queryKey: ['system', 'training', 'lessons'],
    queryFn: () => api.get<Lesson[]>('/system/training/lessons'),
  })

  const summaryQuery = useQuery({
    queryKey: ['system', 'training', 'summary'],
    queryFn: () => api.get<TrainingSummary>('/system/training/summary'),
  })

  const lessons = lessonsQuery.data ?? []
  const summary = summaryQuery.data
  const percent = Math.min(100, Math.max(0, summary?.completion_percent ?? 0))

  return (
    <div>
      <PageHeader
        title={t('sysTraining.title')}
        subtitle={t('sysTraining.subtitle')}
        icon={<GraduationCap className="h-5 w-5" />}
      />

      <Card title={t('sysTraining.progress')} className="mb-4">
        {summaryQuery.isLoading ? (
          <LoadingBlock />
        ) : summaryQuery.isError ? (
          <ErrorState error={summaryQuery.error} onRetry={() => void summaryQuery.refetch()} />
        ) : summary ? (
          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="tabular text-sm text-shell-700">
                {t('sysTraining.completedCount', {
                  done: summary.completed_lessons,
                  total: summary.total_lessons,
                })}
              </p>
              <span className="tabular text-sm font-semibold text-shell-800">
                {formatNumber(percent, { decimals: 1 })}%
              </span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-shell-100">
              <div
                className="h-full rounded-full bg-brand-600"
                style={{ width: `${percent}%` }}
              />
            </div>
            <p className="tabular mt-2 text-2xs text-shell-400">
              {t('sysTraining.totalMinutes', { count: summary.total_minutes })}
            </p>
          </div>
        ) : null}
      </Card>

      {lessonsQuery.isLoading ? (
        <LoadingBlock />
      ) : lessonsQuery.isError ? (
        <ErrorState error={lessonsQuery.error} onRetry={() => void lessonsQuery.refetch()} />
      ) : lessons.length === 0 ? (
        <EmptyState title={t('sysTraining.noLessons')} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {lessons.map((lesson) => (
            <button
              key={lesson.id}
              type="button"
              onClick={() => setOpenLesson(lesson.id)}
              className="card p-4 text-left transition-shadow hover:shadow-pop"
            >
              <div className="mb-2 flex items-start justify-between gap-2">
                <span className="badge-muted">{lesson.code}</span>
                {lesson.is_completed ? (
                  <CheckCircle2 className="h-5 w-5 text-ok-600" />
                ) : (
                  <Circle className="h-5 w-5 text-shell-300" />
                )}
              </div>
              <h3 className="text-sm font-semibold text-shell-800">{lesson.title}</h3>
              {lesson.summary && (
                <p className="mt-1 line-clamp-3 text-xs text-shell-500">{lesson.summary}</p>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-2 text-2xs text-shell-400">
                <span className="tabular flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {t('sysTraining.minutes', { count: lesson.estimated_minutes })}
                </span>
                {lesson.module && <span className="badge-info">{lesson.module}</span>}
                {lesson.step_count > 0 && (
                  <span className="tabular">
                    {formatNumber(lesson.step_count)} {t('sysTraining.steps')}
                  </span>
                )}
              </div>
              {lesson.progress_percent > 0 && !lesson.is_completed && (
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-shell-100">
                  <div
                    className="h-full rounded-full bg-brand-500"
                    style={{ width: `${Math.min(100, lesson.progress_percent)}%` }}
                  />
                </div>
              )}
              <span className="mt-3 inline-block text-xs font-medium text-brand-600">
                {lesson.is_completed ? t('sysTraining.review') : t('sysTraining.start')}
              </span>
            </button>
          ))}
        </div>
      )}

      {openLesson !== null && (
        <LessonView lessonId={openLesson} onClose={() => setOpenLesson(null)} />
      )}
    </div>
  )
}
