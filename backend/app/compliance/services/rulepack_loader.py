"""
Kural paketi yükleyicisi ve yaşam döngüsü yönetimi.

JSON paketlerini okur, doğrular, veritabanına taslak olarak alır ve
onay/aktivasyon geçişlerini yönetir. Yükleyicinin taşıdığı beş değişmez:

1.  **Dosya asla kendi durumunu belirleyemez.** Dosyada ne yazarsa yazsın,
    içe alınan her paket ``DRAFT`` olarak başlar. Aksi hâlde bir paket
    dosyasına ``"status": "ACTIVE"`` yazmak, tüm onay sürecini atlardı.
2.  **Kaynak yalnızca resmî alan adlarından olabilir.** ``OFFICIAL_SOURCE_HOSTS``
    dışındaki bir bağlantı doğrulamayı düşürür; "resmî kaynak" alanına rastgele
    bir adres yazılabilseydi, referans alanının kanıt değeri kalmazdı.
3.  **İçerik özeti onaya bağlanır.** ``content_hash`` paketin normatif
    içeriğinin SHA-256'sıdır; onay bu değer üzerine verilir. İçerik ya da
    kaynak değişirse özet değişir, onay artık paketi kapsamaz ve aktivasyon
    reddedilir — yeniden onay gerekir.
4.  **Dört göz kuralı.** Paketi incelemeye gönderen kişi onaylayan kişi
    olamaz.
5.  **Aktivasyon insan onayı ister.** ``activate`` onay arar; ayrıca ORM olay
    dinleyicisi (``app.compliance.models.rules``) doğrudan yazımları da
    reddeder. Tek katmanlı bir kontrol, o katmanı atlayan her yeni çağrı
    yolunda delinirdi.

Paket dosyası biçimi (schema_version 1.0)::

    {
      "schema_version": "1.0",
      "pack_key": "kvkk-core",
      "version": "0.1.0-draft",
      "jurisdiction": "TR",
      "regulation_code": "KVKK-6698",
      "title_tr": "...", "title_en": "...",
      "retrieved_date": "2026-08-18",
      "context_keys": ["processes_personal_data", ...],
      "legal_sources": [ { "source_key": "...", ... } ],
      "rules": [ { "rule_id": "...", ... } ]
    }
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.models.rules import (
    LegalSource,
    Rule,
    RuleApproval,
    RulePack,
)
from app.compliance.rule_enums import (
    ALLOWED_TRANSITIONS,
    UNKNOWN,
    ApprovalDecision,
    ApproverRole,
    ConfidenceLevel,
    Criticality,
    LifecycleStatus,
    SourceHashKind,
    SourceVerification,
)
from app.compliance.services.rule_engine import (
    canonical_json,
    collect_fields,
    evaluate_predicate,
    hash_object,
    sha256_hex,
)
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import dumps, loads, parse_date
from app.models.base import utcnow

log = get_logger("app.compliance.rulepack_loader")

#: Bu yükleyicinin anladığı paket şeması.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

PACKS_ROOT: Path = Path(__file__).resolve().parents[1] / "rulepacks"

#: Kural kaynağı olarak kabul edilen resmî alan adları. Listeye ekleme yapmak
#: bilinçli bir insan kararıdır ve kod değişikliği gerektirir; böylece bir
#: paket dosyası kendi "resmî kaynağını" icat edemez.
OFFICIAL_SOURCE_HOSTS: frozenset[str] = frozenset(
    {
        "www.mevzuat.gov.tr",
        "mevzuat.gov.tr",
        "www.kvkk.gov.tr",
        "kvkk.gov.tr",
        "eur-lex.europa.eu",
    }
)

#: Paket düzeyinde zorunlu alanlar.
_PACK_REQUIRED = (
    "schema_version", "pack_key", "version", "jurisdiction", "regulation_code",
    "title_tr", "title_en", "retrieved_date", "context_keys",
    "legal_sources", "rules",
)

#: Kural düzeyinde zorunlu alanlar. ``requires_human_review`` bilerek
#: zorunludur: varsayılana bırakılsaydı, alanı unutan bir paket sessizce
#: otomatik uyumluluk üretebilirdi.
_RULE_REQUIRED = (
    "rule_id", "title_tr", "title_en", "description_tr", "description_en",
    "legal_source_key", "article_ref", "severity", "confidence",
    "requires_human_review", "applicability", "condition",
)

_SOURCE_REQUIRED = (
    "source_key", "jurisdiction", "regulation_code", "authority",
    "title_tr", "title_en", "official_url", "retrieved_date",
)

_MAX_RULES_PER_PACK = 500

#: Enum üyeleri yerine düz dizge kümeleri: doğrulama girdisi JSON'dan gelir ve
#: karşılaştırma her zaman dizge üzerinden yapılmalıdır.
_SEVERITIES: frozenset[str] = frozenset(str(c) for c in Criticality)
_CONFIDENCES: frozenset[str] = frozenset(str(c) for c in ConfidenceLevel)


# ===========================================================================
# Parmak izleri
# ===========================================================================
def _iso(value: Any) -> str | None:
    """Tarihi ISO dizgeye indirger; JSON'dan gelenle ORM'den gelen eşitlensin."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def source_fingerprint(src: Mapping[str, Any]) -> dict[str, Any]:
    """Bir kaynağın normatif alanları — özet bunun üzerinden hesaplanır."""
    return {
        "source_key": src.get("source_key"),
        "version": str(src.get("version") or "1"),
        "jurisdiction": src.get("jurisdiction"),
        "regulation_code": src.get("regulation_code"),
        "authority": src.get("authority"),
        "title_tr": src.get("title_tr"),
        "title_en": src.get("title_en"),
        "official_url": src.get("official_url"),
        "language": src.get("language") or "tr",
        "publication_date": _iso(src.get("publication_date")),
        "effective_date": _iso(src.get("effective_date")),
        "retrieved_date": _iso(src.get("retrieved_date")),
    }


