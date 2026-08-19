"""
HSP tohum verisi — Van Sales'ın gerçek otomatik karar noktaları.

Buradaki üç makine uydurma değildir; keşif taramasının
(``app.compliance.scanners.discovery``) "insan onayı YOK" olarak işaretlediği
kod yollarına karşılık gelir:

    VS-CREDIT-GATE     customer_service.check_credit
                       Bir satışı otomatik reddedebilir.  Ekonomik etki;
                       kararın kendisi (DECIDE) ile sonucun uygulanması (ACT)
                       ayrı ayrı beyan edilir.

    VS-RISK-SCORER     ledger_service.risk_score + customer_service.churn_candidates
                       Müşteriyi profilleyip risk/terk skoru üretir.
                       Veriden çıkarım (KNOW) ile sınıflandırma (DECIDE) ayrı.

    VS-FLEET-TRACKER   models.route.GpsEvent (gps_events)
                       Çalışanın konumunu sürekli kaydeder.  Özerklik etkisi;
                       yalnızca KNOW alanında.

Tohumlama iki ilkeye uyar:

*   **Yinelenebilir.**  İkinci çağrı yeni satır üretmez.
*   **Sürümlenir.**  Bir beyan ya da politikanın içeriği değiştiyse satır
    güncellenmez; eski sürüm pasifleştirilip yeni sürüm yazılır.  Geçmişte
    hangi kural altında karar verildiği okunabilir kalmalıdır.

Hukuki dayanak alanları bilinçli olarak ``REVIEW_REQUIRED`` bırakılmıştır.
Bu kayıtların hangi mevzuat hükmüne dayandığına karar vermek insanın işidir;
tohum verisi bir madde numarası tahmin etmez.  Fonksiyonun dönüşündeki
``review_required`` listesi, insana bırakılan kararları açıkça sayar.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.models.hsp import (
    DEFAULT_TENANT_ID,
    LEGAL_BASIS_REVIEW,
    GrantSource,
    ImpactDimension,
    ImpactLevel,
    Machine,
    MachineActionManifest,
    MachineKind,
    PassportStatus,
    PolicyEffect,
    RightsPolicy,
    SovereigntyDomain,
    SubjectKind,
)
from app.compliance.models.hsp import (
    MachinePassport as Passport,
)
from app.compliance.services.hsp_engine import issue_capability_token
from app.core.logging_config import get_logger
from app.core.utils import dumps
from app.models.base import utcnow

log = get_logger("app.compliance.hsp.seed")

# --- Makine kodları --------------------------------------------------------
MACHINE_CREDIT_GATE = "VS-CREDIT-GATE"
MACHINE_RISK_SCORER = "VS-RISK-SCORER"
MACHINE_FLEET_TRACKER = "VS-FLEET-TRACKER"

# --- Eylem kodları ---------------------------------------------------------
ACTION_CREDIT_EVALUATE = "credit.limit.evaluate"
ACTION_CREDIT_BLOCK = "credit.limit.block_sale"
ACTION_RISK_PROFILE = "customer.risk.profile"
ACTION_CHURN_CLASSIFY = "customer.churn.classify"
ACTION_LOCATION_TRACK = "employee.location.track"

# --- Politika kodları ------------------------------------------------------
POLICY_CREDIT_EVALUATE = "HSP-CREDIT-EVAL"
POLICY_CREDIT_BLOCK = "HSP-CREDIT-BLOCK"
POLICY_RISK_PROFILE = "HSP-RISK-KNOW"
POLICY_CHURN_CLASSIFY = "HSP-CHURN-DECIDE"
POLICY_LOCATION_TRACK = "HSP-GPS-KNOW"

# --- Jeton referansları ----------------------------------------------------
TOKEN_RISK_PROFILE = "TOK-RISK-KNOW-001"
TOKEN_LOCATION_TRACK = "TOK-GPS-KNOW-001"

#: İtiraz yolu — makbuzda görünen, insanın karara itiraz edebileceği adres.
APPEAL_PATH = "/compliance/hsp/appeal"

#: Pasaport geçerlilik süresi.  Sonsuz pasaport yoktur; yenileme zorunludur.
PASSPORT_DAYS = 365
#: Yetenek jetonu süresi.  Süre dolduğunda motor EXPIRED döner ve yetki
#: yenilenene kadar eylem durur — bu bir hata değil, tasarımdır.
TOKEN_DAYS = 90


# ===========================================================================
# Yardımcılar
# ===========================================================================
def _get_machine(db: Session, tenant_id: int, code: str) -> Machine | None:
    return db.execute(
        select(Machine).where(Machine.tenant_id == tenant_id, Machine.code == code)
    ).scalar_one_or_none()


def _upsert_machine(
    db: Session, *, tenant_id: int, code: str, fields: dict[str, Any], user_id: int | None
) -> Machine:
    """
    Makineyi oluşturur ya da tanımını tazeler.

    ``status`` bilerek tazelenmez: bir operatör makineyi askıya aldıysa, tohum
    betiğinin yeniden çalışması onu sessizce geri açmamalıdır.
    """
    machine = _get_machine(db, tenant_id, code)
    if machine is None:
        machine = Machine(tenant_id=tenant_id, code=code, created_by_id=user_id, **fields)
        db.add(machine)
        db.flush()
        return machine
    for key, value in fields.items():
        setattr(machine, key, value)
    machine.updated_by_id = user_id
    db.flush()
    return machine


def _ensure_passport(
    db: Session, *, tenant_id: int, machine: Machine, domains: list[str], user_id: int | None
) -> Passport:
    """Geçerli pasaport yoksa düzenler.  Var olan süreli pasaport uzatılmaz."""
    now = utcnow()
    rows = (
        db.execute(
            select(Passport).where(
                Passport.tenant_id == tenant_id, Passport.machine_id == machine.id
            )
        )
        .scalars()
        .all()
    )
    valid = [p for p in rows if p.status == PassportStatus.VALID and not p.is_expired(now)]
    if valid:
        return max(valid, key=lambda p: (p.expires_at, p.id))

    passport = Passport(
        tenant_id=tenant_id,
        machine_id=machine.id,
        serial=f"{machine.code}-P{len(rows) + 1:03d}",
        issuer="Van Sales Compliance Layer",
        issued_at=now,
        expires_at=now + timedelta(days=PASSPORT_DAYS),
        declared_domains=dumps(domains),
        model_ref=machine.source_ref,
        status=PassportStatus.VALID,
        version=len(rows) + 1,
        supersedes_id=max((p.id for p in rows), default=None),
        created_by_id=user_id,
    )
    db.add(passport)
    db.flush()
    return passport


#: Sürüm karşılaştırmasına giren alanlar.  Bunlardan biri değişmişse beyan
#: güncellenmez, yeni sürüm yazılır.
_MANIFEST_TRACKED = (
    "domain", "title", "purpose", "impact_level", "impact_dimensions",
    "data_categories", "is_reversible", "reversal_path",
    "human_review_available", "appeal_path", "legal_basis_ref", "source_ref",
)

_POLICY_TRACKED = (
    "domain", "action_code", "subject_kind", "machine_id", "effect",
    "requires_human_approval", "requires_capability_token", "max_impact_level",
    "condition_json", "purpose", "legal_basis_ref", "appeal_path", "priority",
    "title", "description",
)


def _differs(row: Any, fields: dict[str, Any], tracked: tuple[str, ...]) -> bool:
    return any(
        key in fields and getattr(row, key) != fields[key] for key in tracked
    )


def _upsert_manifest(
    db: Session,
    *,
    tenant_id: int,
    machine: Machine,
    action_code: str,
    fields: dict[str, Any],
    user_id: int | None,
) -> MachineActionManifest:
    rows = (
        db.execute(
            select(MachineActionManifest).where(
                MachineActionManifest.tenant_id == tenant_id,
                MachineActionManifest.machine_id == machine.id,
                MachineActionManifest.action_code == action_code,
            )
        )
        .scalars()
        .all()
    )
    active = [r for r in rows if r.is_active]
    if active:
        current = max(active, key=lambda r: (r.version, r.id))
        if not _differs(current, fields, _MANIFEST_TRACKED):
            return current
        # Beyanın içeriği değişti: eskisi pasifleşir, yenisi sürüm alır.
        current.is_active = False
        supersedes = current.id
    else:
        supersedes = max((r.id for r in rows), default=None)

    manifest = MachineActionManifest(
        tenant_id=tenant_id,
        machine_id=machine.id,
        action_code=action_code,
        version=max((r.version for r in rows), default=0) + 1,
        supersedes_id=supersedes,
        is_active=True,
        created_by_id=user_id,
        **fields,
    )
    db.add(manifest)
    db.flush()
    return manifest


def _upsert_policy(
    db: Session, *, tenant_id: int, code: str, fields: dict[str, Any], user_id: int | None
) -> RightsPolicy:
    rows = (
        db.execute(
            select(RightsPolicy).where(
                RightsPolicy.tenant_id == tenant_id, RightsPolicy.code == code
            )
        )
        .scalars()
        .all()
    )
    active = [r for r in rows if r.is_active]
    if active:
        current = max(active, key=lambda r: (r.version, r.id))
        if not _differs(current, fields, _POLICY_TRACKED):
            return current
        current.is_active = False
        supersedes = current.id
    else:
        supersedes = max((r.id for r in rows), default=None)

    policy = RightsPolicy(
        tenant_id=tenant_id,
        code=code,
        version=max((r.version for r in rows), default=0) + 1,
        supersedes_id=supersedes,
        is_active=True,
        issued_by_user_id=user_id,
        created_by_id=user_id,
        **fields,
    )
    db.add(policy)
    db.flush()
    return policy


# ===========================================================================
# Tohumlama
# ===========================================================================
def seed(
    db: Session,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    issued_by_user_id: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Van Sales'ın üç otomatik karar noktası için HSP kaydını kurar.

    Dönüş: makine/beyan/politika/jeton kodlarından kimliklere eşlemeler ve
    ``review_required`` — insanın karara bağlaması gereken noktalar.
    """
    machines: dict[str, int] = {}
    manifests: dict[str, int] = {}
    policies: dict[str, int] = {}
    tokens: dict[str, int] = {}

    # -----------------------------------------------------------------------
    # 1) Kredi limiti kapısı — ekonomik karar, DECIDE + ACT
    # -----------------------------------------------------------------------
    credit = _upsert_machine(
        db,
        tenant_id=tenant_id,
        code=MACHINE_CREDIT_GATE,
        user_id=issued_by_user_id,
        fields={
            "name": "Kredi limiti kapısı",
            "kind": MachineKind.RULE_ENGINE,
            "description": (
                "Müşterinin bakiyesi + yeni belge tutarı kredi limitini aşarsa "
                "satışı reddeder. Kural tabanlıdır, model değildir."
            ),
            "operator_ref": "Finans / Muhasebe",
            "source_ref": "backend/app/services/customer_service.py::check_credit",
            "is_autonomous": True,
        },
    )
    machines[MACHINE_CREDIT_GATE] = credit.id
    _ensure_passport(
        db,
        tenant_id=tenant_id,
        machine=credit,
        domains=[SovereigntyDomain.DECIDE, SovereigntyDomain.ACT],
        user_id=issued_by_user_id,
    )

    manifests[ACTION_CREDIT_EVALUATE] = _upsert_manifest(
        db,
        tenant_id=tenant_id,
        machine=credit,
        action_code=ACTION_CREDIT_EVALUATE,
        user_id=issued_by_user_id,
        fields={
            "domain": SovereigntyDomain.DECIDE,
            "title": "Kredi limiti değerlendirmesi",
            "purpose": (
                "Yeni borcun kayıtlı kredi limitini aşıp aşmadığına karar vermek."
            ),
            "impact_level": ImpactLevel.HIGH,
            "impact_dimensions": dumps([ImpactDimension.ECONOMIC]),
            "data_categories": dumps(["KISISEL"]),
            "is_reversible": True,
            "reversal_path": "Limit güncellemesi sonrası yeniden değerlendirme",
            "human_review_available": True,
            "appeal_path": APPEAL_PATH,
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "source_ref": "backend/app/services/customer_service.py::check_credit",
        },
    ).id

    manifests[ACTION_CREDIT_BLOCK] = _upsert_manifest(
        db,
        tenant_id=tenant_id,
        machine=credit,
        action_code=ACTION_CREDIT_BLOCK,
        user_id=issued_by_user_id,
        fields={
            "domain": SovereigntyDomain.ACT,
            "title": "Satışın otomatik reddi",
            "purpose": (
                "Limit aşımı veya bloke müşteri durumunda belgenin "
                "oluşturulmasını engellemek."
            ),
            "impact_level": ImpactLevel.HIGH,
            "impact_dimensions": dumps(
                [ImpactDimension.ECONOMIC, ImpactDimension.AUTONOMY]
            ),
            "data_categories": dumps(["KISISEL"]),
            # Ret geri alınabilir: yetkili limiti yükseltip işlemi tekrarlatabilir.
            "is_reversible": True,
            "reversal_path": "Yetkili onayıyla limit artırımı veya manuel belge",
            "human_review_available": True,
            "appeal_path": APPEAL_PATH,
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "source_ref": "backend/app/services/customer_service.py::check_credit",
        },
    ).id

    policies[POLICY_CREDIT_EVALUATE] = _upsert_policy(
        db,
        tenant_id=tenant_id,
        code=POLICY_CREDIT_EVALUATE,
        user_id=issued_by_user_id,
        fields={
            "title": "Kredi limiti değerlendirmesine izin",
            "description": (
                "Değerlendirmenin kendisi kural tabanlı, açıklanabilir ve "
                "müşteriye önceden bildirilmiş bir limite dayanır; bu yüzden "
                "her hesaplama için insan onayı beklenmez. Sonucun UYGULANMASI "
                "ayrı bir politikaya tabidir."
            ),
            "domain": SovereigntyDomain.DECIDE,
            "action_code": ACTION_CREDIT_EVALUATE,
            "subject_kind": SubjectKind.CUSTOMER,
            "machine_id": credit.id,
            "effect": PolicyEffect.ALLOW,
            "requires_human_approval": False,
            "requires_capability_token": False,
            "max_impact_level": ImpactLevel.HIGH,
            "purpose": "Tahsilat riskinin sınırlanması",
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "appeal_path": APPEAL_PATH,
            "priority": 200,
            "condition_json": None,
        },
    ).id

    policies[POLICY_CREDIT_BLOCK] = _upsert_policy(
        db,
        tenant_id=tenant_id,
        code=POLICY_CREDIT_BLOCK,
        user_id=issued_by_user_id,
        fields={
            "title": "Satışın otomatik reddi — insan onayı gerekir",
            "description": (
                "Bir insanın alışverişini makinenin tek başına durdurması "
                "ekonomik bir eylemdir. İzin verilir, ancak insan onayı ya da "
                "kayıtlı bir olağanüstü hâl kararı olmadan uygulanmaz."
            ),
            "domain": SovereigntyDomain.ACT,
            "action_code": ACTION_CREDIT_BLOCK,
            "subject_kind": SubjectKind.CUSTOMER,
            "machine_id": credit.id,
            "effect": PolicyEffect.ALLOW,
            "requires_human_approval": True,
            "requires_capability_token": False,
            "max_impact_level": ImpactLevel.HIGH,
            "purpose": "Tahsilat riskinin sınırlanması",
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "appeal_path": APPEAL_PATH,
            "priority": 200,
            "condition_json": None,
        },
    ).id

    # -----------------------------------------------------------------------
    # 2) Risk / terk skorlaması — profilleme, KNOW + DECIDE
    # -----------------------------------------------------------------------
    scorer = _upsert_machine(
        db,
        tenant_id=tenant_id,
        code=MACHINE_RISK_SCORER,
        user_id=issued_by_user_id,
        fields={
            "name": "Müşteri risk ve terk skorlayıcısı",
            "kind": MachineKind.STATISTICAL_MODEL,
            "description": (
                "Ödeme geçmişi, gecikme ve sipariş aralığından risk/terk skoru "
                "üretir; müşteriyi sınıflandırır."
            ),
            "operator_ref": "Satış / Finans",
            "source_ref": "backend/app/services/ledger_service.py::risk_score",
            "is_autonomous": True,
        },
    )
    machines[MACHINE_RISK_SCORER] = scorer.id
    _ensure_passport(
        db,
        tenant_id=tenant_id,
        machine=scorer,
        domains=[SovereigntyDomain.KNOW, SovereigntyDomain.DECIDE],
        user_id=issued_by_user_id,
    )

    manifests[ACTION_RISK_PROFILE] = _upsert_manifest(
        db,
        tenant_id=tenant_id,
        machine=scorer,
        action_code=ACTION_RISK_PROFILE,
        user_id=issued_by_user_id,
        fields={
            "domain": SovereigntyDomain.KNOW,
            "title": "Müşteri risk profili çıkarımı",
            "purpose": (
                "Ödeme davranışından tahsilat riski göstergesi türetmek."
            ),
            "impact_level": ImpactLevel.MEDIUM,
            "impact_dimensions": dumps(
                [ImpactDimension.ECONOMIC, ImpactDimension.REPUTATION]
            ),
            "data_categories": dumps(["KISISEL"]),
            "is_reversible": True,
            "reversal_path": "Skorun yeniden hesaplanması / silinmesi",
            "human_review_available": True,
            "appeal_path": APPEAL_PATH,
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "source_ref": "backend/app/services/ledger_service.py::risk_score",
        },
    ).id

    manifests[ACTION_CHURN_CLASSIFY] = _upsert_manifest(
        db,
        tenant_id=tenant_id,
        machine=scorer,
        action_code=ACTION_CHURN_CLASSIFY,
        user_id=issued_by_user_id,
        fields={
            "domain": SovereigntyDomain.DECIDE,
            "title": "Terk adayı sınıflandırması",
            "purpose": (
                "Sipariş aralığına göre müşteriyi 'terk adayı' olarak "
                "işaretlemek ve iş listelerine düşürmek."
            ),
            "impact_level": ImpactLevel.MEDIUM,
            "impact_dimensions": dumps(
                [ImpactDimension.ECONOMIC, ImpactDimension.REPUTATION]
            ),
            "data_categories": dumps(["KISISEL"]),
            "is_reversible": True,
            "reversal_path": "Sınıflandırmanın kaldırılması",
            "human_review_available": True,
            "appeal_path": APPEAL_PATH,
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "source_ref": "backend/app/services/customer_service.py::churn_candidates",
        },
    ).id

    policy_risk = _upsert_policy(
        db,
        tenant_id=tenant_id,
        code=POLICY_RISK_PROFILE,
        user_id=issued_by_user_id,
        fields={
            "title": "Risk profili çıkarımına süreli izin",
            "description": (
                "Profilleme süresiz bir yetki olamaz: izin süreli bir yetenek "
                "jetonuna bağlanır, jeton dolduğunda çıkarım durur ve yetkinin "
                "yenilenmesi gerekir."
            ),
            "domain": SovereigntyDomain.KNOW,
            "action_code": ACTION_RISK_PROFILE,
            "subject_kind": SubjectKind.CUSTOMER,
            "machine_id": scorer.id,
            "effect": PolicyEffect.ALLOW,
            "requires_human_approval": False,
            "requires_capability_token": True,
            "max_impact_level": ImpactLevel.MEDIUM,
            "purpose": "Tahsilat riskinin öngörülmesi",
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "appeal_path": APPEAL_PATH,
            "priority": 200,
            "condition_json": None,
        },
    )
    policies[POLICY_RISK_PROFILE] = policy_risk.id

    policies[POLICY_CHURN_CLASSIFY] = _upsert_policy(
        db,
        tenant_id=tenant_id,
        code=POLICY_CHURN_CLASSIFY,
        user_id=issued_by_user_id,
        fields={
            "title": "Terk sınıflandırması — insan incelemesi gerekir",
            "description": (
                "Bir insanı 'kaybedilmiş müşteri' diye etiketlemek, ona nasıl "
                "davranılacağını değiştirir. Etiket üretilebilir; ancak bir "
                "insan görmeden iş akışına dönüşmez."
            ),
            "domain": SovereigntyDomain.DECIDE,
            "action_code": ACTION_CHURN_CLASSIFY,
            "subject_kind": SubjectKind.CUSTOMER,
            "machine_id": scorer.id,
            "effect": PolicyEffect.ALLOW,
            "requires_human_approval": True,
            "requires_capability_token": False,
            "max_impact_level": ImpactLevel.MEDIUM,
            "purpose": "Müşteri ilişkisinin sürdürülmesi",
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "appeal_path": APPEAL_PATH,
            "priority": 200,
            "condition_json": None,
        },
    ).id

    tokens[TOKEN_RISK_PROFILE] = issue_capability_token(
        db,
        tenant_id=tenant_id,
        token_ref=TOKEN_RISK_PROFILE,
        machine_id=scorer.id,
        domain=SovereigntyDomain.KNOW,
        action_code=ACTION_RISK_PROFILE,
        granted_by=GrantSource.CONTROLLER,
        granted_by_user_id=issued_by_user_id,
        valid_days=TOKEN_DAYS,
        policy_id=policy_risk.id,
    ).id

    # -----------------------------------------------------------------------
    # 3) Çalışan konum takibi — özerklik, yalnızca KNOW
    # -----------------------------------------------------------------------
    tracker = _upsert_machine(
        db,
        tenant_id=tenant_id,
        code=MACHINE_FLEET_TRACKER,
        user_id=issued_by_user_id,
        fields={
            "name": "Saha konum takibi",
            "kind": MachineKind.TRACKER,
            "description": (
                "Araç ve plasiyerin konumunu düzenli aralıkla kaydeder "
                "(gps_events)."
            ),
            "operator_ref": "Saha Operasyon",
            "source_ref": "backend/app/models/route.py::GpsEvent",
            "is_autonomous": True,
        },
    )
    machines[MACHINE_FLEET_TRACKER] = tracker.id
    _ensure_passport(
        db,
        tenant_id=tenant_id,
        machine=tracker,
        domains=[SovereigntyDomain.KNOW],
        user_id=issued_by_user_id,
    )

    manifests[ACTION_LOCATION_TRACK] = _upsert_manifest(
        db,
        tenant_id=tenant_id,
        machine=tracker,
        action_code=ACTION_LOCATION_TRACK,
        user_id=issued_by_user_id,
        fields={
            "domain": SovereigntyDomain.KNOW,
            "title": "Çalışan konumunun kaydedilmesi",
            "purpose": (
                "Rota uyumu, ziyaret doğrulaması ve araç güvenliği."
            ),
            "impact_level": ImpactLevel.HIGH,
            "impact_dimensions": dumps(
                [ImpactDimension.AUTONOMY, ImpactDimension.DIGITAL]
            ),
            "data_categories": dumps(["KONUM", "KISISEL"]),
            # Gözlenmiş konum geri alınamaz: kayıt silinse de bilgi öğrenilmiştir.
            "is_reversible": False,
            "reversal_path": None,
            "human_review_available": True,
            "appeal_path": APPEAL_PATH,
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "source_ref": "backend/app/models/route.py::GpsEvent",
        },
    ).id

    policy_gps = _upsert_policy(
        db,
        tenant_id=tenant_id,
        code=POLICY_LOCATION_TRACK,
        user_id=issued_by_user_id,
        fields={
            "title": "Konum takibi — yalnızca açık mesai oturumunda",
            "description": (
                "Takip mesai oturumuna bağlanır: çağıran taraf "
                "'day_session_active' olgusunu bağlamda sağlamak zorundadır. "
                "Olgu sağlanmazsa koşul sağlanmamış sayılır ve sonuç REDDİR — "
                "böylece mesai dışı takip, unutulmuş bir bayrak yüzünden "
                "sessizce mümkün olmaz."
            ),
            "domain": SovereigntyDomain.KNOW,
            "action_code": ACTION_LOCATION_TRACK,
            "subject_kind": SubjectKind.EMPLOYEE,
            "machine_id": tracker.id,
            "effect": PolicyEffect.ALLOW,
            "requires_human_approval": False,
            "requires_capability_token": True,
            "max_impact_level": ImpactLevel.HIGH,
            "condition_json": dumps({"day_session_active": True}),
            "purpose": "Rota uyumu ve saha güvenliği",
            "legal_basis_ref": LEGAL_BASIS_REVIEW,
            "appeal_path": APPEAL_PATH,
            "priority": 200,
        },
    )
    policies[POLICY_LOCATION_TRACK] = policy_gps.id

    tokens[TOKEN_LOCATION_TRACK] = issue_capability_token(
        db,
        tenant_id=tenant_id,
        token_ref=TOKEN_LOCATION_TRACK,
        machine_id=tracker.id,
        domain=SovereigntyDomain.KNOW,
        action_code=ACTION_LOCATION_TRACK,
        granted_by=GrantSource.CONTROLLER,
        granted_by_user_id=issued_by_user_id,
        valid_days=TOKEN_DAYS,
        policy_id=policy_gps.id,
    ).id

    if commit:
        db.commit()

    result: dict[str, Any] = {
        "tenant_id": tenant_id,
        "machines": machines,
        "manifests": manifests,
        "policies": policies,
        "capability_tokens": tokens,
        "review_required": REVIEW_NOTES,
    }
    log.info(
        "HSP seed: %d machine, %d manifest, %d policy, %d token",
        len(machines), len(manifests), len(policies), len(tokens),
    )
    return result


