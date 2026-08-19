"""
Veri envanteri — kişisel verinin nerede durduğu, neden işlendiği, kime gittiği.

Bu modül uyumluluk katmanının omurgasıdır. Bir rıza kaydı, bir saklama süresi
veya bir başvuru cevabı, hangi veriden söz ettiğini bilmeden üretilemez.

İki tasarım kararı tekrar tekrar karşınıza çıkacak:

* **Hiçbir şey varsayılan olarak uyumlu değildir.** Değerlendirme sütunlarının
  varsayılanı ``REVIEW_REQUIRED`` veya ``UNKNOWN``'dır. Boş bırakılmış bir alan
  raporu iyileştirmez, incelenecekler listesini uzatır.
* **Fiziksel şemaya isimle bağlanılır, yabancı anahtarla değil.**
  ``DataField.table_name``/``column_name`` çifti Van Sales tablolarını işaret
  eder ama ORM'e bağımlı değildir. Bir sütun kaldırıldığında envanter satırı
  kaybolmaz; "artık mevcut değil" bulgusuna dönüşür — ki asıl istenen budur.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    AssetKind,
    ComplianceRegime,
    ComplianceState,
    Criticality,
    DataSensitivity,
    DiscoverySource,
    DpaStatus,
    Environment,
    FlowDirection,
    HostingModel,
    IdentifiabilityLevel,
    LegalBasisKind,
    ProcessingRole,
    RecipientKind,
    ReviewStatus,
    StoreKind,
    TransferMechanism,
)
from app.compliance.models.tenant import tenant_fk
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


# ===========================================================================
# Veri sınıflandırması
# ===========================================================================
class DataCategory(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Veri kategorisi (kimlik, iletişim, konum, finans, ...).

    ``is_special_category`` ayrı bir bayraktır ve ``sensitivity`` alanından
    türetilmez: özel nitelikli veri, "çok hassas kişisel veri" değil, ayrı bir
    dayanak rejimine tabi ayrı bir hukuki kategoridir. İki alanı tek sütuna
    indirmek, bu ayrımı kodda görünmez kılardı.
    """

    __tablename__ = "cmp_data_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_data_categories_tenant_code"),
        Index("ix_cmp_data_categories_tenant_sens", "tenant_id", "sensitivity"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    parent_id: Mapped[int | None] = fk(
        "cmp_data_categories.id", nullable=True, ondelete="SET NULL"
    )

    sensitivity: Mapped[str] = mapped_column(
        String(24), default=DataSensitivity.UNKNOWN, nullable=False
    )
    identifiability: Mapped[str] = mapped_column(
        String(24), default=IdentifiabilityLevel.UNKNOWN, nullable=False
    )
    is_special_category: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    special_category_note: Mapped[str | None] = mapped_column(Text)

    #: Kategori düzeyinde varsayılan saklama; somut politika RetentionPolicy'de.
    default_retention_days: Mapped[int | None] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False, index=True
    )

    parent: Mapped["DataCategory | None"] = relationship(
        "DataCategory", back_populates="children", remote_side="DataCategory.id"
    )
    children: Mapped[list["DataCategory"]] = relationship(
        "DataCategory", back_populates="parent"
    )
    fields: Mapped[list["DataField"]] = relationship(back_populates="category")


