"""
Uyumluluk katmanının alan sözlüğü.

Bu modül bilerek ``app.core.enums``'tan ayrı tutulur: iş alanı sözlüğü satış
süreçlerini, buradaki sözlük ise hukuki/denetsel durumları tanımlar. İkisini
tek dosyada birleştirmek, uyumluluk katmanı kaldırıldığında çekirdeği de
kırardı.

Üç tasarım kararı bütün modülü belirler:

1. **Bilinmeyen, uygun değildir.** Her değerlendirme enum'ında ``UNKNOWN`` ve
   ``REVIEW_REQUIRED`` üyeleri vardır ve varsayılan daima bunlardan biridir.
   Kanıt yokluğu asla ``COMPLIANT``'a çözülmez.
2. **Madde numarası enum'a girmez.** Aşağıdaki üyeler dayanak *türlerini*
   adlandırır; hangi kanunun hangi maddesine karşılık geldiği kayıt düzeyinde,
   insan tarafından doldurulan ve doğrulanan bir alandır
   (``LegalBasis.article_reference``). Bir madde numarasını koda gömmek,
   mevzuat değiştiğinde sessizce yanlış rapor üretir.
3. **Değerler kısa büyük harf dizgidir.** SQLite ve PostgreSQL'de aynı şekilde
   saklanır, göçler arasında sabit kalır; okunabilir etiketler i18n
   kataloglarında yaşar.
"""

from __future__ import annotations

from enum import StrEnum

#: Bilinmeyeni tek bir yerden adlandırmak, "", None ve "UNKNOWN" üçlüsünün
#: raporlarda üç ayrı kova gibi görünmesini engeller.
UNKNOWN = "UNKNOWN"


# ===========================================================================
# Ortak değerlendirme durumları
# ===========================================================================
class ComplianceRegime(StrEnum):
    """Bir kaydın hangi düzenleme/standart bağlamında değerlendirildiği."""

    KVKK = "KVKK"
    GDPR = "GDPR"
    EU_AI_ACT = "EU_AI_ACT"
    ISO_27001 = "ISO_27001"
    ISO_27701 = "ISO_27701"
    ISO_42001 = "ISO_42001"
    SOC2 = "SOC2"
    PCI_DSS = "PCI_DSS"
    NIS2 = "NIS2"
    INTERNAL_POLICY = "INTERNAL_POLICY"
    CONTRACTUAL = "CONTRACTUAL"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ComplianceState(StrEnum):
    """
    Bir varlığın uyumluluk değerlendirmesi.

    ``REVIEW_REQUIRED`` ile ``UNKNOWN`` ayrı tutulur: birincisi "baktık, karar
    insana kaldı", ikincisi "hiç bakılmadı" demektir. Raporda bu ikisini
    birleştirmek, incelenmemiş alanı incelenmiş gibi gösterir.
    """

    COMPLIANT = "COMPLIANT"
    PARTIAL = "PARTIAL"
    NON_COMPLIANT = "NON_COMPLIANT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNKNOWN = "UNKNOWN"


