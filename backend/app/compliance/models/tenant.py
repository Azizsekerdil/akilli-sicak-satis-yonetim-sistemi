"""
Kiracı (tenant) ve çalışma alanı — uyumluluk katmanının kapsam köküdür.

Van Sales tek şirketli çalışabilir; buna rağmen uyumluluk kayıtları kiracıya
bağlanır. Sebebi teknik değil hukuki: veri sorumlusu sıfatı tüzel kişiye
aittir ve aynı kurulum içinde iki tüzel kişinin envanteri, rızası ve saklama
politikası birbirine karışamaz. ``tenant_id``'yi sonradan eklemek, üretimdeki
kanıt kayıtlarını yeniden yazmayı gerektirirdi.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    ComplianceRegime,
    Environment,
    ReviewStatus,
    TenantStatus,
    WorkspaceKind,
)
from app.models.base import (
    AuthorMixin,
    Base,
    CodeNameMixin,
    JSONText,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


def tenant_fk():
    """
    Kiracı kapsamındaki her tabloda tekrarlanan sütun.

    ``RESTRICT`` bilinçlidir: bir kiracı satırı silinerek envanterin, rızaların
    ve kanıtların sessizce yetim kalması engellenir. Kiracı kapatılacaksa
    ``status`` alanı ``ARCHIVED``'a çekilir — kayıt silinmez.
    """
    return fk("cmp_tenants.id", ondelete="RESTRICT", index=True)


class Tenant(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    Uyumluluk kapsamının sahibi olan tüzel kişi.

    ``company_id`` bilerek yabancı anahtar değildir: uyumluluk kayıtları,
    tanımladıkları operasyonel satırdan daha uzun yaşamak zorundadır. Şirket
    kaydı arşivlense bile geçmiş dönemin envanteri ve kanıtı okunabilir kalır.
    """

    __tablename__ = "cmp_tenants"
    __table_args__ = (
        UniqueConstraint("code", name="uq_cmp_tenants_code"),
    )

    id: Mapped[int] = pk()
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    legal_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(24), default=TenantStatus.ACTIVE, nullable=False, index=True
    )

    #: Kiracının tabi olduğu ana rejim; ek rejimler JSON listesinde tutulur.
    primary_regime: Mapped[str] = mapped_column(
        String(32), default=ComplianceRegime.UNKNOWN, nullable=False, index=True
    )
    applicable_regimes: Mapped[str | None] = mapped_column(JSONText)

    #: ISO 3166-1 alpha-2. Yurt dışı aktarım değerlendirmesinin referans noktası.
    home_country: Mapped[str | None] = mapped_column(String(2), index=True)
    establishment_note: Mapped[str | None] = mapped_column(Text)

    #: Veri sorumlusu sicil bilgisi. Sicil numarası doğrulanmadan "kayıtlı"
    #: sayılmaz; bu yüzden numara ile doğrulama durumu ayrı sütunlardır.
    registry_id: Mapped[str | None] = mapped_column(String(64))
    registry_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.UNKNOWN, nullable=False
    )
    registry_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    dpo_name: Mapped[str | None] = mapped_column(String(255))
    dpo_email: Mapped[str | None] = mapped_column(String(255))
    dpo_phone: Mapped[str | None] = mapped_column(String(32))
    representative_name: Mapped[str | None] = mapped_column(String(255))
    representative_country: Mapped[str | None] = mapped_column(String(2))
    contact_address: Mapped[str | None] = mapped_column(Text)

    default_language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    default_timezone: Mapped[str] = mapped_column(
        String(64), default="Europe/Istanbul", nullable=False
    )

    #: Programın olgunluğu tek bir sayıya indirgenmez; yalnızca son ölçüm anı
    #: saklanır. Skor hesabı servis katmanının işidir ve kanıta dayanmalıdır.
    last_assessment_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="tenant", lazy="selectin"
    )


class Workspace(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    Kiracı içinde ayrı sorumluluk taşıyan kapsam bölümü.

    Bir işleme faaliyetinin sahibi çoğu zaman şirketin tamamı değil, belirli
    bir birim ya da projedir. Bulguların doğru kişiye düşmesi bu ayrıma bağlı.
    """

    __tablename__ = "cmp_workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_workspaces_tenant_code"),
        Index("ix_cmp_workspaces_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    parent_id: Mapped[int | None] = fk(
        "cmp_workspaces.id", nullable=True, ondelete="SET NULL"
    )

    kind: Mapped[str] = mapped_column(
        String(24), default=WorkspaceKind.BUSINESS_UNIT, nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(16), default=Environment.PRODUCTION, nullable=False
    )
    country: Mapped[str | None] = mapped_column(String(2))

    #: Sorumlu kullanıcı; FK verilmez ki kullanıcı kaydı silinse de kapsamın
    #: geçmişi okunabilir kalsın.
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    owner_label: Mapped[str | None] = mapped_column(String(128))

    #: Van Sales tarafındaki karşılığı (region/branch id gibi) — yalnızca
    #: raporlamada eşleştirme için, referans bütünlüğü iddiası olmadan.
    external_ref: Mapped[str | None] = mapped_column(String(128))

    is_in_scope: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scope_note: Mapped[str | None] = mapped_column(Text)

    tenant: Mapped["Tenant"] = relationship(back_populates="workspaces", lazy="joined")
    parent: Mapped["Workspace | None"] = relationship(
        "Workspace", back_populates="children", remote_side="Workspace.id"
    )
    children: Mapped[list["Workspace"]] = relationship(
        "Workspace", back_populates="parent"
    )


__all__ = ["Tenant", "Workspace", "tenant_fk"]