class DataField(Base, TimestampMixin, AuthorMixin):
    """
    Fiziksel bir sütunun envanter kaydı.

    ``table_name``/``column_name`` çifti Van Sales şemasına **isimle** bağlanır.
    Yabancı anahtar verilmemesi bilinçlidir: envanter, ORM'in mevcut sürümüne
    değil, ölçüm anındaki şemaya tanıklık eder. Sütun silindiğinde kayıt
    kaybolmaz, ``is_present`` yanlışa döner ve fark bulguya dönüşür.
    """

    __tablename__ = "cmp_data_fields"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "table_name", "column_name",
            name="uq_cmp_data_fields_tenant_table_column",
        ),
        Index("ix_cmp_data_fields_tenant_sens", "tenant_id", "sensitivity"),
        Index("ix_cmp_data_fields_tenant_review", "tenant_id", "review_status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    category_id: Mapped[int | None] = fk(
        "cmp_data_categories.id", nullable=True, ondelete="SET NULL"
    )
    data_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )

    table_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_type: Mapped[str | None] = mapped_column(String(64))
    is_nullable: Mapped[bool | None] = mapped_column(Boolean)
    #: Ölçümün yapıldığı andaki kaynak konumu — bulguyu tekrar üretmek için.
    source_module: Mapped[str | None] = mapped_column(String(255))
    source_line: Mapped[int | None] = mapped_column(Integer)

    sensitivity: Mapped[str] = mapped_column(
        String(24), default=DataSensitivity.UNKNOWN, nullable=False
    )
    identifiability: Mapped[str] = mapped_column(
        String(24), default=IdentifiabilityLevel.UNKNOWN, nullable=False
    )
    is_special_category: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_encrypted: Mapped[bool | None] = mapped_column(Boolean)
    is_masked: Mapped[bool | None] = mapped_column(Boolean)
    masking_strategy: Mapped[str | None] = mapped_column(String(64))
    is_exported: Mapped[bool | None] = mapped_column(Boolean)
    is_logged: Mapped[bool | None] = mapped_column(Boolean)

    #: Tarayıcı çıktısı ile insan onayı ayrı tutulur; otomatik sınıflandırma
    #: tek başına kanıt değildir.
    discovered_by: Mapped[str] = mapped_column(
        String(24), default=DiscoverySource.UNKNOWN, nullable=False
    )
    discovered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(Integer)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    #: Son taramada sütunun hâlâ var olup olmadığı. Yanlışa dönmesi silme değil,
    #: incelenecek bir değişiklik anlamına gelir.
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    category: Mapped["DataCategory | None"] = relationship(back_populates="fields")
    data_store: Mapped["DataStore | None"] = relationship(back_populates="fields")


