"""
Uyumluluk ve insan egemenliği uç noktaları.

Yetkilendirme mevcut ``require()`` bağımlılığıyla yapılır ve her uç nokta izin
kataloğundaki **kendi** kaynağına bağlanır (``compliance.*`` / ``hsp.*``).
Ayrıntılı gerekçe için aşağıdaki "Yetkilendirme" bölümüne bakınız: uyumluluk
kanıtına erişim, ticari veriye veya sistem ayarlarına erişimle aynı şey
değildir; bir DPO müşteri bakiyesini hiç görmeden işini yapabilmelidir.

İki tasarım kararı API sözleşmesinin tamamını belirler:

*   ``GET /compliance/overview`` **tek bir uyumluluk skoru döndürmez.**
    Kategori bazında durum, insan incelemesi bekleyen sayısı ve engelleyici
    gerekçeler döner. İnsan hakları duruşu tek bir gamification puanına
    indirgenmez.
*   Karar üreten hiçbir uç nokta sessizce izin vermez. HSP değerlendirmesi
    ``allow=False`` döndüğünde çağıranın eylemi durdurması beklenir; bekleyen,
    süresi dolmuş veya politikası bulunmayan her durum reddir.

Her yazma ucu ``audit_service.record(...)`` çağırır (servis katmanı içinden)
ve ürettiği kararı kanıt zincirine bağlar.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select

from app.compliance.models.hsp import Machine, RightsReceipt
from app.compliance.models.rules import RulePack
from app.compliance.enums import DsrType
from app.compliance.rule_enums import LifecycleStatus
from app.compliance.schemas import (
    ChainVerifyOut,
    ComplianceOverviewOut,
    ConsentIn,
    ConsentOut,
    ConsentWithdrawIn,
    DataFieldOut,
    DsrCreateIn,
    DsrFulfilIn,
    DsrIdentityIn,
    DsrOut,
    DsrTransitionIn,
    EvaluateIn,
    EvaluationOut,
    EvidenceOut,
    FieldReviewIn,
    HspDecisionOut,
    HspAppealIn,
    HspEvaluateIn,
    HspReceiptDetailOut,
    HspReceiptOut,
    NoticeIn,
    NoticeOut,
    ProcessingActivityIn,
    ProcessingActivityOut,
    ProcessingActivityUpdateIn,
    RulePackApproveIn,
    RulePackOut,
    RuleResultOut,
    ScanRequestIn,
    ScanResultOut,
    TransferIn,
    TransferOut,
    WithdrawalOut,
)
from app.compliance.services import (
    activity_service,
    consent_service,
    dsr_service,
    evidence_service,
    hsp_engine,
    inventory_service,
    overview_service,
    rule_engine,
    rulepack_loader,
    tenant_service,
)
from app.core.deps import Ctx, Page, get_page, paginated, require
from app.core.exceptions import BusinessRuleError, NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import loads
from app.models.base import utcnow

log = get_logger("app.api.compliance")

router = APIRouter(prefix="/compliance", tags=["compliance"])

# ===========================================================================
# Yetkilendirme
# ===========================================================================
# Her uç nokta, izin kataloğunda ilan edilmiş KENDİ kaynağına bağlanır.
#
# Önceki sürüm hepsini tek bir kaynağa (``system.settings``) bağlıyordu. Bunun
# iki somut sonucu vardı ve ikisi de yanlıştı:
#
# 1.  Katalogdaki ``compliance.*`` ve ``hsp.*`` izinleri hiçbir şey yapmıyordu.
#     Denetçi (AUDITOR) rolü tam olarak bu izinlerle donatılmıştı ama
#     ``system.settings:VIEW`` izni yoktu; menüde uyumluluk ekranlarını görüyor,
#     API'den 403 alıyordu. İlan edilen ama uygulanmayan izin, dekoratif
#     kontroldür.
# 2.  ``system.settings:UPDATE`` izni olan herkes — asıl işi AI sağlayıcı ayarı
#     değiştirmek olan bir rol dahil — hak politikası onaylayabiliyor, mevzuat
#     paketi aktive edebiliyordu. Ayrı izin ağacının bütün amacı buydu.
#
# Kural: okuma ``VIEW``, oluşturma ``CREATE``, değiştirme ``UPDATE``, onay
# ``APPROVE``, çalıştırma ``EXECUTE``. Onay ile değiştirme kasıtlı olarak
# ayrılmıştır: bir kuralı yürürlüğe sokmak, onu düzenlemekten farklı bir karardır.
#
# ---
#
# Every endpoint binds to the resource the permission catalogue actually
# declares for it, rather than to one shared ``system.settings`` gate. The old
# arrangement made the entire ``compliance.*``/``hsp.*`` permission tree
# decorative (the AUDITOR role held those permissions and was still refused)
# while simultaneously handing rule-pack approval to anyone who could edit a
# setting. Approval is kept distinct from update on purpose: putting a rule into
# force is a different decision from editing it.


def _gate(resource: str, action: str):
    """Dependency enforcing ``resource:action`` and returning the context."""

    def _dep(ctx: Ctx = Depends(require(resource, action))) -> Ctx:
        return ctx

    return _dep


# Overview
_overview_read = _gate("compliance.overview", "VIEW")

# Inventory / record of processing
_inventory_read = _gate("compliance.inventory", "VIEW")
_inventory_create = _gate("compliance.inventory", "CREATE")
_inventory_update = _gate("compliance.inventory", "UPDATE")
_inventory_execute = _gate("compliance.inventory", "EXECUTE")

# Notices & consent
_consent_read = _gate("compliance.consent", "VIEW")
_consent_create = _gate("compliance.consent", "CREATE")
_consent_update = _gate("compliance.consent", "UPDATE")

# Data-subject requests
_dsr_read = _gate("compliance.dsr", "VIEW")
_dsr_create = _gate("compliance.dsr", "CREATE")
_dsr_update = _gate("compliance.dsr", "UPDATE")
_dsr_approve = _gate("compliance.dsr", "APPROVE")

# Cross-border transfers
_transfer_read = _gate("compliance.transfers", "VIEW")
_transfer_write = _gate("compliance.transfers", "CREATE")

# Rule packs
_rulepack_read = _gate("compliance.rulepacks", "VIEW")
_rulepack_update = _gate("compliance.rulepacks", "UPDATE")
_rulepack_approve = _gate("compliance.rulepacks", "APPROVE")
_rulepack_execute = _gate("compliance.rulepacks", "EXECUTE")

# Evidence chain
_evidence_read = _gate("compliance.evidence", "VIEW")

# Human-control layer
_hsp_evaluate = _gate("hsp.evaluate", "EXECUTE")
_hsp_receipt_read = _gate("hsp.receipts", "VIEW")


def _tenant_id_or_none(ctx: Ctx, code: str | None) -> int | None:
    tenant = tenant_service.resolve(ctx.db, code)
    return tenant.id if tenant else None


def _empty_page(page: Page) -> dict[str, Any]:
    """
    Kiracı henüz kurulmamışken listeler boş döner.

    Okuma isteği kiracı yaratmaz: bir GET'in yan etkiyle yapılandırma satırı
    üretmesi, denetim kaydını gürültüye boğar ve "kim bu kiracıyı açtı"
    sorusunu cevaplanamaz hâle getirir.
    """
    return paginated([], 0, page)


# ===========================================================================
# Genel durum
# ===========================================================================
@router.get(
    "/overview",
    response_model=ComplianceOverviewOut,
    summary="Uyumluluk durum tablosu (tek skor DÖNMEZ)",
)
def overview(
    ctx: Ctx = Depends(_overview_read),
    tenant: str | None = Query(default=None, description="Kiracı kodu"),
) -> Any:
    resolved = tenant_service.resolve(ctx.db, tenant)
    if resolved is None:
        return ComplianceOverviewOut(
            generated_at=utcnow(),
            tenant=None,
            categories=[],
            totals={"categories": 0},
            review_queue=["tenant_not_configured"],
        )

    data = overview_service.build(ctx.db, tenant_id=resolved.id)
    data["tenant"] = tenant_service.brief(resolved)
    return data


# ===========================================================================
# Envanter
# ===========================================================================
@router.get("/inventory/fields", summary="Kişisel veri envanteri (sayfalı)")
def list_inventory_fields(
    ctx: Ctx = Depends(_inventory_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    sensitivity: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    table_name: str | None = Query(default=None),
    term: str | None = Query(default=None),
    present_only: bool = Query(default=False),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = inventory_service.list_fields(
        ctx.db,
        tenant_id=tenant_id,
        sensitivity=sensitivity,
        review_status=review_status,
        table_name=table_name,
        term=term,
        present_only=present_only,
        offset=page.offset,
        limit=page.limit,
    )
    items = [DataFieldOut(**inventory_service.field_to_dict(r)) for r in rows]
    return paginated(items, total, page)


@router.get(
    "/inventory/summary",
    summary="Envanter özeti (tablo/alan/tanımlayıcı sayıları)",
)
def inventory_summary(
    ctx: Ctx = Depends(_inventory_read),
    tenant: str | None = Query(default=None),
) -> dict[str, Any]:
    """
    Envanterin sayısal özeti — envanter ekranının üst şeridini besler.

    Her sayı taramanın yazdığı satırlardan **ölçülür**; hiçbiri elle
    girilmez. Kiracı kurulmamışken sıfırlarla döner ve ``scanned=false``
    taşır: "hiç kişisel veri yok" ile "henüz taranmadı" aynı şey değildir ve
    ekranın ikisini karıştırmaması gerekir.
    """
    empty = {
        "tables": 0,
        "fields": 0,
        "direct_identifiers": 0,
        "location_fields": 0,
        "special_category_candidates": 0,
        "unknown_lawful_basis": 0,
        "unknown_retention": 0,
        "review_required": 0,
        "scanned": False,
    }
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return empty
    return inventory_service.field_summary(ctx.db, tenant_id=tenant_id)


@router.post(
    "/inventory/scan",
    response_model=ScanResultOut,
    summary="Keşif taramasını çalıştır ve envanteri güncelle",
)
def run_inventory_scan(
    payload: ScanRequestIn = Body(default_factory=ScanRequestIn),
    ctx: Ctx = Depends(_inventory_execute),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    result = inventory_service.run_scan(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        full=payload.full,
        seed_transfers=payload.seed_transfers,
        note=payload.note,
    )
    ctx.db.commit()
    return ScanResultOut(**result)


@router.post(
    "/inventory/fields/{field_id}/review",
    response_model=DataFieldOut,
    summary="Envanter alanı için insan incelemesi kaydet",
)
def review_inventory_field(
    field_id: int,
    payload: FieldReviewIn,
    ctx: Ctx = Depends(_inventory_update),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    field = inventory_service.review_field(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        field_id=field_id,
        review_status=payload.review_status,
        sensitivity=str(payload.sensitivity) if payload.sensitivity else None,
        identifiability=str(payload.identifiability) if payload.identifiability else None,
        notes=payload.notes,
    )
    ctx.db.commit()
    return DataFieldOut(**inventory_service.field_to_dict(field))


# ===========================================================================
# İşleme faaliyetleri
# ===========================================================================
@router.get("/processing-activities", summary="İşleme faaliyetleri (sayfalı)")
def list_processing_activities(
    ctx: Ctx = Depends(_inventory_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    review_only: bool = Query(default=False),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = activity_service.list_activities(
        ctx.db,
        tenant_id=tenant_id,
        code=code,
        state=state,
        review_only=review_only,
        offset=page.offset,
        limit=page.limit,
    )
    items = [
        ProcessingActivityOut(
            **activity_service.activity_to_dict(
                row, reasons=activity_service.activity_review_reasons(ctx.db, row)
            )
        )
        for row in rows
    ]
    return paginated(items, total, page)


@router.post(
    "/processing-activities",
    response_model=ProcessingActivityOut,
    status_code=201,
    summary="İşleme faaliyeti oluştur",
)
def create_processing_activity(
    payload: ProcessingActivityIn,
    ctx: Ctx = Depends(_inventory_create),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    activity, reasons = activity_service.create_activity(
        ctx.db, ctx, tenant_id=resolved.id, data=payload.model_dump(exclude_none=True)
    )
    ctx.db.commit()
    return ProcessingActivityOut(
        **activity_service.activity_to_dict(activity, reasons=reasons)
    )


@router.put(
    "/processing-activities/{activity_id}",
    response_model=ProcessingActivityOut,
    summary="İşleme faaliyetini güncelle",
)
def update_processing_activity(
    activity_id: int,
    payload: ProcessingActivityUpdateIn,
    ctx: Ctx = Depends(_inventory_update),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    data = payload.model_dump(exclude_none=True)
    # Kod kaydın kimliğidir; gövdeden gelen değer yok sayılır ki bir güncelleme
    # sessizce başka bir faaliyete dönüşmesin.
    data.pop("code", None)
    activity, reasons = activity_service.update_activity(
        ctx.db, ctx, tenant_id=resolved.id, activity_id=activity_id, data=data
    )
    ctx.db.commit()
    return ProcessingActivityOut(
        **activity_service.activity_to_dict(activity, reasons=reasons)
    )


# ===========================================================================
# Aydınlatma metinleri
# ===========================================================================
@router.get("/notices", summary="Aydınlatma metni sürümleri (sayfalı)")
def list_notices(
    ctx: Ctx = Depends(_consent_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    notice_code: str | None = Query(default=None),
    language: str | None = Query(default=None),
    status: str | None = Query(default=None),
    current_only: bool = Query(default=False),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = consent_service.list_notices(
        ctx.db,
        tenant_id=tenant_id,
        notice_code=notice_code,
        language=language,
        status=status,
        current_only=current_only,
        offset=page.offset,
        limit=page.limit,
    )
    items = [NoticeOut(**consent_service.notice_to_dict(r)) for r in rows]
    return paginated(items, total, page)


@router.get(
    "/notices/{notice_id}",
    response_model=NoticeOut,
    summary="Aydınlatma metni ayrıntısı (gövde dâhil)",
)
def get_notice(
    notice_id: int,
    ctx: Ctx = Depends(_consent_read),
    tenant: str | None = Query(default=None),
) -> Any:
    """
    Tek bir aydınlatma metni sürümü.

    Gövde yalnızca burada döner; listede taşınmaz. Bir aydınlatma metninin
    değeri tam metnindedir, ama yüzlerce sürümün gövdesini listeye koymak
    hem ağı hem ekranı boğar.
    """
    from app.compliance.models.consent import NoticeVersion

    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        raise NotFoundError("compliance.notice.not_found", params={"id": notice_id})
    notice = ctx.db.execute(
        select(NoticeVersion).where(
            NoticeVersion.id == notice_id, NoticeVersion.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if notice is None:
        raise NotFoundError("compliance.notice.not_found", params={"id": notice_id})
    return NoticeOut(**consent_service.notice_to_dict(notice, include_body=True))


@router.post(
    "/notices",
    response_model=NoticeOut,
    status_code=201,
    summary="Aydınlatma metninin yeni sürümünü yaz",
)
def create_notice(
    payload: NoticeIn,
    ctx: Ctx = Depends(_consent_create),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    notice = consent_service.publish_notice(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        notice_code=payload.notice_code,
        title=payload.title,
        body=payload.body,
        language=payload.language,
        kind=str(payload.kind),
        covered_activity_codes=payload.covered_activity_codes,
        display_url=payload.display_url,
        display_channel=str(payload.display_channel),
        effective_from=payload.effective_from,
        publish=payload.publish,
    )
    ctx.db.commit()
    return NoticeOut(**consent_service.notice_to_dict(notice, include_body=True))


# ===========================================================================
# Rıza
# ===========================================================================
@router.get("/consents", summary="Rıza kayıtları (sayfalı)")
def list_consents(
    ctx: Ctx = Depends(_consent_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    subject_ref: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    purpose_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
    current_only: bool = Query(default=True),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = consent_service.list_consents(
        ctx.db,
        tenant_id=tenant_id,
        subject_ref=subject_ref,
        subject_type=subject_type,
        purpose_code=purpose_code,
        status=status,
        current_only=current_only,
        offset=page.offset,
        limit=page.limit,
    )
    items = [
        ConsentOut(**consent_service.consent_to_dict(record, code))
        for record, code in rows
    ]
    return paginated(items, total, page)


@router.post(
    "/consents", response_model=ConsentOut, status_code=201, summary="Rıza kaydı oluştur"
)
def create_consent(
    payload: ConsentIn,
    ctx: Ctx = Depends(_consent_create),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    record = consent_service.record_consent(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        subject_type=payload.subject_type,
        subject_ref=payload.subject_ref,
        purpose_code=payload.purpose_code,
        purpose_name=payload.purpose_name,
        status=payload.status,
        is_explicit=payload.is_explicit,
        channel=str(payload.channel),
        notice_version_id=payload.notice_version_id,
        scope_text=payload.scope_text,
        scope_codes=payload.scope_codes,
        collected_at=payload.collected_at,
        expires_at=payload.expires_at,
        proof_reference=payload.proof_reference,
        language=payload.language,
    )
    ctx.db.commit()
    return ConsentOut(**consent_service.consent_to_dict(record, payload.purpose_code))


@router.post(
    "/consents/{consent_id}/withdraw",
    response_model=WithdrawalOut,
    summary="Rızayı geri al",
)
def withdraw_consent(
    consent_id: int,
    payload: ConsentWithdrawIn = Body(default_factory=ConsentWithdrawIn),
    ctx: Ctx = Depends(_consent_update),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    withdrawal = consent_service.withdraw(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        consent_id=consent_id,
        reason=str(payload.reason),
        reason_text=payload.reason_text,
        channel=str(payload.channel),
        requested_at=payload.requested_at,
        effective_at=payload.effective_at,
        triggers_erasure=payload.triggers_erasure,
    )
    ctx.db.commit()
    return WithdrawalOut.model_validate(withdrawal)


# ===========================================================================
# İlgili kişi başvuruları
# ===========================================================================
@router.get("/dsr", summary="İlgili kişi başvuruları (sayfalı)")
def list_dsr(
    ctx: Ctx = Depends(_dsr_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    request_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    subject_ref: str | None = Query(default=None),
    open_only: bool = Query(default=False),
    overdue_only: bool = Query(default=False),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = dsr_service.list_requests(
        ctx.db,
        tenant_id=tenant_id,
        request_type=request_type,
        status=status,
        subject_ref=subject_ref,
        open_only=open_only,
        overdue_only=overdue_only,
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([DsrOut(**row) for row in rows], total, page)


@router.post("/dsr", response_model=DsrOut, status_code=201, summary="Başvuru kaydet")
def create_dsr(
    payload: DsrCreateIn,
    ctx: Ctx = Depends(_dsr_create),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    request = dsr_service.create_request(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        subject_type=payload.subject_type,
        request_type=str(payload.request_type),
        subject_ref=payload.subject_ref,
        subject_contact=payload.subject_contact,
        channel=str(payload.channel),
        received_at=payload.received_at,
        due_at=payload.due_at,
        due_basis=payload.due_basis,
        description=payload.description,
        requested_scope=payload.requested_scope,
        assigned_to_user_id=payload.assigned_to_user_id,
        submitted_by_agent=payload.submitted_by_agent,
        agent_name=payload.agent_name,
    )
    ctx.db.commit()
    return DsrOut(**dsr_service.request_to_dict(request))


@router.get("/dsr/{request_id}", response_model=DsrOut, summary="Başvuru ayrıntısı")
def get_dsr(
    request_id: int,
    ctx: Ctx = Depends(_dsr_read),
    tenant: str | None = Query(default=None),
) -> Any:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        raise NotFoundError("compliance.dsr.not_found", params={"id": request_id})
    return DsrOut(**dsr_service.get_detail(ctx.db, tenant_id=tenant_id, request_id=request_id))


@router.post(
    "/dsr/{request_id}/identity",
    response_model=DsrOut,
    summary="Kimlik doğrulama denemesi kaydet",
)
def verify_dsr_identity(
    request_id: int,
    payload: DsrIdentityIn,
    ctx: Ctx = Depends(_dsr_update),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    dsr_service.verify_identity(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        request_id=request_id,
        method=str(payload.method),
        outcome=str(payload.outcome),
        document_type=payload.document_type,
        failure_reason=payload.failure_reason,
        notes=payload.notes,
    )
    ctx.db.commit()
    return DsrOut(
        **dsr_service.get_detail(ctx.db, tenant_id=resolved.id, request_id=request_id)
    )


@router.post(
    "/dsr/{request_id}/transition",
    response_model=DsrOut,
    summary="Açık başvuruyu bir sonraki çalışma durumuna taşı",
)
def transition_dsr(
    request_id: int,
    payload: DsrTransitionIn,
    ctx: Ctx = Depends(_dsr_update),
    tenant: str | None = Query(default=None),
) -> Any:
    """
    Durum değişikliği — kapanış hariç.

    Kapanış ``/fulfil`` üzerinden yürür: orada gerekçe zorunlu, kimlik
    doğrulaması şart ve kapanış kanıtı zincire yazılıyor.
    """
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    dsr_service.transition(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        request_id=request_id,
        to_status=payload.to_status,
        note=payload.note,
    )
    ctx.db.commit()
    return DsrOut(
        **dsr_service.get_detail(ctx.db, tenant_id=resolved.id, request_id=request_id)
    )


@router.post("/dsr/{request_id}/fulfil", response_model=DsrOut, summary="Başvuruyu kapat")
def fulfil_dsr(
    request_id: int,
    payload: DsrFulfilIn = Body(default_factory=DsrFulfilIn),
    ctx: Ctx = Depends(_dsr_approve),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    dsr_service.fulfil(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        request_id=request_id,
        outcome=payload.outcome,
        response_summary=payload.response_summary,
        response_channel=str(payload.response_channel),
        rejection_reason=payload.rejection_reason,
    )
    ctx.db.commit()
    return DsrOut(
        **dsr_service.get_detail(ctx.db, tenant_id=resolved.id, request_id=request_id)
    )


# ===========================================================================
# Yurt dışı aktarımlar
# ===========================================================================
@router.get("/transfers", summary="Yurt dışı aktarım kayıtları (sayfalı)")
def list_transfers(
    ctx: Ctx = Depends(_transfer_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    mechanism: str | None = Query(default=None),
    country: str | None = Query(default=None),
    review_only: bool = Query(default=False),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = activity_service.list_transfers(
        ctx.db,
        tenant_id=tenant_id,
        mechanism=mechanism,
        country=country,
        review_only=review_only,
        offset=page.offset,
        limit=page.limit,
    )
    items = [TransferOut(**activity_service.transfer_to_dict(r)) for r in rows]
    return paginated(items, total, page)


@router.post(
    "/transfers",
    response_model=TransferOut,
    summary="Aktarım kaydını oluştur veya güncelle",
)
def upsert_transfer(
    payload: TransferIn,
    ctx: Ctx = Depends(_transfer_write),
    tenant: str | None = Query(default=None),
) -> Any:
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    transfer, _ = activity_service.upsert_transfer(
        ctx.db, ctx, tenant_id=resolved.id, data=payload.model_dump(exclude_none=True)
    )
    ctx.db.commit()
    return TransferOut(**activity_service.transfer_to_dict(transfer))


# ===========================================================================
# Kural paketleri
# ===========================================================================
def _pack_out(ctx: Ctx, pack: RulePack) -> RulePackOut:
    approvals = rulepack_loader.effective_approvals(ctx.db, pack)
    return RulePackOut(
        id=pack.id,
        pack_key=pack.pack_key,
        version=pack.version,
        schema_version=pack.schema_version,
        jurisdiction=pack.jurisdiction,
        regulation_code=pack.regulation_code,
        title_tr=pack.title_tr,
        title_en=pack.title_en,
        status=pack.status,
        content_hash=pack.content_hash,
        source_hash=pack.source_hash,
        rule_count=pack.rule_count,
        requires_human_review=pack.requires_human_review,
        retrieved_date=pack.retrieved_date,
        effective_from=pack.effective_from,
        effective_to=pack.effective_to,
        activated_at=pack.activated_at,
        activated_by_id=pack.activated_by_id,
        withdrawn_at=pack.withdrawn_at,
        created_at=pack.created_at,
        has_effective_approval=bool(approvals),
        approval_count=len(approvals),
    )


@router.get(
    "/rulepacks/{pack_id}",
    response_model=RulePackOut,
    summary="Kural paketi ayrıntısı",
)
def get_rulepack(pack_id: int, ctx: Ctx = Depends(_rulepack_read)) -> Any:
    pack = ctx.db.get(RulePack, pack_id)
    if pack is None:
        raise NotFoundError("compliance.rulepack.not_found", params={"id": pack_id})
    return _pack_out(ctx, pack)


@router.get("/rulepacks", summary="Kural paketleri (sayfalı)")
def list_rulepacks(
    ctx: Ctx = Depends(_rulepack_read),
    page: Page = Depends(get_page),
    status: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
    pack_key: str | None = Query(default=None),
) -> dict[str, Any]:
    conds: list[Any] = []
    if status:
        conds.append(RulePack.status == status)
    if jurisdiction:
        conds.append(RulePack.jurisdiction == jurisdiction)
    if pack_key:
        conds.append(RulePack.pack_key == pack_key)

    rows = (
        ctx.db.execute(
            select(RulePack).where(*conds).order_by(RulePack.pack_key, RulePack.id.desc())
        )
        .scalars()
        .all()
    )
    window = rows[page.offset : page.offset + page.limit]
    return paginated([_pack_out(ctx, p) for p in window], len(rows), page)


@router.post(
    "/rulepacks/{pack_id}/approve",
    response_model=RulePackOut,
    summary="Kural paketine insan kararı ver",
)
def approve_rulepack(
    pack_id: int,
    payload: RulePackApproveIn,
    ctx: Ctx = Depends(_rulepack_approve),
) -> Any:
    pack = ctx.db.get(RulePack, pack_id)
    if pack is None:
        raise NotFoundError("compliance.rulepack.not_found", params={"id": pack_id})

    # Taslak bir paket doğrudan onaylanamaz; önce incelemeye alınır. Bu adımı
    # istemciye bırakmak, "onayla" düğmesinin incelemeyi atlamasına yol açardı.
    if pack.status == LifecycleStatus.DRAFT:
        rulepack_loader.submit_for_review(ctx.db, pack, user_id=ctx.user_id)

    rulepack_loader.record_decision(
        ctx.db,
        pack,
        approver_id=ctx.user_id,
        decision=str(payload.decision),
        approver_role=str(payload.approver_role),
        approver_name=ctx.user.full_name,
        comment=payload.comment,
        evidence_url=payload.evidence_url,
    )
    ctx.db.commit()
    return _pack_out(ctx, pack)


@router.post(
    "/rulepacks/{pack_id}/activate",
    response_model=RulePackOut,
    summary="Onaylanmış kural paketini yürürlüğe al",
)
def activate_rulepack(pack_id: int, ctx: Ctx = Depends(_rulepack_update)) -> Any:
    pack = ctx.db.get(RulePack, pack_id)
    if pack is None:
        raise NotFoundError("compliance.rulepack.not_found", params={"id": pack_id})

    rulepack_loader.activate(ctx.db, pack, activated_by_id=ctx.user_id)
    ctx.db.commit()
    return _pack_out(ctx, pack)


@router.post(
    "/evaluate",
    response_model=EvaluationOut,
    summary="Bir bağlamı yürürlükteki kural paketine karşı değerlendir",
)
def evaluate_context(
    payload: EvaluateIn,
    ctx: Ctx = Depends(_rulepack_execute),
    tenant: str | None = Query(default=None),
) -> Any:
    """
    Değerlendirme yalnızca **yürürlükteki** (``ACTIVE``) bir paketle yapılır.

    Onaylanmamış bir paketle üretilen sonuç, dayanağı olmayan bir iddiadır;
    bu yüzden paket bulunamadığında istek hata döner. Sessizce "uyumlu"
    dönmek, uyumluluk katmanının yapabileceği en zararlı davranıştır.
    """
    resolved = tenant_service.require(ctx.db, ctx, tenant)
    if not payload.persist:
        # Deneme çalıştırması sonunda oturum geri alınır. Kiracı kurulumu bu
        # geri almanın kapsamına girmemeli: yapılandırma satırının varlığı
        # değerlendirmenin denenmiş olmasına bağlı değildir.
        ctx.db.commit()

    if payload.pack_key:
        pack = rulepack_loader.active_pack(ctx.db, pack_key=payload.pack_key)
        if pack is None:
            raise BusinessRuleError(
                "compliance.rulepack.no_active_version",
                params={"pack_key": payload.pack_key},
            )
    else:
        packs = rulepack_loader.active_packs(ctx.db, jurisdiction=payload.jurisdiction)
        if not packs:
            raise BusinessRuleError(
                "compliance.rulepack.none_active",
                params={"jurisdiction": payload.jurisdiction or "ANY"},
                detail="No approved and activated rule pack is available to evaluate against.",
            )
        if len(packs) > 1:
            raise ValidationError(
                "compliance.evaluate.pack_key_required",
                params={"candidates": ",".join(p.pack_key for p in packs)},
            )
        pack = packs[0]

    context = dict(payload.context)
    context.setdefault("tenant_id", resolved.id)

    evaluations = rule_engine.evaluate_pack(
        ctx.db,
        pack=pack,
        context=context,
        tenant_id=resolved.id,
        persist=payload.persist,
    )
    summary = rule_engine.summarise(evaluations)

    if payload.persist:
        ctx.db.commit()
    else:
        # Deneme çalıştırmasında hiçbir satır kalıcı olmamalıdır.
        ctx.db.rollback()

    results = [
        RuleResultOut(
            rule_id=row.rule_id,
            article_ref=row.article_ref,
            outcome=row.outcome,
            severity=row.severity,
            confidence=row.confidence,
            requires_human_review=row.requires_human_review,
            applicability_result=row.applicability_result,
            condition_result=row.condition_result,
            missing_evidence=loads(row.missing_evidence, []) or [],
            matched_exceptions=loads(row.matched_exceptions, []) or [],
            triggered_reviews=loads(row.triggered_reviews, []) or [],
            reasons=loads(row.reasons, []) or [],
            evaluation_id=row.id if payload.persist else None,
        )
        for row in evaluations
    ]

    return EvaluationOut(
        evaluated_at=(evaluations[0].evaluated_at if evaluations else utcnow()),
        pack_key=pack.pack_key,
        pack_version=pack.version,
        pack_status=pack.status,
        total=summary["total"],
        counts=summary["counts"],
        needs_attention=summary["needs_attention"],
        # Tek bir kural bile insan incelemesi istiyorsa sonuç insan onayı
        # olmadan rapora giremez.
        human_review_required=any(r.requires_human_review for r in results) or not results,
        engine_version=summary["engine_version"],
        persisted=payload.persist,
        results=results,
    )


# ===========================================================================
# Kanıt
# ===========================================================================
@router.get("/evidence", summary="Kanıt kayıtları (sayfalı)")
def list_evidence(
    ctx: Ctx = Depends(_evidence_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: int | None = Query(default=None),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    rows, total = evidence_service.list_artifacts(
        ctx.db,
        tenant_id=tenant_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        offset=page.offset,
        limit=page.limit,
    )
    items = [EvidenceOut(**evidence_service.to_dict(r)) for r in rows]
    return paginated(items, total, page)


@router.get(
    "/evidence/verify",
    response_model=ChainVerifyOut,
    summary="Kanıt zincirinin bütünlüğünü doğrula",
)
def verify_evidence_chain(
    ctx: Ctx = Depends(_evidence_read),
    tenant: str | None = Query(default=None),
) -> Any:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        # Kiracı yoksa doğrulanacak zincir de yoktur; bu "geçerli" değil,
        # "doğrulanmamış"tır.
        return ChainVerifyOut(
            valid=True, checked=0, status="UNVERIFIED", reason="tenant_not_configured"
        )
    return ChainVerifyOut(**evidence_service.verify(ctx.db, tenant_id=tenant_id))


# ===========================================================================
# Human Sovereignty Protocol
# ===========================================================================
@router.post(
    "/hsp/evaluate",
    response_model=HspDecisionOut,
    summary="Bir makine eyleminin yapılabilirliğini değerlendir",
)
def hsp_evaluate(
    payload: HspEvaluateIn,
    ctx: Ctx = Depends(_hsp_evaluate),
    tenant: str | None = Query(default=None),
) -> Any:
    """
    Karar motorun kendisinden gelir ve her çağrı bir makbuz bırakır —
    reddedilenler dâhil. Yalnızca izin verilenleri kaydeden bir sistem, kaç kez
    reddettiğini bilemez ve denetlenemez.

    Kayıtlı olmayan bir makine için istek reddedilir: beyan edilmemiş bir
    bileşenin insan üzerinde işlem yapmasına izin verilemez.
    """
    resolved = tenant_service.require(ctx.db, ctx, tenant)

    machine = ctx.db.execute(
        select(Machine).where(
            Machine.tenant_id == resolved.id, Machine.code == payload.machine_code
        )
    ).scalar_one_or_none()
    if machine is None:
        raise NotFoundError(
            "compliance.hsp.machine_not_registered",
            params={"machine_code": payload.machine_code},
        )

    context = dict(payload.context)
    context.setdefault("tenant_id", resolved.id)
    if payload.purpose:
        context.setdefault("purpose", payload.purpose)

    decision = hsp_engine.evaluate(
        ctx.db,
        subject_ref=payload.subject_ref,
        machine_id=machine.id,
        manifest=payload.action_code,
        context=context,
    )
    ctx.db.commit()
    return HspDecisionOut(**decision.to_dict())


@router.get("/hsp/receipts", summary="Hak makbuzları (sayfalı)")
def list_hsp_receipts(
    ctx: Ctx = Depends(_hsp_receipt_read),
    page: Page = Depends(get_page),
    tenant: str | None = Query(default=None),
    subject_ref: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    action_code: str | None = Query(default=None),
) -> dict[str, Any]:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return _empty_page(page)

    conds: list[Any] = [RightsReceipt.tenant_id == tenant_id]
    if subject_ref:
        conds.append(RightsReceipt.subject_ref == subject_ref)
    if verdict:
        conds.append(RightsReceipt.verdict == verdict)
    if action_code:
        conds.append(RightsReceipt.action_code == action_code)

    rows = (
        ctx.db.execute(select(RightsReceipt).where(*conds).order_by(RightsReceipt.id.desc()))
        .scalars()
        .all()
    )
    window = rows[page.offset : page.offset + page.limit]
    items = [
        HspReceiptOut(
            id=r.id,
            tenant_id=r.tenant_id,
            request_id=r.request_id,
            subject_ref=r.subject_ref,
            machine_id=r.machine_id,
            machine_code=r.machine_code,
            action_code=r.action_code,
            domain=r.domain,
            question=r.question,
            verdict=r.verdict,
            allow=r.allow,
            reasons=r.reasons(),
            policy_id=r.policy_id,
            policy_code=r.policy_code,
            policy_version=r.policy_version,
            capability_token_id=r.capability_token_id,
            override_id=r.override_id,
            appeal_path=r.appeal_path,
            human_review_required=r.human_review_required,
            decided_at=r.decided_at,
            previous_hash=r.previous_hash,
            content_hash=r.content_hash,
        )
        for r in window
    ]
    return paginated(items, len(rows), page)


#: İtirazın makbuza bağlandığı işaret. Serbest metin içinde aranabilir olması
#: kasıtlı: makbuz tablosuna sütun eklemek, salt-eklenir zinciri değiştirmek
#: anlamına gelirdi.
_APPEAL_MARKER = "HSP-RECEIPT#"


def _receipt_fields(r: RightsReceipt) -> dict[str, Any]:
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "request_id": r.request_id,
        "subject_ref": r.subject_ref,
        "machine_id": r.machine_id,
        "machine_code": r.machine_code,
        "action_code": r.action_code,
        "domain": r.domain,
        "question": r.question,
        "verdict": r.verdict,
        "allow": r.allow,
        "reasons": r.reasons(),
        "policy_id": r.policy_id,
        "policy_code": r.policy_code,
        "policy_version": r.policy_version,
        "capability_token_id": r.capability_token_id,
        "override_id": r.override_id,
        "appeal_path": r.appeal_path,
        "human_review_required": r.human_review_required,
        "decided_at": r.decided_at,
        "previous_hash": r.previous_hash,
        "content_hash": r.content_hash,
    }


def _find_appeal(ctx: Ctx, receipt: RightsReceipt):
    """The data-subject request filed against this receipt, if there is one."""
    from app.compliance.models.dsr import DataSubjectRequest

    return ctx.db.execute(
        select(DataSubjectRequest)
        .where(
            DataSubjectRequest.tenant_id == receipt.tenant_id,
            DataSubjectRequest.description.like(f"%{_APPEAL_MARKER}{receipt.id}%"),
        )
        .order_by(DataSubjectRequest.id.desc())
    ).scalars().first()


def _receipt_detail(ctx: Ctx, receipt: RightsReceipt) -> HspReceiptDetailOut:
    data = _receipt_fields(receipt)
    appeal = _find_appeal(ctx, receipt)
    if appeal is not None:
        data.update(
            appeal_reference=appeal.reference,
            appeal_reason=appeal.description,
            appeal_contact=appeal.subject_contact,
            appeal_channel=appeal.channel,
            appeal_submitted_at=appeal.received_at,
            appeal_outcome=appeal.status,
            appeal_decided_at=appeal.responded_at,
        )
    return HspReceiptDetailOut(**data)


def _get_receipt(ctx: Ctx, receipt_id: int, tenant: str | None) -> RightsReceipt:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    conds: list[Any] = [RightsReceipt.id == receipt_id]
    if tenant_id is not None:
        conds.append(RightsReceipt.tenant_id == tenant_id)
    receipt = ctx.db.execute(select(RightsReceipt).where(*conds)).scalar_one_or_none()
    if receipt is None:
        raise NotFoundError("compliance.hsp.receipt_not_found", params={"id": receipt_id})
    return receipt


@router.get(
    "/hsp/receipts/verify",
    response_model=ChainVerifyOut,
    summary="Makbuz zincirinin bütünlüğünü doğrula",
)
def verify_receipt_chain(
    ctx: Ctx = Depends(_hsp_receipt_read),
    tenant: str | None = Query(default=None),
) -> Any:
    tenant_id = _tenant_id_or_none(ctx, tenant)
    if tenant_id is None:
        return ChainVerifyOut(
            valid=True, checked=0, status="UNVERIFIED", reason="tenant_not_configured"
        )
    return ChainVerifyOut(**hsp_engine.verify_chain(ctx.db, tenant_id=tenant_id))


# NOTE ON ORDER: the two routes below sit *after* ``/hsp/receipts/verify`` on
# purpose.  FastAPI matches in declaration order, so a ``{receipt_id}`` route
# declared first swallows the literal ``/verify`` path and the chain-integrity
# endpoint starts answering 422 "not an integer".  Keep literal segments ahead
# of the parameterised ones.

@router.get(
    "/hsp/receipts/{receipt_id}",
    response_model=HspReceiptDetailOut,
    summary="Makbuz ayrıntısı (varsa itiraz durumu ile)",
)
def get_hsp_receipt(
    receipt_id: int,
    ctx: Ctx = Depends(_hsp_receipt_read),
    tenant: str | None = Query(default=None),
) -> Any:
    return _receipt_detail(ctx, _get_receipt(ctx, receipt_id, tenant))


@router.post(
    "/hsp/receipts/{receipt_id}/appeal",
    response_model=HspReceiptDetailOut,
    status_code=201,
    summary="Karara itiraz et (ilgili kişi başvurusu olarak kaydedilir)",
)
def appeal_hsp_receipt(
    receipt_id: int,
    payload: HspAppealIn,
    ctx: Ctx = Depends(_dsr_create),
    tenant: str | None = Query(default=None),
) -> Any:
    """
    İtirazı kaydeder.

    İki tasarım kararı:

    *   **Makbuz değiştirilmez.** ``cmp_hsp_receipt`` salt eklenir; bir kararın
        sonradan düzeltilmesi zinciri kırardı ve "karar o an neydi" sorusunu
        cevaplanamaz hâle getirirdi. İtiraz ayrı bir kayıttır ve makbuza
        *atıfta bulunur*.
    *   **Yeni bir iş akışı icat edilmez.** Otomatik karara itiraz, zaten
        çalışan ilgili kişi başvurusu hattına ``AUTOMATED_DECISION_REVIEW``
        türüyle girer: aynı kimlik doğrulama, aynı süre takibi, aynı kanıt
        zinciri, aynı ekran. Ayrı bir kuyruk, ikinci bir yarı-uygulanmış
        süreç demek olurdu.

    Yetki ``compliance.dsr:CREATE`` üzerindedir; itiraz etmek bir başvuru
    açmaktır, makbuzu okumak değil.
    """
    receipt = _get_receipt(ctx, receipt_id, tenant)
    existing = _find_appeal(ctx, receipt)
    if existing is not None:
        raise BusinessRuleError(
            "compliance.hsp.appeal_exists",
            params={"reference": existing.reference},
        )

    resolved = tenant_service.require(ctx.db, ctx, tenant)
    subject_type, _, _ = receipt.subject_ref.partition(":")
    description = (
        f"{_APPEAL_MARKER}{receipt.id} ({receipt.action_code}/{receipt.verdict}) "
        f"{payload.reason.strip()}"
    )
    request = dsr_service.create_request(
        ctx.db,
        ctx,
        tenant_id=resolved.id,
        subject_type=(subject_type or "UNKNOWN").upper()[:32],
        request_type=DsrType.AUTOMATED_DECISION_REVIEW,
        subject_ref=receipt.subject_ref,
        subject_contact=payload.contact,
        description=description,
        requested_scope=receipt.action_code,
    )
    ctx.db.commit()
    log.info(
        "HSP appeal filed: receipt=%s request=%s", receipt.id, request.reference
    )
    return _receipt_detail(ctx, receipt)



__all__ = ["router"]
