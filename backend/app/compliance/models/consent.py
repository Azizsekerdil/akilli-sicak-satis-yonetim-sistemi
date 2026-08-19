"""
Aydınlatma, açık rıza ve rızanın geri alınması — üç ayrı kayıt.

Bu modülün en önemli tasarım kararı, birleştirmediği şeydir.

**Aydınlatma ile rıza aynı kayıtta tutulamaz.** Aydınlatma yükümlülüğü, rıza
alınıp alınmamasından bağımsız olarak doğar; sözleşmenin ifası veya hukuki
yükümlülük gibi dayanaklarla işlenen verilerde rıza yoktur ama aydınlatma
yine gerekir. Tek bir "onay verildi" satırı tutmak iki ayrı yükümlülüğü tek
bir kanıta indirger ve ikisinden birinin eksikliğini görünmez kılar. Bu yüzden
``NoticeVersion`` metnin kendisini ve sürümünü, ``ConsentRecord`` ise iradenin
beyanını taşır; bağ ``ConsentRecord.notice_version_id`` üzerinden kurulur ve
"hangi metin gösterildi?" sorusu yıllar sonra da cevaplanabilir.

**Rızanın geri alınması ayrı bir olaydır.** ``ConsentRecord`` üzerindeki bir
alanı güncellemek, geri alma anını ve kanalını kaybederdi; oysa ispat yükü
tam da o ana ilişkindir. ``WithdrawalRecord`` bu olayı kendi kanıtıyla saklar.

**Rıza kayıtları append-only'dir.** Rıza kapsamı değiştiğinde eski satır
güncellenmez; yeni sürüm yazılır ve eskisi ``superseded_by_id`` ile ona
bağlanır. Böylece "o tarihte neye rıza verilmişti?" sorusu geçmişe dönük
olarak cevaplanabilir.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    ConsentStatus,
    IntakeChannel,
    NoticeKind,
    NoticeStatus,
    ReviewStatus,
    WithdrawalReason,
)
from app.compliance.models.tenant import tenant_fk
from app.models.base import (
    AuthorMixin,
    Base,
    JSONText,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class NoticeVersion(Base, TimestampMixin, AuthorMixin):
    """
    Aydınlatma metninin belirli bir sürümü ve dili.

    ``body_hash`` metnin tam özetidir. Bir aydınlatma metninin sonradan
    "düzeltilmesi" yaygındır; özet olmadan, ilgili kişiye gösterilen metnin
    bugün veritabanında duran metin olduğu iddia edilemez.

    Metin sürümleri güncellenmez: değişiklik yeni sürümdür ve ``supersedes_id``
    ile eskisine bağlanır.
    """

    __tablename__ = "cmp_notice_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "notice_code", "version", "language",
            name="uq_cmp_notice_versions_tenant_code_version_lang",
        ),
        Index("ix_cmp_notice_versions_current", "tenant_id", "notice_code", "is_current"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    #: Metin ailesi (ör. "MUSTERI_AYDINLATMA"); sürümler bu kodu paylaşır.
    notice_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), default=NoticeKind.PRIVACY_NOTICE, nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text)

    #: Metnin hangi işleme faaliyetlerini kapsadığı — kod listesi.
    covered_activity_codes: Mapped[str | None] = mapped_column(JSONText)
    covered_purpose_codes: Mapped[str | None] = mapped_column(JSONText)

    status: Mapped[str] = mapped_column(
        String(16), default=NoticeStatus.DRAFT, nullable=False, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Aynı anda yalnızca bir sürüm yürürlükte olmalıdır; bunu uygulamak
    #: servis katmanının işi — kısıt olarak kurulamaz çünkü dil başına ayrı
    #: yürürlük mümkündür.
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    display_url: Mapped[str | None] = mapped_column(String(512))
    display_channel: Mapped[str] = mapped_column(
        String(24), default=IntakeChannel.UNKNOWN, nullable=False
    )

    approved_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    supersedes_id: Mapped[int | None] = fk(
        "cmp_notice_versions.id", nullable=True, ondelete="RESTRICT"
    )
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    consents: Mapped[list["ConsentRecord"]] = relationship(back_populates="notice_version")


class ConsentRecord(Base, TimestampMixin, AuthorMixin):
    """
    Tek bir irade beyanı — append-only.

    ``is_explicit`` ayrı bir bayraktır: örtülü onay ile açık rıza aynı hukuki
    değeri taşımaz ve özel nitelikli veri ile yurt dışı aktarımda yalnızca
    ikincisi iş görür. Tek bir "onaylandı" bayrağı bu farkı siler.

    ``subject_ref`` yabancı anahtar değildir. Rıza kaydı, ilgili kişinin
    operasyonel kaydı silindikten sonra da ispat aracı olarak durmalıdır;
    ayrıca rıza müşteri olmayan kişilerden de alınabilir.
    """

    __tablename__ = "cmp_consent_records"
    __table_args__ = (
        Index(
            "ix_cmp_consent_records_subject",
            "tenant_id", "subject_type", "subject_ref",
        ),
        Index(
            "ix_cmp_consent_records_current",
            "tenant_id", "purpose_id", "is_current",
        ),
        Index("ix_cmp_consent_records_status_time", "tenant_id", "status", "collected_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Ham kimlik yerine arama yapılabilmesi için tuzlanmış özet; ham değer
    #: gerekmeyen raporlarda bu sütun kullanılır.
    subject_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    purpose_id: Mapped[int | None] = fk("cmp_purposes.id", nullable=True, ondelete="RESTRICT")
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )

    #: Rızanın alındığı anda gösterilen aydınlatma metni. Boş olması, rızanın
    #: aydınlatma olmadan alındığı anlamına gelir ve bulgu üretir.
    notice_version_id: Mapped[int | None] = fk(
        "cmp_notice_versions.id", nullable=True, ondelete="RESTRICT"
    )
    notice_shown_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notice_acknowledged: Mapped[bool | None] = mapped_column(Boolean)

    status: Mapped[str] = mapped_column(
        String(16), default=ConsentStatus.PENDING, nullable=False, index=True
    )
    is_explicit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Rızanın kapsamı serbest metin olarak da saklanır: sonradan enum'a
    #: sığmayan kapsamlar çıkar ve o zaman ham beyan tek kanıttır.
    scope_text: Mapped[str | None] = mapped_column(Text)
    scope_codes: Mapped[str | None] = mapped_column(JSONText)

    channel: Mapped[str] = mapped_column(
        String(24), default=IntakeChannel.UNKNOWN, nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    collected_by_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)

    #: İspat unsurları. IP ve cihaz bilgisi de kişisel veridir; bu yüzden
    #: kendileri de envantere girer ve saklama süresine tabidir.
    proof_reference: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    device_label: Mapped[str | None] = mapped_column(String(128))
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[int | None] = fk(
        "cmp_consent_records.id", nullable=True, ondelete="RESTRICT"
    )
    superseded_by_id: Mapped[int | None] = fk(
        "cmp_consent_records.id", nullable=True, ondelete="RESTRICT"
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    notice_version: Mapped["NoticeVersion | None"] = relationship(back_populates="consents")
    withdrawals: Mapped[list["WithdrawalRecord"]] = relationship(
        back_populates="consent_record", lazy="selectin"
    )


class WithdrawalRecord(Base, TimestampMixin, AuthorMixin):
    """
    Rızanın geri alınması — kendi kanıtını taşıyan ayrı olay.

    ``effective_at`` ile ``processed_at`` ayrıdır ve bu ayrım denetimde
    doğrudan ölçülür: geri alma beyanının verildiği an ile işlemenin fiilen
    durdurulduğu an arasındaki gecikme, uyumsuzluğun en sık görülen biçimidir.

    ``downstream_notified`` alanı sessizce ``True`` varsayılmaz: alıcılara
    bildirim yapılmadıysa geri alma tamamlanmamıştır.
    """

    __tablename__ = "cmp_withdrawal_records"
    __table_args__ = (
        Index(
            "ix_cmp_withdrawal_records_subject",
            "tenant_id", "subject_type", "subject_ref",
        ),
        Index("ix_cmp_withdrawal_records_effective", "tenant_id", "effective_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    consent_record_id: Mapped[int] = fk("cmp_consent_records.id", ondelete="RESTRICT")

    #: Rıza kaydından kopyalanır; rıza satırına gitmeden kişi bazlı sorgu
    #: yapılabilmesi için — geri alma taleplerinde süre kritiktir.
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)

    reason: Mapped[str] = mapped_column(
        String(32), default=WithdrawalReason.SUBJECT_REQUEST, nullable=False
    )
    reason_text: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(
        String(24), default=IntakeChannel.UNKNOWN, nullable=False
    )

    requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_by_user_id: Mapped[int | None] = mapped_column(Integer)

    #: Geri alma yalnızca ileriye etkilidir; geçmiş işlemenin hukuka
    #: uygunluğunu kendiliğinden ortadan kaldırmaz. Bu ayrımın kayıtta
    #: görünmesi, sonraki değerlendirmenin doğru yapılmasını sağlar.
    stops_future_processing: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    triggers_erasure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    erasure_note: Mapped[str | None] = mapped_column(Text)

    downstream_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    downstream_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    downstream_recipients: Mapped[str | None] = mapped_column(JSONText)

    proof_reference: Mapped[str | None] = mapped_column(String(512))
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    consent_record: Mapped["ConsentRecord"] = relationship(back_populates="withdrawals")


__all__ = ["ConsentRecord", "NoticeVersion", "WithdrawalRecord"]
