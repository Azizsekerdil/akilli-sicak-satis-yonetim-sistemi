"""
Uyumluluk ORM kayıt defteri.

Bu paketi içe aktarmak, ``cmp_`` önekli tabloların tamamını
``app.core.db.Base.metadata``'ya kaydeder — Alembic autogenerate ve
``create_all()`` buna dayanır.

Tablo öneki ``cmp_`` bilinçlidir: mevcut 71 operasyonel tabloyla ad çakışması
imkânsız hâle gelir, uyumluluk şeması tek bir ``LIKE 'cmp_%'`` ile ayrılabilir
ve katman kaldırılmak istendiğinde neyin kaldırılacağı belirsiz kalmaz.

İçe aktarma sırası bağımlılık yönündedir (kiracı → kanıt → envanter → ...)
ki şema yukarıdan aşağı okunduğunda anlaşılır olsun. Yabancı anahtarlar dizgi
hedefleriyle kurulduğu için sıralama teknik bir zorunluluk değil, okunabilirlik
tercihidir.
"""

from __future__ import annotations

from app.compliance.models.tenant import Tenant, Workspace, tenant_fk
from app.compliance.models.rules import (
    LegalSource,
    Rule,
    RuleApproval,
    RuleEvaluation,
    RulePack,
)
from app.compliance.models.evidence import (
    CHAIN_SEED,
    Control,
    ControlTest,
    EvidenceArtifact,
    Finding,
    Risk,
    RiskControl,
    chain_digest,
    content_digest,
    verify_chain,
)
from app.compliance.models.inventory import (
    ActivityDataCategory,
    ActivityPurpose,
    DataCategory,
    DataField,
    DataFlow,
    DataStore,
    LegalBasis,
    ProcessingActivity,
    Purpose,
    Recipient,
    Subprocessor,
    SystemAsset,
    Transfer,
    Vendor,
)
from app.compliance.models.consent import ConsentRecord, NoticeVersion, WithdrawalRecord
from app.compliance.models.retention import LegalHold, RetentionEvent, RetentionPolicy
from app.compliance.models.dsr import (
    DataSubjectRequest,
    FulfilmentTask,
    IdentityVerification,
)
from app.compliance.models.hsp import (
    ActionRequest,
    CapabilityToken,
    Delegation,
    EmergencyOverride,
    HumanSovereigntyNode,
    Machine,
    MachineActionManifest,
    MachinePassport,
    Revocation,
    RightsPolicy,
    RightsReceipt,
)

__all__ = [
    # tenant
    "Tenant", "Workspace", "tenant_fk",
    # versioned legal rule packs
    "LegalSource", "RulePack", "Rule", "RuleApproval", "RuleEvaluation",
    # evidence / assurance
    "EvidenceArtifact", "Risk", "Control", "RiskControl", "ControlTest", "Finding",
    "CHAIN_SEED", "chain_digest", "content_digest", "verify_chain",
    # inventory
    "DataCategory", "DataField", "SystemAsset", "DataStore", "ProcessingActivity",
    "Purpose", "LegalBasis", "Recipient", "DataFlow", "Transfer", "Vendor",
    "Subprocessor", "ActivityDataCategory", "ActivityPurpose",
    # consent
    "NoticeVersion", "ConsentRecord", "WithdrawalRecord",
    # retention
    "RetentionPolicy", "RetentionEvent", "LegalHold",
    # data subject requests
    "DataSubjectRequest", "IdentityVerification", "FulfilmentTask",
    # human sovereignty protocol
    "HumanSovereigntyNode", "Machine", "MachinePassport", "MachineActionManifest",
    "RightsPolicy", "CapabilityToken", "Delegation", "ActionRequest",
    "RightsReceipt", "EmergencyOverride", "Revocation",
]
