"""
Saklama, imha ve hukuki muhafaza.

Saklama politikası yazmak kolaydır; onu **çalıştırdığını kanıtlamak** zordur.
Bu modül ikinciye göre kurulur:

* ``RetentionPolicy`` kuralı tanımlar ve kuralın *neye dayandığını* saklar.
  Süresi bilinmeyen bir politika ``NULL`` gün ile ``REVIEW_REQUIRED`` kalır;
  varsayılan bir süre uydurmak, denetimde asılsız bir iddiaya dönüşürdü.
* ``RetentionEvent`` her çalıştırmayı ayrı satır olarak yazar — append-only.
  Planlanan, onay bekleyen, yürütülen ve engellenen çalıştırmalar aynı tabloda
  ama ayrı sonuçlarla durur; "imha yapıldı" iddiası satırla karşılaştırılır.
* ``LegalHold`` imhayı durdurur ve durdurmanın **sebebini** taşır. Sessizce
  atlanmış bir imha ile hukuki muhafaza nedeniyle bilinçli durdurulmuş imha
  denetimde aynı şey değildir; ``RetentionEvent.legal_hold_id`` bu farkı kayda
  geçirir.

Otomatik imha varsayılan olarak insan onayına bağlıdır
(``requires_human_approval = True``). Geri alınamaz bir işlemi onaysız
çalıştırmayı varsayılan yapmak, HSP'nin devre dışı kaldığı ilk yer olurdu.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    LegalHoldStatus,
    RetentionAction,
    RetentionEventOutcome,
    RetentionTrigger,
    ReviewStatus,
)
from app.compliance.models.tenant import tenant_fk
from app.models.base import (
    AuthorMixin,
    Base,
    CodeNameMixin,
    JSONText,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class RetentionPolicy(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Bir veri kümesi için saklama kuralı.

    Hedef, ``DataCategory``/``DataStore`` üzerinden mantıksal olarak ya da
    ``table_name``/``column_name`` üzerinden fiziksel olarak belirtilebilir.
    Fiziksel bağ isimledir, yabancı anahtarla değil — ``DataField`` ile aynı
    gerekçeyle: politika, şemanın bugünkü hâline değil ölçüm anındaki hâline
    tanıklık eder.

    ``minimum_period_days`` ve ``retention_period_days`` ayrı alanlardır çünkü
    çatışabilirler: mevzuattan doğan asgari saklama ile veri minimizasyonundan
    doğan azami saklama aynı veri için farklı yönlere çeker, ve bu çatışmanın
    görünür olması gerekir.
    """

    __tablename__ = "cmp_retention_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_retention_policies_tenant_code"),
        Index("ix_cmp_retention_policies_tenant_next", "tenant_id", "next_run_at"),
        Index("ix_cmp_retention_policies_target", "tenant_id", "table_name"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    data_category_id: Mapped[int | None] = fk(
        "cmp_data_categories.id", nullable=True, ondelete="SET NULL"
    )
    data_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )
    table_name: Mapped[str | None] = mapped_column(String(128))
    column_name: Mapped[str | None] = mapped_column(String(128))
    #: Hangi satırların kapsandığı — insan tarafından okunabilir ölçüt.
    scope_filter: Mapped[str | None] = mapped_column(Text)

    #: NULL, "süre belirlenmedi" demektir ve otomatik imhayı engeller.
    retention_period_days: Mapped[int | None] = mapped_column(Integer)
    minimum_period_days: Mapped[int | None] = mapped_column(Integer)
    maximum_period_days: Mapped[int | None] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(
        String(32), default=RetentionTrigger.UNKNOWN, nullable=False
    )
    trigger_field: Mapped[str | None] = mapped_column(String(128))
    action_at_expiry: Mapped[str] = mapped_column(
        String(24), default=RetentionAction.REVIEW, nullable=False
    )

    #: Sürenin dayanağı. Mevzuattan geliyorsa dayanak kaydına bağlanır;
    #: bağlanmamış bir süre iddiası doğrulanmamış sayılır.
    legal_basis_id: Mapped[int | None] = fk(
        "cmp_legal_bases.id", nullable=True, ondelete="SET NULL"
    )
    justification: Mapped[str | None] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(String(255))
    source_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    #: Geri alınamaz bir işlem için varsayılan onay: insan. Bu varsayılanı
    #: gevşetmek bilinçli bir karar olmalı, ihmalin sonucu değil.
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    dry_run_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    approved_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False, index=True
    )

    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    events: Mapped[list["RetentionEvent"]] = relationship(back_populates="policy")


