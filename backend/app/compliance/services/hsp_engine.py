"""
Human Sovereignty Protocol — yetki değerlendirme motoru.

Motorun tek işi şu soruyu **eylemden önce** cevaplamaktır:

    "Bu makine bu insana bu alanda ne yapmaya yetkilidir?"

Cevap bir karar (:class:`Decision`) ve bir makbuzdur (``cmp_hsp_receipt``).
Karar tek bir sayı değildir: bir *verdict* ve gerekçe listesidir.  İnsan
haklarını 0-100 arası bir "uyum skoruna" indirgemek, kararın nedenini
gizlediği için burada bilinçli olarak yapılmaz.

Değerlendirme sırası — sıralama tesadüfi değildir, her adım bir öncekinin
tartışmasını kapatır:

    1. makine tanınıyor mu, çalışır durumda mı
    2. eylem beyan edilmiş mi (manifest)
    3. pasaport geçerli mi, alan beyan edilmiş mi
    4. geri alma var mı            -> REVOKED (politikaya bakılmadan)
    5. insan düğümü askıya alınmış mı
    6. politika izin veriyor mu    -> yoksa DENY (varsayılan ret)
    7. politika koşulları bağlamda sağlanıyor mu
    8. devir gerekiyorsa geçerli mi
    9. yetenek jetonu gerekiyorsa geçerli mi -> süresi dolmuşsa EXPIRED
   10. insan onayı gerekiyor mu    -> REQUIRE_HUMAN_APPROVAL

Fail-closed ilkeleri:

*   Beklenmeyen bir hata izin üretmez; ``DENY`` üretir.
*   Makbuz yazılamıyorsa karar ``ALLOW`` olamaz.  Kaydedilemeyen bir yetki
    kullanılmış sayılamaz; "kanıtı yok ama izin verdim" HSP'de geçersizdir.
*   ``UNKNOWN`` hiçbir yerde ``ALLOW``a çevrilmez.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.compliance.models.hsp import (
    DEFAULT_TENANT_ID,
    IMPACT_ORDER,
    WILDCARD,
    ActionRequest,
    CapabilityToken,
    Delegation,
    EmergencyOverride,
    GrantSource,
    GrantStatus,
    HspVerdict,
    HumanSovereigntyNode,
    ImpactLevel,
    Machine,
    MachineActionManifest,
    MachinePassport,
    MachineStatus,
    NodeStatus,
    PassportStatus,
    PolicyEffect,
    Revocation,
    RevocationTarget,
    RightsPolicy,
    RightsReceipt,
    SubjectKind,
)
from app.core.exceptions import PermissionDeniedError
from app.core.logging_config import get_logger
from app.core.utils import D, dumps
from app.models.base import utcnow

log = get_logger("app.compliance.hsp")

#: Politika bir itiraz yolu tanımlamamışsa makbuza yazılan son çare.
#: Boş bir itiraz yolu, itiraz hakkının olmaması demektir; o yüzden asla
#: boş bırakılmaz.
DEFAULT_APPEAL_PATH = "/compliance/hsp/appeal"

#: Bağlam sözlüğünde saklanmayacak anahtarlar — makbuz bir kimlik bilgisi
#: deposu hâline gelmemeli.
_SENSITIVE_CONTEXT_KEYS = {
    "password", "password_hash", "token", "access_token", "refresh_token",
    "api_key", "apikey", "secret", "authorization", "client_secret",
    "private_key",
}
_CONTEXT_LIMIT = 4000


# ---------------------------------------------------------------------------
# Gerekçe kodları — makine tarafından okunabilir, sabit, i18n anahtarı biçimli
# ---------------------------------------------------------------------------
R_MACHINE_UNKNOWN = "hsp.deny.machine_unknown"
R_MACHINE_INACTIVE = "hsp.deny.machine_not_operational"
R_TENANT_MISMATCH = "hsp.deny.tenant_mismatch"
R_MANIFEST_UNDECLARED = "hsp.deny.action_not_declared"
R_MANIFEST_INACTIVE = "hsp.deny.manifest_inactive"
R_MANIFEST_FOREIGN = "hsp.deny.manifest_belongs_to_another_machine"
R_PASSPORT_MISSING = "hsp.deny.passport_missing"
R_PASSPORT_SUSPENDED = "hsp.deny.passport_suspended"
R_PASSPORT_EXPIRED = "hsp.expired.passport"
R_PASSPORT_REVOKED = "hsp.revoked.passport"
R_DOMAIN_NOT_DECLARED = "hsp.deny.domain_not_declared"
R_REVOKED = "hsp.revoked.grant"
R_NODE_SUSPENDED = "hsp.deny.subject_suspended"
R_NODE_UNREGISTERED = "hsp.note.subject_not_registered"
R_NO_POLICY = "hsp.deny.no_policy"
R_POLICY_APPLIED = "hsp.policy.applied"
R_POLICY_DENY = "hsp.deny.policy_effect_deny"
R_IMPACT_EXCEEDS = "hsp.deny.impact_exceeds_policy"
R_CONDITION_UNMET = "hsp.deny.condition_unmet"
R_CONDITION_MISSING = "hsp.deny.condition_absent_from_context"
R_TOKEN_MISSING = "hsp.deny.capability_token_missing"
R_TOKEN_EXPIRED = "hsp.expired.capability_token"
R_TOKEN_EXHAUSTED = "hsp.expired.capability_token_exhausted"
R_TOKEN_REVOKED = "hsp.revoked.capability_token"
R_TOKEN_APPLIED = "hsp.grant.capability_token"
R_AMOUNT_OVER_LIMIT = "hsp.review.amount_exceeds_token_limit"
R_DELEGATION_MISSING = "hsp.deny.delegation_missing"
R_DELEGATION_EXPIRED = "hsp.expired.delegation"
R_DELEGATION_REVOKED = "hsp.revoked.delegation"
R_DELEGATION_SCOPE = "hsp.deny.delegation_out_of_scope"
R_HUMAN_REQUIRED = "hsp.review.human_approval_required"
R_HUMAN_REFUSED = "hsp.deny.human_refused"
R_HUMAN_GRANTED = "hsp.grant.human_approval"
R_HUMAN_UNATTRIBUTED = "hsp.review.approval_not_attributable"
R_SEVERE_IMPACT = "hsp.review.severe_impact_not_waivable"
R_OVERRIDE_USED = "hsp.override.used"
R_ALLOWED = "hsp.allow.policy_permits"
R_INTERNAL_ERROR = "hsp.deny.internal_error"
R_RECEIPT_FAILED = "hsp.deny.receipt_not_written"


# ---------------------------------------------------------------------------
# Karar
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    """
    Motorun cevabı.

    ``allow`` yalnızca ``verdict == ALLOW`` iken doğrudur; çağıran tarafın
    ``verdict``i yorumlamak zorunda kalmaması için ayrıca taşınır.
    ``reasons`` her zaman doludur — gerekçesiz karar verilmez.
    """

    allow: bool
    verdict: str
    reasons: list[str]
    receipt_id: int | None = None
    appeal_path: str | None = None
    policy_code: str | None = None
    request_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "receipt_id": self.receipt_id,
            "appeal_path": self.appeal_path,
            "policy_code": self.policy_code,
            "request_id": self.request_id,
        }


@dataclass
class _State:
    """Değerlendirme boyunca biriken bağlam; makbuz bundan yazılır."""

    tenant_id: int
    subject_ref: str
    now: datetime
    machine: Machine | None = None
    machine_id: int | None = None
    manifest: MachineActionManifest | None = None
    action_code: str = "UNKNOWN"
    domain: str = "UNKNOWN"
    policy: RightsPolicy | None = None
    token: CapabilityToken | None = None
    override: EmergencyOverride | None = None
    node: HumanSovereigntyNode | None = None
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    human_review_required: bool = False

    def add(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def machine_code(self) -> str | None:
        return self.machine.code if self.machine is not None else None

    def appeal_path(self) -> str:
        for candidate in (
            self.policy.appeal_path if self.policy is not None else None,
            self.manifest.appeal_path if self.manifest is not None else None,
            self.node.appeal_path if self.node is not None else None,
        ):
            if candidate:
                return candidate
        return DEFAULT_APPEAL_PATH


# ---------------------------------------------------------------------------
# Genel giriş noktası
# ---------------------------------------------------------------------------
def evaluate(
    db: Session,
    *,
    subject_ref: str,
    machine_id: int,
    manifest: MachineActionManifest | str | int,
    context: dict[str, Any] | None = None,
    commit: bool = False,
) -> Decision:
    """
    Bir eylemin yapılabilirliğini değerlendirir ve makbuz üretir.

    ``manifest`` bir ORM nesnesi, birincil anahtar ya da eylem kodu olabilir;
    kod verildiğinde makinenin en yüksek sürümlü etkin beyanı seçilir.

    ``context`` çağrı anına ait olgular: ``tenant_id``, ``amount``,
    ``human_approval``, ``delegated_by``, politika koşullarının değerleri.
    Bilinmeyen bir olgu sağlanmamış sayılır; sağlanmamış olgu izin üretmez.

    Çağıranın işlemini **commit etmez** (``commit=False``); karar ile eylem
    aynı işlemde ya birlikte kalıcı olur ya da birlikte geri alınır.
    """
    ctx = dict(context or {})
    state = _State(
        tenant_id=int(ctx.get("tenant_id") or DEFAULT_TENANT_ID),
        subject_ref=str(subject_ref or "").strip() or "UNKNOWN",
        now=utcnow(),
        machine_id=machine_id,
    )
    try:
        return _evaluate(db, state, manifest, ctx, commit)
    except Exception as exc:  # pragma: no cover - savunma amaçlı
        # Motorun kendi hatası, makinenin lehine yorumlanmaz.
        log.exception("HSP evaluation failed: %s", exc)
        state.add(R_INTERNAL_ERROR)
        return _fail_closed(db, state, ctx)


def enforce(
    db: Session,
    *,
    subject_ref: str,
    machine_id: int,
    manifest: MachineActionManifest | str | int,
    context: dict[str, Any] | None = None,
    commit: bool = False,
) -> Decision:
    """
    :func:`evaluate` ile aynı, ama izin verilmediğinde exception fırlatır.

    Çağıran tarafın dönüş değerini kontrol etmeyi unutup sessizce devam
    etmesini imkânsız kılar — HSP'de en tehlikeli hata, kararı okumamaktır.
    """
    decision = evaluate(
        db,
        subject_ref=subject_ref,
        machine_id=machine_id,
        manifest=manifest,
        context=context,
        commit=commit,
    )
    if not decision.allow:
        raise PermissionDeniedError(
            "compliance.hsp.denied",
            params={
                "verdict": decision.verdict,
                "reasons": ",".join(decision.reasons),
                "appeal_path": decision.appeal_path or DEFAULT_APPEAL_PATH,
                "receipt_id": decision.receipt_id,
            },
            detail=f"HSP {decision.verdict}: {'; '.join(decision.reasons)}",
        )
    return decision


# ---------------------------------------------------------------------------
# Değerlendirme adımları
# ---------------------------------------------------------------------------
def _evaluate(
    db: Session,
    st: _State,
    manifest: MachineActionManifest | str | int,
    ctx: dict[str, Any],
    commit: bool,
) -> Decision:
    # --- 1) Makine ---------------------------------------------------------
    machine = db.get(Machine, st.machine_id) if st.machine_id else None
    if machine is None:
        st.add(R_MACHINE_UNKNOWN)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    st.machine = machine
    if machine.tenant_id != st.tenant_id:
        # Kiracı sınırı geçilemez: A kiracısının makinesi B'nin insanına
        # dokunamaz, politika ne derse desin.
        st.add(R_TENANT_MISMATCH)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    if not machine.is_operational():
        st.add(R_MACHINE_INACTIVE)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)

    # --- 2) Eylem beyanı ---------------------------------------------------
    man = _resolve_manifest(db, machine, manifest)
    if man is None:
        st.add(R_MANIFEST_UNDECLARED)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    st.manifest = man
    st.action_code = man.action_code
    st.domain = man.domain
    if man.machine_id != machine.id:
        st.add(R_MANIFEST_FOREIGN)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    if not man.is_active:
        st.add(R_MANIFEST_INACTIVE)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    st.evidence["impact_level"] = man.impact_level
    st.evidence["impact_dimensions"] = man.dimensions()

    # --- 3) Pasaport -------------------------------------------------------
    verdict = _check_passport(db, st)
    if verdict is not None:
        return _finalize(db, st, verdict, ctx, commit)

    # --- 4) Geri alma ------------------------------------------------------
    revocation = _matching_revocation(db, st)
    if revocation is not None:
        st.add(f"{R_REVOKED}:{revocation.target_kind}")
        st.evidence["revocation_id"] = revocation.id
        return _finalize(db, st, HspVerdict.REVOKED, ctx, commit)

    # --- 5) İnsan düğümü ---------------------------------------------------
    st.node = _find_node(db, st)
    if st.node is not None and st.node.status != NodeStatus.ACTIVE:
        st.add(R_NODE_SUSPENDED)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    if st.node is None:
        # Kayıtsız özne otomatik ret değildir — kaydı tutmak sorumlunun
        # ödevidir, öznenin değil.  Ama makbuza yazılır ki görünsün.
        st.add(R_NODE_UNREGISTERED)

    # --- 6) Politika -------------------------------------------------------
    subject_kind = (
        st.node.subject_kind
        if st.node is not None
        else str(ctx.get("subject_kind") or SubjectKind.UNKNOWN)
    )
    policy = _select_policy(db, st, subject_kind)
    if policy is None:
        st.add(R_NO_POLICY)  # varsayılan ret
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    st.policy = policy
    st.add(f"{R_POLICY_APPLIED}:{policy.label()}")
    if policy.effect != PolicyEffect.ALLOW:
        st.add(R_POLICY_DENY)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)
    if _impact_rank(man.impact_level) > _impact_rank(policy.max_impact_level):
        # Düşük etki için yazılmış bir izin, ağırlaşmış bir eylemi kapsayamaz.
        st.add(f"{R_IMPACT_EXCEEDS}:{man.impact_level}>{policy.max_impact_level}")
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)

    # --- 7) Politika koşulları --------------------------------------------
    unmet = _unmet_conditions(policy, ctx)
    if unmet:
        for reason in unmet:
            st.add(reason)
        return _finalize(db, st, HspVerdict.DENY, ctx, commit)

    # --- 8) Devir ----------------------------------------------------------
    verdict = _check_delegation(db, st, ctx)
    if verdict is not None:
        return _finalize(db, st, verdict, ctx, commit)

    # --- 9) Yetenek jetonu -------------------------------------------------
    verdict = _check_token(db, st, policy)
    if verdict is not None:
        return _finalize(db, st, verdict, ctx, commit)

    # --- 9b) Ekonomik üst sınır -------------------------------------------
    if _amount_over_limit(st, ctx):
        st.add(R_AMOUNT_OVER_LIMIT)
        st.human_review_required = True
        return _finalize(db, st, HspVerdict.REQUIRE_HUMAN_APPROVAL, ctx, commit)

    # --- 10) İnsan onayı ---------------------------------------------------
    verdict = _check_human_approval(db, st, policy, man, ctx)
    if verdict is not None:
        return _finalize(db, st, verdict, ctx, commit)

    st.add(R_ALLOWED)
    return _finalize(db, st, HspVerdict.ALLOW, ctx, commit)


def _resolve_manifest(
    db: Session, machine: Machine, manifest: MachineActionManifest | str | int
) -> MachineActionManifest | None:
    if isinstance(manifest, MachineActionManifest):
        return manifest
    if isinstance(manifest, int):
        return db.get(MachineActionManifest, manifest)
    code = str(manifest or "").strip()
    if not code:
        return None
    rows = (
        db.execute(
            select(MachineActionManifest).where(
                MachineActionManifest.tenant_id == machine.tenant_id,
                MachineActionManifest.machine_id == machine.id,
                MachineActionManifest.action_code == code,
                MachineActionManifest.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    # En yüksek sürüm geçerlidir; beyanlar güncellenmez, sürümlenir.
    return max(rows, key=lambda m: (m.version, m.id))


def _check_passport(db: Session, st: _State) -> str | None:
    """Pasaportsuz makine hiçbir alanda işlem yapamaz."""
    assert st.machine is not None
    rows = (
        db.execute(
            select(MachinePassport).where(
                MachinePassport.machine_id == st.machine.id,
                MachinePassport.tenant_id == st.tenant_id,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        st.add(R_PASSPORT_MISSING)
        return HspVerdict.DENY

    valid = [p for p in rows if p.status == PassportStatus.VALID and not p.is_expired(st.now)]
    if valid:
        passport = max(valid, key=lambda p: (p.expires_at, p.id))
        st.evidence["passport_serial"] = passport.serial
        st.evidence["passport_expires_at"] = passport.expires_at.isoformat()
        if st.domain not in passport.domains():
            # Beyan edilmemiş alan, izin verilmemiş alandır.
            st.add(f"{R_DOMAIN_NOT_DECLARED}:{st.domain}")
            return HspVerdict.DENY
        return None

    # Geçerli pasaport yok — nedenini ayırt et; hepsi sessizce "yok" değildir.
    if any(p.status == PassportStatus.REVOKED for p in rows):
        st.add(R_PASSPORT_REVOKED)
        return HspVerdict.REVOKED
    if any(p.status == PassportStatus.VALID and p.is_expired(st.now) for p in rows):
        st.add(R_PASSPORT_EXPIRED)
        return HspVerdict.EXPIRED
    if any(p.status == PassportStatus.EXPIRED for p in rows):
        st.add(R_PASSPORT_EXPIRED)
        return HspVerdict.EXPIRED
    st.add(R_PASSPORT_SUSPENDED)
    return HspVerdict.DENY


def _matching_revocation(db: Session, st: _State) -> Revocation | None:
    """
    Geri alma politikadan önce sorulur.

    Bir insan "artık hayır" dediğinde bunun bir politikayla tartışılması
    gerekmez.  Jeton/devir/politika/pasaport hedefli geri almalar, hedefin
    kendi durumuna da yansıdığı için burada değil ilgili adımda görülür
    (bkz. :func:`revoke`).
    """
    machine_id = st.machine.id if st.machine is not None else None
    rows = (
        db.execute(
            select(Revocation).where(
                Revocation.tenant_id == st.tenant_id,
                Revocation.is_active.is_(True),
                Revocation.effective_from <= st.now,
                Revocation.target_kind.in_(
                    [
                        RevocationTarget.MACHINE,
                        RevocationTarget.SUBJECT_ALL,
                        RevocationTarget.ACTION,
                    ]
                ),
                or_(Revocation.machine_id.is_(None), Revocation.machine_id == machine_id),
                or_(
                    Revocation.subject_ref.is_(None),
                    Revocation.subject_ref == st.subject_ref,
                ),
                or_(Revocation.domain.is_(None), Revocation.domain == st.domain),
                or_(
                    Revocation.action_code.is_(None),
                    Revocation.action_code == st.action_code,
                ),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.target_kind == RevocationTarget.MACHINE:
            if row.machine_id == machine_id or row.target_id == machine_id:
                return row
        elif row.target_kind == RevocationTarget.SUBJECT_ALL:
            # Kapsamsız bir "hepsi" geri alması her özneyi etkilemez;
            # hedef özne açıkça yazılmış olmalıdır.
            if row.subject_ref == st.subject_ref:
                return row
        elif row.target_kind == RevocationTarget.ACTION:
            if row.action_code == st.action_code:
                return row
    return None


def _find_node(db: Session, st: _State) -> HumanSovereigntyNode | None:
    return db.execute(
        select(HumanSovereigntyNode).where(
            HumanSovereigntyNode.tenant_id == st.tenant_id,
            HumanSovereigntyNode.subject_ref == st.subject_ref,
        )
    ).scalar_one_or_none()


def _select_policy(db: Session, st: _State, subject_kind: str) -> RightsPolicy | None:
    """
    En özgül etkin politikayı seçer.

    Eşit özgüllükte birden fazla politika varsa DENY olan kazanır: çelişkili
    kural yazılmışsa güvenli taraf reddir.
    """
    machine_id = st.machine.id if st.machine is not None else None
    rows = (
        db.execute(
            select(RightsPolicy).where(
                RightsPolicy.tenant_id == st.tenant_id,
                RightsPolicy.is_active.is_(True),
                RightsPolicy.domain == st.domain,
                RightsPolicy.action_code.in_([st.action_code, WILDCARD]),
                RightsPolicy.subject_kind.in_([subject_kind, WILDCARD]),
                or_(RightsPolicy.machine_id.is_(None), RightsPolicy.machine_id == machine_id),
                or_(
                    RightsPolicy.effective_from.is_(None),
                    RightsPolicy.effective_from <= st.now,
                ),
                or_(
                    RightsPolicy.effective_until.is_(None),
                    RightsPolicy.effective_until > st.now,
                ),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    def rank(p: RightsPolicy) -> tuple[int, int, int, int, int]:
        return (
            p.priority,
            1 if p.machine_id is not None else 0,
            1 if p.action_code != WILDCARD else 0,
            1 if p.subject_kind != WILDCARD else 0,
            p.version,
        )

    best = max(rank(p) for p in rows)
    tied = [p for p in rows if rank(p) == best]
    for candidate in tied:
        if candidate.effect == PolicyEffect.DENY:
            return candidate
    return max(tied, key=lambda p: p.id)


def _unmet_conditions(policy: RightsPolicy, ctx: dict[str, Any]) -> list[str]:
    """
    Politika koşullarını bağlamla karşılaştırır.

    Bağlamda olmayan anahtar "sağlanmadı" sayılır.  Eksik kanıtı "sorun yok"
    diye yorumlamak, koşulu hiç yazmamakla aynı kapıya çıkar.
    """
    unmet: list[str] = []
    for key, expected in policy.conditions().items():
        if key not in ctx:
            unmet.append(f"{R_CONDITION_MISSING}:{key}")
        elif ctx[key] != expected:
            unmet.append(f"{R_CONDITION_UNMET}:{key}")
    return unmet


def _check_delegation(db: Session, st: _State, ctx: dict[str, Any]) -> str | None:
    """Bağlam bir vekâlet iddia ediyorsa, o vekâlet doğrulanır."""
    delegator = ctx.get("delegated_by")
    if not delegator:
        return None

    machine_id = st.machine.id if st.machine is not None else None
    rows = (
        db.execute(
            select(Delegation).where(
                Delegation.tenant_id == st.tenant_id,
                Delegation.delegator_subject_ref == str(delegator),
                or_(Delegation.machine_id.is_(None), Delegation.machine_id == machine_id),
            )
        )
        .scalars()
        .all()
    )
    scoped = [d for d in rows if d.covers(st.domain, st.action_code)]
    if not scoped:
        st.add(R_DELEGATION_MISSING if not rows else R_DELEGATION_SCOPE)
        return HspVerdict.DENY

    usable = [
        d
        for d in scoped
        if d.status == GrantStatus.ACTIVE and d.valid_from <= st.now < d.valid_until
    ]
    if usable:
        st.evidence["delegation_id"] = usable[0].id
        return None
    if any(d.status == GrantStatus.REVOKED for d in scoped):
        st.add(R_DELEGATION_REVOKED)
        return HspVerdict.REVOKED
    st.add(R_DELEGATION_EXPIRED)
    return HspVerdict.EXPIRED


def _check_token(db: Session, st: _State, policy: RightsPolicy) -> str | None:
    """
    Yetenek jetonu denetimi.

    İki ayrı durum bilinçli olarak ayrılır:

    * hiç jeton verilmemişse ve politika jeton istiyorsa -> ``DENY``;
    * jeton verilmiş ama süresi dolmuş/tükenmişse -> ``EXPIRED``.

    İkincisi politika jeton istemese bile geçerlidir: bir kez süreli yetki
    verilmişse, o yetkinin sona ermesi sessizce yok sayılamaz.  "Süresi doldu"
    ile "hiç sorulmadı" farklı olgulardır ve makbuzda farklı görünmelidir.
    """
    machine_id = st.machine.id if st.machine is not None else None
    rows = (
        db.execute(
            select(CapabilityToken).where(
                CapabilityToken.tenant_id == st.tenant_id,
                CapabilityToken.machine_id == machine_id,
                CapabilityToken.domain.in_([st.domain, WILDCARD]),
                CapabilityToken.action_code.in_([st.action_code, WILDCARD]),
                or_(
                    CapabilityToken.subject_ref.is_(None),
                    CapabilityToken.subject_ref == st.subject_ref,
                ),
            )
        )
        .scalars()
        .all()
    )

    usable = [t for t in rows if t.is_usable(st.now)]
    if usable:
        # En uzun süreli geçerli jeton kullanılır; kısa ömürlüler saklı kalır.
        token = max(usable, key=lambda t: (t.expires_at, t.id))
        st.token = token
        st.add(f"{R_TOKEN_APPLIED}:{token.token_ref}")
        st.evidence["capability_token_ref"] = token.token_ref
        st.evidence["capability_expires_at"] = token.expires_at.isoformat()
        return None

    if not rows:
        if policy.requires_capability_token:
            st.add(R_TOKEN_MISSING)
            return HspVerdict.DENY
        return None

    if any(t.status == GrantStatus.REVOKED for t in rows):
        st.add(R_TOKEN_REVOKED)
        return HspVerdict.REVOKED

    expired_now = [t for t in rows if t.status == GrantStatus.ACTIVE and t.expires_at <= st.now]
    for token in expired_now:
        # Durum geçişi; kanıt kaydı değil, yaşam döngüsü alanı.
        token.status = GrantStatus.EXPIRED
    if expired_now:
        st.add(R_TOKEN_EXPIRED)
        return HspVerdict.EXPIRED
    if any(t.is_exhausted() for t in rows):
        st.add(R_TOKEN_EXHAUSTED)
        return HspVerdict.EXPIRED

    st.add(R_TOKEN_EXPIRED)
    return HspVerdict.EXPIRED


def _amount_over_limit(st: _State, ctx: dict[str, Any]) -> bool:
    """Jeton bir tutar tavanı taşıyorsa, tavanı aşan eylem insana yükselir."""
    if st.token is None or st.token.amount_limit is None:
        return False
    if "amount" not in ctx:
        return False
    amount: Decimal = D(ctx.get("amount"))
    over = amount > st.token.amount_limit
    if over:
        st.evidence["amount"] = str(amount)
        st.evidence["amount_limit"] = str(st.token.amount_limit)
    return over


def _requires_human(policy: RightsPolicy, manifest: MachineActionManifest) -> bool:
    """
    İnsan onayı gerekli mi?

    Politika açıkça isteyebilir.  Bunun ötesinde ``SEVERE`` etki her hâlükârda
    insan ister: geri döndürülemez fiziksel/ekonomik zarar veren bir eylem için
    "önceden onaylandı" demek, onayı anlamsızlaştırır.
    """
    if policy.requires_human_approval:
        return True
    return manifest.impact_level == ImpactLevel.SEVERE


def _check_human_approval(
    db: Session,
    st: _State,
    policy: RightsPolicy,
    manifest: MachineActionManifest,
    ctx: dict[str, Any],
) -> str | None:
    if not _requires_human(policy, manifest):
        return None
    if not policy.requires_human_approval:
        st.add(R_SEVERE_IMPACT)

    approval = ctx.get("human_approval")
    if isinstance(approval, dict):
        if approval.get("granted") is False:
            # Açık insan reddi, "onay bekleniyor" değil, karardır.
            st.add(R_HUMAN_REFUSED)
            st.evidence["refused_by_user_id"] = approval.get("user_id")
            return HspVerdict.DENY
        if approval.get("granted") is True:
            user_id = approval.get("user_id")
            if not user_id:
                # Kime ait olduğu bilinmeyen onay, onay değildir.
                st.add(R_HUMAN_UNATTRIBUTED)
                st.human_review_required = True
                return HspVerdict.REQUIRE_HUMAN_APPROVAL
            st.add(f"{R_HUMAN_GRANTED}:{user_id}")
            st.evidence["approved_by_user_id"] = user_id
            return None

    override = _active_override(db, st)
    if override is not None:
        # Override yalnızca beklenen insan onayının yerine geçer; yasağı açmaz.
        st.override = override
        st.add(f"{R_OVERRIDE_USED}:{override.code}")
        st.evidence["override_code"] = override.code
        st.evidence["override_expires_at"] = override.expires_at.isoformat()
        st.human_review_required = True
        return None

    st.add(R_HUMAN_REQUIRED)
    st.human_review_required = True
    return HspVerdict.REQUIRE_HUMAN_APPROVAL


def _active_override(db: Session, st: _State) -> EmergencyOverride | None:
    machine_id = st.machine.id if st.machine is not None else None
    rows = (
        db.execute(
            select(EmergencyOverride).where(
                EmergencyOverride.tenant_id == st.tenant_id,
                EmergencyOverride.status == "ACTIVE",
                EmergencyOverride.valid_from <= st.now,
                EmergencyOverride.expires_at > st.now,
                or_(
                    EmergencyOverride.machine_id.is_(None),
                    EmergencyOverride.machine_id == machine_id,
                ),
                or_(
                    EmergencyOverride.subject_ref.is_(None),
                    EmergencyOverride.subject_ref == st.subject_ref,
                ),
                or_(
                    EmergencyOverride.domain.is_(None),
                    EmergencyOverride.domain == st.domain,
                ),
                or_(
                    EmergencyOverride.action_code.is_(None),
                    EmergencyOverride.action_code == st.action_code,
                ),
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


def _impact_rank(level: str | None) -> int:
    #: Tanınmayan seviye en yükseğe sayılır: bilinmeyen etki hafife alınmaz.
    return IMPACT_ORDER.get(str(level or ""), IMPACT_ORDER[ImpactLevel.SEVERE])


# ---------------------------------------------------------------------------
# Kanıt yazımı
# ---------------------------------------------------------------------------
def _safe_context(ctx: dict[str, Any]) -> str | None:
    """Bağlamı makbuza yazılabilir hâle getirir: kimlik bilgileri düşer."""
    if not ctx:
        return None
    cleaned = {
        k: ("***" if k.lower() in _SENSITIVE_CONTEXT_KEYS else v)
        for k, v in ctx.items()
        if not str(k).startswith("_")
    }
    raw = dumps(cleaned)
    return raw[:_CONTEXT_LIMIT]


def _question(st: _State) -> str:
    machine = st.machine_code or f"machine:{st.machine_id}"
    return f"{machine} -> {st.action_code} ({st.domain}) / {st.subject_ref}"[:512]


def _receipt_payload(r: RightsReceipt) -> str:
    """
    Makbuzun kanonik, yeniden üretilebilir içeriği.

    Yalnızca **saklanan** sütunlardan kurulur — saat okuması, rastgele değer
    yoktur — böylece doğrulama sırasında yeniden hesaplanabilir ve bir alanı
    sonradan düzeltilmiş satır yakalanır.
    """
    return dumps(
        {
            "t": r.tenant_id,
            "rq": r.request_id,
            "s": r.subject_ref,
            "m": r.machine_id,
            "mc": r.machine_code,
            "a": r.action_code,
            "d": r.domain,
            "q": r.question,
            "v": r.verdict,
            "al": r.allow,
            "rs": r.reasons_json,
            "p": r.policy_id,
            "pc": r.policy_code,
            "pv": r.policy_version,
            "ct": r.capability_token_id,
            "ov": r.override_id,
            "ev": r.evidence_json,
            "ap": r.appeal_path,
            "hr": r.human_review_required,
            "ts": r.decided_at.isoformat() if r.decided_at else None,
        }
    )


def _compute_hash(previous: str | None, payload: str) -> str:
    return hashlib.sha256(f"{previous or ''}|{payload}".encode()).hexdigest()


def _last_hash(db: Session, tenant_id: int) -> str | None:
    return db.execute(
        select(RightsReceipt.content_hash)
        .where(RightsReceipt.tenant_id == tenant_id)
        .order_by(RightsReceipt.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _write_receipt(
    db: Session, st: _State, verdict: str, allow: bool, request_id: int | None
) -> RightsReceipt:
    receipt = RightsReceipt(
        tenant_id=st.tenant_id,
        request_id=request_id,
        subject_ref=st.subject_ref,
        machine_id=st.machine.id if st.machine is not None else st.machine_id,
        machine_code=st.machine_code,
        action_code=st.action_code,
        domain=st.domain,
        question=_question(st),
        verdict=str(verdict),
        allow=allow,
        reasons_json=dumps(st.reasons),
        policy_id=st.policy.id if st.policy is not None else None,
        policy_code=st.policy.code if st.policy is not None else None,
        policy_version=st.policy.version if st.policy is not None else None,
        capability_token_id=st.token.id if st.token is not None else None,
        override_id=st.override.id if st.override is not None else None,
        evidence_json=dumps(st.evidence) if st.evidence else None,
        appeal_path=st.appeal_path(),
        human_review_required=st.human_review_required,
    )
    # decided_at özetin parçasıdır; hash'ten önce belirlenmelidir.
    receipt.decided_at = st.now
    receipt.previous_hash = _last_hash(db, st.tenant_id)
    receipt.content_hash = _compute_hash(receipt.previous_hash, _receipt_payload(receipt))
    db.add(receipt)
    db.flush()
    return receipt


def _finalize(
    db: Session, st: _State, verdict: str, ctx: dict[str, Any], commit: bool
) -> Decision:
    """
    Talebi ve makbuzu yazar, kararı döndürür.

    Reddedilen talepler de yazılır: yalnızca izin verilenleri kaydeden bir
    sistem kaç kez reddettiğini bilemez, dolayısıyla denetlenemez.
    """
    allow = verdict == HspVerdict.ALLOW
    if not st.reasons:  # gerekçesiz karar olmaz
        st.add(R_INTERNAL_ERROR)

    request = ActionRequest(
        tenant_id=st.tenant_id,
        subject_ref=st.subject_ref,
        machine_id=st.machine.id if st.machine is not None else st.machine_id,
        machine_code=st.machine_code,
        manifest_id=st.manifest.id if st.manifest is not None else None,
        action_code=st.action_code,
        domain=st.domain,
        purpose=(str(ctx.get("purpose") or "") or None),
        context_json=_safe_context(ctx),
        requested_at=st.now,
        requested_by_user_id=ctx.get("requested_by_user_id"),
        correlation_ref=(str(ctx.get("correlation_ref") or "") or None),
        verdict=str(verdict),
        allow=allow,
        policy_id=st.policy.id if st.policy is not None else None,
    )
    db.add(request)
    db.flush()

    receipt = _write_receipt(db, st, verdict, allow, request.id)
    request.receipt_id = receipt.id

    if allow:
        if st.token is not None:
            st.token.use_count += 1
            if st.token.is_exhausted():
                st.token.status = GrantStatus.CONSUMED
        if st.override is not None:
            st.override.use_count += 1
    db.flush()

    if commit:
        db.commit()

    log.info(
        "hsp verdict=%s machine=%s action=%s subject=%s receipt=%s reasons=%s",
        verdict, st.machine_code, st.action_code, st.subject_ref,
        receipt.id, ",".join(st.reasons),
    )
    return Decision(
        allow=allow,
        verdict=str(verdict),
        reasons=list(st.reasons),
        receipt_id=receipt.id,
        appeal_path=st.appeal_path(),
        policy_code=st.policy.code if st.policy is not None else None,
        request_id=request.id,
    )


def _fail_closed(db: Session, st: _State, ctx: dict[str, Any]) -> Decision:
    """
    Motor hata verdiğinde çalışan son çare.

    Makbuz yine yazılmaya çalışılır; yazılamazsa karar yine REDDİR ve
    ``receipt_id`` boş döner.  Oturum bilinçli olarak geri alınmaz: çağıranın
    bekleyen değişikliklerini bu katman yok edemez, DENY zaten çağıranı işlemi
    iptal etmeye zorlar.
    """
    try:
        return _finalize(db, st, HspVerdict.DENY, ctx, False)
    except Exception as exc:  # pragma: no cover - yalnızca depo erişilemezken
        log.error("HSP receipt could not be written: %s", exc)
        reasons = list(st.reasons)
        if R_RECEIPT_FAILED not in reasons:
            reasons.append(R_RECEIPT_FAILED)
        return Decision(
            allow=False,
            verdict=str(HspVerdict.DENY),
            reasons=reasons,
            receipt_id=None,
            appeal_path=st.appeal_path(),
        )


# ---------------------------------------------------------------------------
# Doğrulama ve sorgular
# ---------------------------------------------------------------------------
def verify_chain(
    db: Session, *, tenant_id: int = DEFAULT_TENANT_ID, limit: int | None = None
) -> dict[str, Any]:
    """
    Bir kiracının makbuz zincirini yürür ve ilk kırık halkayı bildirir.

    ``{"valid": bool, "checked": int, "broken_at": id|None, "reason": str|None}``
    """
    stmt = (
        select(RightsReceipt)
        .where(RightsReceipt.tenant_id == tenant_id)
        .order_by(RightsReceipt.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()

    previous: str | None = None
    checked = 0
    for row in rows:
        checked += 1
        if not row.content_hash:
            return _broken(checked, row.id, "missing_hash")
        if row.previous_hash != previous:
            return _broken(checked, row.id, "previous_hash_mismatch")
        # İçerikten yeniden hesaplanır: yalnızca halkayı değil, satırın
        # sonradan düzeltilmiş bir alanını da yakalar.
        if _compute_hash(previous, _receipt_payload(row)) != row.content_hash:
            return _broken(checked, row.id, "content_hash_mismatch")
        previous = row.content_hash
    return {"valid": True, "checked": checked, "broken_at": None, "reason": None}


def _broken(checked: int, row_id: int, reason: str) -> dict[str, Any]:
    log.error("HSP receipt chain broken at id=%s reason=%s", row_id, reason)
    return {"valid": False, "checked": checked, "broken_at": row_id, "reason": reason}


def subject_receipts(
    db: Session,
    *,
    subject_ref: str,
    tenant_id: int = DEFAULT_TENANT_ID,
    limit: int = 100,
) -> list[RightsReceipt]:
    """
    Bir insan hakkında verilmiş kararların listesi.

    Makbuzun varlık sebebi budur: kişi, hakkında ne karar verildiğini
    görebilmelidir.
    """
    return list(
        db.execute(
            select(RightsReceipt)
            .where(
                RightsReceipt.tenant_id == tenant_id,
                RightsReceipt.subject_ref == subject_ref,
            )
            .order_by(RightsReceipt.id.desc())
            .limit(max(1, int(limit)))
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Yazma yardımcıları
# ---------------------------------------------------------------------------
def ensure_node(
    db: Session,
    *,
    subject_ref: str,
    subject_kind: str = SubjectKind.UNKNOWN,
    tenant_id: int = DEFAULT_TENANT_ID,
    display_label: str | None = None,
    appeal_path: str | None = None,
    commit: bool = False,
) -> HumanSovereigntyNode:
    """Egemenlik düğümünü bulur ya da oluşturur.  Kişisel veri yazılmaz."""
    node = db.execute(
        select(HumanSovereigntyNode).where(
            HumanSovereigntyNode.tenant_id == tenant_id,
            HumanSovereigntyNode.subject_ref == subject_ref,
        )
    ).scalar_one_or_none()
    if node is None:
        node = HumanSovereigntyNode(
            tenant_id=tenant_id,
            subject_ref=subject_ref,
            subject_kind=str(subject_kind),
            display_label=display_label,
            appeal_path=appeal_path or DEFAULT_APPEAL_PATH,
        )
        db.add(node)
        db.flush()
    if commit:
        db.commit()
    return node


def issue_capability_token(
    db: Session,
    *,
    token_ref: str,
    machine_id: int,
    domain: str,
    action_code: str = WILDCARD,
    subject_ref: str | None = None,
    granted_by: str = GrantSource.UNKNOWN,
    granted_by_user_id: int | None = None,
    valid_days: int = 90,
    max_uses: int = 0,
    amount_limit: Decimal | None = None,
    policy_id: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    commit: bool = False,
) -> CapabilityToken:
    """
    Süreli yetki verir.

    ``valid_days`` zorunlu olarak sonludur; süresiz jeton üretilemez.  Var olan
    aynı ``token_ref`` yeniden düzenlenmez — tekrar çağrı mevcut jetonu döner,
    böylece tohumlama yinelenebilir kalır.
    """
    existing = db.execute(
        select(CapabilityToken).where(
            CapabilityToken.tenant_id == tenant_id,
            CapabilityToken.token_ref == token_ref,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = utcnow()
    token = CapabilityToken(
        tenant_id=tenant_id,
        token_ref=token_ref,
        machine_id=machine_id,
        subject_ref=subject_ref,
        domain=str(domain),
        action_code=action_code,
        granted_by=str(granted_by),
        granted_by_user_id=granted_by_user_id,
        policy_id=policy_id,
        issued_at=now,
        expires_at=now + timedelta(days=max(1, int(valid_days))),
        max_uses=max(0, int(max_uses)),
        amount_limit=amount_limit,
    )
    db.add(token)
    db.flush()
    if commit:
        db.commit()
    return token


def revoke(
    db: Session,
    *,
    target_kind: str,
    target_id: int | None = None,
    machine_id: int | None = None,
    subject_ref: str | None = None,
    action_code: str | None = None,
    domain: str | None = None,
    reason_code: str = "UNSPECIFIED",
    reason: str | None = None,
    source: str = GrantSource.SUBJECT,
    revoked_by_user_id: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    commit: bool = False,
) -> Revocation:
    """
    Yetkiyi geri alır.

    Geri alma kaydı yazılırken hedefin kendi durumu da güncellenir; böylece
    "geri alındı" bilgisi iki yerde tutarsız kalamaz.  Makine ve özne hedefli
    geri almalar motorun 4. adımında görülür.
    """
    now = utcnow()
    row = Revocation(
        tenant_id=tenant_id,
        target_kind=str(target_kind),
        target_id=target_id,
        machine_id=machine_id,
        subject_ref=subject_ref,
        action_code=action_code,
        domain=domain,
        reason_code=reason_code,
        reason=reason,
        source=str(source),
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=now,
        effective_from=now,
    )
    db.add(row)
    db.flush()

    if target_kind == RevocationTarget.CAPABILITY_TOKEN and target_id:
        token = db.get(CapabilityToken, target_id)
        if token is not None:
            token.status = GrantStatus.REVOKED
            token.revoked_at = now
            token.revocation_id = row.id
    elif target_kind == RevocationTarget.DELEGATION and target_id:
        delegation = db.get(Delegation, target_id)
        if delegation is not None:
            delegation.status = GrantStatus.REVOKED
            delegation.revoked_at = now
    elif target_kind == RevocationTarget.POLICY and target_id:
        policy = db.get(RightsPolicy, target_id)
        if policy is not None:
            policy.is_active = False
    elif target_kind == RevocationTarget.PASSPORT and target_id:
        passport = db.get(MachinePassport, target_id)
        if passport is not None:
            passport.status = PassportStatus.REVOKED
    elif target_kind == RevocationTarget.MACHINE and (target_id or machine_id):
        machine = db.get(Machine, target_id or machine_id)
        if machine is not None:
            machine.status = MachineStatus.SUSPENDED

    db.flush()
    if commit:
        db.commit()
    return row


__all__ = [
    "Decision",
    "DEFAULT_APPEAL_PATH",
    "evaluate",
    "enforce",
    "ensure_node",
    "issue_capability_token",
    "revoke",
    "subject_receipts",
    "verify_chain",
]
