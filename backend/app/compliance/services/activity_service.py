"""
İşleme faaliyeti (ROPA) ve yurt dışı aktarım servisi.

**Eksiksizlik uyumluluk değildir.** Bu modül yalnızca kaydın *belgelenme*
durumunu ölçer: dayanak kaydı var mı, madde referansı doğrulanmış mı, özel
nitelikli veri için ayrı dayanak yazılmış mı, aktarımın mekanizması ve etki
değerlendirmesi duruyor mu. Tüm alanların dolu olması kaydı "uyumlu" yapmaz.

Bu yüzden sistem bu tablolara hiçbir koşulda ``COMPLIANT`` yazmaz. Eksik varsa
``REVIEW_REQUIRED``, kayıt eksiksizse ``UNKNOWN`` yazılır — ikincisi
"belgelendi, ama henüz onaylı bir kural paketine karşı değerlendirilmedi"
demektir. Uyumluluk kararı ``/compliance/evaluate`` üzerinden üretilir ve
``cmp_rule_evaluation`` içinde yaşar.

Dayanak bir enum değil **kayıttır**: hangi metne dayanıldığı, kaynağın nerede
olduğu ve kimin doğruladığı ayrı sütunlarda durur. Doğrulanmamış bir madde
referansı ``REVIEW_REQUIRED`` kalır.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.enums import (
    ComplianceRegime,
    ComplianceState,
    EvidenceKind,
    LegalBasisKind,
    ProcessingRole,
    ReviewStatus,
    TransferMechanism,
)
from app.compliance.models.inventory import LegalBasis, ProcessingActivity, Purpose, Transfer
from app.compliance.services import evidence_service
from app.core.deps import Ctx
from app.core.exceptions import ConflictError, NotFoundError
from app.core.utils import dumps, loads
from app.models.base import utcnow
from app.services import audit_service

SUBJECT_ACTIVITY = "cmp_processing_activity"
SUBJECT_TRANSFER = "cmp_transfer"

#: Somut bir belgeye dayanan aktarım mekanizmaları. Bunlar seçildiğinde belge
#: atfı boş bırakılamaz: belge gösterilmeden "mekanizma var" demek, denetimde
#: hiçbir şey ifade etmez.
_DOCUMENTED_MECHANISMS: frozenset[str] = frozenset(
    {
        TransferMechanism.ADEQUACY_DECISION,
        TransferMechanism.STANDARD_CONTRACTUAL_CLAUSES,
        TransferMechanism.BINDING_CORPORATE_RULES,
        TransferMechanism.UNDERTAKING,
        TransferMechanism.AUTHORITY_AUTHORISATION,
    }
)


# ===========================================================================
# Dayanak ve amaç kayıtları
# ===========================================================================
def resolve_legal_basis(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    code: str,
    kind: str = LegalBasisKind.UNKNOWN,
    name: str | None = None,
    regime: str = ComplianceRegime.UNKNOWN,
    article_reference: str | None = None,
    source_url: str | None = None,
) -> LegalBasis:
    """
    Dayanak kaydını bul, yoksa oluştur.

    Yeni kayıtta madde referansı **doğrulanmamış** sayılır. Referansın doğru
    olduğunu sistem bilemez; bilmediğini bilmek, bildiğini varsaymaktan
    güvenlidir.
    """
    basis = db.execute(
        select(LegalBasis).where(
            LegalBasis.tenant_id == tenant_id, LegalBasis.code == code
        )
    ).scalar_one_or_none()
    if basis is not None:
        return basis

    basis = LegalBasis(
        tenant_id=tenant_id,
        code=code,
        name=name or code,
        kind=kind,
        regime=regime,
        article_reference=article_reference,
        article_reference_status=ReviewStatus.REVIEW_REQUIRED,
        source_url=source_url,
        # Meşru menfaat dayanağı denge testi olmadan kullanılamaz; bunu
        # kaydın kendisi taşır ki değerlendirme sırasında unutulmasın.
        balancing_test_required=(kind == LegalBasisKind.LEGITIMATE_INTERESTS),
        created_by_id=ctx.user_id,
    )
    db.add(basis)
    db.flush()
    return basis


def resolve_purpose(
    db: Session, ctx: Ctx, *, tenant_id: int, code: str, name: str | None = None
) -> Purpose:
    purpose = db.execute(
        select(Purpose).where(Purpose.tenant_id == tenant_id, Purpose.code == code)
    ).scalar_one_or_none()
    if purpose is not None:
        return purpose
    purpose = Purpose(
        tenant_id=tenant_id,
        code=code,
        name=name or code,
        review_status=ReviewStatus.REVIEW_REQUIRED,
        created_by_id=ctx.user_id,
    )
    db.add(purpose)
    db.flush()
    return purpose


# ===========================================================================
# İşleme faaliyeti
# ===========================================================================
def activity_review_reasons(
    db: Session, activity: ProcessingActivity
) -> list[str]:
    """
    Kaydın neden insan incelemesi beklediği — makine okunur gerekçe kodları.

    Boş liste "uyumlu" demek değildir; "belgelenmesi gereken alanlar dolu"
    demektir.
    """
    reasons: list[str] = []

    if activity.legal_basis_id is None:
        reasons.append("legal_basis_missing")
    else:
        basis = db.get(LegalBasis, activity.legal_basis_id)
        if basis is None:
            reasons.append("legal_basis_missing")
        else:
            if basis.article_reference_status != ReviewStatus.ACCEPTED:
                reasons.append("legal_reference_unverified")
            if basis.balancing_test_required and not basis.balancing_test_done:
                reasons.append("balancing_test_missing")

    if activity.processes_special_category and activity.special_category_legal_basis_id is None:
        # Sıradan kişisel veri için geçerli bir dayanak, özel nitelikli veriyi
        # kendiliğinden kapsamaz.
        reasons.append("special_category_basis_missing")

    if activity.purpose_id is None:
        reasons.append("purpose_missing")
    if activity.retention_policy_id is None:
        reasons.append("retention_policy_missing")
    if not loads(activity.data_subject_categories, []):
        reasons.append("data_subject_categories_empty")

    if activity.cross_border_transfer:
        has_transfer = bool(
            db.execute(
                select(func.count(Transfer.id)).where(
                    Transfer.tenant_id == activity.tenant_id,
                    Transfer.activity_id == activity.id,
                )
            ).scalar_one()
        )
        if not has_transfer:
            reasons.append("cross_border_without_transfer_record")

    if activity.involves_automated_decision and not (activity.human_oversight_note or "").strip():
        reasons.append("human_oversight_undocumented")
    if activity.dpia_status != ReviewStatus.ACCEPTED:
        reasons.append("dpia_" + str(activity.dpia_status).lower())

    return reasons


def _apply_activity_fields(
    db: Session, ctx: Ctx, *, tenant_id: int, activity: ProcessingActivity, data: dict[str, Any]
) -> None:
    if data.get("name") is not None:
        activity.name = data["name"]
    if data.get("name_en") is not None:
        activity.name_en = data["name_en"]
    if data.get("description") is not None:
        activity.description = data["description"]
    if data.get("workspace_id") is not None:
        activity.workspace_id = data["workspace_id"]
    if data.get("controller_role") is not None:
        activity.controller_role = str(data["controller_role"])

    if data.get("purpose_code"):
        purpose = resolve_purpose(
            db, ctx, tenant_id=tenant_id, code=data["purpose_code"],
            name=data.get("purpose_name"),
        )
        activity.purpose_id = purpose.id

    if data.get("legal_basis_code"):
        basis = resolve_legal_basis(
            db,
            ctx,
            tenant_id=tenant_id,
            code=data["legal_basis_code"],
            kind=str(data.get("legal_basis_kind") or LegalBasisKind.UNKNOWN),
            name=data.get("legal_basis_name"),
            article_reference=data.get("article_reference"),
        )
        activity.legal_basis_id = basis.id

    if data.get("special_category_legal_basis_code"):
        special = resolve_legal_basis(
            db,
            ctx,
            tenant_id=tenant_id,
            code=data["special_category_legal_basis_code"],
            kind=str(data.get("special_category_legal_basis_kind") or LegalBasisKind.UNKNOWN),
        )
        activity.special_category_legal_basis_id = special.id

    for field in (
        "processes_special_category",
        "involves_automated_decision",
        "involves_profiling",
        "involves_ai",
        "cross_border_transfer",
    ):
        if data.get(field) is not None:
            setattr(activity, field, bool(data[field]))

    if data.get("data_subject_categories") is not None:
        activity.data_subject_categories = dumps(data["data_subject_categories"])
    if data.get("security_measures") is not None:
        activity.security_measures = data["security_measures"]
    if data.get("human_oversight_note") is not None:
        activity.human_oversight_note = data["human_oversight_note"]
    if data.get("owner_user_id") is not None:
        activity.owner_user_id = data["owner_user_id"]
    if data.get("department") is not None:
        activity.department = data["department"]
    if data.get("subject_count_estimate") is not None:
        activity.subject_count_estimate = data["subject_count_estimate"]


def create_activity(
    db: Session, ctx: Ctx, *, tenant_id: int, data: dict[str, Any]
) -> tuple[ProcessingActivity, list[str]]:
    code = data["code"]
    existing = db.execute(
        select(ProcessingActivity).where(
            ProcessingActivity.tenant_id == tenant_id,
            ProcessingActivity.code == code,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("compliance.activity.code_exists", params={"code": code})

    activity = ProcessingActivity(
        tenant_id=tenant_id,
        code=code,
        name=data.get("name") or code,
        controller_role=str(data.get("controller_role") or ProcessingRole.UNKNOWN),
        dpia_status=ReviewStatus.REVIEW_REQUIRED,
        compliance_state=ComplianceState.REVIEW_REQUIRED,
        review_status=ReviewStatus.REVIEW_REQUIRED,
        created_by_id=ctx.user_id,
    )
    db.add(activity)
    db.flush()

    _apply_activity_fields(db, ctx, tenant_id=tenant_id, activity=activity, data=data)
    reasons = _settle_activity_state(db, activity)
    db.flush()

    _record_activity_evidence(db, ctx, activity=activity, reasons=reasons, created=True,
                              note=data.get("change_note"))
    return activity, reasons


def update_activity(
    db: Session, ctx: Ctx, *, tenant_id: int, activity_id: int, data: dict[str, Any]
) -> tuple[ProcessingActivity, list[str]]:
    activity = db.get(ProcessingActivity, activity_id)
    if activity is None or activity.tenant_id != tenant_id or activity.is_deleted:
        raise NotFoundError("compliance.activity.not_found", params={"id": activity_id})

    before = activity_to_dict(activity, reasons=activity_review_reasons(db, activity))
    _apply_activity_fields(db, ctx, tenant_id=tenant_id, activity=activity, data=data)
    activity.updated_by_id = ctx.user_id
    reasons = _settle_activity_state(db, activity)
    db.flush()

    _record_activity_evidence(
        db, ctx, activity=activity, reasons=reasons, created=False,
        note=data.get("change_note"), before=before,
    )
    return activity, reasons


def _settle_activity_state(db: Session, activity: ProcessingActivity) -> list[str]:
    """Belgelenme durumunu yeniden hesapla — asla ``COMPLIANT`` yazmadan."""
    reasons = activity_review_reasons(db, activity)
    activity.compliance_state = (
        ComplianceState.REVIEW_REQUIRED if reasons else ComplianceState.UNKNOWN
    )
    activity.review_status = (
        ReviewStatus.REVIEW_REQUIRED if reasons else activity.review_status
    )
    activity.last_reviewed_at = utcnow()
    return reasons


def _record_activity_evidence(
    db: Session,
    ctx: Ctx,
    *,
    activity: ProcessingActivity,
    reasons: list[str],
    created: bool,
    note: str | None,
    before: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "code": activity.code,
        "name": activity.name,
        "purpose_id": activity.purpose_id,
        "legal_basis_id": activity.legal_basis_id,
        "special_category_legal_basis_id": activity.special_category_legal_basis_id,
        "processes_special_category": activity.processes_special_category,
        "cross_border_transfer": activity.cross_border_transfer,
        "involves_automated_decision": activity.involves_automated_decision,
        "involves_profiling": activity.involves_profiling,
        "involves_ai": activity.involves_ai,
        "dpia_status": activity.dpia_status,
        "compliance_state": activity.compliance_state,
        "review_reasons": reasons,
        "change_note": note,
    }
    if before is not None:
        payload["previous"] = {
            k: before.get(k)
            for k in ("legal_basis_id", "purpose_id", "compliance_state", "review_reasons")
        }

    evidence_service.append(
        db,
        tenant_id=activity.tenant_id,
        kind=EvidenceKind.DOCUMENT,
        title=f"Processing activity {activity.code}",
        description="İşleme faaliyeti kaydının son hâli ve eksik listesi.",
        subject_type=SUBJECT_ACTIVITY,
        subject_id=activity.id,
        payload=payload,
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    audit_service.record(
        db,
        "CREATE" if created else "UPDATE",
        entity_type=SUBJECT_ACTIVITY,
        entity_id=activity.id,
        entity_label=activity.code,
        summary=f"Processing activity {'created' if created else 'updated'}",
        old_values=before,
        new_values={
            "compliance_state": activity.compliance_state,
            "review_reasons": reasons,
        },
        **ctx.audit_kwargs(),
    )


def list_activities(
    db: Session,
    *,
    tenant_id: int,
    code: str | None = None,
    state: str | None = None,
    review_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[ProcessingActivity], int]:
    conds: list[Any] = [
        ProcessingActivity.tenant_id == tenant_id,
        ProcessingActivity.is_deleted.is_(False),
    ]
    if code:
        conds.append(ProcessingActivity.code == code)
    if state:
        conds.append(ProcessingActivity.compliance_state == state)
    if review_only:
        conds.append(ProcessingActivity.review_status == ReviewStatus.REVIEW_REQUIRED)

    total = int(
        db.execute(select(func.count(ProcessingActivity.id)).where(*conds)).scalar_one() or 0
    )
    rows = (
        db.execute(
            select(ProcessingActivity)
            .where(*conds)
            .order_by(ProcessingActivity.code)
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def activity_to_dict(
    activity: ProcessingActivity, *, reasons: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": activity.id,
        "tenant_id": activity.tenant_id,
        "code": activity.code,
        "name": activity.name,
        "name_en": activity.name_en,
        "description": activity.description,
        "workspace_id": activity.workspace_id,
        "controller_role": activity.controller_role,
        "purpose_id": activity.purpose_id,
        "legal_basis_id": activity.legal_basis_id,
        "special_category_legal_basis_id": activity.special_category_legal_basis_id,
        "processes_special_category": activity.processes_special_category,
        "data_subject_categories": loads(activity.data_subject_categories, []) or [],
        "subject_count_estimate": activity.subject_count_estimate,
        "retention_policy_id": activity.retention_policy_id,
        "security_measures": activity.security_measures,
        "dpia_required": activity.dpia_required,
        "dpia_status": activity.dpia_status,
        "involves_automated_decision": activity.involves_automated_decision,
        "involves_profiling": activity.involves_profiling,
        "involves_ai": activity.involves_ai,
        "human_oversight_note": activity.human_oversight_note,
        "cross_border_transfer": activity.cross_border_transfer,
        "owner_user_id": activity.owner_user_id,
        "department": activity.department,
        "compliance_state": activity.compliance_state,
        "review_status": activity.review_status,
        "last_reviewed_at": activity.last_reviewed_at,
        "next_review_due_at": activity.next_review_due_at,
        "created_at": activity.created_at,
        "review_reasons": reasons if reasons is not None else [],
    }


# ===========================================================================
# Yurt dışı aktarım
# ===========================================================================
def transfer_review_reasons(transfer: Transfer) -> list[str]:
    """Aktarım kaydının neden değerlendirilmemiş sayıldığı."""
    reasons: list[str] = []

    if transfer.mechanism == TransferMechanism.UNKNOWN:
        reasons.append("mechanism_unknown")
    elif transfer.mechanism == TransferMechanism.NONE_IDENTIFIED:
        reasons.append("mechanism_none_identified")
    elif transfer.mechanism in _DOCUMENTED_MECHANISMS and not (
        transfer.mechanism_reference or ""
    ).strip():
        reasons.append("mechanism_reference_missing")

    if not transfer.destination_country:
        reasons.append("destination_country_unknown")
    if transfer.mechanism == TransferMechanism.ADEQUACY_DECISION and (
        transfer.adequacy_status != ReviewStatus.ACCEPTED
    ):
        # Yeterlilik kararı iddiası doğrulanmadan dayanak sayılmaz.
        reasons.append("adequacy_unverified")
    if not transfer.tia_performed:
        reasons.append("transfer_impact_assessment_missing")
    if not transfer.subprocessors_disclosed:
        reasons.append("subprocessors_undisclosed")

    return reasons


def upsert_transfer(
    db: Session, ctx: Ctx, *, tenant_id: int, data: dict[str, Any]
) -> tuple[Transfer, list[str]]:
    """
    Aktarım kaydını oluştur ya da güncelle.

    Tarayıcının ürettiği aday satır burada insan beyanıyla tamamlanır; satır
    silinip yeniden yaratılmaz, çünkü adayın ne zaman ve nasıl bulunduğu
    bilgisi kanıt zincirinde bu satıra bağlıdır.
    """
    code = data["code"]
    transfer = db.execute(
        select(Transfer).where(Transfer.tenant_id == tenant_id, Transfer.code == code)
    ).scalar_one_or_none()
    created = transfer is None

    if transfer is None:
        transfer = Transfer(
            tenant_id=tenant_id,
            code=code,
            name=data.get("name") or code,
            created_by_id=ctx.user_id,
        )
        db.add(transfer)
        db.flush()

    for field in (
        "name",
        "name_en",
        "description",
        "activity_id",
        "destination_country",
        "destination_country_name",
        "destination_region",
        "mechanism_reference",
        "adequacy_reference",
        "supplementary_measures",
        "data_categories_note",
        "frequency",
    ):
        if data.get(field) is not None:
            setattr(transfer, field, data[field])

    if data.get("mechanism") is not None:
        transfer.mechanism = str(data["mechanism"])
    if data.get("tia_performed") is not None:
        transfer.tia_performed = bool(data["tia_performed"])
        if transfer.tia_performed and transfer.tia_performed_at is None:
            transfer.tia_performed_at = utcnow()
    if data.get("tia_outcome") is not None:
        transfer.tia_outcome = str(data["tia_outcome"])
    if data.get("subprocessors_disclosed") is not None:
        transfer.subprocessors_disclosed = bool(data["subprocessors_disclosed"])
    if data.get("adequacy_status") is not None:
        transfer.adequacy_status = str(data["adequacy_status"])
    transfer.updated_by_id = ctx.user_id

    reasons = transfer_review_reasons(transfer)
    transfer.status = (
        ComplianceState.REVIEW_REQUIRED if reasons else ComplianceState.UNKNOWN
    )
    db.flush()

    evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.DOCUMENT,
        title=f"Transfer record {transfer.code}",
        description="Yurt dışı aktarım kaydının son hâli ve eksik listesi.",
        subject_type=SUBJECT_TRANSFER,
        subject_id=transfer.id,
        payload={
            "code": transfer.code,
            "destination_country": transfer.destination_country,
            "mechanism": transfer.mechanism,
            "mechanism_reference": transfer.mechanism_reference,
            "adequacy_status": transfer.adequacy_status,
            "tia_performed": transfer.tia_performed,
            "tia_outcome": transfer.tia_outcome,
            "subprocessors_disclosed": transfer.subprocessors_disclosed,
            "status": transfer.status,
            "review_reasons": reasons,
            "change_note": data.get("change_note"),
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    audit_service.record(
        db,
        "CREATE" if created else "UPDATE",
        entity_type=SUBJECT_TRANSFER,
        entity_id=transfer.id,
        entity_label=transfer.code,
        summary=f"Transfer record {'created' if created else 'updated'}",
        new_values={
            "destination_country": transfer.destination_country,
            "mechanism": transfer.mechanism,
            "review_reasons": reasons,
        },
        **ctx.audit_kwargs(),
    )
    return transfer, reasons


def list_transfers(
    db: Session,
    *,
    tenant_id: int,
    mechanism: str | None = None,
    country: str | None = None,
    review_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Transfer], int]:
    conds: list[Any] = [Transfer.tenant_id == tenant_id]
    if mechanism:
        conds.append(Transfer.mechanism == mechanism)
    if country:
        conds.append(Transfer.destination_country == country)
    if review_only:
        conds.append(Transfer.status == ComplianceState.REVIEW_REQUIRED)

    total = int(db.execute(select(func.count(Transfer.id)).where(*conds)).scalar_one() or 0)
    rows = (
        db.execute(
            select(Transfer).where(*conds).order_by(Transfer.code).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def transfer_to_dict(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": transfer.id,
        "tenant_id": transfer.tenant_id,
        "code": transfer.code,
        "name": transfer.name,
        "description": transfer.description,
        "activity_id": transfer.activity_id,
        "recipient_id": transfer.recipient_id,
        "vendor_id": transfer.vendor_id,
        "destination_country": transfer.destination_country,
        "destination_country_name": transfer.destination_country_name,
        "destination_region": transfer.destination_region,
        "mechanism": transfer.mechanism,
        "mechanism_reference": transfer.mechanism_reference,
        "adequacy_reference": transfer.adequacy_reference,
        "adequacy_status": transfer.adequacy_status,
        "tia_performed": transfer.tia_performed,
        "tia_performed_at": transfer.tia_performed_at,
        "tia_outcome": transfer.tia_outcome,
        "supplementary_measures": transfer.supplementary_measures,
        "subprocessors_disclosed": transfer.subprocessors_disclosed,
        "data_categories_note": transfer.data_categories_note,
        "frequency": transfer.frequency,
        "status": transfer.status,
        "valid_from": transfer.valid_from,
        "valid_until": transfer.valid_until,
        "created_at": transfer.created_at,
        "review_reasons": transfer_review_reasons(transfer),
    }


def status_summary(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """Genel durum tablosunun faaliyet ve aktarım satırları."""
    activities, activity_total = list_activities(
        db, tenant_id=tenant_id, offset=0, limit=10_000
    )
    transfers, transfer_total = list_transfers(db, tenant_id=tenant_id, offset=0, limit=10_000)

    activity_pending = sum(
        1 for a in activities if a.review_status == ReviewStatus.REVIEW_REQUIRED
    )
    transfer_pending = sum(1 for t in transfers if transfer_review_reasons(t))

    def _last(rows: list[Any]) -> datetime | None:
        stamps = [r.created_at for r in rows if r.created_at]
        return max(stamps) if stamps else None

    return {
        "activities": {
            "total": activity_total,
            "pending_review": activity_pending,
            "automated_decisions": sum(1 for a in activities if a.involves_automated_decision),
            "special_category": sum(1 for a in activities if a.processes_special_category),
            "last_activity_at": _last(activities),
        },
        "transfers": {
            "total": transfer_total,
            "pending_review": transfer_pending,
            "unknown_mechanism": sum(
                1 for t in transfers if t.mechanism == TransferMechanism.UNKNOWN
            ),
            "without_tia": sum(1 for t in transfers if not t.tia_performed),
            "last_activity_at": _last(transfers),
        },
    }