# ===========================================================================
# Sistem ve depolama
# ===========================================================================
class SystemAsset(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Uygulama, servis, cihaz veya üçüncü taraf sistem."""

    __tablename__ = "cmp_system_assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_system_assets_tenant_code"),
        Index("ix_cmp_system_assets_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    workspace_id: Mapped[int | None] = fk(
        "cmp_workspaces.id", nullable=True, ondelete="SET NULL"
    )
    vendor_id: Mapped[int | None] = fk("cmp_vendors.id", nullable=True, ondelete="SET NULL")

    kind: Mapped[str] = mapped_column(
        String(24), default=AssetKind.APPLICATION, nullable=False
    )
    environment: Mapped[str] = mapped_column(
        String(16), default=Environment.UNKNOWN, nullable=False
    )
    hosting_model: Mapped[str] = mapped_column(
        String(24), default=HostingModel.UNKNOWN, nullable=False
    )
    hosting_country: Mapped[str | None] = mapped_column(String(2), index=True)
    hosting_provider: Mapped[str | None] = mapped_column(String(255))

    version: Mapped[str | None] = mapped_column(String(64))
    repository_url: Mapped[str | None] = mapped_column(String(512))
    endpoint_url: Mapped[str | None] = mapped_column(String(512))
    is_internet_facing: Mapped[bool | None] = mapped_column(Boolean)
    #: Üç durumlu bilerek: NULL "ölçülmedi", False "ölçüldü ve yok" demektir.
    contains_personal_data: Mapped[bool | None] = mapped_column(Boolean)
    processes_special_category: Mapped[bool | None] = mapped_column(Boolean)

    criticality: Mapped[str] = mapped_column(
        String(16), default=Criticality.UNKNOWN, nullable=False
    )
    owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    stores: Mapped[list["DataStore"]] = relationship(back_populates="asset")


class DataStore(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    Verinin fiilen durduğu yer.

    ``connection_hint`` yalnızca insan için bir işarettir (sunucu adı, şema adı).
    Kimlik bilgisi buraya yazılmaz; uyumluluk envanterinin bir sır deposuna
    dönüşmesi, korumaya çalıştığı riski üretirdi.
    """

    __tablename__ = "cmp_data_stores"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_data_stores_tenant_code"),
        Index("ix_cmp_data_stores_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    asset_id: Mapped[int | None] = fk(
        "cmp_system_assets.id", nullable=True, ondelete="SET NULL"
    )

    kind: Mapped[str] = mapped_column(
        String(24), default=StoreKind.RELATIONAL_DB, nullable=False
    )
    connection_hint: Mapped[str | None] = mapped_column(String(255))
    schema_name: Mapped[str | None] = mapped_column(String(128))
    location_country: Mapped[str | None] = mapped_column(String(2), index=True)
    location_region: Mapped[str | None] = mapped_column(String(96))

    is_encrypted_at_rest: Mapped[bool | None] = mapped_column(Boolean)
    encryption_note: Mapped[str | None] = mapped_column(String(255))
    backup_note: Mapped[str | None] = mapped_column(String(255))
    access_control_note: Mapped[str | None] = mapped_column(Text)

    contains_special_category: Mapped[bool | None] = mapped_column(Boolean)
    record_count_estimate: Mapped[int | None] = mapped_column(Integer)
    estimate_measured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    asset: Mapped["SystemAsset | None"] = relationship(back_populates="stores")
    fields: Mapped[list["DataField"]] = relationship(back_populates="data_store")


# ===========================================================================
# Amaç ve hukuki dayanak
# ===========================================================================
class Purpose(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    İşleme amacı.

    Amaç ile dayanak ayrı varlıklardır: aynı amaç farklı veri kategorileri için
    farklı dayanaklara oturabilir (örneğin sözleşme ifası ile açık rıza aynı
    faaliyette yan yana bulunabilir).
    """

    __tablename__ = "cmp_purposes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_purposes_tenant_code"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    parent_id: Mapped[int | None] = fk("cmp_purposes.id", nullable=True, ondelete="SET NULL")

    #: Rıza gerektirip gerektirmediği amaca bakılarak belirlenir; bu bayrak
    #: aydınlatma metni ile rıza kaydının eşleşmesini denetlemek için kullanılır.
    requires_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_secondary_use: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compatibility_note: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    parent: Mapped["Purpose | None"] = relationship(
        "Purpose", back_populates="children", remote_side="Purpose.id"
    )
    children: Mapped[list["Purpose"]] = relationship("Purpose", back_populates="parent")


class LegalBasis(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Hukuki dayanak — enum değil, **kayıt**.

    Dayanağı bir enum üyesine indirgemek üç şeyi kaybettirirdi: dayanağın
    metnini, hangi kaynağa dayandığını ve kimin doğruladığını. Denetimde
    sorulan soru "hangi dayanak?" değil, "bu dayanağı nereden biliyorsunuz?"
    olduğu için üçü de saklanır.

    ``article_reference`` serbest metindir ve sistem tarafından **doğrulanmaz**.
    Doğrulanmamış bir referans ``REVIEW_REQUIRED`` kalır; madde numarasını koda
    gömmek, mevzuat değiştiğinde sessizce yanlış rapor üretirdi.
    """

    __tablename__ = "cmp_legal_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_legal_bases_tenant_code"),
        Index("ix_cmp_legal_bases_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    kind: Mapped[str] = mapped_column(
        String(32), default=LegalBasisKind.UNKNOWN, nullable=False
    )
    regime: Mapped[str] = mapped_column(
        String(32), default=ComplianceRegime.UNKNOWN, nullable=False
    )

    #: Mevzuat metninden alıntı veya iç politikanın ilgili bölümü.
    basis_text: Mapped[str | None] = mapped_column(Text)
    article_reference: Mapped[str | None] = mapped_column(String(128))
    article_reference_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    source_title: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(512))
    source_retrieved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    verified_by_user_id: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    #: Meşru menfaat dayanağı denge testi olmadan kullanılamaz; testin yapılıp
    #: yapılmadığı dayanağın kendi kaydında durur.
    balancing_test_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    balancing_test_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    balancing_test_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ===========================================================================
