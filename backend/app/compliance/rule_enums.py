"""
Kural motorunun alan sözlüğü.

Bu dosya ``app.compliance.enums`` yerine geçmez, onu tamamlar. Ortak uyumluluk
sözlüğü (rejim, hassasiyet, kanıt türü, risk...) orada yaşar; burada yalnızca
**versiyonlu kural paketi ve değerlendirme motoru** için gereken, başka hiçbir
yerde anlamı olmayan üyeler tanımlanır.

Ayrı dosya olmasının somut sebebi ``EvaluationOutcome``: ortak sözlükteki
``ComplianceState`` bir varlığın genel durumunu anlatır ve ``PARTIAL`` üyesi
taşır. Kural motorunun sonucu ise beş kapalı değerden biri olmak zorundadır ve
``INSUFFICIENT_EVIDENCE``i mutlaka içermelidir — "kısmen uyumlu" demek,
kanıtın eksik olduğunu söylemenin yerini tutmaz.

Ortak sözlükte hâlihazırda doğru tanımlanmış olan üyeler yeniden yazılmaz,
buradan yeniden dışa verilir; motorun tek bir sözlük dosyasına bakması yeter.
"""

from __future__ import annotations

from enum import StrEnum

from app.compliance.enums import UNKNOWN, ConfidenceLevel, Criticality, EvidenceIntegrity

# ---------------------------------------------------------------------------
# Yaşam döngüsü
# ---------------------------------------------------------------------------


class LifecycleStatus(StrEnum):
    """
    Kural paketi ve kural yaşam döngüsü.

    ``ACTIVE``a geçiş yalnızca insan onayı (``RuleApproval``) ile mümkündür;
    kısıt hem servis katmanında hem de ORM olay dinleyicisinde ayrı ayrı
    uygulanır. Tek bir yerde uygulansaydı, o yeri atlayan her yeni çağrı
    onayı da atlardı.
    """

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


#: İzin verilen durum geçişleri. Burada olmayan her geçiş reddedilir —
#: "DRAFT -> ACTIVE" gibi onayı atlayan bir sıçrama sessizce kabul edilmez.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    LifecycleStatus.DRAFT: frozenset(
        {LifecycleStatus.IN_REVIEW, LifecycleStatus.WITHDRAWN}
    ),
    LifecycleStatus.IN_REVIEW: frozenset(
        {LifecycleStatus.DRAFT, LifecycleStatus.APPROVED, LifecycleStatus.WITHDRAWN}
    ),
    LifecycleStatus.APPROVED: frozenset(
        {LifecycleStatus.ACTIVE, LifecycleStatus.IN_REVIEW, LifecycleStatus.WITHDRAWN}
    ),
    LifecycleStatus.ACTIVE: frozenset(
        {LifecycleStatus.SUPERSEDED, LifecycleStatus.WITHDRAWN}
    ),
    LifecycleStatus.SUPERSEDED: frozenset({LifecycleStatus.WITHDRAWN}),
    LifecycleStatus.WITHDRAWN: frozenset(),
}


# ---------------------------------------------------------------------------
# Değerlendirme
# ---------------------------------------------------------------------------


class EvaluationOutcome(StrEnum):
    """
    Bir kuralın bir bağlam üzerindeki sonucu — kapalı küme.

    ``INSUFFICIENT_EVIDENCE`` ile ``COMPLIANT`` arasındaki ayrım bu katmanın
    en önemli tasarım kararıdır: kanıt bulunamaması uyumluluk anlamına gelmez.
    """

    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class PredicateResult(StrEnum):
    """
    Üç değerli mantık sonucu.

    İki değerli mantık uyumluluk için yanlış araçtır: bilinmeyen bir olguyu
    ``False`` saymak, kuralı sessizce "ihlal edilmiş" ya da "uygulanmaz"
    göstererek gerçeği gizler. Kleene üç değerli mantığı bilinmeyeni
    bilinmeyen olarak taşır ve sonuca kadar götürür.
    """

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class EvaluatorKind(StrEnum):
    """Değerlendirmeyi kimin ürettiği."""

    ENGINE = "ENGINE"
    HUMAN = "HUMAN"


# ---------------------------------------------------------------------------
# Onay
# ---------------------------------------------------------------------------


class ApprovalDecision(StrEnum):
    """
    Onay kararı.

    Ortak sözlükteki ``ReviewStatus`` bir sürecin nerede durduğunu anlatır;
    burada kaydedilen ise verilmiş, imzalanmış ve geri alınabilir tek bir
    karardır. İkisi karıştırılırsa "incelemede" durumu yürürlükte bir onay
    gibi okunabilir.
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ApproverRole(StrEnum):
    """Onayı veren kişinin hangi sıfatla imzaladığı."""

    REVIEWER = "REVIEWER"
    APPROVER = "APPROVER"
    DPO = "DPO"
    LEGAL_COUNSEL = "LEGAL_COUNSEL"


# ---------------------------------------------------------------------------
# Kaynak bütünlüğü
# ---------------------------------------------------------------------------


class SourceHashKind(StrEnum):
    """
    ``source_hash`` alanının neyin özeti olduğu.

    Bu ayrım dürüstlük gereğidir: elimizde resmî metnin kendisi yoksa
    ``SOURCE_TEXT`` demek, doğrulanmamış bir kanıt iddiası üretir. Referans
    alanlarından türetilen parmak izi ``REFERENCE_FINGERPRINT`` olarak
    işaretlenir ve resmî metnin indirilip doğrulandığı anlamına gelmez.
    """

    SOURCE_TEXT = "SOURCE_TEXT"
    REFERENCE_FINGERPRINT = "REFERENCE_FINGERPRINT"
    UNKNOWN = "UNKNOWN"


class SourceVerification(StrEnum):
    """Resmî kaynağın bir insan tarafından doğrulanıp doğrulanmadığı."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"


#: Bağlam değerinde geçtiğinde "bilinmiyor" sayılan dizgeler. Keşif tarayıcısı
#: (Faz 0) bilinmeyeni bu sözcüklerle raporlar; kural motoru da aynı sözlüğü
#: kullanır ki envanterden gelen bir ``UNKNOWN`` karşılaştırmada sessizce
#: ``False``a dönüşüp kuralı yanlış tarafa düşürmesin.
UNKNOWN_TOKENS: frozenset[str] = frozenset(
    {"UNKNOWN", "REVIEW_REQUIRED", "BILINMIYOR", "BİLİNMİYOR", "N/A", "TBD"}
)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "UNKNOWN",
    "UNKNOWN_TOKENS",
    "ApprovalDecision",
    "ApproverRole",
    "ConfidenceLevel",
    "Criticality",
    "EvaluationOutcome",
    "EvaluatorKind",
    "EvidenceIntegrity",
    "LifecycleStatus",
    "PredicateResult",
    "SourceHashKind",
    "SourceVerification",
]
