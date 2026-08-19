"""
Kanıt, risk, kontrol ve bulgu.

Bu modülün merkezinde tek bir iddia var: **bir uyumluluk raporu ancak
dayandığı kanıt kadar değerlidir.** Bu yüzden ``EvidenceArtifact`` yalnızca bir
dosya işaretçisi değil, zincirlenmiş bir kayıttır; sonradan düzenlenen bir
kanıt satırı doğrulamada ortaya çıkar.

Zincir deseni ``app.services.audit_service`` ile aynı fikri paylaşır ama ayrı
kurulur ve iki noktada ondan ayrılır:

* Zincir **kiracı başına** yürür. Tek bir küresel zincir olsaydı, bir kiracının
  kanıt eklemesi diğerinin geçmişini yeniden hesaplamayı gerektirirdi ve çok
  kiracılı bir kurulumda zincir sürekli çakışırdı.
* İçerik özeti (``content_hash``) ile zincir özeti (``chain_hash``) ayrıdır.
  Birincisi kanıtın *kendisini* (dosya baytları, dışa aktarılan JSON) bağlar;
  ikincisi bu kaydın *sırasını* bağlar. İkisi tek sütunda birleştirilseydi,
  aynı belgenin iki kiracıda aynı özeti taşıdığı görülemezdi.

Doğrulama sonucu kanıt satırına **yazılmaz**: append-only bir kaydı doğrulama
sırasında güncellemek, korumaya çalıştığı özelliği ortadan kaldırırdı.
:func:`verify_chain` bulgusunu dönüş değeri olarak verir.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compliance.enums import (
    ComplianceRegime,
    ConfidenceLevel,
    ControlImplementation,
    ControlTestResult,
    ControlType,
    EvidenceIntegrity,
    EvidenceKind,
    FindingSeverity,
    FindingSource,
    FindingStatus,
    ReviewStatus,
    RiskCategory,
    RiskImpact,
    RiskLikelihood,
    RiskStatus,
    RiskTreatment,
)
from app.compliance.models.tenant import tenant_fk
from app.core.exceptions import BusinessRuleError
from app.core.utils import dumps
from app.models.base import (
    AuthorMixin,
    Base,
    CodeNameMixin,
    JSONText,
    Money,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)

#: Zincirin ilk halkasında önceki özet yoktur; boş dizge yerine sabit bir
#: tohum kullanmak, "önceki yok" ile "önceki silinmiş" durumlarını ayırt
#: edilebilir kılar.
CHAIN_SEED = "cmp-evidence-v1"

#: Kanonik gösterime giren ve sütun varsayılanı olan alanların varsayılanları.
#:
#: SQLAlchemy'de ``mapped_column(default=...)`` INSERT anında uygulanır —
#: yani ``seal()`` özeti hesapladıktan **sonra**. Bu alanlar mühürleme
#: sırasında elle doldurulmazsa satır, hesaplandığı hâlinden farklı olarak
#: veritabanına yazılır ve sağlam bir zincir doğrulamada ``CONTENT_MISMATCH``
#: verir. Kanonik gösterime yeni bir varsayılanlı sütun eklenirse buraya da
#: eklenmelidir.
_SEALED_DEFAULTS: dict[str, str] = {
    "kind": EvidenceKind.OTHER.value,
    "collector_kind": "MANUAL",
}


def content_digest(data: bytes | str) -> str:
    """Kanıtın kendi içeriğinin SHA-256 özeti."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def chain_digest(previous_hash: str | None, payload: str) -> str:
    """Zincir halkası: önceki halka + bu kaydın kanonik içeriği."""
    return hashlib.sha256(f"{previous_hash or CHAIN_SEED}|{payload}".encode()).hexdigest()