# İşleme faaliyeti (ROPA)
# ===========================================================================
class ProcessingActivity(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    İşleme faaliyeti kaydı — envanterin merkezî satırı.

    Özel nitelikli veri için **ayrı** bir dayanak alanı vardır. Tek dayanak
    alanı bırakılsaydı, sıradan kişisel veri için geçerli bir dayanağın özel
    nitelikli veriyi de kapsadığı izlenimi doğardı; oysa bu iki kategori ayrı
    dayanak gerektirir.
    """

    __tablename__ = "cmp_processing_activities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "code", name="uq_cmp_processing_activities_tenant_code"
        ),
        Index("ix_cmp_processing_activities_tenant_state", "tenant_id", "compliance_state"),
        Index("ix_cmp_processing_activities_tenant_review", "tenant_id", "next_review_due_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    workspace_id: Mapped[int | None] = fk(
        "cmp_workspaces.id", nullable=True, ondelete="SET NULL"
    )

    controller_role: Mapped[str] = mapped_column(
        String(24), default=ProcessingRole.UNKNOWN, nullable=False
    )
    joint_controller_note: Mapped[str | None] = mapped_column(Text)

    purpose_id: Mapped[int | None] = fk("cmp_purposes.id", nullable=True, ondelete="SET NULL")
    legal_basis_id: Mapped[int | None] = fk(
        "cmp_legal_bases.id", nullable=True, ondelete="SET NULL"
    )
    special_category_legal_basis_id: Mapped[int | None] = fk(
        "cmp_legal_bases.id", nullable=True, ondelete="SET NULL"
    )
    processes_special_category: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    #: İlgili kişi grupları — kod listesi (DataSubjectCategory değerleri).
    data_subject_categories: Mapped[str | None] = mapped_column(JSONText)
    subject_count_estimate: Mapped[int | None] = mapped_column(Integer)

    retention_policy_id: Mapped[int | None] = fk(
        "cmp_retention_policies.id", nullable=True, ondelete="SET NULL"
    )
    security_measures: Mapped[str | None] = mapped_column(Text)

    #: Etki değerlendirmesi: gerekli mi (üç durumlu), yapıldı mı, kanıtı nerede.
    dpia_required: Mapped[bool | None] = mapped_column(Boolean)
    dpia_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    dpia_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    involves_automated_decision: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    involves_profiling: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    involves_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Otomatik karar varsa insan denetiminin nasıl kurulduğu yazılmak
    #: zorundadır; boş bırakılması HSP açısından bulgu üretir.
    human_oversight_note: Mapped[str | None] = mapped_column(Text)

    cross_border_transfer: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    department: Mapped[str | None] = mapped_column(String(128))

    compliance_state: Mapped[str] = mapped_column(
        String(24), default=ComplianceState.UNKNOWN, nullable=False
    )
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_review_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    data_categories: Mapped[list["ActivityDataCategory"]] = relationship(
        back_populates="activity", lazy="selectin"
    )
    purposes: Mapped[list["ActivityPurpose"]] = relationship(
        back_populates="activity", lazy="selectin"
    )


class ActivityDataCategory(Base, TimestampMixin, AuthorMixin):
    """
    Faaliyet ile veri kategorisi arasındaki bağ.

    JSON listesi yerine tablo olmasının sebebi sorgu değil bütünlük: "hangi
    faaliyetler özel nitelikli veri işliyor?" sorusunun kategoriden faaliyete
    doğru da cevaplanabilmesi gerekir.
    """

    __tablename__ = "cmp_activity_data_categories"
    __table_args__ = (
        UniqueConstraint(
            "activity_id", "data_category_id",
            name="uq_cmp_activity_data_categories_pair",
        ),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    activity_id: Mapped[int] = fk("cmp_processing_activities.id", ondelete="CASCADE")
    data_category_id: Mapped[int] = fk("cmp_data_categories.id", ondelete="RESTRICT")

    #: Kategori genelde özel nitelikli olmasa da bu faaliyette öyle olabilir.
    is_special_category: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_mandatory: Mapped[bool | None] = mapped_column(Boolean)
    minimisation_note: Mapped[str | None] = mapped_column(Text)

    activity: Mapped["ProcessingActivity"] = relationship(back_populates="data_categories")
    data_category: Mapped["DataCategory"] = relationship()


class ActivityPurpose(Base, TimestampMixin, AuthorMixin):
    """
    Faaliyetin ikincil amaçları ve her amaç için ayrı dayanak.

    ``ProcessingActivity.purpose_id`` birincil amacı tutar; gerçek hayatta bir
    faaliyet birden çok amaca hizmet eder ve her amacın dayanağı farklı
    olabilir. Bu tablo o farkı kaybetmemek için var.
    """

    __tablename__ = "cmp_activity_purposes"
    __table_args__ = (
        UniqueConstraint(
            "activity_id", "purpose_id", name="uq_cmp_activity_purposes_pair"
        ),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    activity_id: Mapped[int] = fk("cmp_processing_activities.id", ondelete="CASCADE")
    purpose_id: Mapped[int] = fk("cmp_purposes.id", ondelete="RESTRICT")
    legal_basis_id: Mapped[int | None] = fk(
        "cmp_legal_bases.id", nullable=True, ondelete="SET NULL"
    )

    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    activity: Mapped["ProcessingActivity"] = relationship(back_populates="purposes")
    purpose: Mapped["Purpose"] = relationship()


# ===========================================================================
# Alıcılar, akışlar, aktarımlar
# ===========================================================================
class Recipient(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """Verinin aktarıldığı taraf — iç birim, işleyen, kamu kurumu veya üçüncü kişi."""

    __tablename__ = "cmp_recipients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_recipients_tenant_code"),
        Index("ix_cmp_recipients_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    vendor_id: Mapped[int | None] = fk("cmp_vendors.id", nullable=True, ondelete="SET NULL")

    kind: Mapped[str] = mapped_column(
        String(24), default=RecipientKind.UNKNOWN, nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(24), default=ProcessingRole.UNKNOWN, nullable=False
    )
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    is_cross_border: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    contract_reference: Mapped[str | None] = mapped_column(String(255))
    dpa_status: Mapped[str] = mapped_column(
        String(24), default=DpaStatus.UNKNOWN, nullable=False
    )
    contact_email: Mapped[str | None] = mapped_column(String(255))
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )


class DataFlow(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    İki nokta arasındaki somut veri hareketi.

    Faaliyet "neden"i, akış "nasıl ve nereye"yi anlatır. İkisini birleştirmek,
    aynı faaliyetin farklı taşıma güvenliğine sahip iki akışını tek satıra
    sıkıştırırdı.
    """

    __tablename__ = "cmp_data_flows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_data_flows_tenant_code"),
        Index("ix_cmp_data_flows_tenant_activity", "tenant_id", "activity_id"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )
    source_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )
    target_store_id: Mapped[int | None] = fk(
        "cmp_data_stores.id", nullable=True, ondelete="SET NULL"
    )
    recipient_id: Mapped[int | None] = fk(
        "cmp_recipients.id", nullable=True, ondelete="SET NULL"
    )
    transfer_id: Mapped[int | None] = fk(
        "cmp_transfers.id", nullable=True, ondelete="SET NULL"
    )

    direction: Mapped[str] = mapped_column(
        String(16), default=FlowDirection.INTERNAL, nullable=False
    )
    transport: Mapped[str | None] = mapped_column(String(64))
    is_encrypted_in_transit: Mapped[bool | None] = mapped_column(Boolean)
    frequency: Mapped[str | None] = mapped_column(String(64))
    volume_estimate: Mapped[str | None] = mapped_column(String(64))
    is_cross_border: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    data_categories_note: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )


