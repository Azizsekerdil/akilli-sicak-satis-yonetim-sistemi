"""
Uyumluluk API'sinin giriş/çıkış şemaları (Pydantic v2).

İki kural bu dosyanın tamamını belirler:

1. **Tek bir uyumluluk skoru yoktur.** ``ComplianceOverviewOut`` kategori
   bazında durum, insan incelemesi bekleyen sayısı ve eksik kanıt sayısı
   döner. "82/100" gibi birleşik bir sayı farklı ağırlıktaki eksikleri aynı
   paydada toplar ve asıl sorunun üstünü örter; şemada böyle bir alan bilerek
   yoktur.
2. **Bilinmeyen olumlu değildir.** Doldurulmamış dayanak, mekanizma veya madde
   referansı ``UNKNOWN`` olarak taşınır ve ilgili kayıt insan incelemesi
   bekleyen tarafta sayılır. Şema bu alanları hiçbir yerde varsayılan bir
   "uygun" değere çekmez.

Çıkış modelleri ORM satırlarından servis katmanında elle eşlenir; böylece bir
model sütunu değiştiğinde API sözleşmesi sessizce değişmez.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.compliance.enums import (
    ComplianceRegime,
    ConsentStatus,
    DataSensitivity,
    DsrStatus,
    DsrType,
    IdentifiabilityLevel,
    IdentityVerificationMethod,
    IntakeChannel,
    LegalBasisKind,
    NoticeKind,
    ProcessingRole,
    ReviewStatus,
    TransferMechanism,
    VerificationOutcome,
    WithdrawalReason,
)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ===========================================================================
# Kiracı
# ===========================================================================
class TenantBriefOut(_Out):
    id: int
    code: str
    name: str
    legal_name: str | None = None
    status: str
    primary_regime: str
    home_country: str | None = None
    default_language: str = "tr"
    last_assessment_at: datetime | None = None


# ===========================================================================
# Genel durum
# ===========================================================================
class OverviewCategoryOut(BaseModel):
    """
    Tek bir uyumluluk başlığının durumu.

    ``state`` bir puan değil, ``ComplianceState`` sözlüğünden bir değerdir.
    ``blocking_reasons`` boş değilse başlık hiçbir koşulda ``COMPLIANT``
    olamaz.
    """

    key: str
    label_tr: str
    label_en: str
    state: str
    total: int = 0
    reviewed: int = 0
    pending_human_review: int = 0
    missing_evidence: int = 0
    last_activity_at: datetime | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class EvidenceChainOut(BaseModel):
    valid: bool
    checked: int
    broken_at: int | None = None
    status: str
    reason: str | None = None


class ComplianceOverviewOut(BaseModel):
    """
    Uyumluluk durum tablosu.

    ``totals`` yalnızca sayımdır; ağırlıklandırılmış bir puan değildir.
    """

    generated_at: datetime
    tenant: TenantBriefOut | None = None
    categories: list[OverviewCategoryOut] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    evidence_chain: EvidenceChainOut | None = None
    review_queue: list[str] = Field(default_factory=list)
    disclaimer_key: str = "compliance.overview.disclaimer"


# ===========================================================================
# Envanter
# ===========================================================================
class DataFieldOut(_Out):
    id: int
    tenant_id: int
    table_name: str
    column_name: str
    column_type: str | None = None
    sensitivity: str
    identifiability: str
    is_special_category: bool = False
    source_module: str | None = None
    source_line: int | None = None
    discovered_by: str
    discovered_at: datetime | None = None
    review_status: str
    confirmed_at: datetime | None = None
    is_present: bool = True
    last_seen_at: datetime | None = None
    notes: str | None = None


class ScanRequestIn(BaseModel):
    """
    Keşif taraması isteği.

    ``full`` taraması bağımlılık lisanslarını, AI sağlayıcılarını ve otomasyon
    noktalarını da ölçer; kurulu paketleri sorgulamayı gerektirdiği için
    belirgin biçimde yavaştır. Varsayılan hızlı taramadır.
    """

    full: bool = False
    #: Bulunan aktif bulut AI sağlayıcılarından aday aktarım kaydı üretilsin mi?
    seed_transfers: bool = True
    note: str | None = Field(default=None, max_length=512)


class ScanResultOut(BaseModel):
    """
    Tarama sonucu.

    ``disappeared`` ve ``reclassified`` ayrı raporlanır: kaybolan bir sütun ve
    insan kararıyla çelişen bir ölçüm, sıradan güncellemelerle aynı kovaya
    konulamaz.
    """

    status: str
    scope: str
    started_at: datetime
    finished_at: datetime | None = None
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    disappeared: int = 0
    reclassified: int = 0
    personal: int = 0
    special_candidates: int = 0
    location: int = 0
    direct_identifiers: int = 0
    tables: int = 0
    transfer_candidates: int = 0
    review_notes: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence_id: int | None = None
    error: str | None = None


class FieldReviewIn(BaseModel):
    """Bir envanter alanının insan incelemesi sonucu."""

    review_status: Literal["ACCEPTED", "REJECTED", "IN_REVIEW", "NOT_APPLICABLE"]
    sensitivity: DataSensitivity | None = None
    identifiability: IdentifiabilityLevel | None = None
    notes: str | None = Field(default=None, max_length=2000)


# ===========================================================================
# İşleme faaliyeti
# ===========================================================================
class ProcessingActivityIn(BaseModel):
    """
    İşleme faaliyeti girdisi.

    Amaç ve dayanak *kod* ile verilir; kayıt yoksa oluşturulur ve insan
    incelemesi bekleyen olarak işaretlenir. Madde referansı serbest metindir ve
    sistem tarafından doğrulanmaz.
    """

    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    workspace_id: int | None = None
    controller_role: ProcessingRole = ProcessingRole.UNKNOWN

    purpose_code: str | None = Field(default=None, max_length=64)
    purpose_name: str | None = Field(default=None, max_length=255)

    legal_basis_code: str | None = Field(default=None, max_length=64)
    legal_basis_kind: LegalBasisKind = LegalBasisKind.UNKNOWN
    legal_basis_name: str | None = Field(default=None, max_length=255)
    article_reference: str | None = Field(default=None, max_length=128)

    special_category_legal_basis_code: str | None = Field(default=None, max_length=64)
    special_category_legal_basis_kind: LegalBasisKind = LegalBasisKind.UNKNOWN
    processes_special_category: bool = False

    data_subject_categories: list[str] = Field(default_factory=list)
    subject_count_estimate: int | None = Field(default=None, ge=0)
    security_measures: str | None = None

    involves_automated_decision: bool = False
    involves_profiling: bool = False
    involves_ai: bool = False
    human_oversight_note: str | None = None
    cross_border_transfer: bool = False

    owner_user_id: int | None = None
    department: str | None = Field(default=None, max_length=128)
    change_note: str | None = Field(default=None, max_length=512)


class ProcessingActivityUpdateIn(ProcessingActivityIn):
    """Güncellemede kod değişmez; gövdedeki ``code`` alanı yok sayılır."""

    code: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=255)


class ProcessingActivityOut(_Out):
    id: int
    tenant_id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    workspace_id: int | None = None
    controller_role: str
    purpose_id: int | None = None
    legal_basis_id: int | None = None
    special_category_legal_basis_id: int | None = None
    processes_special_category: bool = False
    data_subject_categories: list[str] = Field(default_factory=list)
    subject_count_estimate: int | None = None
    retention_policy_id: int | None = None
    security_measures: str | None = None
    dpia_required: bool | None = None
    dpia_status: str
    involves_automated_decision: bool = False
    involves_profiling: bool = False
    involves_ai: bool = False
    human_oversight_note: str | None = None
    cross_border_transfer: bool = False
    owner_user_id: int | None = None
    department: str | None = None
    compliance_state: str
    review_status: str
    last_reviewed_at: datetime | None = None
    next_review_due_at: datetime | None = None
    created_at: datetime | None = None
    #: Neden insan incelemesi bekliyor — boş liste "uyumlu" demek değildir.
    review_reasons: list[str] = Field(default_factory=list)


# ===========================================================================
# Aydınlatma metni
# ===========================================================================
class NoticeIn(BaseModel):
    notice_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=255)
    body: str = Field(min_length=10)
    language: Literal["tr", "en"] = "tr"
    kind: NoticeKind = NoticeKind.PRIVACY_NOTICE
    covered_activity_codes: list[str] = Field(default_factory=list)
    display_url: str | None = Field(default=None, max_length=512)
    display_channel: IntakeChannel = IntakeChannel.UNKNOWN
    effective_from: datetime | None = None
    #: True ise sürüm yayımlanır ve önceki yürürlük kaydı düşürülür.
    publish: bool = False


class NoticeOut(_Out):
    id: int
    tenant_id: int
    notice_code: str
    version: str
    language: str
    kind: str
    title: str
    body_hash: str
    status: str
    is_current: bool = False
    published_at: datetime | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    display_url: str | None = None
    review_status: str
    supersedes_id: int | None = None
    evidence_id: int | None = None
    created_at: datetime | None = None
    #: Gövde yalnızca tek kayıt okunurken doldurulur; listede taşınmaz.
    body: str | None = None


# ===========================================================================
# Rıza
# ===========================================================================
class ConsentIn(BaseModel):
    subject_type: str = Field(min_length=2, max_length=32)
    subject_ref: str = Field(min_length=1, max_length=128)
    purpose_code: str = Field(min_length=2, max_length=64)
    purpose_name: str | None = Field(default=None, max_length=255)
    status: Literal["GIVEN", "REFUSED", "PENDING"] = "GIVEN"
    #: Açık rıza ile örtülü onay aynı hukuki değeri taşımaz.
    is_explicit: bool = False
    channel: IntakeChannel = IntakeChannel.UNKNOWN
    notice_version_id: int | None = None
    scope_text: str | None = None
    scope_codes: list[str] = Field(default_factory=list)
    collected_at: datetime | None = None
    expires_at: datetime | None = None
    proof_reference: str | None = Field(default=None, max_length=512)
    language: Literal["tr", "en"] = "tr"


class ConsentWithdrawIn(BaseModel):
    reason: WithdrawalReason = WithdrawalReason.SUBJECT_REQUEST
    reason_text: str | None = Field(default=None, max_length=2000)
    channel: IntakeChannel = IntakeChannel.UNKNOWN
    requested_at: datetime | None = None
    effective_at: datetime | None = None
    triggers_erasure: bool = False


class ConsentOut(_Out):
    id: int
    tenant_id: int
    subject_type: str
    subject_ref: str
    purpose_id: int | None = None
    purpose_code: str | None = None
    version: int
    supersedes_id: int | None = None
    superseded_by_id: int | None = None
    status: str
    #: Kayıtlı durum ile *şu an* geçerli olan durum ayrı raporlanır: süresi
    #: dolmuş bir rıza satırı hâlâ ``GIVEN`` yazar ama geçerli değildir.
    effective_status: str
    is_explicit: bool = False
    channel: str
    notice_version_id: int | None = None
    collected_at: datetime | None = None
    expires_at: datetime | None = None
    proof_reference: str | None = None
    review_status: str
    is_current: bool = True
    evidence_id: int | None = None
    created_at: datetime | None = None


class WithdrawalOut(_Out):
    id: int
    tenant_id: int
    consent_record_id: int
    subject_type: str
    subject_ref: str
    reason: str
    reason_text: str | None = None
    channel: str
    requested_at: datetime | None = None
    effective_at: datetime
    processed_at: datetime | None = None
    stops_future_processing: bool = True
    triggers_erasure: bool = False
    #: Alıcılara bildirim yapılmadıkça geri alma tamamlanmış sayılmaz.
    downstream_notified: bool = False
    evidence_id: int | None = None
    review_status: str


# ===========================================================================
# İlgili kişi başvurusu
# ===========================================================================
class DsrCreateIn(BaseModel):
    subject_type: str = Field(min_length=2, max_length=32)
    request_type: DsrType
    subject_ref: str | None = Field(default=None, max_length=128)
    subject_contact: str | None = Field(default=None, max_length=255)
    channel: IntakeChannel = IntakeChannel.UNKNOWN
    received_at: datetime | None = None
    #: Yanıt süresi bu katmanda hesaplanmaz. Verilmezse son tarih boş kalır ve
    #: başvuru insan incelemesinde görünür.
    due_at: datetime | None = None
    due_basis: str | None = Field(default=None, max_length=255)
    description: str | None = None
    requested_scope: str | None = None
    assigned_to_user_id: int | None = None
    submitted_by_agent: bool = False
    agent_name: str | None = Field(default=None, max_length=255)


class DsrIdentityIn(BaseModel):
    method: IdentityVerificationMethod = IdentityVerificationMethod.UNKNOWN
    outcome: VerificationOutcome = VerificationOutcome.PENDING
    document_type: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class DsrFulfilIn(BaseModel):
    """
    Başvuruyu kapatan karar.

    ``REJECTED`` seçildiğinde ``rejection_reason`` zorunludur: gerekçesiz ret,
    ilgili kişinin itiraz hakkını fiilen ortadan kaldırır.
    """

    outcome: Literal["FULFILLED", "PARTIALLY_FULFILLED", "REJECTED"] = "FULFILLED"
    response_summary: str | None = Field(default=None, max_length=4000)
    response_channel: IntakeChannel = IntakeChannel.UNKNOWN
    rejection_reason: str | None = Field(default=None, max_length=4000)


class DsrVerificationOut(BaseModel):
    id: int
    method: str
    outcome: str
    attempted_at: datetime
    failure_reason: str | None = None


class DsrTransitionIn(BaseModel):
    """
    Durum değişikliği isteği.

    ``note`` zorunludur: gerekçesiz bir durum değişikliği, denetimde "birisi
    seçti" dışında hiçbir şey söylemez.
    """

    to_status: str = Field(min_length=3, max_length=32)
    note: str = Field(min_length=3, max_length=2000)


class DsrOut(_Out):
    id: int
    tenant_id: int
    reference: str
    request_type: str
    status: str
    subject_type: str
    subject_ref: str | None = None
    channel: str
    received_at: datetime
    due_at: datetime | None = None
    extended_due_at: datetime | None = None
    due_basis: str | None = None
    due_basis_status: str
    identity_verified: bool = False
    identity_verified_at: datetime | None = None
    assigned_to_user_id: int | None = None
    description: str | None = None
    responded_at: datetime | None = None
    response_summary: str | None = None
    rejection_reason: str | None = None
    escalated: bool = False
    is_closed: bool = False
    #: Son tarih tanımsızsa ``None`` — "gecikmedi" ile "süre bilinmiyor" aynı
    #: şey değildir.
    is_overdue: bool | None = None
    days_open: int = 0
    verifications: list[DsrVerificationOut] = Field(default_factory=list)


# ===========================================================================
# Yurt dışı aktarım
# ===========================================================================
class TransferIn(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    activity_id: int | None = None
    #: ISO 3166-1 alpha-2. Uç noktanın alan adından ülke çıkarılmaz.
    destination_country: str | None = Field(default=None, min_length=2, max_length=2)
    destination_country_name: str | None = Field(default=None, max_length=96)
    destination_region: str | None = Field(default=None, max_length=96)
    mechanism: TransferMechanism = TransferMechanism.UNKNOWN
    mechanism_reference: str | None = Field(default=None, max_length=255)
    adequacy_reference: str | None = Field(default=None, max_length=255)
    adequacy_status: ReviewStatus | None = None
    tia_performed: bool | None = None
    tia_outcome: str | None = Field(default=None, max_length=24)
    supplementary_measures: str | None = None
    subprocessors_disclosed: bool | None = None
    data_categories_note: str | None = None
    frequency: str | None = Field(default=None, max_length=64)
    change_note: str | None = Field(default=None, max_length=512)


class TransferOut(_Out):
    id: int
    tenant_id: int
    code: str
    name: str
    description: str | None = None
    activity_id: int | None = None
    recipient_id: int | None = None
    vendor_id: int | None = None
    destination_country: str | None = None
    destination_country_name: str | None = None
    destination_region: str | None = None
    mechanism: str
    mechanism_reference: str | None = None
    adequacy_reference: str | None = None
    adequacy_status: str
    tia_performed: bool = False
    tia_performed_at: datetime | None = None
    tia_outcome: str
    supplementary_measures: str | None = None
    subprocessors_disclosed: bool = False
    data_categories_note: str | None = None
    frequency: str | None = None
    status: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime | None = None
    review_reasons: list[str] = Field(default_factory=list)


# ===========================================================================
# Kural paketleri ve değerlendirme
# ===========================================================================
class RulePackOut(_Out):
    id: int
    pack_key: str
    version: str
    schema_version: str
    jurisdiction: str
    regulation_code: str
    title_tr: str
    title_en: str
    status: str
    content_hash: str
    source_hash: str
    rule_count: int
    requires_human_review: bool
    retrieved_date: Any | None = None
    effective_from: Any | None = None
    effective_to: Any | None = None
    activated_at: datetime | None = None
    activated_by_id: int | None = None
    withdrawn_at: datetime | None = None
    created_at: datetime | None = None
    #: Paketin **güncel içeriğini** kapsayan, geri alınmamış onay var mı?
    has_effective_approval: bool = False
    approval_count: int = 0


class RulePackApproveIn(BaseModel):
    """
    Kural paketi kararı.

    ``comment`` zorunludur: gerekçesiz bir onay, denetimde "birisi tıkladı"
    dışında hiçbir bilgi taşımaz. Dört göz kuralı servis katmanında uygulanır —
    paketi hazırlayan kişi kendi işini onaylayamaz.
    """

    decision: Literal["APPROVED", "REJECTED", "CHANGES_REQUESTED"] = "APPROVED"
    comment: str = Field(min_length=3, max_length=2000)
    approver_role: Literal["REVIEWER", "APPROVER", "DPO", "LEGAL_COUNSEL"] = "DPO"
    evidence_url: str | None = Field(default=None, max_length=512)


class RuleResultOut(BaseModel):
    rule_id: str
    article_ref: str | None = None
    outcome: str
    severity: str | None = None
    confidence: str | None = None
    requires_human_review: bool = True
    applicability_result: str
    condition_result: str
    missing_evidence: list[str] = Field(default_factory=list)
    matched_exceptions: list[str] = Field(default_factory=list)
    triggered_reviews: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evaluation_id: int | None = None


class EvaluateIn(BaseModel):
    """
    Bir bağlamı kural paketine karşı değerlendirir.

    ``context`` düz bir sözlüktür; kurallar noktalı yol ile alan okur. Bağlamda
    bulunmayan bir alan "yok" sayılmaz, ``UNKNOWN`` sayılır — eksik kanıt
    uyumluluk üretmez.
    """

    context: dict[str, Any]
    pack_key: str | None = Field(default=None, max_length=64)
    jurisdiction: str | None = Field(default=None, max_length=8)
    #: False ise sonuç kaydedilmez (deneme çalıştırması).
    persist: bool = True


class EvaluationOut(BaseModel):
    """
    Değerlendirme sonucu.

    ``counts`` her sonuç türünden kaç kural olduğunu verir. Tek bir yüzde
    üretilmez: kanıtı eksik bir kuralı uyumlu bir kuralla aynı paydada toplayan
    bir skor, eksikliği sayısal olarak seyreltir.
    """

    evaluated_at: datetime
    pack_key: str
    pack_version: str
    pack_status: str
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    needs_attention: int = 0
    human_review_required: bool = True
    engine_version: str | None = None
    persisted: bool = False
    results: list[RuleResultOut] = Field(default_factory=list)


# ===========================================================================
# Kanıt
# ===========================================================================
class EvidenceOut(_Out):
    id: int
    tenant_id: int
    sequence_no: int
    kind: str
    title: str
    description: str | None = None
    subject_type: str | None = None
    subject_id: int | None = None
    subject_ref: str | None = None
    collected_at: datetime
    collected_by_user_id: int | None = None
    collected_by_label: str | None = None
    collector_kind: str
    source: str | None = None
    source_uri: str | None = None
    byte_size: int | None = None
    storage_path: str | None = None
    content_hash: str
    previous_hash: str | None = None
    chain_hash: str
    supersedes_id: int | None = None
    created_at: datetime | None = None


# ===========================================================================
# Human Sovereignty Protocol
# ===========================================================================
class HspEvaluateIn(BaseModel):
    """
    Motora sorulan somut soru: "bu makine, bu insan için, bu eylemi yapabilir
    mi?"

    ``context`` politikadaki koşulların okunduğu sözlüktür. Bir koşul anahtarı
    bağlamda yoksa koşul **sağlanmamış** sayılır ve sonuç izin üretmez.
    """

    machine_code: str = Field(min_length=1, max_length=64)
    action_code: str = Field(min_length=1, max_length=96)
    subject_ref: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)
    purpose: str | None = Field(default=None, max_length=255)


class HspDecisionOut(BaseModel):
    """
    Kararın çağırana dönen hâli.

    ``allow`` yalnızca ``verdict == ALLOW`` olduğunda ``True``'dur. Bekleyen,
    süresi dolmuş, geri alınmış ya da eşleşen politikası olmayan her durum
    ``allow=False`` döner — sessiz fail-open yoktur.
    """

    allow: bool
    verdict: str
    reasons: list[str] = Field(default_factory=list)
    receipt_id: int | None = None
    request_id: int | None = None
    policy_code: str | None = None
    appeal_path: str | None = None


class HspReceiptOut(_Out):
    id: int
    tenant_id: int
    request_id: int | None = None
    subject_ref: str
    machine_id: int | None = None
    machine_code: str | None = None
    action_code: str
    domain: str
    question: str
    verdict: str
    allow: bool
    reasons: list[str] = Field(default_factory=list)
    policy_id: int | None = None
    policy_code: str | None = None
    policy_version: int | None = None
    capability_token_id: int | None = None
    override_id: int | None = None
    appeal_path: str | None = None
    human_review_required: bool = False
    decided_at: datetime
    previous_hash: str | None = None
    content_hash: str


class HspAppealIn(BaseModel):
    """
    Bir karara itiraz.

    ``reason`` zorunludur: gerekçesiz bir itiraz, incelemeyi yapan insana
    hiçbir şey söylemez ve kuyruğu doldurmaktan başka işe yaramaz.
    """

    reason: str = Field(min_length=3, max_length=4000)
    contact: str | None = Field(default=None, max_length=255)


class HspReceiptDetailOut(HspReceiptOut):
    """
    Tek makbuz + varsa itirazının durumu.

    Makbuz tablosu **salt eklenir**; itiraz makbuzu değiştirmez. İtiraz, ilgili
    kişi başvurusu olarak (``AUTOMATED_DECISION_REVIEW``) ayrı kaydedilir ve
    burada yalnızca *gösterilir*. Böylece "karar değişti mi" sorusunun cevabı
    makbuz zincirini bozmadan izlenebilir kalır.
    """

    appeal_reference: str | None = None
    appeal_reason: str | None = None
    appeal_contact: str | None = None
    appeal_channel: str | None = None
    appeal_submitted_at: datetime | None = None
    appeal_outcome: str | None = None
    appeal_decided_at: datetime | None = None


class ChainVerifyOut(BaseModel):
    """Zincir bütünlük raporu (kanıt, makbuz veya değerlendirme zinciri)."""

    valid: bool
    checked: int
    broken_at: int | None = None
    reason: str | None = None
    status: str | None = None


__all__ = [
    "ChainVerifyOut",
    "ComplianceOverviewOut",
    "ComplianceRegime",
    "ConsentIn",
    "ConsentOut",
    "ConsentStatus",
    "ConsentWithdrawIn",
    "DataFieldOut",
    "DsrCreateIn",
    "DsrFulfilIn",
    "DsrIdentityIn",
    "DsrOut",
    "DsrStatus",
    "DsrVerificationOut",
    "EvaluateIn",
    "EvaluationOut",
    "EvidenceChainOut",
    "EvidenceOut",
    "FieldReviewIn",
    "HspDecisionOut",
    "HspEvaluateIn",
    "HspReceiptOut",
    "NoticeIn",
    "NoticeOut",
    "OverviewCategoryOut",
    "ProcessingActivityIn",
    "ProcessingActivityOut",
    "ProcessingActivityUpdateIn",
    "RulePackApproveIn",
    "RulePackOut",
    "RuleResultOut",
    "ScanRequestIn",
    "ScanResultOut",
    "TenantBriefOut",
    "TransferIn",
    "TransferOut",
    "WithdrawalOut",
]