#: Tohum verisinin insana bıraktığı kararlar.  Bunlar "eksik" değil, bilinçli
#: olarak makineye bırakılmayan sorulardır.
REVIEW_NOTES: tuple[str, ...] = (
    "Beş beyanın da hukuki dayanağı REVIEW_REQUIRED; dayanak türü ve varsa "
    "madde referansı insan tarafından belirlenmelidir.",
    "Konum takibinin ölçülülüğü (ping sıklığı, saklama süresi, mesai dışı "
    "kapsam) değerlendirilmemiştir; politika yalnızca mesai oturumu koşulunu "
    "getirir.",
    "Risk ve terk skorlaması için verilen yetenek jetonu veri sorumlusu "
    "tarafından verilmiş sayılmıştır; ilgili kişinin rızasına dayanıp "
    "dayanmadığı belirlenmemiştir.",
    "Kredi reddine itiraz yolunun operasyonel karşılığı (kim, hangi sürede "
    "cevaplar) tanımlanmamıştır.",
    "Çalışan konum takibinde aydınlatma metninin varlığı doğrulanmamıştır.",
)


def main() -> None:  # pragma: no cover - elle çalıştırma yolu
    """``python -m app.compliance.services.hsp_seed``"""
    from app.core.db import session_scope

    with session_scope() as db:
        summary = seed(db, commit=False)
    print(dumps(summary, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "seed",
    "REVIEW_NOTES",
    "APPEAL_PATH",
    "MACHINE_CREDIT_GATE",
    "MACHINE_RISK_SCORER",
    "MACHINE_FLEET_TRACKER",
    "ACTION_CREDIT_EVALUATE",
    "ACTION_CREDIT_BLOCK",
    "ACTION_RISK_PROFILE",
    "ACTION_CHURN_CLASSIFY",
    "ACTION_LOCATION_TRACK",
    "POLICY_CREDIT_EVALUATE",
    "POLICY_CREDIT_BLOCK",
    "POLICY_RISK_PROFILE",
    "POLICY_CHURN_CLASSIFY",
    "POLICY_LOCATION_TRACK",
    "TOKEN_RISK_PROFILE",
    "TOKEN_LOCATION_TRACK",
]
