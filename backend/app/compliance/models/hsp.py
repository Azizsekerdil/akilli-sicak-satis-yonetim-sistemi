"""
Human Sovereignty Protocol (HSP) — kalıcı veri modeli.

Bu katmanın cevapladığı tek soru şudur: *bu makine bu insana ne yapmaya
yetkilidir?*  Soru eylemden SONRA denetlenmez, eylemden ÖNCE sorulur.

Yetki üç ayrı alanda ayrı ayrı değerlendirilir; çünkü bir sistemin bir insan
hakkında bilgi öğrenmesi, o insan hakkında karar vermesi ve o insanın
dünyasında bir şey yapması aynı şey değildir ve aynı izinle örtülemez:

    KNOW    sistem insan hakkında ne öğreniyor
    DECIDE  insan hakkında hangi kararı veriyor
    ACT     insanın dijital/ekonomik/bilişsel/fiziksel dünyasında ne yapıyor

Tasarım kararları ve gerekçeleri:

*   **Varsayılan reddir.**  Açık bir :class:`RightsPolicy` izni yoksa eylem
    yapılamaz.  "Politika bulunamadı" bir hata değil, geçerli bir DENY
    gerekçesidir.  Bilinmeyen sessizce izin sayılmaz.
*   **Süre zorunludur.**  Pasaport, yetenek jetonu ve devir kayıtlarının
    bitiş tarihi ``nullable=False``.  Süresiz yetki, geri alınması unutulan
    yetkidir; HSP'de yetki yenilenmek zorundadır.
*   **Kanıt kayıtları append-only.**  Karar makbuzları (:class:`RightsReceipt`)
    ve talep kayıtları (:class:`ActionRequest`) güncellenmez; yapılandırma
    değişirse yeni SÜRÜM yazılır (``version`` + ``supersedes_id``).
*   **Kanıt tabloları yabancı anahtar taşımaz.**  Makbuz, hakkında karar
    verilen makine silinse bile ayakta kalmalıdır; ``ON DELETE CASCADE`` ile
    yok edilebilen bir kanıt kanıt değildir.  Yapılandırma tabloları arasında
    yabancı anahtar kullanılır, kanıt tablolarında kullanılmaz.
*   **İnsan hakları tek bir skora indirgenmez.**  Motor bir "uyum puanı"
    üretmez; bir *verdict* ve makine tarafından okunabilir bir *gerekçe
    listesi* üretir.  Skor, gerekçeyi gizler.

Mevzuat atıfları bilinçli olarak boş bırakılmıştır: ``legal_basis_ref``
alanının varsayılan değeri ``UNKNOWN``'dır ve tohum verisinde
``REVIEW_REQUIRED`` olarak işaretlenir.  Madde numarası tahmin edilmez.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.utils import dumps, loads
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)

#: Tek şirketli kurulumlarda (Van Sales'ın bugünkü hâli) kullanılan kiracı
#: kimliği.
#:
#: ``tenant_id`` bu modülde bilinçli olarak ``cmp_tenants``a yabancı anahtar
#: DEĞİLDİR.  İki nedeni var: (1) HSP kanıt tabloları hiçbir yapılandırma
#: satırına bağlanmaz, silinen bir satır yüzünden kaybolabilen kanıt kanıt
#: değildir; (2) bileşik indeksler zaten ``tenant_id`` ile başlar, ayrıca tekil
#: indeks eklemek aynı sütunu iki kez indekslerdi.  Kiracı bütünlüğü servis
#: katmanında doğrulanır.
DEFAULT_TENANT_ID = 1

#: Politika ve jetonlarda "her değer" anlamına gelen joker.  ``None`` yerine
#: açık bir joker kullanılır: ``NULL`` "bilinmiyor" demektir, ``*`` "hepsi".
WILDCARD = "*"


# ===========================================================================
# Alan sözlüğü
# ===========================================================================
class SovereigntyDomain(StrEnum):
    """Yetkinin değerlendirildiği üç alan."""

    KNOW = "KNOW"
    DECIDE = "DECIDE"
    ACT = "ACT"


class HspVerdict(StrEnum):
    """Motorun döndürebileceği kararlar.  ``ALLOW`` dışındakiler eylemi durdurur."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN_APPROVAL = "REQUIRE_HUMAN_APPROVAL"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class PolicyEffect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class ImpactLevel(StrEnum):
    """
    Eylemin insan üzerindeki etkisi.

    ``SEVERE`` özel bir eşiktir: geri döndürülemez fiziksel veya ekonomik zarar
    anlamına gelir ve politika yazarı tarafından önceden feragat edilemez
    (bkz. ``hsp_engine._requires_human``).
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    SEVERE = "SEVERE"


#: Sıralama karşılaştırması için; StrEnum kendi başına sıralanamaz.
IMPACT_ORDER: dict[str, int] = {
    ImpactLevel.LOW: 1,
    ImpactLevel.MEDIUM: 2,
    ImpactLevel.HIGH: 3,
    ImpactLevel.SEVERE: 4,
}


class ImpactDimension(StrEnum):
    """Eylemin insanın hangi dünyasına dokunduğu."""

    DIGITAL = "DIGITAL"
    ECONOMIC = "ECONOMIC"
    COGNITIVE = "COGNITIVE"
    PHYSICAL = "PHYSICAL"
    AUTONOMY = "AUTONOMY"
    REPUTATION = "REPUTATION"


class MachineKind(StrEnum):
    RULE_ENGINE = "RULE_ENGINE"
    STATISTICAL_MODEL = "STATISTICAL_MODEL"
    ML_MODEL = "ML_MODEL"
    LLM_AGENT = "LLM_AGENT"
    TRACKER = "TRACKER"
    HUMAN_IN_LOOP_TOOL = "HUMAN_IN_LOOP_TOOL"
    UNKNOWN = "UNKNOWN"


class SubjectKind(StrEnum):
    CUSTOMER = "CUSTOMER"
    EMPLOYEE = "EMPLOYEE"
    CONTACT = "CONTACT"
    VISITOR = "VISITOR"
    UNKNOWN = "UNKNOWN"


class NodeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class MachineStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class PassportStatus(StrEnum):
    VALID = "VALID"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class GrantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


class GrantSource(StrEnum):
    """Yetkiyi kimin verdiği — makbuzda görünmesi gereken bilgi."""

    SUBJECT = "SUBJECT"                    # insanın kendi rızası
    CONTROLLER = "CONTROLLER"              # veri sorumlusu / işveren kararı
    DELEGATE = "DELEGATE"                  # vekil
    LEGAL_OBLIGATION = "LEGAL_OBLIGATION"  # yasal yükümlülük — dayanağı ayrıca yazılır
    UNKNOWN = "UNKNOWN"


class DelegateKind(StrEnum):
    HUMAN = "HUMAN"
    MACHINE = "MACHINE"


class RevocationTarget(StrEnum):
    CAPABILITY_TOKEN = "CAPABILITY_TOKEN"
    DELEGATION = "DELEGATION"
    POLICY = "POLICY"
    PASSPORT = "PASSPORT"
    MACHINE = "MACHINE"
    ACTION = "ACTION"
    SUBJECT_ALL = "SUBJECT_ALL"


class OverrideStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


#: ``legal_basis_ref`` için kullanılabilecek işaretler.  Madde numarası
#: uydurmak yerine bilinmeyen açıkça bilinmeyen olarak yazılır.
LEGAL_BASIS_UNKNOWN = "UNKNOWN"
LEGAL_BASIS_REVIEW = "REVIEW_REQUIRED"


def _json_list(raw: str | None) -> list[str]:
    """Text sütununda saklanan JSON listesini güvenle çözer."""
    value = loads(raw, [])
    return [str(v) for v in value] if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    value = loads(raw, {})
    return dict(value) if isinstance(value, dict) else {}


# ===========================================================================
# 1) Egemenlik düğümü — insan
# ===========================================================================
class HumanSovereigntyNode(Base, TimestampMixin):
    """
    Sistemin yetkisini üzerinde kullandığı insanın kaydı.

    Burada kişisel veri **kopyalanmaz**.  ``subject_ref`` ana sisteme bir
    işaretçidir (``customer:1234``, ``user:57``); adı, telefonu, adresi bu
    tabloda tutulmaz.  Uyumluluk katmanının kendisi yeni bir kişisel veri
    havuzu yaratırsa çözdüğünden büyük bir sorun üretmiş olur.
    """

    __tablename__ = "cmp_hsp_node"
    __table_args__ = (
        UniqueConstraint("tenant_id", "subject_ref", name="uq_cmp_hsp_node_subject"),
        Index("ix_cmp_hsp_node_tenant_kind", "tenant_id", "subject_kind"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )

    #: "<varlık>:<kimlik>" biçiminde opak işaretçi.  Ana sistemin birincil
    #: anahtarını taşır, kişisel veriyi taşımaz.
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_kind: Mapped[str] = mapped_column(
        String(24), default=SubjectKind.UNKNOWN, nullable=False
    )

    #: Operasyonel etiket (ör. "Müşteri #1234").  Kişi adı YAZILMAZ.
    display_label: Mapped[str | None] = mapped_column(String(128))

    jurisdiction: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    #: Üç durumlu: YES / NO / UNKNOWN.  Boolean kullanılmaz; "bilinmiyor"
    #: durumunu ``False`` ile temsil etmek, bilinmeyeni sessizce "hayır"a
    #: çevirir ve reşit olmayan bir kişiyi yetişkin gibi işler.
    minor_status: Mapped[str] = mapped_column(String(8), default="UNKNOWN", nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=NodeStatus.ACTIVE, nullable=False, index=True
    )
    #: Bu insanın kararlara itiraz edebileceği yol.  Politikadaki yol boşsa
    #: makbuza bu değer yazılır.
    appeal_path: Mapped[str | None] = mapped_column(String(255))
    preferred_language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    def is_active(self) -> bool:
        return self.status == NodeStatus.ACTIVE


# ===========================================================================
# 2) Makine ve pasaportu
# ===========================================================================
class Machine(Base, TimestampMixin, AuthorMixin):
    """
    Bir insan üzerinde otomatik işlem yapabilen bileşen.

    "Makine" burada yapay zekâ demek değildir: bir SQL sorgusu kadar basit bir
    kural motoru da bir insanın satın almasını engelliyorsa makinedir.
    ``source_ref`` bileşenin koddaki yerini tutar; böylece kayıt soyut bir
    beyan olmaktan çıkıp denetlenebilir bir işaretçiye dönüşür.
    """

    __tablename__ = "cmp_hsp_machine"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_hsp_machine_code"),
    )

    id: Mapped[int] = pk()
    #: Ayrı bir tekil indeks yok: (tenant_id, code) tekilliği zaten tenant_id
    #: ile başlayan bir indeks üretir.
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), default=MachineKind.UNKNOWN, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text)

    #: Makineden sorumlu insan/birim.  Sahibi olmayan makine çalıştırılmamalıdır.
    operator_ref: Mapped[str | None] = mapped_column(String(128))
    owner_user_id: Mapped[int | None] = mapped_column(Integer)

    #: Koddaki yeri: "app/services/customer_service.py:540".
    source_ref: Mapped[str | None] = mapped_column(String(255))
    #: İnsan onayı olmadan tetiklenebiliyor mu?
    is_autonomous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=MachineStatus.ACTIVE, nullable=False, index=True
    )

    def is_operational(self) -> bool:
        return self.status == MachineStatus.ACTIVE


class MachinePassport(Base, TimestampMixin, AuthorMixin):
    """
    Makinenin süreli kimlik belgesi.

    Pasaportsuz makine hiçbir alanda işlem yapamaz; bildirmediği bir alanda da
    yapamaz.  ``expires_at`` zorunludur: süresiz pasaport, kimsenin yenilemeyi
    hatırlamadığı kalıcı yetkidir.
    """

    __tablename__ = "cmp_hsp_passport"
    __table_args__ = (
        UniqueConstraint("tenant_id", "serial", name="uq_cmp_hsp_passport_serial"),
        Index("ix_cmp_hsp_passport_machine", "machine_id", "status"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    machine_id: Mapped[int] = fk("cmp_hsp_machine.id", ondelete="CASCADE", index=False)

    serial: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    #: JSON listesi: ["KNOW", "DECIDE"].  Bildirilmeyen alan kullanılamaz.
    declared_domains: Mapped[str] = mapped_column(Text, default=lambda: dumps([]), nullable=False)

    model_ref: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64))
    #: Modelin/kuralın sürümüne bağlı özet.  İmza değil — imza altyapısı bu
    #: fazın kapsamı dışında, o yüzden "signature" adı bilerek kullanılmadı.
    attestation_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(16), default=PassportStatus.VALID, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(Integer)

    def domains(self) -> list[str]:
        return _json_list(self.declared_domains)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utcnow())


# ===========================================================================
# 3) Eylem beyanı
# ===========================================================================
class MachineActionManifest(Base, TimestampMixin, AuthorMixin):
    """
    Makinenin "şunu yapmak istiyorum" beyanı.

    Beyan edilmemiş eylem yapılamaz.  Beyan, eylemin hangi alanda olduğunu,
    insanın hangi dünyasına dokunduğunu, geri alınabilir olup olmadığını ve
    itiraz yolunun ne olduğunu önceden yazmaya zorlar.  Manifest sürümlenir;
    davranış değiştiğinde eski satır güncellenmez, yeni sürüm yazılır.
    """

    __tablename__ = "cmp_hsp_action_manifest"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "machine_id", "action_code", "version",
            name="uq_cmp_hsp_manifest_version",
        ),
        Index("ix_cmp_hsp_manifest_lookup", "tenant_id", "action_code", "domain"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    machine_id: Mapped[int] = fk("cmp_hsp_machine.id", ondelete="CASCADE")

    action_code: Mapped[str] = mapped_column(String(96), nullable=False)
    domain: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)

    impact_level: Mapped[str] = mapped_column(
        String(16), default=ImpactLevel.MEDIUM, nullable=False
    )
    #: JSON listesi: ["ECONOMIC", "AUTONOMY"].
    impact_dimensions: Mapped[str] = mapped_column(
        Text, default=lambda: dumps([]), nullable=False
    )
    #: JSON listesi: işlenen veri kategorileri (keşif tarayıcısının sözlüğüyle
    #: aynı adları kullanır: KISISEL / KONUM / OZEL_NITELIKLI / DIGER).
    data_categories: Mapped[str] = mapped_column(
        Text, default=lambda: dumps([]), nullable=False
    )

    is_reversible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reversal_path: Mapped[str | None] = mapped_column(String(255))
    human_review_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    appeal_path: Mapped[str | None] = mapped_column(String(255))

    #: Hukuki dayanak *işaretçisi*.  Madde numarası tahmin edilmez; bilinmiyorsa
    #: UNKNOWN, incelenmesi gerekiyorsa REVIEW_REQUIRED yazılır.
    legal_basis_ref: Mapped[str] = mapped_column(
        String(64), default=LEGAL_BASIS_UNKNOWN, nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(String(255))

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    def dimensions(self) -> list[str]:
        return _json_list(self.impact_dimensions)

    def categories(self) -> list[str]:
        return _json_list(self.data_categories)


# ===========================================================================
# 4) Hak politikası
# ===========================================================================
class RightsPolicy(Base, TimestampMixin, AuthorMixin):
    """
    "Bu makine bu alanda bu insana ne yapabilir?" sorusunun yazılı cevabı.

    Eşleşen politika yoksa cevap REDDİR — bu tablodaki bir satırın yokluğu,
    izin verilmediği anlamına gelir.  ``machine_id`` boş bırakıldığında
    politika tüm makineleri kapsar; joker (``*``) eylem kodu ve özne türü için
    kullanılır.

    ``max_impact_level`` politikanın kapsadığı en yüksek etkiyi sınırlar:
    düşük etkili bir eylem için yazılmış bir izin, sonradan ağırlaşan bir
    eylemi sessizce kapsayamaz.

    ``condition_json`` çağrı bağlamında sağlanması gereken eşitlikleri tutar
    (ör. ``{"day_session_active": true}``).  Bağlamda anahtar YOKSA koşul
    sağlanmamış sayılır ve sonuç REDDİR; eksik kanıt izin üretmez.
    """

    __tablename__ = "cmp_hsp_rights_policy"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", "version", name="uq_cmp_hsp_policy_version"),
        Index("ix_cmp_hsp_policy_lookup", "tenant_id", "domain", "action_code"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    domain: Mapped[str] = mapped_column(String(8), nullable=False)
    action_code: Mapped[str] = mapped_column(String(96), default=WILDCARD, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), default=WILDCARD, nullable=False)
    #: NULL = tüm makineler.
    machine_id: Mapped[int | None] = fk("cmp_hsp_machine.id", nullable=True, ondelete="CASCADE")

    effect: Mapped[str] = mapped_column(String(8), default=PolicyEffect.DENY, nullable=False)
    #: Güvenli varsayılan: aksi açıkça yazılmadıkça insan onayı beklenir.
    requires_human_approval: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    requires_capability_token: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    max_impact_level: Mapped[str] = mapped_column(
        String(16), default=ImpactLevel.MEDIUM, nullable=False
    )
    #: JSON nesnesi; bağlamda sağlanması gereken eşitlikler.
    condition_json: Mapped[str | None] = mapped_column(Text)

    purpose: Mapped[str | None] = mapped_column(Text)
    legal_basis_ref: Mapped[str] = mapped_column(
        String(64), default=LEGAL_BASIS_UNKNOWN, nullable=False
    )
    appeal_path: Mapped[str | None] = mapped_column(String(255))
    retention_days: Mapped[int | None] = mapped_column(Integer)

    #: Büyük değer önce değerlendirilir.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    issued_by_user_id: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    def conditions(self) -> dict[str, Any]:
        return _json_dict(self.condition_json)

    def label(self) -> str:
        return f"{self.code}@v{self.version}"


# ===========================================================================
# 5) Yetenek jetonu ve devir
# ===========================================================================
class CapabilityToken(Base, TimestampMixin):
    """
    Süreli, sayılı ve geri alınabilir yetki.

    Politika "ne yapılabilir"i, jeton "şu ana kadar, şu kadar kez, şu tutara
    kadar yapılabilir"i söyler.  Süresi dolan jeton sessizce yok sayılmaz;
    motor açıkça ``EXPIRED`` döner (bkz. ``hsp_engine._check_token``).
    """

    __tablename__ = "cmp_hsp_capability_token"
    __table_args__ = (
        UniqueConstraint("tenant_id", "token_ref", name="uq_cmp_hsp_token_ref"),
        Index("ix_cmp_hsp_token_lookup", "tenant_id", "machine_id", "action_code"),
        Index("ix_cmp_hsp_token_subject", "tenant_id", "subject_ref"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    token_ref: Mapped[str] = mapped_column(String(64), nullable=False)

    machine_id: Mapped[int] = fk("cmp_hsp_machine.id", ondelete="CASCADE", index=False)
    #: NULL = kiracı genelinde geçerli.  Doluysa yalnızca o insan için.
    subject_ref: Mapped[str | None] = mapped_column(String(128))
    domain: Mapped[str] = mapped_column(String(8), nullable=False)
    action_code: Mapped[str] = mapped_column(String(96), default=WILDCARD, nullable=False)

    granted_by: Mapped[str] = mapped_column(
        String(24), default=GrantSource.UNKNOWN, nullable=False
    )
    granted_by_user_id: Mapped[int | None] = mapped_column(Integer)
    policy_id: Mapped[int | None] = mapped_column(Integer)
    scope_json: Mapped[str | None] = mapped_column(Text)

    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 0 = sınırsız kullanım.
    max_uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Ekonomik eylemler için üst sınır; aşılırsa insan onayına yükseltilir.
    amount_limit: Mapped[Decimal | None] = mapped_column(Money)

    status: Mapped[str] = mapped_column(
        String(16), default=GrantStatus.ACTIVE, nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revocation_id: Mapped[int | None] = mapped_column(Integer)

    def is_exhausted(self) -> bool:
        return self.max_uses > 0 and self.use_count >= self.max_uses

    def is_usable(self, now: datetime | None = None) -> bool:
        if self.status != GrantStatus.ACTIVE:
            return False
        if self.expires_at <= (now or utcnow()):
            return False
        return not self.is_exhausted()


class Delegation(Base, TimestampMixin):
    """
    Bir insanın yetkisini başka bir insana veya makineye devretmesi.

    Devir, devredenin sahip olduğundan fazlasını veremez; motor devri yalnızca
    *ek koşul* olarak doğrular, tek başına izin kaynağı saymaz.  Bitiş tarihi
    zorunludur.
    """

    __tablename__ = "cmp_hsp_delegation"
    __table_args__ = (
        Index("ix_cmp_hsp_delegation_lookup", "tenant_id", "delegator_subject_ref"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    delegator_subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    delegate_kind: Mapped[str] = mapped_column(
        String(8), default=DelegateKind.HUMAN, nullable=False
    )
    delegate_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_id: Mapped[int | None] = fk(
        "cmp_hsp_machine.id", nullable=True, ondelete="CASCADE"
    )

    #: JSON listeleri; boş liste "kısıt yok" değil, "kapsam yok" demektir.
    domains: Mapped[str] = mapped_column(Text, default=lambda: dumps([]), nullable=False)
    action_codes: Mapped[str] = mapped_column(Text, default=lambda: dumps([]), nullable=False)

    reason: Mapped[str | None] = mapped_column(Text)
    evidence_ref: Mapped[str | None] = mapped_column(String(255))

    valid_from: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=GrantStatus.ACTIVE, nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def domain_list(self) -> list[str]:
        return _json_list(self.domains)

    def action_list(self) -> list[str]:
        return _json_list(self.action_codes)

    def covers(self, domain: str, action_code: str) -> bool:
        doms = self.domain_list()
        acts = self.action_list()
        domain_ok = domain in doms or WILDCARD in doms
        action_ok = action_code in acts or WILDCARD in acts
        return domain_ok and action_ok


# ===========================================================================
# 6) Talep, makbuz — kanıt katmanı (append-only, yabancı anahtarsız)
# ===========================================================================
class ActionRequest(Base):
    """
    Motora sorulan somut soru.

    Her değerlendirme — reddedilenler dâhil — bir satır bırakır.  Yalnızca izin
    verilenleri kaydeden bir sistem, kaç kez reddettiğini bilemez ve bu da
    denetlenemez hâle gelir.
    """

    __tablename__ = "cmp_hsp_action_request"
    __table_args__ = (
        Index("ix_cmp_hsp_request_subject", "tenant_id", "subject_ref", "requested_at"),
        Index("ix_cmp_hsp_request_machine", "tenant_id", "machine_id", "requested_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Kanıt kaydı; yapılandırma silinse bile ayakta kalması için FK yok.
    machine_id: Mapped[int | None] = mapped_column(Integer)
    machine_code: Mapped[str | None] = mapped_column(String(64))
    manifest_id: Mapped[int | None] = mapped_column(Integer)

    action_code: Mapped[str] = mapped_column(String(96), nullable=False)
    domain: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(255))
    context_json: Mapped[str | None] = mapped_column(Text)

    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer)
    correlation_ref: Mapped[str | None] = mapped_column(String(64), index=True)

    verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    allow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    policy_id: Mapped[int | None] = mapped_column(Integer)
    receipt_id: Mapped[int | None] = mapped_column(Integer)


class RightsReceipt(Base):
    """
    Karar makbuzu — insanın elinde kalan kanıt.

    Ne soruldu, ne karar verildi, hangi politika uygulandı, gerekçe neydi, ne
    zaman oldu ve itiraz nereye yapılır: hepsi tek satırda ve zincirli özetle
    korunur.  ``content_hash = sha256(previous_hash + kanonik_içerik)``;
    ``app.services.audit_service`` ile aynı desen kullanılır, böylece geçmişe
    dönük bir düzeltme zinciri kırar ve doğrulama nerede kırıldığını söyler.

    Zincir **kiracı başına** yürütülür: çok kiracılı bir kurulumda bir kiracının
    makbuzlarını dışa aktarmak, diğerinin zincirini bozmadan mümkün olmalıdır.

    Bu tablonun güncelleme yolu yoktur.  Düzeltme, yeni bir makbuzla yapılır.
    """

    __tablename__ = "cmp_hsp_receipt"
    __table_args__ = (
        Index("ix_cmp_hsp_receipt_chain", "tenant_id", "id"),
        Index("ix_cmp_hsp_receipt_subject", "tenant_id", "subject_ref", "decided_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    request_id: Mapped[int | None] = mapped_column(Integer)

    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_id: Mapped[int | None] = mapped_column(Integer)
    machine_code: Mapped[str | None] = mapped_column(String(64))
    action_code: Mapped[str] = mapped_column(String(96), nullable=False)
    domain: Mapped[str] = mapped_column(String(8), nullable=False)

    #: İnsan tarafından okunabilir soru: "VS-CREDIT-GATE, customer:12 için
    #: credit.limit.block_sale (ACT) yapabilir mi?"
    question: Mapped[str] = mapped_column(String(512), nullable=False)

    verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    allow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: JSON listesi.  Tek bir skora indirgenmez — gerekçeler ayrı ayrı durur.
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)

    policy_id: Mapped[int | None] = mapped_column(Integer)
    policy_code: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[int | None] = mapped_column(Integer)
    capability_token_id: Mapped[int | None] = mapped_column(Integer)
    override_id: Mapped[int | None] = mapped_column(Integer)

    evidence_json: Mapped[str | None] = mapped_column(Text)
    appeal_path: Mapped[str | None] = mapped_column(String(255))
    human_review_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    def reasons(self) -> list[str]:
        return _json_list(self.reasons_json)


# ===========================================================================
# 7) Olağanüstü hâl ve geri alma
# ===========================================================================
class EmergencyOverride(Base, TimestampMixin):
    """
    İnsanın camı kırdığı an.

    Kapsamı bilerek dardır: bir override, yalnızca ``REQUIRE_HUMAN_APPROVAL``
    kararını ``ALLOW``a çevirebilir — yani beklenen insan onayının yerine
    geçer.  Açık bir DENY politikasını, geri alınmış bir yetkiyi veya süresi
    dolmuş bir jetonu **açamaz**; aksi hâlde "acil durum" her yasağı aşan bir
    ana anahtara dönüşür.

    ``expires_at`` zorunlu, ``review_required`` varsayılan olarak açıktır:
    her kullanım sonradan bir insan tarafından incelenmek üzere işaretlenir.
    """

    __tablename__ = "cmp_hsp_emergency_override"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_cmp_hsp_override_code"),
        Index("ix_cmp_hsp_override_active", "tenant_id", "status", "expires_at"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    machine_id: Mapped[int | None] = fk(
        "cmp_hsp_machine.id", nullable=True, ondelete="CASCADE"
    )
    subject_ref: Mapped[str | None] = mapped_column(String(128))
    action_code: Mapped[str | None] = mapped_column(String(96))
    domain: Mapped[str | None] = mapped_column(String(8))

    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Serbest metin zorunlu: gerekçesiz acil durum kaydı denetlenemez.
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    authorized_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_by_role: Mapped[str | None] = mapped_column(String(32))

    valid_from: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=OverrideStatus.ACTIVE, nullable=False
    )

    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(Integer)

    def is_open(self, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        return (
            self.status == OverrideStatus.ACTIVE
            and self.valid_from <= moment < self.expires_at
        )


class Revocation(Base, TimestampMixin):
    """
    Verilmiş bir yetkinin geri alınması.

    Geri alma her şeyin üstündedir: motor politikaya bakmadan önce geri alma
    kaydına bakar ve eşleşme varsa ``REVOKED`` döner.  Bir insan "artık hayır"
    dediğinde, bunun bir politikayla tartışılması gerekmez.
    """

    __tablename__ = "cmp_hsp_revocation"
    __table_args__ = (
        Index("ix_cmp_hsp_revocation_scope", "tenant_id", "target_kind", "is_active"),
        Index("ix_cmp_hsp_revocation_subject", "tenant_id", "subject_ref"),
    )

    id: Mapped[int] = pk()
    tenant_id: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_TENANT_ID, nullable=False
    )
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Hedefin birincil anahtarı (jeton, politika, pasaport...).
    target_id: Mapped[int | None] = mapped_column(Integer)
    target_ref: Mapped[str | None] = mapped_column(String(128))

    machine_id: Mapped[int | None] = fk(
        "cmp_hsp_machine.id", nullable=True, ondelete="CASCADE"
    )
    subject_ref: Mapped[str | None] = mapped_column(String(128))
    action_code: Mapped[str | None] = mapped_column(String(96))
    domain: Mapped[str | None] = mapped_column(String(8))

    reason_code: Mapped[str] = mapped_column(String(32), default="UNSPECIFIED", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(24), default=GrantSource.SUBJECT, nullable=False
    )
    revoked_by_user_id: Mapped[int | None] = mapped_column(Integer)
    revoked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


__all__ = [
    "DEFAULT_TENANT_ID",
    "WILDCARD",
    "IMPACT_ORDER",
    "LEGAL_BASIS_UNKNOWN",
    "LEGAL_BASIS_REVIEW",
    "SovereigntyDomain",
    "HspVerdict",
    "PolicyEffect",
    "ImpactLevel",
    "ImpactDimension",
    "MachineKind",
    "SubjectKind",
    "NodeStatus",
    "MachineStatus",
    "PassportStatus",
    "GrantStatus",
    "GrantSource",
    "DelegateKind",
    "RevocationTarget",
    "OverrideStatus",
    "HumanSovereigntyNode",
    "Machine",
    "MachinePassport",
    "MachineActionManifest",
    "RightsPolicy",
    "CapabilityToken",
    "Delegation",
    "ActionRequest",
    "RightsReceipt",
    "EmergencyOverride",
    "Revocation",
]
