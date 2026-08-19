"""
Aydınlatma metni ve rıza servisi.

Üç kayıt bir arada çalışır: ``NoticeVersion`` gösterilen metnin sürümünü,
``ConsentRecord`` irade beyanını, ``WithdrawalRecord`` ise geri almayı taşır.

Neden geri alma ayrı bir satır? Çünkü ispat yükü öyle gerektiriyor. Rıza
kaydının üzerine "geri alındı" yazmak, beyanın ne zaman verildiği ile ne zaman
geri alındığı arasındaki süreyi siler; oysa denetimde ölçülen tam olarak o
süredir. Rıza satırı beyanın kendisidir, geri alma satırı ise ikinci bir
olaydır ve kendi kanıtını taşır.

Aydınlatma metninin özeti rıza satırına da işlenir. Metin sürümü sonradan
arşivlense bile "hangi metne rıza verildi" sorusu cevaplanabilir kalır.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.enums import (
    ConsentStatus,
    EvidenceKind,
    IntakeChannel,
    NoticeStatus,
    ReviewStatus,
    WithdrawalReason,
)
from app.compliance.models.consent import ConsentRecord, NoticeVersion, WithdrawalRecord
from app.compliance.models.inventory import Purpose
from app.compliance.services import evidence_service
from app.core.deps import Ctx
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import dumps
from app.models.base import utcnow
from app.services import audit_service

log = get_logger("app.compliance.consent")

SUBJECT_NOTICE = "cmp_notice_version"
SUBJECT_CONSENT = "cmp_consent_record"
SUBJECT_WITHDRAWAL = "cmp_withdrawal_record"

#: İspat gücü zayıf kabul edilen kanallar. Reddedilmezler ama kayıt insan
#: incelemesi bayrağıyla doğar: devralınan bir listenin rıza kanıtı, formun
#: kendisi olmadan ayakta durmaz.
_WEAK_PROOF_CHANNELS = frozenset(
    {IntakeChannel.IMPORTED, IntakeChannel.UNKNOWN, IntakeChannel.PHONE}
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# Amaç kaydı
# ===========================================================================
def resolve_purpose(
    db: Session, ctx: Ctx, *, tenant_id: int, code: str, name: str | None = None
) -> Purpose:
    """
    Amaç kaydını bul, yoksa oluştur.

    Yeni amaç ``REVIEW_REQUIRED`` doğar: bir amacın rıza gerektirip
    gerektirmediği hukuki bir değerlendirmedir ve kod adından çıkarılamaz.
    """
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
# Aydınlatma metni
# ===========================================================================
def current_notice(
    db: Session, *, tenant_id: int, notice_code: str, language: str
) -> NoticeVersion | None:
    """Yürürlükteki sürüm; yoksa en son yazılan sürüm."""
    published = db.execute(
        select(NoticeVersion)
        .where(
            NoticeVersion.tenant_id == tenant_id,
            NoticeVersion.notice_code == notice_code,
            NoticeVersion.language == language,
            NoticeVersion.is_current.is_(True),
        )
        .limit(1)
    ).scalar_one_or_none()
    if published is not None:
        return published
    return db.execute(
        select(NoticeVersion)
        .where(
            NoticeVersion.tenant_id == tenant_id,
            NoticeVersion.notice_code == notice_code,
            NoticeVersion.language == language,
        )
        .order_by(NoticeVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _next_version(db: Session, *, tenant_id: int, notice_code: str, language: str) -> str:
    """
    Bir sonraki sürüm etiketi.

    Sürüm sütunu metindir (paket sürümleriyle aynı desen), ama sayısal artış
    beklenir; sayıya çevrilemeyen bir etiket varsa satır sayısına düşülür ki
    aynı etiket iki kez üretilmesin.
    """
    rows = (
        db.execute(
            select(NoticeVersion.version).where(
                NoticeVersion.tenant_id == tenant_id,
                NoticeVersion.notice_code == notice_code,
                NoticeVersion.language == language,
            )
        )
        .scalars()
        .all()
    )
    highest = 0
    for value in rows:
        try:
            highest = max(highest, int(str(value).strip()))
        except (TypeError, ValueError):
            highest = max(highest, len(rows))
    return str(highest + 1)


def publish_notice(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    notice_code: str,
    title: str,
    body: str,
    language: str = "tr",
    kind: str,
    covered_activity_codes: list[str] | None = None,
    display_url: str | None = None,
    display_channel: str = IntakeChannel.UNKNOWN,
    effective_from: datetime | None = None,
    publish: bool = False,
) -> NoticeVersion:
    """
    Metnin yeni bir sürümünü yaz.

    Yayımlanırsa aynı kod ve dildeki önceki yürürlük kaydı düşürülür: iki
    sürümün aynı anda "yürürlükte" görünmesi, hangi metnin gösterildiğini
    belirsizleştirir ve rızanın dayanağını tartışmalı hâle getirir.
    """
    previous = current_notice(
        db, tenant_id=tenant_id, notice_code=notice_code, language=language
    )
    version = _next_version(
        db, tenant_id=tenant_id, notice_code=notice_code, language=language
    )
    now = utcnow()

    notice = NoticeVersion(
        tenant_id=tenant_id,
        notice_code=notice_code,
        version=version,
        language=language,
        kind=kind,
        title=title,
        body=body,
        body_hash=_digest(body),
        covered_activity_codes=dumps(covered_activity_codes or []),
        status=NoticeStatus.PUBLISHED if publish else NoticeStatus.DRAFT,
        published_at=now if publish else None,
        effective_from=effective_from or (now if publish else None),
        is_current=bool(publish),
        display_url=display_url,
        display_channel=display_channel,
        review_status=ReviewStatus.REVIEW_REQUIRED,
        supersedes_id=previous.id if previous else None,
        created_by_id=ctx.user_id,
    )
    db.add(notice)
    db.flush()

    if publish and previous is not None and previous.is_current:
        previous.is_current = False
        previous.status = NoticeStatus.SUPERSEDED
        previous.effective_until = now
        previous.updated_by_id = ctx.user_id

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.NOTICE_TEXT,
        title=f"Notice {notice_code} v{version} ({language})",
        description="Aydınlatma metni sürümü ve içerik özeti.",
        subject_type=SUBJECT_NOTICE,
        subject_id=notice.id,
        payload={
            "notice_code": notice_code,
            "version": version,
            "language": language,
            "kind": kind,
            "title": title,
            "body_hash": notice.body_hash,
            "status": notice.status,
            "supersedes_id": notice.supersedes_id,
            "covered_activity_codes": covered_activity_codes or [],
        },
        # Zincire giren özet metnin kendisinin özetidir: aynı metin başka bir
        # kiracıda da yayımlansa aynı parmak izini taşır.
        content_hash=notice.body_hash,
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    notice.evidence_id = artifact.id
    db.flush()

    audit_service.record(
        db,
        "CREATE",
        entity_type=SUBJECT_NOTICE,
        entity_id=notice.id,
        entity_label=f"{notice_code} v{version}",
        summary=f"Notice version created ({notice.status})",
        new_values={
            "notice_code": notice_code,
            "version": version,
            "language": language,
            "status": notice.status,
        },
        **ctx.audit_kwargs(),
    )
    return notice


def list_notices(
    db: Session,
    *,
    tenant_id: int,
    notice_code: str | None = None,
    language: str | None = None,
    status: str | None = None,
    current_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[NoticeVersion], int]:
    conds: list[Any] = [NoticeVersion.tenant_id == tenant_id]
    if notice_code:
        conds.append(NoticeVersion.notice_code == notice_code)
    if language:
        conds.append(NoticeVersion.language == language)
    if status:
        conds.append(NoticeVersion.status == status)
    if current_only:
        conds.append(NoticeVersion.is_current.is_(True))

    total = int(
        db.execute(select(func.count(NoticeVersion.id)).where(*conds)).scalar_one() or 0
    )
    rows = (
        db.execute(
            select(NoticeVersion)
            .where(*conds)
            .order_by(NoticeVersion.notice_code, NoticeVersion.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def notice_to_dict(notice: NoticeVersion, *, include_body: bool = False) -> dict[str, Any]:
    return {
        "id": notice.id,
        "tenant_id": notice.tenant_id,
        "notice_code": notice.notice_code,
        "version": notice.version,
        "language": notice.language,
        "kind": notice.kind,
        "title": notice.title,
        "body_hash": notice.body_hash,
        "status": notice.status,
        "is_current": notice.is_current,
        "published_at": notice.published_at,
        "effective_from": notice.effective_from,
        "effective_until": notice.effective_until,
        "display_url": notice.display_url,
        "review_status": notice.review_status,
        "supersedes_id": notice.supersedes_id,
        "evidence_id": notice.evidence_id,
        "created_at": notice.created_at,
        "body": notice.body if include_body else None,
    }


# ===========================================================================
# Rıza
# ===========================================================================
def current_consent(
    db: Session, *, tenant_id: int, subject_type: str, subject_ref: str, purpose_id: int
) -> ConsentRecord | None:
    return db.execute(
        select(ConsentRecord)
        .where(
            ConsentRecord.tenant_id == tenant_id,
            ConsentRecord.subject_type == subject_type,
            ConsentRecord.subject_ref == subject_ref,
            ConsentRecord.purpose_id == purpose_id,
            ConsentRecord.is_current.is_(True),
        )
        .order_by(ConsentRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def record_consent(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    subject_type: str,
    subject_ref: str,
    purpose_code: str,
    purpose_name: str | None = None,
    status: str = ConsentStatus.GIVEN,
    is_explicit: bool = False,
    channel: str = IntakeChannel.UNKNOWN,
    notice_version_id: int | None = None,
    scope_text: str | None = None,
    scope_codes: list[str] | None = None,
    collected_at: datetime | None = None,
    expires_at: datetime | None = None,
    proof_reference: str | None = None,
    language: str = "tr",
) -> ConsentRecord:
    """
    Yeni bir rıza beyanı kaydet.

    Aydınlatma metni verilmişse **yayımlanmış** olması aranır: taslak bir
    metne dayanan rıza, ilgili kişinin neyi kabul ettiğini ispatlayamaz.

    Aynı özne ve amaç için önceki yürürlükteki kayıt düşürülür ve yeni satır
    ona ``supersedes_id`` ile bağlanır; eski satırın içeriği değişmez.
    """
    purpose = resolve_purpose(
        db, ctx, tenant_id=tenant_id, code=purpose_code, name=purpose_name
    )

    notice = None
    if notice_version_id is not None:
        notice = db.get(NoticeVersion, notice_version_id)
        if notice is None or notice.tenant_id != tenant_id:
            raise NotFoundError(
                "compliance.notice.not_found", params={"id": notice_version_id}
            )
        if notice.status != NoticeStatus.PUBLISHED:
            raise ValidationError(
                "compliance.consent.notice_not_published",
                params={"notice_id": notice_version_id, "status": notice.status},
            )

    now = utcnow()
    if expires_at is not None and expires_at <= now:
        raise ValidationError("compliance.consent.expiry_in_past")

    previous = current_consent(
        db,
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_ref=subject_ref,
        purpose_id=purpose.id,
    )

    record = ConsentRecord(
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_ref=subject_ref,
        subject_hash=_digest(f"{subject_type}:{subject_ref}"),
        purpose_id=purpose.id,
        notice_version_id=notice.id if notice else None,
        notice_shown_at=now if notice else None,
        notice_acknowledged=True if notice else None,
        status=status,
        is_explicit=is_explicit,
        scope_text=scope_text,
        scope_codes=dumps(scope_codes or []),
        channel=channel,
        collected_at=collected_at or now,
        collected_by_user_id=ctx.user_id,
        expires_at=expires_at,
        language=language,
        proof_reference=proof_reference,
        ip_address=ctx.ip,
        user_agent=(ctx.user_agent or "")[:512] or None,
        version=(previous.version + 1) if previous else 1,
        supersedes_id=previous.id if previous else None,
        is_current=True,
        # Zayıf kanal ya da aydınlatmasız rıza otomatik geçerli sayılmaz.
        review_status=(
            ReviewStatus.REVIEW_REQUIRED
            if (channel in _WEAK_PROOF_CHANNELS or notice is None)
            else ReviewStatus.ACCEPTED
        ),
        created_by_id=ctx.user_id,
    )
    db.add(record)
    db.flush()

    if previous is not None:
        previous.is_current = False
        previous.superseded_by_id = record.id
        previous.updated_by_id = ctx.user_id

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.CONSENT_PROOF,
        title=f"Consent {status}: {subject_type}:{subject_ref} / {purpose_code}",
        description="Rıza beyanının kanıt kaydı.",
        subject_type=SUBJECT_CONSENT,
        subject_id=record.id,
        subject_ref=f"{subject_type}:{subject_ref}",
        payload=_consent_payload(record, purpose_code=purpose_code, notice=notice),
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    record.evidence_id = artifact.id
    db.flush()

    audit_service.record(
        db,
        "CREATE",
        entity_type=SUBJECT_CONSENT,
        entity_id=record.id,
        entity_label=f"{subject_type}:{subject_ref}/{purpose_code}",
        summary=f"Consent recorded ({status})",
        new_values={
            "status": status,
            "channel": channel,
            "is_explicit": is_explicit,
            "version": record.version,
            "notice_version_id": record.notice_version_id,
            "review_status": record.review_status,
        },
        **ctx.audit_kwargs(),
    )
    return record


def withdraw(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    consent_id: int,
    reason: str = WithdrawalReason.SUBJECT_REQUEST,
    reason_text: str | None = None,
    channel: str = IntakeChannel.UNKNOWN,
    requested_at: datetime | None = None,
    effective_at: datetime | None = None,
    triggers_erasure: bool = False,
) -> WithdrawalRecord:
    """
    Rızayı geri al.

    Geri alma, rıza satırının üzerine yazılmaz; kendi kanıtını taşıyan ayrı bir
    olaydır. Rıza satırının durumu ``WITHDRAWN``'a çekilir ki güncel durum tek
    sorguda okunabilsin, ama beyanın kendisi (kanal, tarih, metin özeti) olduğu
    gibi kalır.

    ``downstream_notified`` bilinçle ``False`` doğar: alıcılara bildirim
    yapılmadıkça geri alma tamamlanmış sayılmaz.
    """
    consent = db.get(ConsentRecord, consent_id)
    if consent is None or consent.tenant_id != tenant_id:
        raise NotFoundError("compliance.consent.not_found", params={"id": consent_id})
    if consent.status == ConsentStatus.WITHDRAWN:
        raise ConflictError(
            "compliance.consent.already_withdrawn", params={"id": consent_id}
        )
    if not consent.is_current:
        raise ConflictError(
            "compliance.consent.not_current",
            params={"id": consent_id, "superseded_by": consent.superseded_by_id},
        )

    now = utcnow()
    effective = effective_at or now

    withdrawal = WithdrawalRecord(
        tenant_id=tenant_id,
        consent_record_id=consent.id,
        subject_type=consent.subject_type,
        subject_ref=consent.subject_ref,
        reason=reason,
        reason_text=reason_text,
        channel=channel,
        requested_at=requested_at or now,
        effective_at=effective,
        processed_at=now,
        processed_by_user_id=ctx.user_id,
        stops_future_processing=True,
        triggers_erasure=triggers_erasure,
        downstream_notified=False,
        review_status=ReviewStatus.REVIEW_REQUIRED,
        created_by_id=ctx.user_id,
    )
    db.add(withdrawal)
    db.flush()

    previous_status = consent.status
    consent.status = ConsentStatus.WITHDRAWN
    consent.updated_by_id = ctx.user_id

    artifact = evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.DECISION_RECORD,
        title=f"Consent withdrawn: {consent.subject_type}:{consent.subject_ref}",
        description="Rızanın geri alınması — beyan satırı değiştirilmedi.",
        subject_type=SUBJECT_WITHDRAWAL,
        subject_id=withdrawal.id,
        subject_ref=f"{consent.subject_type}:{consent.subject_ref}",
        payload={
            "consent_record_id": consent.id,
            "previous_status": previous_status,
            "reason": reason,
            "reason_text": reason_text,
            "channel": channel,
            "requested_at": withdrawal.requested_at.isoformat()
            if withdrawal.requested_at
            else None,
            "effective_at": effective.isoformat(),
            "processed_at": now.isoformat(),
            "triggers_erasure": triggers_erasure,
            "downstream_notified": False,
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    withdrawal.evidence_id = artifact.id
    db.flush()

    audit_service.record(
        db,
        "UPDATE",
        entity_type=SUBJECT_CONSENT,
        entity_id=consent.id,
        entity_label=f"{consent.subject_type}:{consent.subject_ref}",
        summary="Consent withdrawn",
        old_values={"status": previous_status},
        new_values={
            "status": consent.status,
            "withdrawal_id": withdrawal.id,
            "reason": reason,
        },
        **ctx.audit_kwargs(),
    )
    log.info(
        "consent %s withdrawn tenant=%s reason=%s", consent.id, tenant_id, reason
    )
    return withdrawal


def _consent_payload(
    record: ConsentRecord, *, purpose_code: str, notice: NoticeVersion | None
) -> dict[str, Any]:
    """
    Kanıt yükü.

    Kişi adı ve iletişim bilgisi bilerek dışarıda bırakılır: kanıt kaydına
    kişisel veri kopyalamak, uyumluluk katmanını yeni bir veri havuzuna
    çevirir. Taşınan tek kimlik, ana sisteme işaret eden opak referanstır.
    """
    return {
        "subject_type": record.subject_type,
        "subject_ref": record.subject_ref,
        "purpose_code": purpose_code,
        "version": record.version,
        "status": record.status,
        "is_explicit": record.is_explicit,
        "channel": record.channel,
        "collected_at": record.collected_at.isoformat() if record.collected_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "notice_version_id": record.notice_version_id,
        "notice_hash": notice.body_hash if notice else None,
        "proof_reference": record.proof_reference,
        "supersedes_id": record.supersedes_id,
        "review_status": record.review_status,
    }


def list_consents(
    db: Session,
    *,
    tenant_id: int,
    subject_ref: str | None = None,
    subject_type: str | None = None,
    purpose_code: str | None = None,
    status: str | None = None,
    current_only: bool = True,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[tuple[ConsentRecord, str | None]], int]:
    """Rıza kayıtları ve her satırın amaç kodu."""
    conds: list[Any] = [ConsentRecord.tenant_id == tenant_id]
    if subject_ref:
        conds.append(ConsentRecord.subject_ref == subject_ref)
    if subject_type:
        conds.append(ConsentRecord.subject_type == subject_type)
    if status:
        conds.append(ConsentRecord.status == status)
    if current_only:
        conds.append(ConsentRecord.is_current.is_(True))
    if purpose_code:
        purpose_id = db.execute(
            select(Purpose.id).where(
                Purpose.tenant_id == tenant_id, Purpose.code == purpose_code
            )
        ).scalar_one_or_none()
        conds.append(ConsentRecord.purpose_id == (purpose_id or -1))

    total = int(
        db.execute(select(func.count(ConsentRecord.id)).where(*conds)).scalar_one() or 0
    )
    rows = db.execute(
        select(ConsentRecord, Purpose.code)
        .outerjoin(Purpose, Purpose.id == ConsentRecord.purpose_id)
        .where(*conds)
        .order_by(ConsentRecord.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [(row[0], row[1]) for row in rows], total


def effective_status(record: ConsentRecord, *, now: datetime | None = None) -> str:
    """
    Kaydın *şu anki* geçerliliği.

    Saklanan durum ile geçerli durum ayrı raporlanır: süresi dolmuş bir rıza
    satırında hâlâ ``GIVEN`` yazar, ama o rıza geçerli değildir. Satırı
    sonradan güncellemek beyanı değiştirmek olurdu; bu yüzden hesap okuma
    anında yapılır.
    """
    if not record.is_current:
        return ConsentStatus.SUPERSEDED
    if record.status == ConsentStatus.GIVEN and record.expires_at is not None:
        if record.expires_at <= (now or utcnow()):
            return ConsentStatus.EXPIRED
    return record.status


def consent_to_dict(record: ConsentRecord, purpose_code: str | None = None) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "subject_type": record.subject_type,
        "subject_ref": record.subject_ref,
        "purpose_id": record.purpose_id,
        "purpose_code": purpose_code,
        "version": record.version,
        "supersedes_id": record.supersedes_id,
        "superseded_by_id": record.superseded_by_id,
        "status": record.status,
        "effective_status": effective_status(record),
        "is_explicit": record.is_explicit,
        "channel": record.channel,
        "notice_version_id": record.notice_version_id,
        "collected_at": record.collected_at,
        "expires_at": record.expires_at,
        "proof_reference": record.proof_reference,
        "review_status": record.review_status,
        "is_current": record.is_current,
        "evidence_id": record.evidence_id,
        "created_at": record.created_at,
    }


def status_summary(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """
    Genel durum tablosunun rıza satırı.

    Süresi dolmuş rızalar ayrı sayılır: veritabanında ``GIVEN`` yazan bir satır
    raporda geçerli görünürse rapor gerçekten yanlış olur.
    """
    rows = (
        db.execute(
            select(ConsentRecord).where(
                ConsentRecord.tenant_id == tenant_id,
                ConsentRecord.is_current.is_(True),
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    counts = {"given": 0, "withdrawn": 0, "expired": 0, "other": 0}
    pending_review = 0
    last: datetime | None = None
    for row in rows:
        state = effective_status(row, now=now)
        if state == ConsentStatus.GIVEN:
            counts["given"] += 1
        elif state == ConsentStatus.WITHDRAWN:
            counts["withdrawn"] += 1
        elif state == ConsentStatus.EXPIRED:
            counts["expired"] += 1
        else:
            counts["other"] += 1
        if row.review_status == ReviewStatus.REVIEW_REQUIRED:
            pending_review += 1
        if row.collected_at and (last is None or row.collected_at > last):
            last = row.collected_at

    published_notices = int(
        db.execute(
            select(func.count(NoticeVersion.id)).where(
                NoticeVersion.tenant_id == tenant_id,
                NoticeVersion.is_current.is_(True),
            )
        ).scalar_one()
        or 0
    )
    unprocessed_withdrawals = int(
        db.execute(
            select(func.count(WithdrawalRecord.id)).where(
                WithdrawalRecord.tenant_id == tenant_id,
                WithdrawalRecord.downstream_notified.is_(False),
            )
        ).scalar_one()
        or 0
    )

    return {
        "total": len(rows),
        "counts": counts,
        "pending_review": pending_review,
        "published_notices": published_notices,
        "withdrawals_without_downstream_notice": unprocessed_withdrawals,
        "last_activity_at": last,
    }