class ReviewStatus(StrEnum):
    """İnsan incelemesinin nerede durduğu — uyumluluk kararından bağımsızdır."""

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IN_REVIEW = "IN_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(StrEnum):
    """Otomatik bir bulgunun ne kadar güvenilir olduğu."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class Criticality(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# Kiracı / çalışma alanı
# ===========================================================================
class TenantStatus(StrEnum):
    PENDING_SETUP = "PENDING_SETUP"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


class WorkspaceKind(StrEnum):
    """Kiracı içi kapsam bölümü — sorumluluk sınırlarını ayırır."""

    LEGAL_ENTITY = "LEGAL_ENTITY"
    BUSINESS_UNIT = "BUSINESS_UNIT"
    REGION = "REGION"
    PROJECT = "PROJECT"
    SYSTEM = "SYSTEM"


class Environment(StrEnum):
    PRODUCTION = "PRODUCTION"
    STAGING = "STAGING"
    DEVELOPMENT = "DEVELOPMENT"
    TEST = "TEST"
    LOCAL = "LOCAL"
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# Veri envanteri
# ===========================================================================
class DataSensitivity(StrEnum):
    """
    Veri hassasiyeti.

    ``SPECIAL_CATEGORY`` ayrı bir üyedir çünkü özel nitelikli veri, sıradan
    kişisel verinin "daha hassas" hâli değil, ayrı bir hukuki dayanak rejimine
    tabi ayrı bir kategoridir.
    """

    SPECIAL_CATEGORY = "SPECIAL_CATEGORY"
    PERSONAL = "PERSONAL"
    LOCATION = "LOCATION"
    FINANCIAL = "FINANCIAL"
    PSEUDONYMISED = "PSEUDONYMISED"
    ANONYMISED = "ANONYMISED"
    NON_PERSONAL = "NON_PERSONAL"
    UNKNOWN = "UNKNOWN"


class IdentifiabilityLevel(StrEnum):
    """Alanın tek başına veya birleştirilerek kimliği ne ölçüde belirlediği."""

    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    PSEUDONYMOUS = "PSEUDONYMOUS"
    ANONYMOUS = "ANONYMOUS"
    UNKNOWN = "UNKNOWN"


class DataSubjectCategory(StrEnum):
    CUSTOMER = "CUSTOMER"
    PROSPECT = "PROSPECT"
    EMPLOYEE = "EMPLOYEE"
    CONTRACTOR = "CONTRACTOR"
    DRIVER = "DRIVER"
    SUPPLIER_CONTACT = "SUPPLIER_CONTACT"
    VISITOR = "VISITOR"
    CHILD = "CHILD"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class DiscoverySource(StrEnum):
    """Bir envanter satırının nereden geldiği — kanıt değerini belirler."""

    SCANNER = "SCANNER"
    MANUAL = "MANUAL"
    IMPORT = "IMPORT"
    VENDOR_DECLARATION = "VENDOR_DECLARATION"
    UNKNOWN = "UNKNOWN"


class AssetKind(StrEnum):
    APPLICATION = "APPLICATION"
    SERVICE = "SERVICE"
    DATABASE = "DATABASE"
    FILE_STORE = "FILE_STORE"
    MOBILE_APP = "MOBILE_APP"
    DESKTOP_APP = "DESKTOP_APP"
    THIRD_PARTY_SAAS = "THIRD_PARTY_SAAS"
    AI_MODEL = "AI_MODEL"
    DEVICE = "DEVICE"
    NETWORK = "NETWORK"
    BACKUP = "BACKUP"
    LOG_SINK = "LOG_SINK"
    QUEUE = "QUEUE"
    OTHER = "OTHER"


class HostingModel(StrEnum):
    ON_PREMISE = "ON_PREMISE"
    PRIVATE_CLOUD = "PRIVATE_CLOUD"
    PUBLIC_CLOUD = "PUBLIC_CLOUD"
    HYBRID = "HYBRID"
    VENDOR_HOSTED = "VENDOR_HOSTED"
    END_USER_DEVICE = "END_USER_DEVICE"
    UNKNOWN = "UNKNOWN"


class StoreKind(StrEnum):
    RELATIONAL_DB = "RELATIONAL_DB"
    DOCUMENT_DB = "DOCUMENT_DB"
    KEY_VALUE = "KEY_VALUE"
    FILE_SYSTEM = "FILE_SYSTEM"
    OBJECT_STORAGE = "OBJECT_STORAGE"
    BACKUP_ARCHIVE = "BACKUP_ARCHIVE"
    LOG_STORE = "LOG_STORE"
    CACHE = "CACHE"
    EXPORT_FILE = "EXPORT_FILE"
    SPREADSHEET = "SPREADSHEET"
    EMAIL = "EMAIL"
    PAPER = "PAPER"
    OTHER = "OTHER"


class ProcessingRole(StrEnum):
    """
    Tarafın işleme rolü.

    KVKK'daki "veri sorumlusu / veri işleyen" ayrımı ile GDPR'daki
    "controller / processor" ayrımı birebir örtüşmez; bu yüzden üyeler
    nötr adlandırılır ve rejime bağlama kaydın kendi alanında yapılır.
    """

    CONTROLLER = "CONTROLLER"
    JOINT_CONTROLLER = "JOINT_CONTROLLER"
    PROCESSOR = "PROCESSOR"
    SUB_PROCESSOR = "SUB_PROCESSOR"
    RECIPIENT_ONLY = "RECIPIENT_ONLY"
    UNKNOWN = "UNKNOWN"


class RecipientKind(StrEnum):
    INTERNAL_UNIT = "INTERNAL_UNIT"
    GROUP_COMPANY = "GROUP_COMPANY"
    PROCESSOR = "PROCESSOR"
    JOINT_CONTROLLER = "JOINT_CONTROLLER"
    THIRD_PARTY_CONTROLLER = "THIRD_PARTY_CONTROLLER"
    PUBLIC_AUTHORITY = "PUBLIC_AUTHORITY"
    DATA_SUBJECT = "DATA_SUBJECT"
    UNKNOWN = "UNKNOWN"


class FlowDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    INTERNAL = "INTERNAL"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class LegalBasisKind(StrEnum):
    """
    Hukuki dayanak *türü*.

    Bu üyeler dayanağın niteliğini adlandırır, bir kanun maddesine karşılık
    gelmez. Somut madde referansı ``LegalBasis.article_reference`` alanında,
    kaynağıyla birlikte ve insan doğrulamasına tabi olarak tutulur.
    """

    CONSENT = "CONSENT"
    EXPLICIT_CONSENT = "EXPLICIT_CONSENT"
    CONTRACT_NECESSITY = "CONTRACT_NECESSITY"
    LEGAL_OBLIGATION = "LEGAL_OBLIGATION"
    EXPRESSLY_PROVIDED_BY_LAW = "EXPRESSLY_PROVIDED_BY_LAW"
    VITAL_INTERESTS = "VITAL_INTERESTS"
    FACTUAL_IMPOSSIBILITY = "FACTUAL_IMPOSSIBILITY"
    PUBLIC_INTEREST_TASK = "PUBLIC_INTEREST_TASK"
    LEGITIMATE_INTERESTS = "LEGITIMATE_INTERESTS"
    ESTABLISHMENT_OF_RIGHTS = "ESTABLISHMENT_OF_RIGHTS"
    MADE_PUBLIC_BY_SUBJECT = "MADE_PUBLIC_BY_SUBJECT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# Yurt dışı aktarım
# ===========================================================================
class TransferMechanism(StrEnum):
    """
    Aktarımın dayandığı araç.

    ``NONE_IDENTIFIED`` ile ``UNKNOWN`` ayrıdır: birincisi "arandı, bulunamadı"
    (yani aktarım dayanaksız görünüyor), ikincisi "henüz bakılmadı" demektir.
    """

    ADEQUACY_DECISION = "ADEQUACY_DECISION"
    STANDARD_CONTRACTUAL_CLAUSES = "STANDARD_CONTRACTUAL_CLAUSES"
    BINDING_CORPORATE_RULES = "BINDING_CORPORATE_RULES"
    UNDERTAKING = "UNDERTAKING"
    AUTHORITY_AUTHORISATION = "AUTHORITY_AUTHORISATION"
    EXPLICIT_CONSENT = "EXPLICIT_CONSENT"
    DEROGATION = "DEROGATION"
    NONE_IDENTIFIED = "NONE_IDENTIFIED"
    UNKNOWN = "UNKNOWN"


class DpaStatus(StrEnum):
    """Veri işleme sözleşmesinin durumu."""

    SIGNED = "SIGNED"
    IN_NEGOTIATION = "IN_NEGOTIATION"
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    MISSING = "MISSING"
    NOT_REQUIRED = "NOT_REQUIRED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# Aydınlatma ve rıza
# ===========================================================================
class NoticeKind(StrEnum):
    """
    Aydınlatma/bilgilendirme metninin türü.

    Aydınlatma yükümlülüğü rızadan bağımsızdır: rıza alınmayan işlemelerde de
    aydınlatma gerekir. Bu yüzden metin türleri rıza durumundan ayrı bir
    sözlükte yaşar.
    """

    PRIVACY_NOTICE = "PRIVACY_NOTICE"
    EMPLOYEE_NOTICE = "EMPLOYEE_NOTICE"
    COOKIE_NOTICE = "COOKIE_NOTICE"
    CCTV_NOTICE = "CCTV_NOTICE"
    LOCATION_TRACKING_NOTICE = "LOCATION_TRACKING_NOTICE"
    AI_DISCLOSURE = "AI_DISCLOSURE"
    MARKETING_NOTICE = "MARKETING_NOTICE"
    TERMS = "TERMS"
    OTHER = "OTHER"


class NoticeStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


class ConsentStatus(StrEnum):
    PENDING = "PENDING"
    GIVEN = "GIVEN"
    REFUSED = "REFUSED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    INVALID = "INVALID"


class IntakeChannel(StrEnum):
    """Rızanın alındığı veya başvurunun geldiği kanal — ispat yükünü etkiler."""

    WEB_FORM = "WEB_FORM"
    MOBILE_APP = "MOBILE_APP"
    HANDHELD_DEVICE = "HANDHELD_DEVICE"
    PAPER = "PAPER"
    WET_SIGNATURE = "WET_SIGNATURE"
    E_SIGNATURE = "E_SIGNATURE"
    VERBAL_RECORDED = "VERBAL_RECORDED"
    EMAIL = "EMAIL"
    REGISTERED_EMAIL = "REGISTERED_EMAIL"
    POST = "POST"
    IN_PERSON = "IN_PERSON"
    PHONE = "PHONE"
    API = "API"
    IMPORTED = "IMPORTED"
    UNKNOWN = "UNKNOWN"


class WithdrawalReason(StrEnum):
    SUBJECT_REQUEST = "SUBJECT_REQUEST"
    PURPOSE_ENDED = "PURPOSE_ENDED"
    NOTICE_CHANGED = "NOTICE_CHANGED"
    EXPIRY = "EXPIRY"
    CONTROLLER_DECISION = "CONTROLLER_DECISION"
    DATA_ERROR = "DATA_ERROR"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# ===========================================================================
# Saklama ve imha
# ===========================================================================
class RetentionTrigger(StrEnum):
    """Saklama süresinin hangi olaydan itibaren saydığı."""

    FROM_CREATION = "FROM_CREATION"
    FROM_LAST_ACTIVITY = "FROM_LAST_ACTIVITY"
    FROM_RELATIONSHIP_END = "FROM_RELATIONSHIP_END"
    FROM_CONTRACT_END = "FROM_CONTRACT_END"
    FROM_CONSENT_WITHDRAWAL = "FROM_CONSENT_WITHDRAWAL"
    FROM_FISCAL_YEAR_END = "FROM_FISCAL_YEAR_END"
    EVENT_BASED = "EVENT_BASED"
    INDEFINITE = "INDEFINITE"
    UNKNOWN = "UNKNOWN"


class RetentionAction(StrEnum):
    DELETE = "DELETE"
    ANONYMISE = "ANONYMISE"
    PSEUDONYMISE = "PSEUDONYMISE"
    ARCHIVE = "ARCHIVE"
    RETURN_TO_CONTROLLER = "RETURN_TO_CONTROLLER"
    REVIEW = "REVIEW"
    RETAIN = "RETAIN"


class RetentionEventOutcome(StrEnum):
    """
    İmha çalışmasının sonucu.

    ``BLOCKED_BY_LEGAL_HOLD`` ayrı bir sonuçtur: sessizce atlanmış bir imha ile
    hukuki muhafaza nedeniyle bilinçli durdurulmuş imha denetimde aynı şey
    değildir.
    """

    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTED = "EXECUTED"
    PARTIAL = "PARTIAL"
    BLOCKED_BY_LEGAL_HOLD = "BLOCKED_BY_LEGAL_HOLD"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class LegalHoldStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


# ===========================================================================
# İlgili kişi başvuruları
# ===========================================================================
class DsrType(StrEnum):
    """
    Başvuru türü.

    Üyeler talebin içeriğini adlandırır; hangi rejimin hangi hakkı tanıdığı
    ve süre sınırı kayıt düzeyinde, doğrulanmış kaynakla belirlenir.
    """

    ACCESS = "ACCESS"
    INFORMATION = "INFORMATION"
    RECTIFICATION = "RECTIFICATION"
    ERASURE = "ERASURE"
    RESTRICTION = "RESTRICTION"
    PORTABILITY = "PORTABILITY"
    OBJECTION = "OBJECTION"
    AUTOMATED_DECISION_REVIEW = "AUTOMATED_DECISION_REVIEW"
    CONSENT_WITHDRAWAL = "CONSENT_WITHDRAWAL"
    THIRD_PARTY_NOTIFICATION = "THIRD_PARTY_NOTIFICATION"
    DAMAGE_CLAIM = "DAMAGE_CLAIM"
    COMPLAINT = "COMPLAINT"
    OTHER = "OTHER"


class DsrStatus(StrEnum):
    RECEIVED = "RECEIVED"
    IDENTITY_PENDING = "IDENTITY_PENDING"
    IDENTITY_FAILED = "IDENTITY_FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_SUBJECT = "AWAITING_SUBJECT"
    ON_HOLD = "ON_HOLD"
    FULFILLED = "FULFILLED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    REJECTED = "REJECTED"
    WITHDRAWN_BY_SUBJECT = "WITHDRAWN_BY_SUBJECT"
    ESCALATED = "ESCALATED"


class IdentityVerificationMethod(StrEnum):
    EXISTING_AUTHENTICATED_SESSION = "EXISTING_AUTHENTICATED_SESSION"
    EMAIL_CHALLENGE = "EMAIL_CHALLENGE"
    SMS_CHALLENGE = "SMS_CHALLENGE"
    REGISTERED_EMAIL = "REGISTERED_EMAIL"
    E_SIGNATURE = "E_SIGNATURE"
    IN_PERSON_ID_CHECK = "IN_PERSON_ID_CHECK"
    NOTARY = "NOTARY"
    KNOWN_CONTACT_DETAILS = "KNOWN_CONTACT_DETAILS"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNKNOWN = "UNKNOWN"


class VerificationOutcome(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ABANDONED = "ABANDONED"


class FulfilmentAction(StrEnum):
    """Başvuruyu karşılamak için yapılması gereken somut iş."""

    LOCATE = "LOCATE"
    EXPORT = "EXPORT"
    RECTIFY = "RECTIFY"
    DELETE = "DELETE"
    ANONYMISE = "ANONYMISE"
    RESTRICT = "RESTRICT"
    STOP_PROCESSING = "STOP_PROCESSING"
    NOTIFY_RECIPIENT = "NOTIFY_RECIPIENT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    RESPOND = "RESPOND"
    OTHER = "OTHER"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ===========================================================================
# Kanıt, risk, kontrol, bulgu
# ===========================================================================
class EvidenceKind(StrEnum):
    SCAN_OUTPUT = "SCAN_OUTPUT"
    CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"
    LOG_EXPORT = "LOG_EXPORT"
    SCREENSHOT = "SCREENSHOT"
    DOCUMENT = "DOCUMENT"
    CONTRACT = "CONTRACT"
    POLICY = "POLICY"
    ATTESTATION = "ATTESTATION"
    TEST_RESULT = "TEST_RESULT"
    DPIA = "DPIA"
    TRANSFER_IMPACT_ASSESSMENT = "TRANSFER_IMPACT_ASSESSMENT"
    DECISION_RECORD = "DECISION_RECORD"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    CONSENT_PROOF = "CONSENT_PROOF"
    NOTICE_TEXT = "NOTICE_TEXT"
    CORRESPONDENCE = "CORRESPONDENCE"
    OTHER = "OTHER"


class EvidenceIntegrity(StrEnum):
    """
    Zincir doğrulamasının sonucu.

    Bu değer kanıt satırına yazılmaz — yazılsaydı append-only kayıt
    değiştirilmiş olurdu. Doğrulama raporunun dönüş değeri olarak kullanılır.
    """

    OK = "OK"
    MISSING_HASH = "MISSING_HASH"
    BROKEN_CHAIN = "BROKEN_CHAIN"
    CONTENT_MISMATCH = "CONTENT_MISMATCH"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    UNVERIFIED = "UNVERIFIED"


class RiskCategory(StrEnum):
    PRIVACY = "PRIVACY"
    SECURITY = "SECURITY"
    AI = "AI"
    LEGAL = "LEGAL"
    OPERATIONAL = "OPERATIONAL"
    THIRD_PARTY = "THIRD_PARTY"
    FUNDAMENTAL_RIGHTS = "FUNDAMENTAL_RIGHTS"
    FINANCIAL = "FINANCIAL"
    OTHER = "OTHER"


class RiskLikelihood(StrEnum):
    RARE = "RARE"
    UNLIKELY = "UNLIKELY"
    POSSIBLE = "POSSIBLE"
    LIKELY = "LIKELY"
    ALMOST_CERTAIN = "ALMOST_CERTAIN"
    UNKNOWN = "UNKNOWN"


class RiskImpact(StrEnum):
    NEGLIGIBLE = "NEGLIGIBLE"
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    MAJOR = "MAJOR"
    SEVERE = "SEVERE"
    UNKNOWN = "UNKNOWN"


class RiskTreatment(StrEnum):
    MITIGATE = "MITIGATE"
    ACCEPT = "ACCEPT"
    TRANSFER = "TRANSFER"
    AVOID = "AVOID"
    UNDECIDED = "UNDECIDED"


class RiskStatus(StrEnum):
    IDENTIFIED = "IDENTIFIED"
    ASSESSED = "ASSESSED"
    TREATMENT_PLANNED = "TREATMENT_PLANNED"
    MITIGATED = "MITIGATED"
    ACCEPTED = "ACCEPTED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"


class ControlType(StrEnum):
    PREVENTIVE = "PREVENTIVE"
    DETECTIVE = "DETECTIVE"
    CORRECTIVE = "CORRECTIVE"
    DETERRENT = "DETERRENT"
    COMPENSATING = "COMPENSATING"


class ControlImplementation(StrEnum):
    AUTOMATED = "AUTOMATED"
    MANUAL = "MANUAL"
    HYBRID = "HYBRID"
    PLANNED = "PLANNED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNKNOWN = "UNKNOWN"


class ControlTestResult(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_TESTED = "NOT_TESTED"


class FindingSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_REMEDIATION = "IN_REMEDIATION"
    RESOLVED = "RESOLVED"
    RISK_ACCEPTED = "RISK_ACCEPTED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    DEFERRED = "DEFERRED"


class FindingSource(StrEnum):
    SCANNER = "SCANNER"
    RULEPACK = "RULEPACK"
    CONTROL_TEST = "CONTROL_TEST"
    DSR_HANDLING = "DSR_HANDLING"
    RETENTION_RUN = "RETENTION_RUN"
    INTERNAL_AUDIT = "INTERNAL_AUDIT"
    EXTERNAL_AUDIT = "EXTERNAL_AUDIT"
    INCIDENT = "INCIDENT"
    MANUAL = "MANUAL"


__all__ = [
    "UNKNOWN",
    "AssetKind",
    "ComplianceRegime",
    "ComplianceState",
    "ConfidenceLevel",
    "ConsentStatus",
    "ControlImplementation",
    "ControlTestResult",
    "ControlType",
    "Criticality",
    "DataSensitivity",
    "DataSubjectCategory",
    "DiscoverySource",
    "DpaStatus",
    "DsrStatus",
    "DsrType",
    "Environment",
    "EvidenceIntegrity",
    "EvidenceKind",
    "FindingSeverity",
    "FindingSource",
    "FindingStatus",
    "FlowDirection",
    "FulfilmentAction",
    "HostingModel",
    "IdentifiabilityLevel",
    "IdentityVerificationMethod",
    "IntakeChannel",
    "LegalBasisKind",
    "LegalHoldStatus",
    "NoticeKind",
    "NoticeStatus",
    "ProcessingRole",
    "RecipientKind",
    "RetentionAction",
    "RetentionEventOutcome",
    "RetentionTrigger",
    "ReviewStatus",
    "RiskCategory",
    "RiskImpact",
    "RiskLikelihood",
    "RiskStatus",
    "RiskTreatment",
    "StoreKind",
    "TaskStatus",
    "TenantStatus",
    "TransferMechanism",
    "VerificationOutcome",
    "WithdrawalReason",
    "WorkspaceKind",
]