def source_hash(src: Mapping[str, Any]) -> str:
    """
    Kaynağın referans parmak izi.

    Resmî metnin kendisi indirilmediği için bu değer ``SOURCE_TEXT`` değil
    ``REFERENCE_FINGERPRINT`` türündedir. Metin doğrulandığında tür ve özet
    birlikte güncellenir; o ana kadar "doğrulandı" iddiası taşımaz.
    """
    return hash_object(source_fingerprint(src))


def rule_fingerprint(rule: Mapping[str, Any], *, src_hash: str) -> dict[str, Any]:
    """Bir kuralın normatif içeriği — sunum alanları dışarıda bırakılmaz."""
    return {
        "rule_id": rule.get("rule_id"),
        "jurisdiction": rule.get("jurisdiction"),
        "regulation_code": rule.get("regulation_code"),
        "article_ref": rule.get("article_ref"),
        "title_tr": rule.get("title_tr"),
        "title_en": rule.get("title_en"),
        "description_tr": rule.get("description_tr"),
        "description_en": rule.get("description_en"),
        "official_source_url": rule.get("official_source_url"),
        "authority": rule.get("authority"),
        "publication_date": _iso(rule.get("publication_date")),
        "effective_date": _iso(rule.get("effective_date")),
        "retrieved_date": _iso(rule.get("retrieved_date")),
        "source_hash": src_hash,
        "applicability": rule.get("applicability"),
        "exceptions": rule.get("exceptions") or [],
        "review_triggers": rule.get("review_triggers") or [],
        "condition": rule.get("condition"),
        "evidence_requirements": rule.get("evidence_requirements") or [],
        "deadline_definition": rule.get("deadline_definition"),
        "timezone_rule": rule.get("timezone_rule"),
        "severity": rule.get("severity"),
        "confidence": rule.get("confidence"),
        "requires_human_review": bool(rule.get("requires_human_review")),
    }


def rule_hash(rule: Mapping[str, Any], *, src_hash: str) -> str:
    return hash_object(rule_fingerprint(rule, src_hash=src_hash))