class EvidenceArtifact(Base):
    """
    Değiştirilemez kanıt kaydı.

    ``TimestampMixin`` bilerek kullanılmaz: ``updated_at`` sütunu, güncellenmesi
    beklenen bir satır izlenimi verirdi. Kanıt güncellenmez; düzeltme gerekirse
    yeni bir kanıt eklenir ve ``supersedes_id`` ile eskisine bağlanır.

    ``subject_type``/``subject_id`` çifti yabancı anahtar değildir. Kanıt,
    anlattığı kaydın silinmesinden sonra da okunabilir olmalıdır; FK verilseydi
    ``RESTRICT`` her silmeyi kilitler, ``CASCADE`` ise kanıtı yok ederdi.
    """

    __tablename__ = "cmp_evidence_artifacts"
    __table_args__ = (
        # Zincirin kiracı içinde tek ve boşluksuz olması bu kısıta dayanır.
        UniqueConstraint(
            "tenant_id", "sequence_no", name="uq_cmp_evidence_artifacts_tenant_seq"
        ),
        Index(
            "ix_cmp_evidence_artifacts_subject",
            "tenant_id", "subject_type", "subject_id",
        ),
        Index(
            "ix_cmp_evidence_artifacts_tenant_collected",
            "tenant_id", "collected_at",
        ),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    #: Kiracı içinde 1'den başlayan kesintisiz sıra. Boşluk, silinmiş bir kanıt
    #: anlamına gelir ve doğrulamada ``SEQUENCE_GAP`` olarak raporlanır.
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[str] = mapped_column(
        String(32), default=EvidenceKind.OTHER, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    subject_type: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_id: Mapped[int | None] = mapped_column(Integer)
    subject_ref: Mapped[str | None] = mapped_column(String(128))

    collected_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    collected_by_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    collected_by_label: Mapped[str | None] = mapped_column(String(128))
    #: Kanıtı üretenin insan mı otomat mı olduğu; delil ağırlığını etkiler.
    collector_kind: Mapped[str] = mapped_column(String(24), default="MANUAL", nullable=False)

    source: Mapped[str | None] = mapped_column(String(255))
    source_uri: Mapped[str | None] = mapped_column(String(512))
    media_type: Mapped[str | None] = mapped_column(String(128))
    byte_size: Mapped[int | None] = mapped_column(Integer)

    #: Büyük kanıtlar dosya sisteminde durur, veritabanında değil.
    storage_path: Mapped[str | None] = mapped_column(String(512))
    #: Küçük ve yapısal kanıtlar (tarama çıktısı, karar kaydı) satır içinde.
    payload: Mapped[str | None] = mapped_column(JSONText)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Düzeltme gerektiğinde eski kanıt silinmez; yenisi eskisini işaret eder.
    supersedes_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    retention_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    def canonical_payload(self) -> str:
        """
        Zincire giren kanonik gösterim.

        Yalnızca **kalıcı** sütunlardan kurulur — saat okuması, rastgele değer
        veya ilişki gezintisi yoktur. Böylece :func:`verify_chain` yıllar sonra
        aynı dizgeyi yeniden üretip, alanı değiştirilmiş ama özeti de yeniden
        yazılmış bir satırı yakalayabilir.
        """
        return dumps(
            {
                "t": self.tenant_id,
                "n": self.sequence_no,
                "k": self.kind,
                "ti": self.title,
                "st": self.subject_type,
                "si": self.subject_id,
                "sr": self.subject_ref,
                "ca": self.collected_at.isoformat() if self.collected_at else None,
                "cb": self.collected_by_user_id,
                "ck": self.collector_kind,
                "so": self.source,
                "su": self.source_uri,
                "bs": self.byte_size,
                "sp": self.storage_path,
                "pl": self.payload,
                "ch": self.content_hash,
                "sup": self.supersedes_id,
            }
        )

    def seal(self, *, sequence_no: int, previous_hash: str | None) -> str:
        """
        Kaydı zincire bağla ve ``chain_hash``'i hesapla.

        Mühürlenmiş bir kaydı yeniden mühürlemek reddedilir: bu, zincirin
        ortasındaki bir halkayı sessizce yeniden yazma girişiminin tek
        yakalanabildiği yerdir.
        """
        if self.chain_hash:
            raise BusinessRuleError(
                "compliance.evidence.already_sealed",
                params={"id": self.id, "sequence_no": self.sequence_no},
            )
        if not self.content_hash:
            raise BusinessRuleError("compliance.evidence.content_hash_missing")
        if self.tenant_id is None:
            # Kiracı özetin parçasıdır; sonradan atanması kaydı sessizce
            # doğrulanamaz hâle getirirdi.
            raise BusinessRuleError("compliance.evidence.tenant_missing")
        if self.collected_at is None:
            self.collected_at = utcnow()
        for attr, value in _SEALED_DEFAULTS.items():
            if getattr(self, attr, None) is None:
                setattr(self, attr, value)

        self.sequence_no = sequence_no
        self.previous_hash = previous_hash
        self.chain_hash = chain_digest(previous_hash, self.canonical_payload())
        return self.chain_hash


def verify_chain(artifacts: Sequence[EvidenceArtifact]) -> dict[str, object]:
    """
    Sıra numarasına göre sıralanmış tek bir kiracının kanıt zincirini doğrula.

    Dönüş: ``{"valid", "checked", "broken_at", "status", "reason"}``.
    Boş liste geçerli sayılır ama ``status`` ``UNVERIFIED`` kalır — hiç kanıt
    olmaması, uyumluluğun kanıtlandığı anlamına gelmez.
    """
    if not artifacts:
        return {
            "valid": True,
            "checked": 0,
            "broken_at": None,
            "status": EvidenceIntegrity.UNVERIFIED.value,
            "reason": "no_evidence",
        }

    previous: str | None = None
    expected_seq = artifacts[0].sequence_no
    checked = 0

    for row in artifacts:
        checked += 1
        if not row.chain_hash or not row.content_hash:
            return _broken(checked, row.id, EvidenceIntegrity.MISSING_HASH)
        if row.sequence_no != expected_seq:
            return _broken(checked, row.id, EvidenceIntegrity.SEQUENCE_GAP)
        if row.previous_hash != previous:
            return _broken(checked, row.id, EvidenceIntegrity.BROKEN_CHAIN)
        if chain_digest(previous, row.canonical_payload()) != row.chain_hash:
            return _broken(checked, row.id, EvidenceIntegrity.CONTENT_MISMATCH)
        previous = row.chain_hash
        expected_seq += 1

    return {
        "valid": True,
        "checked": checked,
        "broken_at": None,
        "status": EvidenceIntegrity.OK.value,
        "reason": None,
    }


def _broken(checked: int, row_id: int, status: EvidenceIntegrity) -> dict[str, object]:
    return {
        "valid": False,
        "checked": checked,
        "broken_at": row_id,
        "status": status.value,
        "reason": status.value.lower(),
    }


class Risk(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Risk kaydı.

    Skor sütunları hesaplanmış değeri saklar ama ``score_method`` olmadan
    anlamsızdır: 12 puanlık bir risk, hangi ölçekle üretildiği bilinmeden
    başka bir kayıtla karşılaştırılamaz.
    """

    __tablename__ = "cmp_risks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_risks_tenant_code"),
        Index("ix_cmp_risks_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    category: Mapped[str] = mapped_column(
        String(24), default=RiskCategory.PRIVACY, nullable=False, index=True
    )
    #: Riskin bağlandığı varlıklar — hiçbiri zorunlu değil, çünkü bir risk
    #: sistemden değil süreçten de doğabilir.
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )
    asset_id: Mapped[int | None] = fk(
        "cmp_system_assets.id", nullable=True, ondelete="SET NULL"
    )
    vendor_id: Mapped[int | None] = fk("cmp_vendors.id", nullable=True, ondelete="SET NULL")
    transfer_id: Mapped[int | None] = fk(
        "cmp_transfers.id", nullable=True, ondelete="SET NULL"
    )

    affected_subjects: Mapped[str | None] = mapped_column(String(255))
    affected_count_estimate: Mapped[int | None] = mapped_column(Integer)

    likelihood: Mapped[str] = mapped_column(
        String(24), default=RiskLikelihood.UNKNOWN, nullable=False
    )
    impact: Mapped[str] = mapped_column(
        String(24), default=RiskImpact.UNKNOWN, nullable=False
    )
    inherent_score: Mapped[int | None] = mapped_column(Integer)
    residual_likelihood: Mapped[str] = mapped_column(
        String(24), default=RiskLikelihood.UNKNOWN, nullable=False
    )
    residual_impact: Mapped[str] = mapped_column(
        String(24), default=RiskImpact.UNKNOWN, nullable=False
    )
    residual_score: Mapped[int | None] = mapped_column(Integer)
    score_method: Mapped[str] = mapped_column(String(64), default="MANUAL_5X5", nullable=False)
    estimated_impact_amount: Mapped[Decimal | None] = mapped_column(Money)
    impact_currency: Mapped[str | None] = mapped_column(String(8))

    treatment: Mapped[str] = mapped_column(
        String(24), default=RiskTreatment.UNDECIDED, nullable=False
    )
    treatment_plan: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(24), default=RiskStatus.IDENTIFIED, nullable=False
    )

    owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    identified_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    review_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    #: Riski kabul etmek bir karardır ve sahibi olmalıdır; bu yüzden kabul
    #: gerekçesi ile kabul eden ayrı ayrı saklanır.
    accepted_by_user_id: Mapped[int | None] = mapped_column(Integer)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acceptance_rationale: Mapped[str | None] = mapped_column(Text)
    acceptance_reference: Mapped[str | None] = mapped_column(String(128))

    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    controls: Mapped[list["RiskControl"]] = relationship(
        back_populates="risk", lazy="selectin"
    )


class Control(Base, CodeNameMixin, TimestampMixin, AuthorMixin):
    """
    Kontrol tanımı.

    ``framework_reference`` bir dış çerçevenin madde numarasını taşıyabilir ama
    bu değeri sistem doğrulamaz; bu yüzden yanında daima
    ``framework_reference_status`` bulunur ve varsayılanı ``REVIEW_REQUIRED``
    olur. Doğrulanmamış bir referansı doğrulanmış gibi göstermek, denetimde
    raporun tamamının güvenilirliğini düşürür.
    """

    __tablename__ = "cmp_controls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_controls_tenant_code"),
        Index("ix_cmp_controls_tenant_effectiveness", "tenant_id", "effectiveness"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()

    control_type: Mapped[str] = mapped_column(
        String(24), default=ControlType.PREVENTIVE, nullable=False
    )
    implementation: Mapped[str] = mapped_column(
        String(24), default=ControlImplementation.UNKNOWN, nullable=False
    )
    objective: Mapped[str | None] = mapped_column(Text)
    procedure: Mapped[str | None] = mapped_column(Text)

    framework: Mapped[str] = mapped_column(
        String(32), default=ComplianceRegime.INTERNAL_POLICY, nullable=False
    )
    framework_reference: Mapped[str | None] = mapped_column(String(128))
    framework_reference_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.REVIEW_REQUIRED, nullable=False
    )

    owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    asset_id: Mapped[int | None] = fk(
        "cmp_system_assets.id", nullable=True, ondelete="SET NULL"
    )
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )

    effectiveness: Mapped[str] = mapped_column(
        String(24), default=ControlTestResult.NOT_TESTED, nullable=False
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    test_frequency_days: Mapped[int | None] = mapped_column(Integer)
    next_test_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)

    #: Otomatik test edilebilen kontroller için çalıştırıcı adı; boşsa kontrol
    #: yalnızca insan tarafından test edilebilir demektir.
    automation_hook: Mapped[str | None] = mapped_column(String(128))
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    tests: Mapped[list["ControlTest"]] = relationship(
        back_populates="control", lazy="selectin"
    )
    risks: Mapped[list["RiskControl"]] = relationship(
        back_populates="control", lazy="selectin"
    )


class RiskControl(Base, TimestampMixin, AuthorMixin):
    """
    Risk ile kontrol arasındaki çok-çoklu bağ.

    Ayrı bir tablo olmasının sebebi bağın kendi verisi olması: aynı kontrol bir
    riski büyük ölçüde, başka bir riski kısmen azaltır.
    """

    __tablename__ = "cmp_risk_controls"
    __table_args__ = (
        UniqueConstraint("risk_id", "control_id", name="uq_cmp_risk_controls_pair"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    risk_id: Mapped[int] = fk("cmp_risks.id", ondelete="CASCADE")
    control_id: Mapped[int] = fk("cmp_controls.id", ondelete="CASCADE")

    #: Kontrolün bu riske katkısı; ölçülmediyse UNKNOWN kalır.
    contribution: Mapped[str] = mapped_column(
        String(24), default=ConfidenceLevel.UNKNOWN, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    risk: Mapped["Risk"] = relationship(back_populates="controls")
    control: Mapped["Control"] = relationship(back_populates="risks")


class ControlTest(Base, TimestampMixin, AuthorMixin):
    """
    Tek bir kontrol testinin sonucu — append-only.

    Test sonucu güncellenmez; yeniden test yeni satırdır. Böylece bir kontrolün
    zaman içindeki etkinliği okunabilir ve "hep geçer durumdaydı" iddiası
    kayıtla karşılaştırılabilir.
    """

    __tablename__ = "cmp_control_tests"
    __table_args__ = (
        Index("ix_cmp_control_tests_control_time", "control_id", "tested_at"),
        Index("ix_cmp_control_tests_tenant_result", "tenant_id", "result"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    control_id: Mapped[int] = fk("cmp_controls.id", ondelete="RESTRICT")

    tested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    tested_by_user_id: Mapped[int | None] = mapped_column(Integer)
    tester_kind: Mapped[str] = mapped_column(String(24), default="HUMAN", nullable=False)
    method: Mapped[str | None] = mapped_column(String(128))
    sample_size: Mapped[int | None] = mapped_column(Integer)
    exceptions_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    result: Mapped[str] = mapped_column(
        String(24), default=ControlTestResult.NOT_TESTED, nullable=False
    )
    observations: Mapped[str | None] = mapped_column(Text)
    next_test_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )

    control: Mapped["Control"] = relationship(back_populates="tests")


class Finding(Base, TimestampMixin, AuthorMixin):
    """
    Bulgu — bir kuralın, testin veya incelemenin ortaya çıkardığı uyumsuzluk.

    ``confidence`` alanı otomatik bulgular için zorunlu bir dürüstlük aracıdır:
    tarayıcı bir alanı "muhtemelen kişisel veri" diye işaretlediğinde bu,
    doğrulanmış bir tespitle aynı ağırlıkta raporlanamaz.
    """

    __tablename__ = "cmp_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_cmp_findings_tenant_ref"),
        Index("ix_cmp_findings_tenant_status_sev", "tenant_id", "status", "severity"),
        Index("ix_cmp_findings_rule", "tenant_id", "rule_id"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = tenant_fk()
    reference: Mapped[str] = mapped_column(String(64), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(
        String(16), default=FindingSeverity.MEDIUM, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default=FindingStatus.OPEN, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(24), default=FindingSource.MANUAL, nullable=False
    )
    confidence: Mapped[str] = mapped_column(
        String(16), default=ConfidenceLevel.UNKNOWN, nullable=False
    )
    regime: Mapped[str] = mapped_column(
        String(32), default=ComplianceRegime.UNKNOWN, nullable=False
    )
    #: Bulguyu üreten kural kimliği (rulepack). Elle açılan bulgularda boştur.
    rule_id: Mapped[str | None] = mapped_column(String(128))
    rule_version: Mapped[str | None] = mapped_column(String(32))

    control_id: Mapped[int | None] = fk("cmp_controls.id", nullable=True, ondelete="SET NULL")
    control_test_id: Mapped[int | None] = fk(
        "cmp_control_tests.id", nullable=True, ondelete="SET NULL"
    )
    risk_id: Mapped[int | None] = fk("cmp_risks.id", nullable=True, ondelete="SET NULL")
    activity_id: Mapped[int | None] = fk(
        "cmp_processing_activities.id", nullable=True, ondelete="SET NULL"
    )
    asset_id: Mapped[int | None] = fk(
        "cmp_system_assets.id", nullable=True, ondelete="SET NULL"
    )
    data_field_id: Mapped[int | None] = fk(
        "cmp_data_fields.id", nullable=True, ondelete="SET NULL"
    )
    vendor_id: Mapped[int | None] = fk("cmp_vendors.id", nullable=True, ondelete="SET NULL")
    transfer_id: Mapped[int | None] = fk(
        "cmp_transfers.id", nullable=True, ondelete="SET NULL"
    )

    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    evidence_id: Mapped[int | None] = fk(
        "cmp_evidence_artifacts.id", nullable=True, ondelete="RESTRICT"
    )
    #: Bulgunun ham bağlamı (dosya, satır, ölçülen değer) — tekrar üretilebilir
    #: olması için kuralın gördüğü veriyle birlikte saklanır.
    context: Mapped[str | None] = mapped_column(JSONText)

    remediation_plan: Mapped[str | None] = mapped_column(Text)
    remediation_owner_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    #: Kapatma gerekçeleri ayrı sütunlardadır: "kabul edildi" ile "yanlış alarm"
    #: aynı kutuya yazılırsa, tekrar eden yanlış alarmlar görünmez olur.
    accepted_by_user_id: Mapped[int | None] = mapped_column(Integer)
    acceptance_rationale: Mapped[str | None] = mapped_column(Text)
    false_positive_reason: Mapped[str | None] = mapped_column(Text)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


__all__ = [
    "CHAIN_SEED",
    "Control",
    "ControlTest",
    "EvidenceArtifact",
    "Finding",
    "Risk",
    "RiskControl",
    "chain_digest",
    "content_digest",
    "verify_chain",
]
