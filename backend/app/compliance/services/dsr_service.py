"""
İlgili kişi başvurusu (DSR) servisi.

Üç kayıt birlikte çalışır: ``DataSubjectRequest`` başvurunun kendisi,
``IdentityVerification`` kimlik doğrulama denemeleri, ``FulfilmentTask`` ise
başvuruyu karşılamak için yapılan somut işler.

İki kural özellikle dikkat ister:

*   **Son tarih hesaplanmaz.** Yanıt süresi yargı alanına ve başvuru türüne
    göre değişir; bu katman süre uydurmaz. Son tarih ya çağıran tarafından
    verilir ve dayanağı yazılır, ya da boş kalır. Boş son tarih "gecikme yok"
    demek değildir; ``due_basis_status`` ``REVIEW_REQUIRED`` kalır ve başvuru
    insan incelemesinde görünür.
*   **Kimliği doğrulanmamış başvuruya veri açılmaz.** Erişim, taşınabilirlik
    ve bilgi taleplerinin olumlu kapatılabilmesi doğrulanmış kimlik ister.
    Doğru prosedürle yanlış kişiye teslim, veri ihlalinin en sık görülen
    biçimidir.

Reddin gerekçesi zorunludur: dayanaksız ret, cevapsızlıkla aynı sonucu doğurur
ve ilgili kişinin itiraz hakkını fiilen ortadan kaldırır.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.compliance.enums import (
    DsrStatus,
    DsrType,
    EvidenceKind,
    IdentityVerificationMethod,
    IntakeChannel,
    ReviewStatus,
    VerificationOutcome,
)
from app.compliance.models.dsr import DataSubjectRequest, IdentityVerification
from app.compliance.services import evidence_service
from app.core.deps import Ctx
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.models.base import utcnow
from app.services import audit_service

log = get_logger("app.compliance.dsr")

SUBJECT_DSR = "cmp_data_subject_request"

#: Başvuruyu kapatan durumlar. Kapanmış başvuru yeniden işlenmez; yeni talep
#: yeni başvurudur.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        DsrStatus.FULFILLED,
        DsrStatus.PARTIALLY_FULFILLED,
        DsrStatus.REJECTED,
        DsrStatus.WITHDRAWN_BY_SUBJECT,
    }
)

#: Karşılanması ilgili kişiye veri açan talep türleri.
_DISCLOSURE_TYPES: frozenset[str] = frozenset(
    {DsrType.ACCESS, DsrType.PORTABILITY, DsrType.INFORMATION}
)

_MAX_REFERENCE_ATTEMPTS = 5


def _next_reference(db: Session, *, tenant_id: int, year: int, attempt: int) -> str:
    """
    Okunabilir dosya numarası: ``DSR-2026-0007``.

    Sayaç aynı yıldaki başvuru sayısından türetilir. Eşzamanlı iki başvuru aynı
    numarayı hedeflerse veritabanı kısıtı ikincisini reddeder ve çağıran bir
    sonraki numarayla yeniden dener — numara üretimini kilit almadan doğru
    tutmanın en ucuz yolu budur.
    """
    prefix = f"DSR-{year}-"
    used = int(
        db.execute(
            select(func.count(DataSubjectRequest.id)).where(
                DataSubjectRequest.tenant_id == tenant_id,
                DataSubjectRequest.reference.like(f"{prefix}%"),
            )
        ).scalar_one()
        or 0
    )
    return f"{prefix}{used + 1 + attempt:04d}"


def create_request(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    subject_type: str,
    request_type: str,
    subject_ref: str | None = None,
    subject_contact: str | None = None,
    channel: str = IntakeChannel.UNKNOWN,
    received_at: datetime | None = None,
    due_at: datetime | None = None,
    due_basis: str | None = None,
    description: str | None = None,
    requested_scope: str | None = None,
    assigned_to_user_id: int | None = None,
    submitted_by_agent: bool = False,
    agent_name: str | None = None,
) -> DataSubjectRequest:
    """Başvuruyu kaydet ve alınma kanıtını zincire yaz."""
    now = utcnow()
    received = received_at or now
    if received > now:
        raise ValidationError("compliance.dsr.received_in_future")
    if due_at is not None and due_at <= received:
        raise ValidationError("compliance.dsr.due_before_received")

    request: DataSubjectRequest | None = None
    for attempt in range(_MAX_REFERENCE_ATTEMPTS):
        candidate = DataSubjectRequest(
            tenant_id=tenant_id,
            reference=_next_reference(
                db, tenant_id=tenant_id, year=received.year, attempt=attempt
            ),
            request_type=request_type,
            status=DsrStatus.RECEIVED,
            subject_type=subject_type,
            subject_ref=subject_ref,
            subject_contact=subject_contact,
            submitted_by_agent=submitted_by_agent,
            agent_name=agent_name,
            channel=channel,
            received_at=received,
            description=description,
            requested_scope=requested_scope,
            due_at=due_at,
            due_basis=due_basis,
            # Süre dayanağı doğrulanmadan "tamam" sayılmaz; dayanak metni
            # yazılmış olsa bile insan doğrulaması bekler.
            due_basis_status=ReviewStatus.REVIEW_REQUIRED,
            assigned_to_user_id=assigned_to_user_id,
            created_by_id=ctx.user_id,
        )
        db.add(candidate)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            continue
        request = candidate
        break

    if request is None:
        raise ConflictError(
            "compliance.dsr.reference_conflict", params={"tenant_id": tenant_id}
        )

    evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.CORRESPONDENCE,
        title=f"DSR intake {request.reference} ({request_type})",
        description="İlgili kişi başvurusunun alınma kaydı.",
        subject_type=SUBJECT_DSR,
        subject_id=request.id,
        subject_ref=f"{subject_type}:{subject_ref}" if subject_ref else subject_type,
        payload={
            "reference": request.reference,
            "request_type": request_type,
            "subject_type": subject_type,
            "subject_ref": subject_ref,
            "channel": channel,
            "received_at": received.isoformat(),
            "due_at": due_at.isoformat() if due_at else None,
            "due_basis": due_basis,
            "due_basis_status": request.due_basis_status,
            "submitted_by_agent": submitted_by_agent,
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    audit_service.record(
        db,
        "CREATE",
        entity_type=SUBJECT_DSR,
        entity_id=request.id,
        entity_label=request.reference,
        summary=f"DSR received ({request_type})",
        new_values={
            "reference": request.reference,
            "request_type": request_type,
            "channel": channel,
            "due_at": due_at.isoformat() if due_at else None,
        },
        **ctx.audit_kwargs(),
    )
    return request


def verify_identity(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    request_id: int,
    method: str = IdentityVerificationMethod.UNKNOWN,
    outcome: str = VerificationOutcome.PENDING,
    document_type: str | None = None,
    failure_reason: str | None = None,
    notes: str | None = None,
) -> IdentityVerification:
    """
    Kimlik doğrulama denemesini kaydet.

    Başarısız denemeler de satır olarak kalır: art arda başarısız denemeler,
    kimlik avı girişiminin ilk işaretidir. Belgenin kendisi saklanmaz.
    """
    request = _get(db, tenant_id=tenant_id, request_id=request_id)
    now = utcnow()

    attempt = IdentityVerification(
        tenant_id=tenant_id,
        request_id=request.id,
        method=method,
        outcome=outcome,
        attempted_at=now,
        completed_at=now if outcome != VerificationOutcome.PENDING else None,
        verified_by_user_id=ctx.user_id,
        document_type=document_type,
        failure_reason=failure_reason,
        notes=notes,
        created_by_id=ctx.user_id,
    )
    db.add(attempt)
    db.flush()

    if outcome == VerificationOutcome.VERIFIED:
        request.identity_verified = True
        request.identity_verified_at = now
        if request.status == DsrStatus.IDENTITY_PENDING:
            request.status = DsrStatus.IN_PROGRESS
    elif outcome == VerificationOutcome.FAILED:
        request.identity_verified = False
        request.status = DsrStatus.IDENTITY_FAILED
    request.updated_by_id = ctx.user_id
    db.flush()

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.ATTESTATION,
        title=f"Identity verification {request.reference}: {outcome}",
        description="Başvuru sahibinin kimlik doğrulama denemesi.",
        subject_type=SUBJECT_DSR,
        subject_id=request.id,
        payload={
            "reference": request.reference,
            "method": method,
            "outcome": outcome,
            "document_type": document_type,
            "failure_reason": failure_reason,
            "attempted_at": now.isoformat(),
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    attempt.evidence_id = artifact.id
    db.flush()

    audit_service.record(
        db,
        "UPDATE",
        entity_type=SUBJECT_DSR,
        entity_id=request.id,
        entity_label=request.reference,
        summary=f"Identity verification: {outcome}",
        new_values={"method": method, "outcome": outcome},
        **ctx.audit_kwargs(),
    )
    return attempt


def fulfil(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    request_id: int,
    outcome: str,
    response_summary: str | None = None,
    response_channel: str = IntakeChannel.UNKNOWN,
    rejection_reason: str | None = None,
) -> DataSubjectRequest:
    """
    Başvuruyu kapat.

    Reddin gerekçesi zorunludur. Veri açan taleplerin olumlu kapanışı
    doğrulanmış kimlik ister; doğrulanmamışsa istek reddedilir ve başvuru açık
    kalır — sessizce "karşılandı" yazmak, denetimde görünmeyen bir ihlal
    üretirdi.
    """
    request = _get(db, tenant_id=tenant_id, request_id=request_id)
    if request.status in TERMINAL_STATUSES:
        raise ConflictError(
            "compliance.dsr.already_closed",
            params={"reference": request.reference, "status": request.status},
        )

    if outcome == DsrStatus.REJECTED:
        if not (rejection_reason or "").strip():
            raise ValidationError("compliance.dsr.rejection_reason_required")
    elif (
        request.request_type in _DISCLOSURE_TYPES and not request.identity_verified
    ):
        raise BusinessRuleError(
            "compliance.dsr.identity_not_verified",
            params={
                "reference": request.reference,
                "request_type": request.request_type,
            },
            detail="A disclosure request cannot be fulfilled before identity verification.",
        )

    now = utcnow()
    previous_status = request.status
    elapsed = (now - request.received_at).days if request.received_at else None

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.DECISION_RECORD,
        title=f"DSR closed {request.reference}: {outcome}",
        description="İlgili kişi başvurusunun kapanış kararı.",
        subject_type=SUBJECT_DSR,
        subject_id=request.id,
        payload={
            "reference": request.reference,
            "request_type": request.request_type,
            "outcome": outcome,
            "previous_status": previous_status,
            "response_summary": response_summary,
            "response_channel": response_channel,
            "rejection_reason": rejection_reason,
            "identity_verified": request.identity_verified,
            "received_at": request.received_at.isoformat() if request.received_at else None,
            "due_at": request.due_at.isoformat() if request.due_at else None,
            "closed_at": now.isoformat(),
            "elapsed_days": elapsed,
            # Son tarih yoksa gecikme ölçülemez; ``None`` bunu açıkça söyler.
            "was_overdue": (
                bool(request.due_at and now > request.due_at) if request.due_at else None
            ),
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )

    request.status = outcome
    request.responded_at = now
    request.response_channel = response_channel
    request.response_summary = response_summary
    request.response_evidence_id = artifact.id
    request.rejection_reason = rejection_reason
    request.updated_by_id = ctx.user_id
    db.flush()

    audit_service.record(
        db,
        "UPDATE",
        entity_type=SUBJECT_DSR,
        entity_id=request.id,
        entity_label=request.reference,
        summary=f"DSR closed: {outcome}",
        old_values={"status": previous_status},
        new_values={"status": outcome, "evidence_id": artifact.id},
        **ctx.audit_kwargs(),
    )
    log.info("dsr %s closed as %s by user=%s", request.reference, outcome, ctx.user_id)
    return request


#: ``transition`` ile girilemeyen durumlar. Kapanış ayrı bir karardır ve
#: ``fulfil`` üzerinden yürür: orada gerekçe zorunlu, kimlik doğrulaması şart
#: ve kapanış kanıtı zincire yazılıyor. Aynı sonuca not düşerek ulaşılabilseydi,
#: o kontrollerin hepsi isteğe bağlı hâle gelirdi.
_CLOSING_STATUSES: frozenset[str] = frozenset(
    {
        DsrStatus.FULFILLED,
        DsrStatus.PARTIALLY_FULFILLED,
        DsrStatus.REJECTED,
    }
)


def transition(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    request_id: int,
    to_status: str,
    note: str,
) -> DataSubjectRequest:
    """
    Açık bir başvuruyu bir sonraki çalışma durumuna taşı.

    Kapsamı bilerek dar:

    *   Kapanmış başvuru yeniden açılmaz — kapanmış bir dosyayı diriltmek,
        süre takibini ve kanıt zincirini anlamsızlaştırır. Yeni talep yeni
        başvurudur.
    *   Kapanış durumlarına buradan geçilemez; onlar ``fulfil`` üzerinden
        yürür (bkz. yukarıdaki not).
    *   Not zorunludur ve kanıt kaydına yazılır: durumu kimin, ne zaman, hangi
        gerekçeyle değiştirdiği sonradan sorulabilir olmalıdır.
    """
    request = _get(db, tenant_id=tenant_id, request_id=request_id)
    target = str(to_status or "").strip().upper()

    if target not in {str(s) for s in DsrStatus}:
        raise ValidationError("compliance.dsr.unknown_status", params={"status": target})
    if not (note or "").strip():
        raise ValidationError("compliance.dsr.transition_note_required")
    if request.status in TERMINAL_STATUSES:
        raise ConflictError(
            "compliance.dsr.already_closed",
            params={"reference": request.reference, "status": request.status},
        )
    if target in _CLOSING_STATUSES:
        raise BusinessRuleError(
            "compliance.dsr.use_fulfil_to_close",
            params={"status": target},
            detail="Closing a request goes through /fulfil so the closure evidence is written.",
        )
    if target == request.status:
        raise ValidationError(
            "compliance.dsr.status_unchanged", params={"status": target}
        )

    previous_status = request.status
    now = utcnow()

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.DECISION_RECORD,
        title=f"DSR status change {request.reference}: {previous_status} -> {target}",
        description="İlgili kişi başvurusunda durum değişikliği.",
        subject_type=SUBJECT_DSR,
        subject_id=request.id,
        payload={
            "reference": request.reference,
            "from_status": previous_status,
            "to_status": target,
            "note": note.strip(),
            "changed_at": now.isoformat(),
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )

    request.status = target
    request.escalated = request.escalated or target == DsrStatus.ESCALATED
    request.updated_by_id = ctx.user_id
    db.flush()

    audit_service.record(
        db,
        "UPDATE",
        entity_type=SUBJECT_DSR,
        entity_id=request.id,
        entity_label=request.reference,
        summary=f"DSR status: {previous_status} -> {target}",
        old_values={"status": previous_status},
        new_values={"status": target, "evidence_id": artifact.id},
        **ctx.audit_kwargs(),
    )
    log.info(
        "dsr %s moved %s -> %s by user=%s",
        request.reference, previous_status, target, ctx.user_id,
    )
    return request


def _get(db: Session, *, tenant_id: int, request_id: int) -> DataSubjectRequest:
    request = db.get(DataSubjectRequest, request_id)
    if request is None or request.tenant_id != tenant_id:
        raise NotFoundError("compliance.dsr.not_found", params={"id": request_id})
    return request


def list_requests(
    db: Session,
    *,
    tenant_id: int,
    request_type: str | None = None,
    status: str | None = None,
    subject_ref: str | None = None,
    open_only: bool = False,
    overdue_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    conds: list[Any] = [DataSubjectRequest.tenant_id == tenant_id]
    if request_type:
        conds.append(DataSubjectRequest.request_type == request_type)
    if status:
        conds.append(DataSubjectRequest.status == status)
    if subject_ref:
        conds.append(DataSubjectRequest.subject_ref == subject_ref)
    if open_only:
        conds.append(DataSubjectRequest.status.not_in(list(TERMINAL_STATUSES)))
    if overdue_only:
        # Son tarihi olmayan başvurular "geciken" sayılmaz; ölçülmemiş bir
        # süreyi ihlal olarak raporlamak, olmayan bir bulguyu üretmek olurdu.
        conds.append(DataSubjectRequest.due_at.is_not(None))
        conds.append(DataSubjectRequest.due_at < utcnow())
        conds.append(DataSubjectRequest.status.not_in(list(TERMINAL_STATUSES)))

    total = int(
        db.execute(select(func.count(DataSubjectRequest.id)).where(*conds)).scalar_one()
        or 0
    )
    rows = (
        db.execute(
            select(DataSubjectRequest)
            .where(*conds)
            .order_by(DataSubjectRequest.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    return [request_to_dict(row, now=now) for row in rows], total


def request_to_dict(
    request: DataSubjectRequest,
    *,
    now: datetime | None = None,
    verifications: list[IdentityVerification] | None = None,
) -> dict[str, Any]:
    moment = now or utcnow()
    is_closed = request.status in TERMINAL_STATUSES
    deadline = request.extended_due_at or request.due_at

    if deadline is None:
        overdue: bool | None = None
    elif is_closed:
        overdue = bool(request.responded_at and request.responded_at > deadline)
    else:
        overdue = moment > deadline

    end = request.responded_at if (is_closed and request.responded_at) else moment
    days_open = max((end - request.received_at).days, 0) if request.received_at else 0

    return {
        "id": request.id,
        "tenant_id": request.tenant_id,
        "reference": request.reference,
        "request_type": request.request_type,
        "status": request.status,
        "subject_type": request.subject_type,
        "subject_ref": request.subject_ref,
        "channel": request.channel,
        "received_at": request.received_at,
        "due_at": request.due_at,
        "extended_due_at": request.extended_due_at,
        "due_basis": request.due_basis,
        "due_basis_status": request.due_basis_status,
        "identity_verified": request.identity_verified,
        "identity_verified_at": request.identity_verified_at,
        "assigned_to_user_id": request.assigned_to_user_id,
        "description": request.description,
        "responded_at": request.responded_at,
        "response_summary": request.response_summary,
        "rejection_reason": request.rejection_reason,
        "escalated": request.escalated,
        "is_closed": is_closed,
        "is_overdue": overdue,
        "days_open": days_open,
        "verifications": [
            {
                "id": v.id,
                "method": v.method,
                "outcome": v.outcome,
                "attempted_at": v.attempted_at,
                "failure_reason": v.failure_reason,
            }
            for v in (verifications or [])
        ],
    }


def get_detail(db: Session, *, tenant_id: int, request_id: int) -> dict[str, Any]:
    request = _get(db, tenant_id=tenant_id, request_id=request_id)
    attempts = (
        db.execute(
            select(IdentityVerification)
            .where(IdentityVerification.request_id == request.id)
            .order_by(IdentityVerification.attempted_at.asc())
        )
        .scalars()
        .all()
    )
    return request_to_dict(request, verifications=list(attempts))


def status_summary(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """Genel durum tablosunun başvuru satırı."""
    rows = (
        db.execute(
            select(DataSubjectRequest).where(DataSubjectRequest.tenant_id == tenant_id)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    total = len(rows)
    open_count = overdue = undated = unverified = 0
    last: datetime | None = None

    for row in rows:
        data = request_to_dict(row, now=now)
        if not data["is_closed"]:
            open_count += 1
            if data["is_overdue"] is True:
                overdue += 1
            elif data["is_overdue"] is None:
                undated += 1
            if not row.identity_verified:
                unverified += 1
        if row.received_at and (last is None or row.received_at > last):
            last = row.received_at

    return {
        "total": total,
        "open": open_count,
        "overdue": overdue,
        #: Son tarihi bilinmeyen açık başvurular — "zamanında" sayılmazlar.
        "without_due_date": undated,
        "identity_unverified_open": unverified,
        "last_activity_at": last,
    }