class RetentionEvent(Base, TimestampMixin, AuthorMixin):
    """
    Tek bir imha/anonimleştirme çalıştırması — append-only.

    ``affected_count`` ile ``dry_run`` birlikte okunur: prova çalıştırmasında
    bulunan 12.000 satır, gerçekten silinmiş 12.000 satırla karıştırılamaz.

    Onay referansı (``approval_reference``) HSP kararına bağlanır. Onay
    gerektiren bir politika onaysız yürütülmüşse bu alan boş kalır ve bulgu
    üretir — sessiz fail-open'ın yakalandığı yer burasıdır.
    """

    __tablename__ = "cmp_retention_events"
    __table_args__ = (
        Index("ix_cmp_retention_events_policy_time", "policy_id", "planned_at"),
        Index("ix_cmp_retention_events_tenant_outcome", "tenant_id", "outcome"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    policy_id: Mapped[int] = fk("cmp_retention_policies.id", ondelete="RESTRICT")

    planned_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    action: Mapped[str] = mapped_column(
        String(24), default=RetentionAction.REVIEW, nullable=False
    )
    outcome: Mapped[str] = mapped_column(
        String(32), default=RetentionEventOutcome.PLANNED, nullable=False
    )

    target_table: Mapped[str | None] = mapped_column(String(128))
    target_filter: Mapped[str | None] = mapped_column(Text)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    affected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: İmha durdurulduysa sebebi kayda geçer; boş bir "atlandı" yeterli değil.
    legal_hold_id: Mapped[int | None] = fk(
        "cmp_legal_holds.id", nullable=True, ondelete="SET NULL"
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(255))

    requested_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approval_reference: Mapped[str | None] = mapped_column(String(128))

    #: Silinen kayıtların kimlikleri saklanmaz — silinen veriyi imha kaydında
    #: yeniden üretmek imhanın kendisini anlamsız kılardı. Yalnızca özet.
    result_summary: Mapped[str | None] = mapped_column(JSONText)
    error_detail: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    policy: Mapped["RetentionPolicy"] = relationship(back_populates="events")
    legal_hold: Mapped["LegalHold | None"] = relationship(back_populates="blocked_events")


class LegalHold(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Hukuki muhafaza — imhayı durduran açık karar.

    ``reason`` zorunludur. Sebebi olmayan bir muhafaza, süresiz saklamanın
    kılıfına dönüşür; oysa muhafaza istisnadır ve kaldırılması da kayda
    geçmelidir (``released_by_user_id``, ``release_note``).
    """

    __tablename__ = "cmp_legal_holds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_legal_holds_tenant_code"),
        Index("ix_cmp_legal_holds_tenant_status", "tenant_id", "status"),
        Index("ix_cmp_legal_holds_scope", "tenant_id", "scope_table"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=LegalHoldStatus.ACTIVE, nullable=False
    )

    scope_table: Mapped[str | None] = mapped_column(String(128))
    scope_filter: Mapped[str | None] = mapped_column(Text)
    data_category_id: Mapped[int | None] = fk(
        "cmp_data_categories.id", nullable=True, ondelete="SET NULL"
    )
    data_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_ref: Mapped[str | None] = mapped_column(String(128), index=True)

    #: Muhafazayı gerektiren merci/dosya; serbest metin, doğrulanmaz.
    authority: Mapped[str | None] = mapped_column(String(255))
    case_reference: Mapped[str | None] = mapped_column(String(128))

    issued_by_user_id: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    released_by_user_id: Mapped[int | None] = mapped_column(Integer)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    release_note: Mapped[str | None] = mapped_column(Text)

    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    blocked_events: Mapped[list["RetentionEvent"]] = relationship(
        back_populates="legal_hold"
    )


__all__ = ["LegalHold", "RetentionEvent", "RetentionPolicy"]
