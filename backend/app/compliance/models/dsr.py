"""
İlgili kişi başvuruları: kayıt, kimlik doğrulama, karşılama görevleri.

Üç varlık ayrı tutulur çünkü üç ayrı yükümlülüğü temsil ederler ve üçü de
bağımsız olarak başarısız olabilir:

* ``DataSubjectRequest`` başvurunun kendisi ve süresi.
* ``IdentityVerification`` kimliğin doğrulanması. Doğrulanmamış bir başvuruya
  kişisel veri göndermek, başvuruyu cevapsız bırakmaktan daha ağır bir
  ihlaldir; bu yüzden doğrulama denemeleri ayrı ve append-only tutulur.
* ``FulfilmentTask`` başvuruyu karşılamak için yapılması gereken somut işler.
  "Cevaplandı" demek yetmez; hangi depoda ne yapıldığı izlenebilir olmalıdır.

**Yasal süre koda gömülmez.** ``due_at`` bir tarihtir; hangi kuraldan
türetildiği ``due_basis`` alanında serbest metin olarak, doğrulama durumu ise
``due_basis_status`` alanında tutulur ve varsayılanı ``REVIEW_REQUIRED``'dır.
Rejime göre değişen ve mevzuat değiştiğinde kayan bir gün sayısını modele
sabitlemek, sessizce yanlış son tarih üreten bir hata kaynağı olurdu.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    DsrStatus,
    DsrType,
    FulfilmentAction,
    IdentityVerificationMethod,
    IntakeChannel,
    ReviewStatus,
    TaskStatus,
    VerificationOutcome,
)
from app.compliance.models.tenant import tenant_fk
from app.models.base import (
    AuthorMixin,
    Base,
    JSONText,
    Money,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class DataSubjectRequest(Base, TimestampMixin, AuthorMixin):
    """
    Bir ilgili kişinin başvurusu.

    ``subject_name`` ve ``subject_contact`` başvuruyu cevaplamak için gereken
    asgari veridir ve kendileri de kişisel veridir: bu tablo kendi saklama
    politikasına tabidir, süresiz tutulamaz.

    Reddedilen başvurularda ``rejection_basis_id`` boş bırakılamaz olmalıdır —
    dayanaksız ret, cevapsızlıkla aynı sonucu doğurur. Zorunluluğu servis
    katmanı uygular; model, dayanağın *yeri* olduğunu garanti eder.
    """

    __tablename__ = "cmp_data_subject_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "reference", name="uq_cmp_data_subject_requests_tenant_ref"
        ),
        Index("ix_cmp_dsr_tenant_status", "tenant_id", "status"),
        Index("ix_cmp_dsr_tenant_due", "tenant_id", "due_at"),
        Index("ix_cmp_dsr_subject", "tenant_id", "subject_type", "subject_ref"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    #: Başvurana bildirilen dosya numarası.
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    request_type: Mapped[str] = mapped_column(
        String(32), default=DsrType.ACCESS, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default=DsrStatus.RECEIVED, nullable=False
    )

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str | None] = mapped_column(String(128))
    subject_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_name: Mapped[str | None] = mapped_column(String(255))
    subject_contact: Mapped[str | None] = mapped_column(String(255))
    #: Vekil aracılığıyla yapılan başvurularda yetki belgesi de doğrulanmalıdır.
    submitted_by_agent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(255))

    channel: Mapped[str] = mapped_column(
        String(24), default=IntakeChannel.UNKNOWN, nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    requested_scope: Mapped[str | None] = mapped_column(Text)
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )

    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Süreyi hangi kuralın doğurduğu — serbest metin, sistemce doğrulanmaz.
    due_basis: Mapped[str | None] = mapped_column(String(255))
    due_basis_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    is_extended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extension_reason: Mapped[str | None] = mapped_column(Text)
    extended_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    identity_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    identity_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    assigned_to_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    response_channel: Mapped[str] = mapped_column(
        String(24), default=IntakeChannel.UNKNOWN, nullable=False
    )
    response_summary: Mapped[str | None] = mapped_column(Text)
    response_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    rejection_reason: Mapped[str | None] = mapped_column(Text)
    rejection_basis_id: Mapped[int | None] = fk(
        "cmp_legal_bases.id", nullable=True, ondelete="SET NULL"
    )

    #: Ücret istisnai hâllerde alınabilir; alındıysa tutarı ve gerekçesi
    #: kayda geçer, çünkü ücret talebi başvuruyu caydırma aracına dönüşebilir.
    fee_amount: Mapped[Decimal | None] = mapped_column(Money)
    fee_currency: Mapped[str | None] = mapped_column(String(8))
    fee_justification: Mapped[str | None] = mapped_column(Text)

    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    authority_case_reference: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)

    verifications: Mapped[list["IdentityVerification"]] = relationship(
        back_populates="request", lazy="selectin"
    )
    tasks: Mapped[list["FulfilmentTask"]] = relationship(
        back_populates="request", lazy="selectin"
    )


class IdentityVerification(Base, TimestampMixin, AuthorMixin):
    """
    Kimlik doğrulama denemesi — append-only.

    Kimlik belgesinin kendisi **saklanmaz**; yalnızca türü ve özeti tutulur.
    Doğrulama amacıyla toplanan bir kimlik fotokopisini süresiz saklamak,
    başvuruyu karşılarken yeni bir uyumsuzluk üretmek olurdu.

    Başarısız denemeler de satır olarak kalır: art arda başarısız denemeler,
    kimlik avı girişiminin ilk işaretidir.
    """

    __tablename__ = "cmp_identity_verifications"
    __table_args__ = (
        Index("ix_cmp_identity_verifications_request", "request_id", "attempted_at"),
        Index("ix_cmp_identity_verifications_tenant_outcome", "tenant_id", "outcome"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    request_id: Mapped[int] = fk("cmp_data_subject_requests.id", ondelete="RESTRICT")

    method: Mapped[str] = mapped_column(
        String(40), default=IdentityVerificationMethod.UNKNOWN, nullable=False
    )
    outcome: Mapped[str] = mapped_column(
        String(24), default=VerificationOutcome.PENDING, nullable=False
    )

    attempted_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_by_user_id: Mapped[int | None] = mapped_column(Integer)

    #: Belgenin türü ve özeti; içeriği değil.
    document_type: Mapped[str | None] = mapped_column(String(64))
    document_hash: Mapped[str | None] = mapped_column(String(64))
    #: Belge geçici olarak saklandıysa ne zaman imha edileceği.
    document_disposal_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    document_disposed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    evidence_reference: Mapped[str | None] = mapped_column(String(512))
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    request: Mapped["DataSubjectRequest"] = relationship(back_populates="verifications")


class FulfilmentTask(Base, TimestampMixin, AuthorMixin):
    """
    Başvuruyu karşılamak için yapılacak somut iş.

    ``requires_human_approval`` varsayılanı ``True``'dur ve silme/anonimleştirme
    gibi geri alınamaz işlerde bilinçli olarak gevşetilmemelidir. Onaysız
    yürütülen bir görev, ``approval_reference`` boş kaldığı için denetimde
    görünür.

    ``legal_hold_id`` görevin neden bloke olduğunu taşır: hukuki muhafaza
    altındaki bir veri silinemez ve bu, başvurunun kısmen karşılanmasının
    meşru sebebidir — ama sebebin yazılı olması şartıyla.
    """

    __tablename__ = "cmp_fulfilment_tasks"
    __table_args__ = (
        Index("ix_cmp_fulfilment_tasks_request_seq", "request_id", "sequence"),
        Index("ix_cmp_fulfilment_tasks_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    request_id: Mapped[int] = fk("cmp_data_subject_requests.id", ondelete="CASCADE")

    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action: Mapped[str] = mapped_column(
        String(24), default=FulfilmentAction.LOCATE, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    data_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )
    target_table: Mapped[str | None] = mapped_column(String(128))
    target_filter: Mapped[str | None] = mapped_column(Text)
    recipient_id: Mapped[int | None] = fk(
        "cmp_recipients.id", nullable=True, ondelete="SET NULL"
    )

    status: Mapped[str] = mapped_column(
        String(24), default=TaskStatus.PENDING, nullable=False
    )
    assigned_to_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    requires_human_approval: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approval_reference: Mapped[str | None] = mapped_column(String(128))

    affected_count: Mapped[int | None] = mapped_column(Integer)
    result_summary: Mapped[str | None] = mapped_column(JSONText)
    result_note: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    legal_hold_id: Mapped[int | None] = fk(
        "cmp_legal_holds.id", nullable=True, ondelete="SET NULL"
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(255))

    request: Mapped["DataSubjectRequest"] = relationship(back_populates="tasks")


__all__ = ["DataSubjectRequest", "FulfilmentTask", "IdentityVerification"]
