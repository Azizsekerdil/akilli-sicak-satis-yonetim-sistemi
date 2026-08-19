"""
Uyumluluk genel durum tablosu.

**Burada tek bir uyumluluk skoru üretilmez.** Bir kurumun insan hakları ve veri
koruma duruşu "82/100" gibi tek bir sayıya indirgenemez: aynı sayı, iki eksik
aydınlatma metniyle bir doğrulanmamış yurt dışı aktarımı aynı kefeye koyar ve
okuyucuyu ikisinin de "biraz eksik" olduğuna ikna eder. Bu yüzden çıktı,
kategori bazında **durum + sayım + engelleyici gerekçe** listesidir.

Sistem hiçbir kategoriye ``COMPLIANT`` yazmaz. Yazabildiği üç değer vardır:

``UNKNOWN``
    Ölçülmemiş. Hiç tarama yapılmamış bir envanter "temiz" değildir.

``REVIEW_REQUIRED``
    Ölçülmüş ve eksik bulunmuş; gerekçeler ``blocking_reasons`` içindedir.

``PARTIAL``
    Kayıtlar eksiksiz görünüyor ama uyumluluk sonucu henüz onaylanmış bir
    kural paketine karşı üretilmedi. Belgelenmiş olmak, uyumlu olmak değildir.

``COMPLIANT`` sonucuna yalnızca insan onaylı bir kural paketine karşı yapılan
değerlendirme ulaşabilir ve o sonuç ``cmp_rule_evaluation`` içinde yaşar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.enums import ComplianceState
from app.compliance.models.evidence import EvidenceArtifact
from app.compliance.models.hsp import HspVerdict, RightsReceipt
from app.compliance.models.rules import RulePack
from app.compliance.rule_enums import LifecycleStatus
from app.compliance.services import (
    activity_service,
    consent_service,
    dsr_service,
    evidence_service,
    hsp_engine,
    inventory_service,
)
from app.models.base import utcnow

#: Kategori etiketleri. Anahtarlar kararlıdır; etiketler yalnızca gösterim
#: içindir ve raporun anlamını değiştirmez.
_LABELS: dict[str, tuple[str, str]] = {
    "inventory": ("Kişisel veri envanteri", "Personal data inventory"),
    "processing_activities": ("İşleme faaliyetleri", "Processing activities"),
    "notices_consents": ("Aydınlatma ve rıza", "Notices and consent"),
    "dsr": ("İlgili kişi başvuruları", "Data subject requests"),
    "transfers": ("Yurt dışı aktarımlar", "Cross-border transfers"),
    "rulepacks": ("Kural paketleri", "Rule packs"),
    "hsp": ("İnsan egemenliği protokolü", "Human sovereignty protocol"),
    "evidence": ("Kanıt zinciri", "Evidence chain"),
}


def _state(*, measured: bool, blocking: list[str]) -> str:
    if not measured:
        return ComplianceState.UNKNOWN
    if blocking:
        return ComplianceState.REVIEW_REQUIRED
    return ComplianceState.PARTIAL


def _category(
    key: str,
    *,
    measured: bool,
    blocking: list[str],
    total: int = 0,
    reviewed: int = 0,
    pending: int = 0,
    missing_evidence: int = 0,
    last_activity_at: datetime | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label_tr, label_en = _LABELS[key]
    return {
        "key": key,
        "label_tr": label_tr,
        "label_en": label_en,
        "state": _state(measured=measured, blocking=blocking),
        "total": total,
        "reviewed": reviewed,
        "pending_human_review": pending,
        "missing_evidence": missing_evidence,
        "last_activity_at": last_activity_at,
        "blocking_reasons": blocking,
        "metrics": metrics or {},
    }


def build(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """Kategori bazında durum tablosunu üret."""
    categories: list[dict[str, Any]] = []

    # --- Envanter ---------------------------------------------------------
    inv = inventory_service.status_summary(db, tenant_id=tenant_id)
    inv_blocking: list[str] = []
    if not inv["scanned"]:
        inv_blocking.append("never_scanned")
    if inv["pending_review"]:
        inv_blocking.append("fields_awaiting_review")
    if inv["special_candidates"]:
        # Özel nitelikli aday alanlar, incelenmiş olsalar bile ayrı görünmeli:
        # bu kategori farklı bir dayanak rejimine tabidir.
        inv_blocking.append("special_category_candidates_present")
    if inv["absent"]:
        inv_blocking.append("fields_disappeared_since_last_scan")
    categories.append(
        _category(
            "inventory",
            measured=bool(inv["scanned"]),
            blocking=inv_blocking,
            total=inv["total"],
            reviewed=inv["reviewed"],
            pending=inv["pending_review"],
            last_activity_at=inv["last_activity_at"],
            metrics={
                "special_candidates": inv["special_candidates"],
                "absent": inv["absent"],
            },
        )
    )

    # --- Faaliyetler ve aktarımlar ---------------------------------------
    reg = activity_service.status_summary(db, tenant_id=tenant_id)
    act = reg["activities"]
    act_evidence = evidence_service.subjects_with_evidence(
        db, tenant_id=tenant_id, subject_type=activity_service.SUBJECT_ACTIVITY
    )
    act_missing = max(act["total"] - len(act_evidence), 0)
    act_blocking: list[str] = []
    if act["total"] == 0:
        act_blocking.append("no_activity_recorded")
    if act["pending_review"]:
        act_blocking.append("activities_awaiting_review")
    if act_missing:
        act_blocking.append("activities_without_evidence")
    categories.append(
        _category(
            "processing_activities",
            measured=act["total"] > 0,
            blocking=act_blocking,
            total=act["total"],
            reviewed=act["total"] - act["pending_review"],
            pending=act["pending_review"],
            missing_evidence=act_missing,
            last_activity_at=act["last_activity_at"],
            metrics={
                "automated_decisions": act["automated_decisions"],
                "special_category": act["special_category"],
            },
        )
    )

    tra = reg["transfers"]
    tra_blocking: list[str] = []
    if tra["pending_review"]:
        tra_blocking.append("transfers_awaiting_review")
    if tra["unknown_mechanism"]:
        tra_blocking.append("transfer_mechanism_unknown")
    if tra["without_tia"]:
        tra_blocking.append("transfer_impact_assessment_missing")
    categories.append(
        _category(
            "transfers",
            measured=tra["total"] > 0,
            blocking=tra_blocking,
            total=tra["total"],
            reviewed=tra["total"] - tra["pending_review"],
            pending=tra["pending_review"],
            last_activity_at=tra["last_activity_at"],
            metrics={
                "unknown_mechanism": tra["unknown_mechanism"],
                "without_tia": tra["without_tia"],
            },
        )
    )

    # --- Aydınlatma ve rıza ----------------------------------------------
    con = consent_service.status_summary(db, tenant_id=tenant_id)
    con_blocking: list[str] = []
    if not con["published_notices"]:
        con_blocking.append("no_published_notice")
    if con["pending_review"]:
        con_blocking.append("consents_awaiting_review")
    if con["counts"]["expired"]:
        con_blocking.append("expired_consents_present")
    if con["withdrawals_without_downstream_notice"]:
        con_blocking.append("withdrawals_not_propagated")
    categories.append(
        _category(
            "notices_consents",
            measured=con["total"] > 0 or con["published_notices"] > 0,
            blocking=con_blocking,
            total=con["total"],
            reviewed=con["total"] - con["pending_review"],
            pending=con["pending_review"],
            last_activity_at=con["last_activity_at"],
            metrics={
                "published_notices": con["published_notices"],
                **{f"consent_{k}": v for k, v in con["counts"].items()},
            },
        )
    )

    # --- Başvurular --------------------------------------------------------
    dsr = dsr_service.status_summary(db, tenant_id=tenant_id)
    dsr_blocking: list[str] = []
    if dsr["overdue"]:
        dsr_blocking.append("overdue_requests")
    if dsr["without_due_date"]:
        # Süresi tanımsız açık başvuru "zamanında" sayılamaz; ölçülemeyen bir
        # yükümlülük, yerine getirilmiş sayılmaz.
        dsr_blocking.append("requests_without_due_date")
    if dsr["identity_unverified_open"]:
        dsr_blocking.append("identity_unverified_open_requests")
    categories.append(
        _category(
            "dsr",
            measured=dsr["total"] > 0,
            blocking=dsr_blocking,
            total=dsr["total"],
            reviewed=dsr["total"] - dsr["open"],
            pending=dsr["open"],
            last_activity_at=dsr["last_activity_at"],
            metrics={
                "open": dsr["open"],
                "overdue": dsr["overdue"],
                "without_due_date": dsr["without_due_date"],
            },
        )
    )

    # --- Kural paketleri ---------------------------------------------------
    pack_rows = db.execute(select(RulePack.status, func.count(RulePack.id)).group_by(
        RulePack.status
    )).all()
    by_status = {str(status): int(count) for status, count in pack_rows}
    pack_total = sum(by_status.values())
    active = by_status.get(LifecycleStatus.ACTIVE, 0)
    pack_blocking: list[str] = []
    if pack_total == 0:
        pack_blocking.append("no_rulepack_imported")
    if not active:
        pack_blocking.append("no_active_rulepack")
    if by_status.get(LifecycleStatus.IN_REVIEW):
        pack_blocking.append("rulepacks_awaiting_approval")
    categories.append(
        _category(
            "rulepacks",
            measured=pack_total > 0,
            blocking=pack_blocking,
            total=pack_total,
            reviewed=active,
            pending=by_status.get(LifecycleStatus.IN_REVIEW, 0),
            metrics=by_status,
        )
    )

    # --- HSP ---------------------------------------------------------------
    receipt_total = int(
        db.execute(
            select(func.count(RightsReceipt.id)).where(
                RightsReceipt.tenant_id == tenant_id
            )
        ).scalar_one()
        or 0
    )
    pending_human = int(
        db.execute(
            select(func.count(RightsReceipt.id)).where(
                RightsReceipt.tenant_id == tenant_id,
                RightsReceipt.verdict == HspVerdict.REQUIRE_HUMAN_APPROVAL,
            )
        ).scalar_one()
        or 0
    )
    denied = int(
        db.execute(
            select(func.count(RightsReceipt.id)).where(
                RightsReceipt.tenant_id == tenant_id,
                RightsReceipt.verdict == HspVerdict.DENY,
            )
        ).scalar_one()
        or 0
    )
    last_receipt = db.execute(
        select(func.max(RightsReceipt.decided_at)).where(
            RightsReceipt.tenant_id == tenant_id
        )
    ).scalar_one()
    hsp_chain = hsp_engine.verify_chain(db, tenant_id=tenant_id)

    hsp_blocking: list[str] = []
    if receipt_total == 0:
        hsp_blocking.append("no_decisions_recorded")
    if pending_human:
        hsp_blocking.append("decisions_awaiting_human_approval")
    if not hsp_chain.get("valid"):
        hsp_blocking.append("receipt_chain_broken")
    categories.append(
        _category(
            "hsp",
            measured=receipt_total > 0,
            blocking=hsp_blocking,
            total=receipt_total,
            reviewed=receipt_total - pending_human,
            pending=pending_human,
            last_activity_at=last_receipt,
            metrics={
                "denied": denied,
                "require_human_approval": pending_human,
                "chain_valid": bool(hsp_chain.get("valid")),
            },
        )
    )

    # --- Kanıt zinciri -----------------------------------------------------
    chain = evidence_service.verify(db, tenant_id=tenant_id)
    evidence_total = int(
        db.execute(
            select(func.count(EvidenceArtifact.id)).where(
                EvidenceArtifact.tenant_id == tenant_id
            )
        ).scalar_one()
        or 0
    )
    ev_blocking: list[str] = []
    if evidence_total == 0:
        ev_blocking.append("no_evidence_collected")
    if not chain.get("valid"):
        ev_blocking.append("chain_broken")
    categories.append(
        _category(
            "evidence",
            measured=evidence_total > 0,
            blocking=ev_blocking,
            total=evidence_total,
            reviewed=evidence_total,
            last_activity_at=db.execute(
                select(func.max(EvidenceArtifact.collected_at)).where(
                    EvidenceArtifact.tenant_id == tenant_id
                )
            ).scalar_one(),
            metrics={"chain_status": chain.get("status")},
        )
    )

    totals = {
        "categories": len(categories),
        "pending_human_review": sum(c["pending_human_review"] for c in categories),
        "missing_evidence": sum(c["missing_evidence"] for c in categories),
        "categories_review_required": sum(
            1 for c in categories if c["state"] == ComplianceState.REVIEW_REQUIRED
        ),
        "categories_unknown": sum(
            1 for c in categories if c["state"] == ComplianceState.UNKNOWN
        ),
        "blocking_reasons": sum(len(c["blocking_reasons"]) for c in categories),
    }

    return {
        "generated_at": utcnow(),
        "categories": categories,
        "totals": totals,
        "evidence_chain": chain,
        "review_queue": [
            c["key"]
            for c in categories
            if c["state"] in (ComplianceState.REVIEW_REQUIRED, ComplianceState.UNKNOWN)
        ],
        "disclaimer_key": "compliance.overview.disclaimer",
    }
