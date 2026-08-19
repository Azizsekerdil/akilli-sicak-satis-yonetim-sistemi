"""
Versiyonlu hukuki kural modeli.

Beş tablo tek bir fikri taşır: **bir uyumluluk iddiası, dayandığı kaynağa ve
onu onaylayan insana kadar geriye izlenebilir olmalıdır.**

    cmp_legal_source     resmî kaynak referansı ve bütünlük özeti
    cmp_rule_pack        sürümlenmiş kural paketi (yaşam döngüsü burada)
    cmp_rule             tek bir yükümlülük ve makine okunur koşulu
    cmp_rule_approval    insan onayı — ``ACTIVE`` olmanın tek yolu
    cmp_rule_evaluation  bir bağlam üzerinde üretilmiş, zincirlenmiş sonuç

Tasarım kararları ve gerekçeleri:

*   **Kural paketleri kiracıya bağlı değildir.** Mevzuat ortak bilgidir; her
    kiracı için ayrı bir KVKK kopyası tutmak, aynı maddenin iki kiracıda
    farklı okunmasına ve sürüm kaymasına davetiye çıkarırdı. Kiracıya bağlı
    olan tek tablo ``cmp_rule_evaluation``dır — sonuç her zaman birinin
    sonucudur.
*   **``cmp_rule_evaluation`` yalnızca eklenir.** Yanlış bir sonuç
    düzeltilmez; yenisi yazılır ve ``previous_evaluation_id`` ile öncekine
    bağlanır. Satırlar ``audit_logs`` desenini izleyerek SHA-256 ile
    zincirlenir, böylece geçmişe dönük sessiz düzenleme fark edilir.
*   **JSON alanları ``Text`` içinde saklanır** (``app.core.utils.dumps/loads``).
    JSONB/ARRAY kullanılsaydı şema SQLite'ta çalışmazdı.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.models.tenant import tenant_fk
from app.compliance.rule_enums import (
    UNKNOWN,
    ApprovalDecision,
    ConfidenceLevel,
    Criticality,
    EvaluationOutcome,
    EvaluatorKind,
    LifecycleStatus,
    PredicateResult,
    SourceHashKind,
    SourceVerification,
)
from app.core.exceptions import BusinessRuleError
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

# ---------------------------------------------------------------------------
# 1) Resmî kaynak
# ---------------------------------------------------------------------------


class LegalSource(Base, TimestampMixin, AuthorMixin):
    """
    Bir mevzuat metnine yapılan referans ve o referansın bütünlük özeti.

    Metnin kendisi burada saklanmaz; saklanan şey **neye dayandığımızın**
    kaydıdır. ``content_hash_kind`` alanı, özetin resmî metinden mi yoksa
    yalnızca referans alanlarından mı türetildiğini açıkça söyler — çünkü
    doğrulanmamış bir kaynağı doğrulanmış göstermek, bu katmanın yapabileceği
    en zararlı hatadır.
    """

    __tablename__ = "cmp_legal_source"
    __table_args__ = (
        UniqueConstraint("source_key", "version", name="uq_cmp_legal_source_key_version"),
        Index("ix_cmp_legal_source_regulation", "jurisdiction", "regulation_code"),
    )

    id: Mapped[int] = pk()

    #: Paket dosyalarındaki ``legal_source_key`` ile eşleşen kararlı anahtar.
    source_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")

    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, default=UNKNOWN)
    regulation_code: Mapped[str] = mapped_column(String(48), nullable=False)
    authority: Mapped[str] = mapped_column(String(255), nullable=False)

    title_tr: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[str] = mapped_column(String(512), nullable=False)
    official_url: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="tr")

    publication_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    #: Referansın ne zaman derlendiği — kaynak tazeliğinin tek ölçüsü.
    retrieved_date: Mapped[date] = mapped_column(Date, nullable=False)

    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SourceHashKind.UNKNOWN
    )
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SourceVerification.UNVERIFIED
    )
    verified_by_id: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    notes_tr: Mapped[str | None] = mapped_column(Text)
    notes_en: Mapped[str | None] = mapped_column(Text)

    #: Yeni sürüm eskiyi geçersizleştirir; eski satır silinmez.
    superseded_by_id: Mapped[int | None] = fk(
        "cmp_legal_source.id", nullable=True, ondelete="SET NULL", index=False
    )

    rules: Mapped[list[Rule]] = relationship(back_populates="legal_source")


# ---------------------------------------------------------------------------
# 2) Kural paketi
# ---------------------------------------------------------------------------


class RulePack(Base, TimestampMixin, AuthorMixin):
    """
    Sürümlenmiş kural kümesi.

    ``content_hash`` paketin normatif içeriğinin SHA-256 özetidir ve pakete ait
    her kuralın parmak izini kapsar. Onay bu özet üzerine verilir: içerik
    değişirse özet değişir, önceki onay artık paketi kapsamaz ve ``ACTIVE``a
    geçiş engellenir. "Kaynak değişirse yeniden onay" kuralının mekanizması
    budur — ayrı bir kontrol listesi değil, imzanın neyi imzaladığı.
    """

    __tablename__ = "cmp_rule_pack"
    __table_args__ = (
        UniqueConstraint("pack_key", "version", name="uq_cmp_rule_pack_key_version"),
        Index("ix_cmp_rule_pack_status_jur", "status", "jurisdiction"),
    )

    id: Mapped[int] = pk()

    pack_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")

    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, default=UNKNOWN)
    regulation_code: Mapped[str] = mapped_column(String(48), nullable=False)

    title_tr: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[str] = mapped_column(String(512), nullable=False)
    description_tr: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LifecycleStatus.DRAFT
    )

    #: Paketin normatif içeriğinin özeti — onay bu değere bağlanır.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Pakete giren tüm kaynak özetlerinin birleşik parmak izi.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Paketin okuduğu bağlam anahtarları (JSON dizi). Yükleyici, yüklemeden
    #: önce kuralların atıf yaptığı her alanın bu listede olduğunu doğrular;
    #: böylece yazım hatası taşıyan bir alan sessizce "bilinmiyor" üretemez.
    context_keys: Mapped[str | None] = mapped_column(JSONText)

    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    retrieved_date: Mapped[date | None] = mapped_column(Date)

    #: Paketin tamamı için insan incelemesi zorunlu mu? Taslak paketlerde True.
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    imported_from: Mapped[str | None] = mapped_column(String(512))
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    activated_by_id: Mapped[int | None] = mapped_column(Integer)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)

    supersedes_id: Mapped[int | None] = fk(
        "cmp_rule_pack.id", nullable=True, ondelete="SET NULL", index=False
    )

    rules: Mapped[list[Rule]] = relationship(
        back_populates="rulepack", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[RuleApproval]] = relationship(
        back_populates="rulepack", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == LifecycleStatus.ACTIVE


# ---------------------------------------------------------------------------
# 3) Kural
# ---------------------------------------------------------------------------


class Rule(Base, TimestampMixin, AuthorMixin):
    """
    Tek bir yükümlülük.

    Metinsel alanlar insanlar için, ``applicability`` / ``condition`` /
    ``exceptions`` / ``review_triggers`` JSON yüklemleri motor içindir. Yüklem
    dili bilinçle küçük tutulmuştur: ``eval`` yok, kod çalıştırma yok, yalnızca
    veri olarak taşınan bir karar ağacı. Bir kural paketi dosyası dışarıdan
    gelebilir; hiçbir koşulda kod yürütememelidir.
    """

    __tablename__ = "cmp_rule"
    __table_args__ = (
        UniqueConstraint("rulepack_id", "rule_id", name="uq_cmp_rule_pack_rule"),
        Index("ix_cmp_rule_lookup", "jurisdiction", "regulation_code", "rule_id"),
        Index("ix_cmp_rule_status_severity", "status", "severity"),
    )

    id: Mapped[int] = pk()
    rulepack_id: Mapped[int] = fk("cmp_rule_pack.id", ondelete="CASCADE", index=False)
    legal_source_id: Mapped[int | None] = fk(
        "cmp_legal_source.id", nullable=True, ondelete="RESTRICT", index=False
    )

    # --- Kimlik ve mevzuat bağı -------------------------------------------
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, default=UNKNOWN)
    regulation_code: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Pakete göre benzersiz iş anahtarı, ör. "KVKK-12-01".
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)

    title_tr: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[str] = mapped_column(String(512), nullable=False)
    description_tr: Mapped[str] = mapped_column(Text, nullable=False)
    description_en: Mapped[str] = mapped_column(Text, nullable=False)

    official_source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    authority: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Madde referansı. Doğrulanamayan bir referans "UNKNOWN" yazılır —
    #: uydurulmuş bir madde numarası, boş bir alandan çok daha zararlıdır.
    article_ref: Mapped[str] = mapped_column(String(64), nullable=False, default=UNKNOWN)

    publication_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    retrieved_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: Bağlı olduğu kaynağın özeti (kaynak değişirse paket yeniden onay ister).
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Kuralın kendi normatif içeriğinin parmak izi.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rulepack_version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LifecycleStatus.DRAFT
    )

    # --- Makine okunur kısım ----------------------------------------------
    #: Kuralın bu bağlama uygulanıp uygulanmadığını belirleyen yüklem (JSON).
    applicability: Mapped[str | None] = mapped_column(JSONText)
    #: Uygulanıyor görünse de kuralı devre dışı bırakan istisnalar (JSON dizi).
    exceptions: Mapped[str | None] = mapped_column(JSONText)
    #: Sonuç ne olursa olsun insan incelemesi tetikleyen durumlar (JSON dizi).
    review_triggers: Mapped[str | None] = mapped_column(JSONText)
    #: Uyumluluk koşulu (JSON yüklem). TRUE ise uyumlu, FALSE ise değil,
    #: UNKNOWN ise kanıt yetersiz.
    condition: Mapped[str | None] = mapped_column(JSONText)
    #: Sonuç üretilebilmesi için bulunması gereken kanıtlar (JSON dizi).
    evidence_requirements: Mapped[str | None] = mapped_column(JSONText)

    #: Süre yükümlülüğünün serbest metin tanımı. Doğrulanmamışsa "UNKNOWN".
    deadline_definition: Mapped[str | None] = mapped_column(Text)
    #: Sürenin hangi saat diliminde hesaplandığı (ör. "Europe/Istanbul").
    timezone_rule: Mapped[str | None] = mapped_column(String(64))

    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Criticality.MEDIUM
    )
    confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConfidenceLevel.UNKNOWN
    )

    # --- İnsan egemenliği --------------------------------------------------
    #: True ise motor bu kural için asla otomatik COMPLIANT üretmez.
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reviewer_id: Mapped[int | None] = mapped_column(Integer)
    approver_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    notes_tr: Mapped[str | None] = mapped_column(Text)
    notes_en: Mapped[str | None] = mapped_column(Text)

    rulepack: Mapped[RulePack] = relationship(back_populates="rules")
    legal_source: Mapped[LegalSource | None] = relationship(back_populates="rules")

    def title(self, lang: str = "tr") -> str:
        return self.title_en if lang == "en" and self.title_en else self.title_tr


# ---------------------------------------------------------------------------
# 4) İnsan onayı
# ---------------------------------------------------------------------------


class RuleApproval(Base, TimestampMixin):
    """
    Bir kural paketine verilen insan kararı.

    ``approved_content_hash`` onay anındaki paket içeriğinin özetidir. Paket
    sonradan değişirse bu değer paketin güncel ``content_hash``ı ile
    tutmayacağı için onay geçersiz sayılır. Onay "paket kimliği" üzerine
    verilseydi, sessizce değiştirilmiş bir paket eski imzayla yürürlüğe
    girebilirdi.

    ``approver_id`` kullanıcı tablosuna yabancı anahtarla bağlanmaz: onay
    kaydı, onayı veren hesap silinse bile ayakta kalmalıdır.
    """

    __tablename__ = "cmp_rule_approval"
    __table_args__ = (
        Index("ix_cmp_rule_approval_pack_decision", "rulepack_id", "decision"),
        Index("ix_cmp_rule_approval_hash", "approved_content_hash"),
    )

    id: Mapped[int] = pk()
    rulepack_id: Mapped[int] = fk("cmp_rule_pack.id", ondelete="CASCADE", index=False)
    #: Kural bazlı onay için doldurulur; paket bazlı onayda boştur.
    rule_pk: Mapped[int | None] = fk(
        "cmp_rule.id", nullable=True, ondelete="CASCADE", index=False
    )

    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    approver_role: Mapped[str] = mapped_column(String(24), nullable=False)
    approver_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    approver_name: Mapped[str | None] = mapped_column(String(128))
    #: Paketi incelemeye gönderen kişi. Onaylayanla aynı olamaz (dört göz).
    submitted_by_id: Mapped[int | None] = mapped_column(Integer)

    #: Onay anındaki paket içeriği özeti.
    approved_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_source_hash: Mapped[str | None] = mapped_column(String(64))
    pack_version: Mapped[str | None] = mapped_column(String(32))

    decided_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )
    comment: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(String(512))

    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by_id: Mapped[int | None] = mapped_column(Integer)
    revoked_reason: Mapped[str | None] = mapped_column(Text)

    #: audit_logs ile aynı desen: onay kayıtları da zincirlenir.
    checksum: Mapped[str | None] = mapped_column(String(64))
    previous_checksum: Mapped[str | None] = mapped_column(String(64))

    rulepack: Mapped[RulePack] = relationship(back_populates="approvals")

    @property
    def is_effective(self) -> bool:
        """Yürürlükteki, geri alınmamış bir onay mı?"""
        return self.decision == ApprovalDecision.APPROVED and not self.is_revoked


# ---------------------------------------------------------------------------
# 5) Değerlendirme sonucu
# ---------------------------------------------------------------------------


class RuleEvaluation(Base):
    """
    Bir kuralın bir bağlam üzerinde ürettiği sonuç — yalnızca eklenir.

    Sonuçlar güncellenmez. Bir değerlendirme yanlışsa yenisi yazılır ve
    ``previous_evaluation_id`` ile öncekine bağlanır; böylece "sonuç ne zaman,
    hangi kural sürümüyle, hangi bağlamla üretildi" sorusu her zaman
    yanıtlanabilir. ``checksum`` alanı ``audit_logs`` desenini izleyerek
    kiracı bazında zincirlenir.

    ``context_snapshot`` ham bağlamın kendisi değildir: yalnızca kuralın atıf
    yaptığı alanlar, yalnızca ilkel değerler olarak saklanır. Kanıt kaydı
    uğruna kişisel veri biriktirmek, uyumluluk katmanını ihlal kaynağına
    çevirirdi.
    """

    __tablename__ = "cmp_rule_evaluation"
    __table_args__ = (
        Index("ix_cmp_rule_evaluation_tenant_time", "tenant_id", "evaluated_at"),
        Index("ix_cmp_rule_evaluation_tenant_outcome", "tenant_id", "outcome"),
        Index("ix_cmp_rule_evaluation_rule", "rulepack_id", "rule_id"),
    )

    id: Mapped[int] = pk()

    #: Kiracı kapsamı. Uyumluluk sonucu her zaman bir kiracıya aittir;
    #: kiracısız bir sonuç yanlış müşteriye raporlanma riski taşır.
    tenant_id: Mapped[int] = tenant_fk()

    rule_pk: Mapped[int | None] = fk(
        "cmp_rule.id", nullable=True, ondelete="RESTRICT", index=False
    )
    rulepack_id: Mapped[int | None] = fk(
        "cmp_rule_pack.id", nullable=True, ondelete="RESTRICT", index=False
    )
    #: Kural satırı ileride arşivlense bile sonucu okunabilir tutan iş anahtarı.
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rulepack_key: Mapped[str | None] = mapped_column(String(64))
    rulepack_version: Mapped[str | None] = mapped_column(String(32))
    rulepack_status: Mapped[str | None] = mapped_column(String(16))
    jurisdiction: Mapped[str | None] = mapped_column(String(8))
    regulation_code: Mapped[str | None] = mapped_column(String(48))
    article_ref: Mapped[str | None] = mapped_column(String(64))

    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[str | None] = mapped_column(String(16))
    requires_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    applicability_result: Mapped[str] = mapped_column(
        String(8), nullable=False, default=PredicateResult.UNKNOWN
    )
    condition_result: Mapped[str] = mapped_column(
        String(8), nullable=False, default=PredicateResult.UNKNOWN
    )

    #: Eksik kanıt anahtarları (JSON dizi). Boş değilse sonuç uyumlu olamaz.
    missing_evidence: Mapped[str | None] = mapped_column(JSONText)
    #: Eşleşen istisna anahtarları (JSON dizi).
    matched_exceptions: Mapped[str | None] = mapped_column(JSONText)
    #: Tetiklenen insan incelemesi nedenleri (JSON dizi).
    triggered_reviews: Mapped[str | None] = mapped_column(JSONText)
    #: Sonucu açıklayan makine okunur gerekçe kodları (JSON dizi).
    reasons: Mapped[str | None] = mapped_column(JSONText)
    #: Sunulan kanıt referansları (JSON dizi) — kanıtın kendisi değil.
    evidence_refs: Mapped[str | None] = mapped_column(JSONText)

    #: Değerlendirilen bağlamın tamamının SHA-256 özeti. Aynı bağlamın yeniden
    #: değerlendirilip değerlendirilmediğini ucuza söyler.
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Yalnızca kuralın okuduğu alanların ilkel değerleri (JSON nesnesi).
    context_snapshot: Mapped[str | None] = mapped_column(JSONText)

    evaluator: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EvaluatorKind.ENGINE
    )
    engine_version: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
    evaluated_by_id: Mapped[int | None] = mapped_column(Integer)

    #: İnsan motorun sonucunu geçersiz kıldıysa dolar. Geçersiz kılma da silme
    #: değil, yeni bir satırdır.
    is_human_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    override_reason: Mapped[str | None] = mapped_column(Text)
    previous_evaluation_id: Mapped[int | None] = fk(
        "cmp_rule_evaluation.id", nullable=True, ondelete="SET NULL", index=False
    )

    checksum: Mapped[str | None] = mapped_column(String(64))
    previous_checksum: Mapped[str | None] = mapped_column(String(64))

    @property
    def is_conclusive(self) -> bool:
        """İnsan müdahalesi olmadan rapora girebilecek bir sonuç mu?"""
        return self.outcome in (
            EvaluationOutcome.COMPLIANT,
            EvaluationOutcome.NON_COMPLIANT,
            EvaluationOutcome.NOT_APPLICABLE,
        )


# ---------------------------------------------------------------------------
# ACTIVE geçişinin ORM düzeyinde zorlanması
# ---------------------------------------------------------------------------
#
# Servis katmanı (``rulepack_loader.activate``) zaten onay arar. Bu dinleyici
# ikinci ve son savunma hattıdır: bir bakım betiği ya da ileride yazılacak bir
# router doğrudan ``pack.status = "ACTIVE"`` yazıp commit ederse kayıt yine de
# reddedilir. İnsan onayı, atlanabilir bir yardımcı fonksiyon değil, veri
# katmanının değişmezidir.
#
# Kontrol her yazımda çalışır, yalnızca durum değiştiğinde değil: ACTIVE bir
# paketin içeriği düzenlenirse ``content_hash`` değişir, mevcut onay artık o
# içeriği kapsamaz ve güncelleme reddedilir.


def _has_effective_approval(connection, pack: RulePack) -> bool:
    """Paketin **güncel içeriğini** kapsayan, geri alınmamış bir onay var mı?"""
    if pack.id is None:
        # Henüz satır yok; ona bağlı bir onay da olamaz.
        return False
    tbl = RuleApproval.__table__
    stmt = (
        select(tbl.c.id)
        .where(tbl.c.rulepack_id == pack.id)
        .where(tbl.c.rule_pk.is_(None))
        .where(tbl.c.decision == str(ApprovalDecision.APPROVED))
        .where(tbl.c.is_revoked.is_(False))
        .where(tbl.c.approved_content_hash == pack.content_hash)
        .limit(1)
    )
    return connection.execute(stmt).first() is not None


def _guard_activation(_mapper, connection, target: RulePack) -> None:
    if target.status != LifecycleStatus.ACTIVE:
        return
    if _has_effective_approval(connection, target):
        return
    previous = inspect(target).attrs.status.history.deleted
    raise BusinessRuleError(
        "compliance.rulepack.activation_requires_human_approval",
        params={
            "pack_key": target.pack_key,
            "version": target.version,
            "previous_status": previous[0] if previous else None,
        },
        detail=(
            "RulePack cannot be ACTIVE without an effective RuleApproval whose "
            "approved_content_hash matches the pack's current content_hash."
        ),
    )


event.listen(RulePack, "before_insert", _guard_activation)
event.listen(RulePack, "before_update", _guard_activation)


__all__ = [
    "LegalSource",
    "Rule",
    "RuleApproval",
    "RuleEvaluation",
    "RulePack",
]
