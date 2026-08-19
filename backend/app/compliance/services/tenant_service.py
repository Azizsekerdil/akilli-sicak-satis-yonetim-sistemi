"""
Kiracı çözümlemesi — uyumluluk API'sinin kapsam kökü.

Her uyumluluk kaydı bir kiracıya aittir. Ürün bugün tek şirketle çalışsa bile
kapsamı istekten çözmek zorundayız: yanlış kiracıya yazılmış bir rıza kaydı,
hiç yazılmamış bir kayıttan daha zararlıdır.

Çözümleme kuralı bilinçli olarak asimetriktir:

* **Okuma** yolları kiracı yaratmaz. Kiracı yoksa ``None`` döner ve uç nokta
  boş liste/„kurulmamış" durumu raporlar. Bir GET isteğinin yan etkiyle satır
  yaratması, denetim kaydını gürültüye boğar.
* **Yazma** yolları kiracıyı garanti eder ve yoksa varsayılan kiracıyı bir kez
  oluşturup bunu denetim kaydına yazar.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.enums import ComplianceRegime, TenantStatus
from app.compliance.models.tenant import Tenant
from app.core.deps import Ctx
from app.core.exceptions import NotFoundError
from app.models.organization import Company
from app.services import audit_service

#: Tek şirketli kurulumda kullanılan kiracı kodu.
DEFAULT_TENANT_CODE = "DEFAULT"


def resolve(db: Session, code: str | None = None) -> Tenant | None:
    """
    İstekteki kiracıyı bul; yoksa ``None``.

    ``code`` verilmediğinde ve tek bir kiracı varsa o kullanılır. Birden fazla
    kiracı varsa hiçbiri seçilmez: "muhtemelen bunu kastetti" tahmini, çok
    kiracılı bir kurulumda yanlış şirketin verisini göstermenin en kısa yoludur.
    """
    stmt = select(Tenant).where(Tenant.is_deleted.is_(False))
    if code:
        return db.execute(stmt.where(Tenant.code == code)).scalar_one_or_none()

    rows = db.execute(stmt.order_by(Tenant.id).limit(2)).scalars().all()
    return rows[0] if len(rows) == 1 else None


def get_or_404(db: Session, code: str) -> Tenant:
    tenant = resolve(db, code)
    if tenant is None:
        raise NotFoundError("compliance.tenant.not_found", params={"code": code})
    return tenant


def require(db: Session, ctx: Ctx, code: str | None = None) -> Tenant:
    """
    Yazma yolları için kiracıyı garanti et.

    Açıkça bir kod istendiyse ve bulunamıyorsa hata verilir — sessizce başka
    bir kiracıya yazmaktansa isteği reddetmek doğrudur. Hiç kiracı yoksa
    varsayılan kiracı oluşturulur ve bu da denetlenir.
    """
    if code:
        return get_or_404(db, code)

    tenant = resolve(db)
    if tenant is not None:
        return tenant

    existing = db.execute(
        select(Tenant).where(Tenant.code == DEFAULT_TENANT_CODE)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return _create_default(db, ctx)


def _create_default(db: Session, ctx: Ctx) -> Tenant:
    """
    Varsayılan kiracıyı, kullanıcının şirketinden okunabilenlerle oluştur.

    Şirket kaydından yalnızca ad ve ülke gibi doğrulanabilir alanlar taşınır.
    Sicil numarası, DPO ve rejim bilgisi **tahmin edilmez**: bunlar hukuki
    beyanlardır ve boş bırakılıp insan tarafından doldurulmaları gerekir.
    """
    company = (
        db.get(Company, ctx.user.company_id) if getattr(ctx.user, "company_id", None) else None
    )

    tenant = Tenant(
        code=DEFAULT_TENANT_CODE,
        name=company.name if company else "Default tenant",
        name_en=company.name_en if company else "Default tenant",
        legal_name=company.legal_name if company else None,
        company_id=company.id if company else None,
        status=TenantStatus.PENDING_SETUP,
        #: Hangi rejime tabi olunduğu bir hukuki değerlendirmedir; kurulum
        #: sırasında tahmin edilmez.
        primary_regime=ComplianceRegime.UNKNOWN,
        default_language=ctx.lang or "tr",
        created_by_id=ctx.user_id,
    )
    db.add(tenant)
    db.flush()

    audit_service.record(
        db,
        "CREATE",
        entity_type="cmp_tenant",
        entity_id=tenant.id,
        entity_label=tenant.code,
        summary="Compliance tenant bootstrapped",
        new_values={
            "code": tenant.code,
            "status": tenant.status,
            "primary_regime": tenant.primary_regime,
        },
        **ctx.audit_kwargs(),
    )
    return tenant


def brief(tenant: Tenant | None) -> dict[str, object] | None:
    """Kiracının API'de görünen özeti."""
    if tenant is None:
        return None
    return {
        "id": tenant.id,
        "code": tenant.code,
        "name": tenant.name,
        "legal_name": tenant.legal_name,
        "status": tenant.status,
        "primary_regime": tenant.primary_regime,
        "home_country": tenant.home_country,
        "default_language": tenant.default_language,
        "last_assessment_at": tenant.last_assessment_at,
    }