def normalise_pack(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Devralınan varsayılanları açıkça yazar.

    Bir kural, yetki alanını ya da yetkili otoriteyi belirtmeyip paketten veya
    kaynağından devralabilir. Özet **devralma sonrası** değerler üzerinden
    hesaplanmazsa, dosyadan hesaplanan imza ile veritabanı satırlarından
    yeniden hesaplanan imza asla tutmaz — ve her aktivasyon "içerik değişmiş"
    diye reddedilirdi. Bu ilk sürümde tam olarak böyle oldu.

    İşlem idempotenttir: zaten doldurulmuş bir sözlüğü değiştirmez, bu yüzden
    hem dosya hem de ORM kaynaklı sözlüklere aynı biçimde uygulanabilir.
    """
    sources = {
        str(s.get("source_key")): s for s in data.get("legal_sources") or []
    }
    pack_retrieved = data.get("retrieved_date")
    rules: list[dict[str, Any]] = []
    for rule in data.get("rules") or []:
        src = sources.get(str(rule.get("legal_source_key")), {})
        merged = dict(rule)
        merged["jurisdiction"] = rule.get("jurisdiction") or data.get("jurisdiction")
        merged["regulation_code"] = (
            rule.get("regulation_code") or data.get("regulation_code")
        )
        merged["official_source_url"] = (
            rule.get("official_source_url") or src.get("official_url")
        )
        merged["authority"] = rule.get("authority") or src.get("authority")
        merged["article_ref"] = rule.get("article_ref") or UNKNOWN
        merged["publication_date"] = (
            rule.get("publication_date") or src.get("publication_date")
        )
        merged["effective_date"] = (
            rule.get("effective_date") or src.get("effective_date")
        )
        merged["retrieved_date"] = rule.get("retrieved_date") or pack_retrieved
        merged["exceptions"] = rule.get("exceptions") or []
        merged["review_triggers"] = rule.get("review_triggers") or []
        merged["evidence_requirements"] = rule.get("evidence_requirements") or []
        merged["requires_human_review"] = bool(rule.get("requires_human_review"))
        rules.append(merged)

    out = dict(data)
    out["rules"] = rules
    return out


def pack_fingerprint(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Paketin bütün normatif içeriği.

    Kural ve kaynak özetleri sıralanır: aynı içeriğin dosyadaki sırası
    değişince imzanın bozulması, gerçek bir içerik değişikliğiyle karışırdı.
    """
    data = normalise_pack(data)
    sources = {s.get("source_key"): s for s in data.get("legal_sources") or []}
    src_hashes = {key: source_hash(src) for key, src in sources.items()}
    rule_hashes = sorted(
        rule_hash(r, src_hash=src_hashes.get(r.get("legal_source_key"), UNKNOWN))
        for r in data.get("rules") or []
    )
    return {
        "pack_key": data.get("pack_key"),
        "version": data.get("version"),
        "schema_version": data.get("schema_version"),
        "jurisdiction": data.get("jurisdiction"),
        "regulation_code": data.get("regulation_code"),
        "title_tr": data.get("title_tr"),
        "title_en": data.get("title_en"),
        "description_tr": data.get("description_tr"),
        "description_en": data.get("description_en"),
        "retrieved_date": _iso(data.get("retrieved_date")),
        "context_keys": sorted(data.get("context_keys") or []),
        "sources": sorted(src_hashes.values()),
        "rules": rule_hashes,
    }


def pack_content_hash(data: Mapping[str, Any]) -> str:
    return hash_object(pack_fingerprint(data))


def pack_source_hash(data: Mapping[str, Any]) -> str:
    """Pakete giren tüm kaynak özetlerinin birleşik parmak izi."""
    return sha256_hex(
        "|".join(sorted(source_hash(s) for s in data.get("legal_sources") or []))
    )


def content_hash_of_stored_pack(pack: RulePack) -> str:
    """
    Veritabanındaki satırlardan paket özetini **yeniden** hesaplar.

    Aktivasyon sırasında saklanan ``content_hash`` ile karşılaştırılır: bir
    kural satırı doğrudan SQL ile düzenlenmişse, saklanan özet güncel içeriği
    artık tarif etmiyor demektir ve paket yürürlüğe alınamaz.
    """
    sources: dict[str, dict[str, Any]] = {}
    rules: list[dict[str, Any]] = []
    for rule in pack.rules:
        src = rule.legal_source
        if src is not None and src.source_key not in sources:
            sources[src.source_key] = {
                "source_key": src.source_key,
                "version": src.version,
                "jurisdiction": src.jurisdiction,
                "regulation_code": src.regulation_code,
                "authority": src.authority,
                "title_tr": src.title_tr,
                "title_en": src.title_en,
                "official_url": src.official_url,
                "language": src.language,
                "publication_date": src.publication_date,
                "effective_date": src.effective_date,
                "retrieved_date": src.retrieved_date,
            }
        rules.append(
            {
                "rule_id": rule.rule_id,
                "legal_source_key": src.source_key if src is not None else None,
                "jurisdiction": rule.jurisdiction,
                "regulation_code": rule.regulation_code,
                "article_ref": rule.article_ref,
                "title_tr": rule.title_tr,
                "title_en": rule.title_en,
                "description_tr": rule.description_tr,
                "description_en": rule.description_en,
                "official_source_url": rule.official_source_url,
                "authority": rule.authority,
                "publication_date": rule.publication_date,
                "effective_date": rule.effective_date,
                "retrieved_date": rule.retrieved_date,
                "applicability": loads(rule.applicability),
                "exceptions": loads(rule.exceptions, []) or [],
                "review_triggers": loads(rule.review_triggers, []) or [],
                "condition": loads(rule.condition),
                "evidence_requirements": loads(rule.evidence_requirements, []) or [],
                "deadline_definition": rule.deadline_definition,
                "timezone_rule": rule.timezone_rule,
                "severity": rule.severity,
                "confidence": rule.confidence,
                "requires_human_review": rule.requires_human_review,
            }
        )
    return pack_content_hash(
        {
            "pack_key": pack.pack_key,
            "version": pack.version,
            "schema_version": pack.schema_version,
            "jurisdiction": pack.jurisdiction,
            "regulation_code": pack.regulation_code,
            "title_tr": pack.title_tr,
            "title_en": pack.title_en,
            "description_tr": pack.description_tr,
            "description_en": pack.description_en,
            "retrieved_date": pack.retrieved_date,
            "context_keys": loads(pack.context_keys, []) or [],
            "legal_sources": list(sources.values()),
            "rules": rules,
        }
    )


# ===========================================================================
# Doğrulama
# ===========================================================================
def _check_url(url: Any, where: str, problems: list[str]) -> None:
    if not isinstance(url, str) or not url:
        problems.append(f"{where}: official source URL is missing")
        return
    parsed = urlparse(url)
    if parsed.scheme != "https":
        problems.append(f"{where}: official source URL must use https ({url})")
        return
    if parsed.hostname not in OFFICIAL_SOURCE_HOSTS:
        problems.append(
            f"{where}: host '{parsed.hostname}' is not in the official source "
            f"allowlist {sorted(OFFICIAL_SOURCE_HOSTS)}"
        )


def _check_predicate(node: Any, where: str, problems: list[str]) -> None:
    """
    Yüklemi boş bir bağlamda çalıştırarak yapısal hataları erkenden yakalar.

    Boş bağlam her alanı bilinmeyene çözer; bu yüzden yalnızca *yapısal*
    hatalar (bilinmeyen işleç, boş grup, hatalı düğüm) hata üretir.
    """
    if node is None:
        problems.append(f"{where}: predicate is missing")
        return
    try:
        evaluate_predicate(node, {})
    except ValidationError as exc:
        problems.append(f"{where}: invalid predicate ({exc.message_key} {exc.params})")


def _check_group(group: Any, where: str, problems: list[str]) -> None:
    if group is None:
        return
    if not isinstance(group, list):
        problems.append(f"{where}: must be a list")
        return
    seen: set[str] = set()
    for i, item in enumerate(group):
        label = f"{where}[{i}]"
        if not isinstance(item, Mapping):
            problems.append(f"{label}: must be an object")
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            problems.append(f"{label}: 'key' is required")
        elif key in seen:
            problems.append(f"{label}: duplicate key '{key}'")
        else:
            seen.add(key)
        _check_predicate(item.get("predicate"), f"{label}.predicate", problems)


def validate_pack(data: Any) -> list[str]:
    """
    Paket sözlüğünü doğrular ve bulunan **tüm** sorunları döndürür.

    İlk hatada durmaz: bir paketi düzeltecek kişinin sorunların tamamını tek
    seferde görmesi, aynı dosyayı on kez çalıştırmasından iyidir.
    """
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return ["pack: root must be a JSON object"]

    for key in _PACK_REQUIRED:
        if data.get(key) in (None, "", [], {}):
            problems.append(f"pack: '{key}' is required")

    schema = data.get("schema_version")
    if schema is not None and schema not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"pack: unsupported schema_version '{schema}' "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    if _iso(data.get("retrieved_date")) is None:
        problems.append("pack: 'retrieved_date' must be an ISO date")

    context_keys = data.get("context_keys")
    if context_keys is not None and not isinstance(context_keys, list):
        problems.append("pack: 'context_keys' must be a list")
        context_keys = []
    declared = set(context_keys or []) | {"tenant_id", "evidence"}

    # --- Kaynaklar --------------------------------------------------------
    sources = data.get("legal_sources")
    source_keys: set[str] = set()
    if not isinstance(sources, list) or not sources:
        problems.append("pack: 'legal_sources' must be a non-empty list")
        sources = []
    for i, src in enumerate(sources):
        where = f"legal_sources[{i}]"
        if not isinstance(src, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        for key in _SOURCE_REQUIRED:
            if src.get(key) in (None, ""):
                problems.append(f"{where}: '{key}' is required")
        key = src.get("source_key")
        if isinstance(key, str) and key:
            if key in source_keys:
                problems.append(f"{where}: duplicate source_key '{key}'")
            source_keys.add(key)
        _check_url(src.get("official_url"), where, problems)
        if _iso(src.get("retrieved_date")) is None:
            problems.append(f"{where}: 'retrieved_date' must be an ISO date")

    # --- Kurallar ---------------------------------------------------------
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        problems.append("pack: 'rules' must be a non-empty list")
        rules = []
    if len(rules) > _MAX_RULES_PER_PACK:
        problems.append(
            f"pack: {len(rules)} rules exceeds the {_MAX_RULES_PER_PACK} limit"
        )

    rule_ids: set[str] = set()
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        rid = rule.get("rule_id")
        if isinstance(rid, str) and rid:
            where = f"rule {rid}"
            if rid in rule_ids:
                problems.append(f"{where}: duplicate rule_id")
            rule_ids.add(rid)
        for key in _RULE_REQUIRED:
            if rule.get(key) in (None, ""):
                problems.append(f"{where}: '{key}' is required")
        if not isinstance(rule.get("requires_human_review"), bool):
            problems.append(f"{where}: 'requires_human_review' must be a boolean")

        src_key = rule.get("legal_source_key")
        if isinstance(src_key, str) and src_key and src_key not in source_keys:
            problems.append(f"{where}: unknown legal_source_key '{src_key}'")

        if rule.get("severity") not in _SEVERITIES:
            problems.append(f"{where}: severity must be one of {sorted(_SEVERITIES)}")
        if rule.get("confidence") not in _CONFIDENCES:
            problems.append(
                f"{where}: confidence must be one of {sorted(_CONFIDENCES)}"
            )
        if rule.get("official_source_url") is not None:
            _check_url(rule.get("official_source_url"), where, problems)
        if _iso(rule.get("retrieved_date") or data.get("retrieved_date")) is None:
            problems.append(f"{where}: 'retrieved_date' must be an ISO date")

        _check_predicate(rule.get("applicability"), f"{where}.applicability", problems)
        _check_predicate(rule.get("condition"), f"{where}.condition", problems)
        _check_group(rule.get("exceptions"), f"{where}.exceptions", problems)
        _check_group(rule.get("review_triggers"), f"{where}.review_triggers", problems)

        evidence = rule.get("evidence_requirements")
        if evidence is not None and not isinstance(evidence, list):
            problems.append(f"{where}: 'evidence_requirements' must be a list")
        elif isinstance(evidence, list):
            for j, req in enumerate(evidence):
                if isinstance(req, str) and req:
                    continue
                if not isinstance(req, Mapping) or not req.get("key"):
                    problems.append(
                        f"{where}.evidence_requirements[{j}]: 'key' is required"
                    )

        # Yazım hatası taşıyan bir alan adı sessizce "bilinmiyor" üretir ve
        # kural hiç çalışmamış olur; bu yüzden atıf yapılan her alan paketin
        # ilan ettiği sözlükte bulunmalıdır.
        used: set[str] = set()
        for node in (rule.get("applicability"), rule.get("condition")):
            used |= collect_fields(node)
        for group in (rule.get("exceptions") or [], rule.get("review_triggers") or []):
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, Mapping):
                        used |= collect_fields(item.get("predicate"))
        for field in sorted(used):
            root = field.split(".")[0]
            if root not in declared:
                problems.append(
                    f"{where}: field '{field}' is not declared in pack context_keys"
                )

    return problems


# ===========================================================================
# Dosya okuma
# ===========================================================================
def discover_pack_files(root: Path | None = None) -> list[Path]:
    """``rulepacks/`` altındaki tüm ``*.json`` dosyaları, kararlı sırayla."""
    base = root or PACKS_ROOT
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.json") if p.is_file())


def load_pack_file(path: Path) -> dict[str, Any]:
    """
    Paket dosyasını okur ve doğrular.

    Kodlama açıkça UTF-8 verilir: Türkçe Windows kurulumlarında varsayılan
    kod sayfası cp1254'tür ve paket metinlerini bozarak özeti sessizce
    değiştirir.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NotFoundError(
            "compliance.rulepack.file_unreadable", params={"path": str(path)}
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "compliance.rulepack.invalid_json",
            params={"path": str(path), "error": str(exc)},
        ) from exc

    problems = validate_pack(data)
    if problems:
        raise ValidationError(
            "compliance.rulepack.validation_failed",
            params={"path": str(path), "problem_count": len(problems)},
            detail="; ".join(problems[:20]),
        )
    return data


# ===========================================================================
# İçe alma
# ===========================================================================
def _upsert_source(
    db: Session, src: Mapping[str, Any], *, user_id: int | None
) -> LegalSource:
    key = str(src["source_key"])
    version = str(src.get("version") or "1")
    digest = source_hash(src)

    existing = db.execute(
        select(LegalSource)
        .where(LegalSource.source_key == key)
        .where(LegalSource.version == version)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.source_hash != digest:
            # Aynı anahtar+sürüm iki farklı içeriği tarif edemez; sessizce
            # üzerine yazmak, onaylanmış paketlerin dayanağını değiştirirdi.
            raise ConflictError(
                "compliance.legal_source.hash_conflict",
                params={"source_key": key, "version": version},
            )
        return existing

    row = LegalSource(
        source_key=key,
        version=version,
        jurisdiction=str(src.get("jurisdiction") or UNKNOWN),
        regulation_code=str(src["regulation_code"]),
        authority=str(src["authority"]),
        title_tr=str(src["title_tr"]),
        title_en=str(src["title_en"]),
        official_url=str(src["official_url"]),
        language=str(src.get("language") or "tr"),
        publication_date=parse_date(src.get("publication_date")),
        effective_date=parse_date(src.get("effective_date")),
        retrieved_date=parse_date(src.get("retrieved_date")),
        source_hash=digest,
        content_hash_kind=str(SourceHashKind.REFERENCE_FINGERPRINT),
        verification_status=str(SourceVerification.UNVERIFIED),
        notes_tr=src.get("notes_tr"),
        notes_en=src.get("notes_en"),
        created_by_id=user_id,
    )
    db.add(row)
    db.flush()
    return row


def import_pack(
    db: Session,
    data: Mapping[str, Any],
    *,
    imported_by_id: int | None = None,
    source_path: str | None = None,
    on_conflict: str = "error",
    audit: bool = False,
) -> RulePack:
    """
    Doğrulanmış bir paket sözlüğünü **taslak** olarak veritabanına alır.

    Dosyada hangi durum yazarsa yazsın sonuç ``DRAFT``tır; yürürlüğe alma
    yalnızca :func:`activate` üzerinden ve insan onayıyla mümkündür.

    ``on_conflict``: ``"error"`` (varsayılan), ``"skip"`` — mevcut paketi
    döndürür, ``"replace_draft"`` — yalnızca taslak bir paketin kurallarını
    yeniler. Onaylanmış ya da yürürlükteki bir paket hiçbir koşulda üzerine
    yazılmaz.
    """
    problems = validate_pack(data)
    if problems:
        raise ValidationError(
            "compliance.rulepack.validation_failed",
            params={"problem_count": len(problems)},
            detail="; ".join(problems[:20]),
        )

    # Devralınan varsayılanlar burada bir kez yazılır; satırlara giden değerler
    # ile özete giren değerler böylece aynı sözlükten okunur.
    data = normalise_pack(data)

    declared_status = str(data.get("status") or LifecycleStatus.DRAFT)
    if declared_status != LifecycleStatus.DRAFT:
        log.warning(
            "Rulepack %s@%s declares status=%s in the file; imported as DRAFT.",
            data.get("pack_key"), data.get("version"), declared_status,
        )

    pack_key = str(data["pack_key"])
    version = str(data["version"])
    content_hash = pack_content_hash(data)

    existing = db.execute(
        select(RulePack)
        .where(RulePack.pack_key == pack_key)
        .where(RulePack.version == version)
    ).scalar_one_or_none()

    if existing is not None:
        if on_conflict == "skip":
            return existing
        if on_conflict != "replace_draft":
            raise ConflictError(
                "compliance.rulepack.already_exists",
                params={"pack_key": pack_key, "version": version},
            )
        if existing.status != LifecycleStatus.DRAFT:
            raise BusinessRuleError(
                "compliance.rulepack.only_draft_can_be_replaced",
                params={"pack_key": pack_key, "version": version,
                        "status": existing.status},
            )
        existing.rules.clear()
        db.flush()
        pack = existing
    else:
        pack = RulePack(pack_key=pack_key, version=version, content_hash=content_hash)
        db.add(pack)

    pack.schema_version = str(data["schema_version"])
    pack.jurisdiction = str(data.get("jurisdiction") or UNKNOWN)
    pack.regulation_code = str(data["regulation_code"])
    pack.title_tr = str(data["title_tr"])
    pack.title_en = str(data["title_en"])
    pack.description_tr = data.get("description_tr")
    pack.description_en = data.get("description_en")
    pack.status = str(LifecycleStatus.DRAFT)
    pack.content_hash = content_hash
    pack.source_hash = pack_source_hash(data)
    pack.context_keys = dumps(sorted(data.get("context_keys") or []))
    pack.retrieved_date = parse_date(data.get("retrieved_date"))
    pack.effective_from = parse_date(data.get("effective_from"))
    pack.effective_to = parse_date(data.get("effective_to"))
    pack.requires_human_review = bool(data.get("requires_human_review", True))
    pack.imported_from = source_path
    pack.created_by_id = pack.created_by_id or imported_by_id
    pack.updated_by_id = imported_by_id
    db.flush()

    sources = {
        str(s["source_key"]): _upsert_source(db, s, user_id=imported_by_id)
        for s in data["legal_sources"]
    }
    src_hashes = {str(s["source_key"]): source_hash(s) for s in data["legal_sources"]}

    for item in data["rules"]:
        key = str(item["legal_source_key"])
        src = sources[key]
        digest = src_hashes[key]
        db.add(
            Rule(
                rulepack_id=pack.id,
                legal_source_id=src.id,
                jurisdiction=str(item.get("jurisdiction") or pack.jurisdiction),
                regulation_code=str(
                    item.get("regulation_code") or pack.regulation_code
                ),
                rule_id=str(item["rule_id"]),
                title_tr=str(item["title_tr"]),
                title_en=str(item["title_en"]),
                description_tr=str(item["description_tr"]),
                description_en=str(item["description_en"]),
                official_source_url=str(
                    item.get("official_source_url") or src.official_url
                ),
                authority=str(item.get("authority") or src.authority),
                article_ref=str(item.get("article_ref") or UNKNOWN),
                publication_date=parse_date(
                    item.get("publication_date") or src.publication_date
                ),
                effective_date=parse_date(
                    item.get("effective_date") or src.effective_date
                ),
                retrieved_date=parse_date(
                    item.get("retrieved_date") or data.get("retrieved_date")
                ),
                source_hash=digest,
                content_hash=rule_hash(item, src_hash=digest),
                rulepack_version=pack.version,
                status=str(LifecycleStatus.DRAFT),
                applicability=dumps(item.get("applicability")),
                exceptions=dumps(item.get("exceptions") or []),
                review_triggers=dumps(item.get("review_triggers") or []),
                condition=dumps(item.get("condition")),
                evidence_requirements=dumps(item.get("evidence_requirements") or []),
                deadline_definition=item.get("deadline_definition"),
                timezone_rule=item.get("timezone_rule"),
                severity=str(item["severity"]),
                confidence=str(item["confidence"]),
                requires_human_review=bool(item["requires_human_review"]),
                notes_tr=item.get("notes_tr"),
                notes_en=item.get("notes_en"),
                created_by_id=imported_by_id,
            )
        )

    pack.rule_count = len(data["rules"])
    db.flush()

    if audit:
        _audit(
            db, "RULEPACK_IMPORT", pack,
            user_id=imported_by_id,
            summary=f"Imported {pack.rule_count} rules as DRAFT",
        )
    log.info(
        "Imported rulepack %s@%s with %d rules (content_hash=%s)",
        pack.pack_key, pack.version, pack.rule_count, pack.content_hash[:12],
    )
    return pack


def import_from_file(
    db: Session, path: Path, *, imported_by_id: int | None = None, **kwargs: Any
) -> RulePack:
    data = load_pack_file(path)
    return import_pack(
        db, data, imported_by_id=imported_by_id, source_path=str(path), **kwargs
    )


def import_all(
    db: Session,
    *,
    root: Path | None = None,
    imported_by_id: int | None = None,
    on_conflict: str = "skip",
) -> list[RulePack]:
    """Depodaki tüm paket dosyalarını taslak olarak alır."""
    return [
        import_from_file(
            db, path, imported_by_id=imported_by_id, on_conflict=on_conflict
        )
        for path in discover_pack_files(root)
    ]


# ===========================================================================
# Yaşam döngüsü
# ===========================================================================
def _transition(pack: RulePack, target: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(pack.status, frozenset())
    if target not in allowed:
        raise BusinessRuleError(
            "compliance.rulepack.illegal_transition",
            params={
                "pack_key": pack.pack_key,
                "from": pack.status,
                "to": str(target),
                "allowed": sorted(allowed),
            },
        )
    pack.status = str(target)


def submit_for_review(
    db: Session, pack: RulePack, *, user_id: int, audit: bool = False
) -> RulePack:
    """Paketi incelemeye gönderir ve gönderen kişiyi kaydeder."""
    _transition(pack, LifecycleStatus.IN_REVIEW)
    pack.updated_by_id = user_id
    db.flush()
    if audit:
        _audit(db, "RULEPACK_REVIEW", pack, user_id=user_id,
               summary="Submitted for human review")
    return pack


def record_decision(
    db: Session,
    pack: RulePack,
    *,
    approver_id: int,
    decision: str = ApprovalDecision.APPROVED,
    approver_role: str = ApproverRole.DPO,
    approver_name: str | None = None,
    submitted_by_id: int | None = None,
    comment: str | None = None,
    evidence_url: str | None = None,
    audit: bool = False,
) -> RuleApproval:
    """
    Bir insan kararını kaydeder.

    Onay, paketin **o andaki** ``content_hash``ı üzerine verilir. Paket
    sonradan değişirse bu onay artık onu kapsamaz — yeniden onay gerekir.

    Dört göz kuralı burada uygulanır: paketi hazırlayan ya da incelemeye
    gönderen kişi, kendi işini onaylayamaz.
    """
    if pack.status not in (LifecycleStatus.IN_REVIEW, LifecycleStatus.APPROVED):
        raise BusinessRuleError(
            "compliance.rulepack.not_in_review",
            params={"pack_key": pack.pack_key, "status": pack.status},
        )

    submitter = submitted_by_id if submitted_by_id is not None else pack.created_by_id
    if submitter is not None and int(submitter) == int(approver_id):
        raise BusinessRuleError(
            "compliance.rulepack.approver_cannot_be_submitter",
            params={"pack_key": pack.pack_key, "user_id": approver_id},
        )

    approval = RuleApproval(
        rulepack_id=pack.id,
        rule_pk=None,
        decision=str(decision),
        approver_role=str(approver_role),
        approver_id=int(approver_id),
        approver_name=approver_name,
        submitted_by_id=submitter,
        approved_content_hash=pack.content_hash,
        approved_source_hash=pack.source_hash,
        pack_version=pack.version,
        comment=comment,
        evidence_url=evidence_url,
    )
    approval.decided_at = utcnow()
    approval.previous_checksum = _last_approval_checksum(db)
    approval.checksum = sha256_hex(
        f"{approval.previous_checksum or ''}|{_approval_payload(approval)}"
    )
    db.add(approval)
    db.flush()

    if decision == ApprovalDecision.APPROVED:
        if pack.status == LifecycleStatus.IN_REVIEW:
            _transition(pack, LifecycleStatus.APPROVED)
        for rule in pack.rules:
            rule.approver_id = int(approver_id)
            rule.approved_at = approval.decided_at
            rule.status = str(LifecycleStatus.APPROVED)
    elif decision in (ApprovalDecision.REJECTED, ApprovalDecision.CHANGES_REQUESTED):
        # Reddedilen paket taslağa döner; "incelemede" durumunda bırakmak,
        # kimsenin beklemediği bir kuyrukta unutulmasına yol açar.
        if pack.status == LifecycleStatus.IN_REVIEW:
            _transition(pack, LifecycleStatus.DRAFT)

    db.flush()
    if audit:
        _audit(
            db, "RULEPACK_DECISION", pack, user_id=approver_id,
            summary=f"{decision} by {approver_role} on {pack.content_hash[:12]}",
        )
    log.info(
        "Rulepack %s@%s decision=%s approver=%s hash=%s",
        pack.pack_key, pack.version, decision, approver_id,
        pack.content_hash[:12],
    )
    return approval


def effective_approvals(db: Session, pack: RulePack) -> list[RuleApproval]:
    """Paketin güncel içeriğini kapsayan, geri alınmamış onaylar."""
    return list(
        db.execute(
            select(RuleApproval)
            .where(RuleApproval.rulepack_id == pack.id)
            .where(RuleApproval.rule_pk.is_(None))
            .where(RuleApproval.decision == str(ApprovalDecision.APPROVED))
            .where(RuleApproval.is_revoked.is_(False))
            .where(RuleApproval.approved_content_hash == pack.content_hash)
            .order_by(RuleApproval.id.asc())
        ).scalars()
    )


def activate(
    db: Session,
    pack: RulePack,
    *,
    activated_by_id: int,
    supersede_previous: bool = True,
    audit: bool = False,
) -> RulePack:
    """
    Paketi yürürlüğe alır.

    Üç kapıdan geçmeden hiçbir paket ``ACTIVE`` olamaz:

    1.  Durum ``APPROVED`` olmalıdır.
    2.  Saklanan ``content_hash``, satırlardan **yeniden hesaplanan** özetle
        birebir aynı olmalıdır — arada doğrudan SQL ile düzenleme yapılmışsa
        aktivasyon durur.
    3.  Bu özeti kapsayan, geri alınmamış bir insan onayı bulunmalıdır.
    """
    if pack.status != LifecycleStatus.APPROVED:
        raise BusinessRuleError(
            "compliance.rulepack.activation_requires_approved_status",
            params={"pack_key": pack.pack_key, "status": pack.status},
        )

    recomputed = content_hash_of_stored_pack(pack)
    if recomputed != pack.content_hash:
        raise BusinessRuleError(
            "compliance.rulepack.content_hash_mismatch",
            params={
                "pack_key": pack.pack_key,
                "stored": pack.content_hash[:12],
                "recomputed": recomputed[:12],
            },
            detail=(
                "Stored content_hash no longer describes the pack's rules; "
                "the pack must be re-reviewed and re-approved."
            ),
        )

    if not effective_approvals(db, pack):
        raise BusinessRuleError(
            "compliance.rulepack.activation_requires_human_approval",
            params={"pack_key": pack.pack_key, "version": pack.version},
        )

    if supersede_previous:
        previous = db.execute(
            select(RulePack)
            .where(RulePack.pack_key == pack.pack_key)
            .where(RulePack.status == str(LifecycleStatus.ACTIVE))
            .where(RulePack.id != pack.id)
        ).scalars().all()
        for old in previous:
            _transition(old, LifecycleStatus.SUPERSEDED)
            for rule in old.rules:
                rule.status = str(LifecycleStatus.SUPERSEDED)
            pack.supersedes_id = pack.supersedes_id or old.id
        db.flush()

    _transition(pack, LifecycleStatus.ACTIVE)
    pack.activated_at = utcnow()
    pack.activated_by_id = activated_by_id
    pack.updated_by_id = activated_by_id
    for rule in pack.rules:
        rule.status = str(LifecycleStatus.ACTIVE)
    db.flush()

    if audit:
        _audit(db, "RULEPACK_ACTIVATE", pack, user_id=activated_by_id,
               summary=f"Activated {pack.rule_count} rules")
    log.info("Rulepack %s@%s ACTIVE", pack.pack_key, pack.version)
    return pack


def revoke_approval(
    db: Session,
    approval: RuleApproval,
    *,
    user_id: int,
    reason: str,
    audit: bool = False,
) -> RuleApproval:
    """
    Bir onayı geri alır ve varsa yürürlükteki paketi ``WITHDRAWN`` yapar.

    Onayı geri alıp paketi yürürlükte bırakmak, dayanağı olmayan bir kuralın
    çalışmaya devam etmesi demek olurdu.
    """
    if approval.is_revoked:
        return approval
    approval.is_revoked = True
    approval.revoked_at = utcnow()
    approval.revoked_by_id = user_id
    approval.revoked_reason = reason
    db.flush()

    pack = approval.rulepack
    if pack is not None and pack.status == LifecycleStatus.ACTIVE:
        if not effective_approvals(db, pack):
            withdraw(db, pack, user_id=user_id, reason=f"approval revoked: {reason}")
    if audit:
        _audit(db, "RULEPACK_REVOKE", pack, user_id=user_id, summary=reason)
    return approval


def withdraw(
    db: Session,
    pack: RulePack,
    *,
    user_id: int,
    reason: str,
    audit: bool = False,
) -> RulePack:
    """Paketi yürürlükten kaldırır. Satır silinmez; durum değişir."""
    _transition(pack, LifecycleStatus.WITHDRAWN)
    pack.withdrawn_at = utcnow()
    pack.withdrawn_reason = reason
    pack.updated_by_id = user_id
    for rule in pack.rules:
        rule.status = str(LifecycleStatus.WITHDRAWN)
    db.flush()
    if audit:
        _audit(db, "RULEPACK_WITHDRAW", pack, user_id=user_id, summary=reason)
    return pack


def active_pack(db: Session, *, pack_key: str) -> RulePack | None:
    """Bir anahtar için yürürlükteki paket (varsa)."""
    return db.execute(
        select(RulePack)
        .where(RulePack.pack_key == pack_key)
        .where(RulePack.status == str(LifecycleStatus.ACTIVE))
        .order_by(RulePack.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def active_packs(db: Session, *, jurisdiction: str | None = None) -> list[RulePack]:
    stmt = select(RulePack).where(RulePack.status == str(LifecycleStatus.ACTIVE))
    if jurisdiction:
        stmt = stmt.where(RulePack.jurisdiction == jurisdiction)
    return list(db.execute(stmt.order_by(RulePack.pack_key.asc())).scalars())


# ===========================================================================
# Onay zinciri ve denetim
# ===========================================================================
def _approval_payload(approval: RuleApproval) -> str:
    """Onayın yeniden üretilebilir gösterimi — yalnızca kalıcı sütunlardan."""
    return canonical_json(
        {
            "p": approval.rulepack_id,
            "r": approval.rule_pk,
            "d": approval.decision,
            "ro": approval.approver_role,
            "a": approval.approver_id,
            "s": approval.submitted_by_id,
            "h": approval.approved_content_hash,
            "sh": approval.approved_source_hash,
            "v": approval.pack_version,
            "t": approval.decided_at.isoformat() if approval.decided_at else None,
        }
    )


def _last_approval_checksum(db: Session) -> str | None:
    return db.execute(
        select(RuleApproval.checksum).order_by(RuleApproval.id.desc()).limit(1)
    ).scalar_one_or_none()


def verify_approval_chain(db: Session) -> dict[str, Any]:
    """Onay zincirini yürür; kırık halkayı bulursa satır kimliğini bildirir."""
    previous: str | None = None
    checked = 0
    for row in db.execute(
        select(RuleApproval).order_by(RuleApproval.id.asc())
    ).scalars():
        checked += 1
        if not row.checksum:
            return {"valid": False, "checked": checked, "broken_at": row.id,
                    "reason": "MISSING_HASH"}
        if row.previous_checksum != previous:
            return {"valid": False, "checked": checked, "broken_at": row.id,
                    "reason": "BROKEN_CHAIN"}
        expected = sha256_hex(f"{previous or ''}|{_approval_payload(row)}")
        if expected != row.checksum:
            return {"valid": False, "checked": checked, "broken_at": row.id,
                    "reason": "CONTENT_MISMATCH"}
        previous = row.checksum
    return {"valid": True, "checked": checked, "broken_at": None, "reason": "OK"}


def _audit(
    db: Session,
    action: str,
    pack: RulePack | None,
    *,
    user_id: int | None,
    summary: str,
) -> None:
    """
    Yönetişim eylemini ana denetim zincirine yazar.

    İçe aktarma gecikmeli yapılır: kural motoru, denetim tablosu bulunmayan
    bir bağlamda (ör. şema doğrulama betiği) da içe aktarılabilmelidir.
    """
    from app.services.audit_service import record

    record(
        db,
        action,
        entity_type="cmp_rule_pack",
        entity_id=pack.id if pack is not None else None,
        entity_label=f"{pack.pack_key}@{pack.version}" if pack is not None else None,
        user_id=user_id,
        summary=summary,
        commit=False,
    )


def pack_report(pack: RulePack) -> dict[str, Any]:
    """Paketin insan tarafından okunabilir özeti — raporlar ve API için."""
    return {
        "pack_key": pack.pack_key,
        "version": pack.version,
        "status": pack.status,
        "jurisdiction": pack.jurisdiction,
        "regulation_code": pack.regulation_code,
        "rule_count": pack.rule_count,
        "content_hash": pack.content_hash,
        "source_hash": pack.source_hash,
        "requires_human_review": pack.requires_human_review,
        "retrieved_date": _iso(pack.retrieved_date),
        "activated_at": pack.activated_at.isoformat() if pack.activated_at else None,
        "rules": [
            {
                "rule_id": r.rule_id,
                "article_ref": r.article_ref,
                "severity": r.severity,
                "confidence": r.confidence,
                "requires_human_review": r.requires_human_review,
                "status": r.status,
            }
            for r in sorted(pack.rules, key=lambda r: r.rule_id)
        ],
    }


def declared_context_keys(packs: Iterable[RulePack]) -> list[str]:
    """Verilen paketlerin okuduğu bağlam alanlarının birleşimi."""
    keys: set[str] = set()
    for pack in packs:
        keys |= set(loads(pack.context_keys, []) or [])
    return sorted(keys)


__all__ = [
    "OFFICIAL_SOURCE_HOSTS",
    "PACKS_ROOT",
    "SUPPORTED_SCHEMA_VERSIONS",
    "activate",
    "active_pack",
    "active_packs",
    "content_hash_of_stored_pack",
    "declared_context_keys",
    "discover_pack_files",
    "effective_approvals",
    "import_all",
    "import_from_file",
    "import_pack",
    "load_pack_file",
    "normalise_pack",
    "pack_content_hash",
    "pack_report",
    "pack_source_hash",
    "record_decision",
    "revoke_approval",
    "rule_hash",
    "source_hash",
    "submit_for_review",
    "validate_pack",
    "verify_approval_chain",
    "withdraw",
]