class Transfer(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Yurt dışı aktarım kaydı.

    ``status`` varsayılanı ``REVIEW_REQUIRED``'dır ve bu bilinçli bir
    katılıktır: bir aktarımın dayanağı gösterilene ve etki değerlendirmesi
    yapılana kadar aktarım "uygun" sayılmaz. Yeterlilik kararı iddiası da
    doğrulanmadan geçerli sayılmaz — bu yüzden referans ile doğrulama durumu
    ayrı sütunlardadır.
    """

    __tablename__ = "cmp_transfers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_transfers_tenant_code"),
        Index("ix_cmp_transfers_tenant_status", "tenant_id", "status"),
        Index("ix_cmp_transfers_tenant_country", "tenant_id", "destination_country"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )
    recipient_id: Mapped[int | None] = fk(
        "cmp_recipients.id", nullable=True, ondelete="SET NULL"
    )
    vendor_id: Mapped[int | None] = fk("cmp_vendors.id", nullable=True, ondelete="SET NULL")

    destination_country: Mapped[str | None] = mapped_column(String(2))
    destination_country_name: Mapped[str | None] = mapped_column(String(96))
    destination_region: Mapped[str | None] = mapped_column(String(96))

    mechanism: Mapped[str] = mapped_column(
        String(32), default=TransferMechanism.UNKNOWN, nullable=False
    )
    mechanism_reference: Mapped[str | None] = mapped_column(String(255))
    mechanism_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    #: Yeterlilik kararı iddiası; doğrulanmadıkça dayanak sayılmaz.
    adequacy_reference: Mapped[str | None] = mapped_column(String(255))
    adequacy_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    #: Aktarım etki değerlendirmesi (TIA).
    tia_performed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tia_performed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tia_outcome: Mapped[str] = mapped_column(
        String(24), default=ComplianceState.UNKNOWN, nullable=False
    )
    tia_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    supplementary_measures: Mapped[str | None] = mapped_column(Text)

    #: Alt işleyen listesi ayrı tabloda tutulur; burada yalnızca beyanın
    #: alınıp alınmadığı ve nereden geldiği izlenir.
    subprocessors_disclosed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    subprocessor_list_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    onward_transfer_allowed: Mapped[bool | None] = mapped_column(Boolean)

    data_categories_note: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(24), default=ComplianceState.REVIEW_REQUIRED, nullable=False
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    subprocessors: Mapped[list["Subprocessor"]] = relationship(
        back_populates="transfer", lazy="selectin"
    )


# ===========================================================================
# Tedarikçi zinciri
# ===========================================================================
class Vendor(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    Tedarikçi / hizmet sağlayıcı.

    ``certifications`` tedarikçinin **beyanıdır**; doğrulama durumu ayrı bir
    sütunda tutulur. Beyanı doğrulanmış gibi raporlamak, tedarikçi denetiminin
    tamamını anlamsızlaştırır.
    """

    __tablename__ = "cmp_vendors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_vendors_tenant_code"),
        Index("ix_cmp_vendors_tenant_dpa", "tenant_id", "dpa_status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    legal_name: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    website: Mapped[str | None] = mapped_column(String(512))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    privacy_contact: Mapped[str | None] = mapped_column(String(255))

    role: Mapped[str] = mapped_column(
        String(24), default=ProcessingRole.UNKNOWN, nullable=False
    )
    criticality: Mapped[str] = mapped_column(
        String(16), default=Criticality.UNKNOWN, nullable=False
    )
    processes_personal_data: Mapped[bool | None] = mapped_column(Boolean)
    is_ai_provider: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    dpa_status: Mapped[str] = mapped_column(
        String(24), default=DpaStatus.UNKNOWN, nullable=False
    )
    dpa_signed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    dpa_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    security_assessment_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    security_assessment_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    security_assessment_evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    #: Tedarikçinin beyan ettiği sertifikalar (kod listesi).
    certifications: Mapped[str | None] = mapped_column(JSONText)
    certification_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    breach_notification_hours: Mapped[int | None] = mapped_column(Integer)
    contract_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    contract_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_plan_note: Mapped[str | None] = mapped_column(Text)

    subprocessors: Mapped[list["Subprocessor"]] = relationship(
        back_populates="vendor", lazy="selectin"
    )


class Subprocessor(Base, TimestampMixin, AuthorMixin):
    """
    Tedarikçinin kullandığı alt işleyen.

    Ayrı tablo olmasının sebebi zincirin derinliği: bir bulut sağlayıcısının
    alt işleyeni değiştiğinde, itiraz süresi ve onay durumu satır bazında
    izlenmelidir. Tedarikçi satırındaki bir JSON listesinde bu geçmiş kaybolur.
    """

    __tablename__ = "cmp_subprocessors"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "vendor_id", "name", name="uq_cmp_subprocessors_vendor_name"
        ),
        Index("ix_cmp_subprocessors_tenant_status", "tenant_id", "approval_status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    vendor_id: Mapped[int] = fk("cmp_vendors.id", ondelete="CASCADE")
    transfer_id: Mapped[int | None] = fk(
        "cmp_transfers.id", nullable=True, ondelete="SET NULL"
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    service_note: Mapped[str | None] = mapped_column(Text)

    disclosed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disclosure_source: Mapped[str | None] = mapped_column(String(512))
    approval_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )
    #: İtiraz hakkı süreye bağlıdır; süre geçtiyse itiraz edilememiş demektir,
    #: onaylanmış değil.
    objection_deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    objected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    objection_note: Mapped[str | None] = mapped_column(Text)

    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    vendor: Mapped["Vendor"] = relationship(back_populates="subprocessors")
    transfer: Mapped["Transfer | None"] = relationship(back_populates="subprocessors")


__all__ = [
    "ActivityDataCategory",
    "ActivityPurpose",
    "DataCategory",
    "DataField",
    "DataFlow",
    "DataStore",
    "LegalBasis",
    "ProcessingActivity",
    "Purpose",
    "Recipient",
    "Subprocessor",
    "SystemAsset",
    "Transfer",
    "Vendor",
]
