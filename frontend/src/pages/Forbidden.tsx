import { ShieldOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

export default function Forbidden() {
  const { t } = useTranslation()
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
      <div className="rounded-full bg-danger-50 p-4 text-danger-600">
        <ShieldOff className="h-8 w-8" />
      </div>
      <h1 className="text-xl font-semibold text-shell-900">403</h1>
      <p className="max-w-sm text-sm text-shell-500">{t('errors.forbidden')}</p>
      <Link to="/" className="btn-primary">
        {t('nav.dashboard')}
      </Link>
    </div>
  )
}
