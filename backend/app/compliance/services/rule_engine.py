"""
Kural değerlendirme motoru.

Motorun tek işi şudur: bir kuralın makine okunur yüklemlerini bir bağlam
sözlüğüne uygulayıp, sonucu geriye izlenebilir bir kayda dönüştürmek.

Üç kural bütün davranışı belirler:

1.  **Bilinmeyen, uygun değildir.** Yüklem dili üç değerlidir
    (``TRUE``/``FALSE``/``UNKNOWN``). Bağlamda bulunmayan ya da ``"UNKNOWN"``
    yazan bir alan karşılaştırmayı ``UNKNOWN``a çözer; ``UNKNOWN`` koşul
    ``INSUFFICIENT_EVIDENCE`` üretir, ``COMPLIANT`` değil.
2.  **Kanıt olmadan uyumluluk olmaz.** ``evidence_requirements`` içindeki
    zorunlu kanıtlardan biri bile sunulmamışsa koşul hiç değerlendirilmez;
    sonuç doğrudan ``INSUFFICIENT_EVIDENCE``tır.
3.  **İnsan onayı gerektiren kural otomatik uyumlu dönemez.**
    ``requires_human_review=True`` olan bir kural, onaylanmamış bir paketteki
    kural ve güven düzeyi düşük bir kural en fazla ``REVIEW_REQUIRED`` döner.

Yüklem dili veri olarak taşınır; ``eval`` ya da başka bir kod yürütme yolu
yoktur. Kural paketleri dışarıdan gelebilen dosyalardır ve hiçbir koşulda kod
çalıştıramamalıdır.

Yüklem biçimleri::

    {"all":  [<yüklem>, ...]}          # Kleene VE
    {"any":  [<yüklem>, ...]}          # Kleene VEYA
    {"none": [<yüklem>, ...]}          # hiçbiri
    {"not":   <yüklem>}
    {"const": true|false}
    {"field": "ad", "op": "<işleç>", "value": <değer>}

İşleçler: ``is_true``, ``is_false``, ``eq``, ``ne``, ``in``, ``not_in``,
``contains``, ``not_contains``, ``gt``, ``gte``, ``lt``, ``lte``, ``exists``,
``missing``, ``is_unknown``, ``non_empty``, ``empty``.

``exists`` / ``missing`` / ``is_unknown`` kasten iki değerlidir: "bu bilgi
var mı?" sorusunun cevabı bilinmez olamaz.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.models.rules import Rule, RuleEvaluation, RulePack
from app.compliance.rule_enums import (
    UNKNOWN_TOKENS,
    ConfidenceLevel,
    EvaluationOutcome,
    EvaluatorKind,
    EvidenceIntegrity,
    LifecycleStatus,
    PredicateResult,
)
from app.core.exceptions import ValidationError
from app.core.logging_config import get_logger
from app.core.utils import dumps, loads
from app.models.base import utcnow

log = get_logger("app.compliance.rule_engine")

#: Motor sürümü her sonuç satırına yazılır. Mantık değişirse eski sonuçların
#: hangi sürümle üretildiği bilinmeden yeniden yorumlanamaz.
ENGINE_VERSION = "2.0.0"

#: Yüklem ağacı derinlik sınırı. Dışarıdan gelen bir paket dosyası, yüz binlerce
#: iç içe düğümle yığını taşırmaya çalışabilir; sınır bunu değerlendirme
#: başlamadan durdurur.
MAX_PREDICATE_DEPTH = 32

#: Anlık görüntüde saklanan metinlerin üst sınırı. Bağlam serbest metin
#: taşıyabilir ve kanıt kaydı uğruna kişisel veri biriktirmek istemiyoruz.
SNAPSHOT_TEXT_LIMIT = 120

TRUE = PredicateResult.TRUE
FALSE = PredicateResult.FALSE
UNKNOWN_R = PredicateResult.UNKNOWN


# ===========================================================================
# Kanonik JSON ve özet
# ===========================================================================
def canonical_json(obj: Any) -> str:
    """
    Yeniden üretilebilir JSON gösterimi.

    Anahtarlar sıralanır ve ayraçlardan boşluk atılır; aynı içerik her zaman
    aynı baytları verir. Özet (hash) bunun üzerine kurulduğu için sözlük
    sırasının değişmesi imzayı bozmamalıdır.
    """

    def _default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, (set, frozenset)):
            return sorted(str(v) for v in value)
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_default,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_object(obj: Any) -> str:
    """Bir Python nesnesinin kanonik JSON'u üzerinden SHA-256 özeti."""
    return sha256_hex(canonical_json(obj))


# ===========================================================================
# Bağlam çözümleme
# ===========================================================================
def _is_unknown_value(value: Any) -> bool:
    """``None`` ve "UNKNOWN" ailesinden dizgeler bilinmeyen sayılır."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in UNKNOWN_TOKENS:
        return True
    return False


def _resolve(context: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """
    Noktalı yolu bağlamda çözer: ``("evidence.ropa.ref")``.

    ``(bulundu_mu, deger)`` döner. "Bulunamadı" ile "bulundu ama None"
    ayrımı korunur; ikisi de bilinmeyene çözülse de gerekçe kodları farklıdır.
    """
    current: Any = context
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        return False, None
    return True, current


def _as_decimal(value: Any) -> Decimal | None:
    """Karşılaştırılabilir sayıya çevirir; çeviremezse ``None``."""
    if isinstance(value, bool):
        # bool, int'in alt sınıfıdır; True > 0 karşılaştırması sessiz saçmalık
        # üretir, bu yüzden sayısal işleçlerde kabul edilmez.
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


# ===========================================================================
# Yüklem değerlendirme (Kleene üç değerli mantık)
# ===========================================================================
def _kleene_and(results: Iterable[str]) -> str:
    """Bir tane FALSE her şeyi düşürür; yoksa bir tane UNKNOWN sonucu belirsizleştirir."""
    seen_unknown = False
    for r in results:
        if r == FALSE:
            return FALSE
        if r == UNKNOWN_R:
            seen_unknown = True
    return UNKNOWN_R if seen_unknown else TRUE


def _kleene_or(results: Iterable[str]) -> str:
    seen_unknown = False
    for r in results:
        if r == TRUE:
            return TRUE
        if r == UNKNOWN_R:
            seen_unknown = True
    return UNKNOWN_R if seen_unknown else FALSE


def _negate(result: str) -> str:
    if result == TRUE:
        return FALSE
    if result == FALSE:
        return TRUE
    return UNKNOWN_R


def _compare(op: str, found: bool, value: Any, expected: Any) -> str:
    # Varlık işleçleri kasten iki değerlidir: "bu bilgi var mı?" sorusunun
    # cevabı bilinmez olamaz, yoksa hiçbir kural kanıt eksikliğini
    # kesin biçimde saptayamaz.
    unknown = not found or _is_unknown_value(value)
    if op == "exists":
        return FALSE if unknown else TRUE
    if op == "missing":
        return TRUE if unknown else FALSE
    if op == "is_unknown":
        return TRUE if unknown else FALSE

    if unknown:
        return UNKNOWN_R

    if op == "is_true":
        return TRUE if value is True else FALSE
    if op == "is_false":
        return TRUE if value is False else FALSE
    if op == "non_empty":
        return FALSE if _is_empty(value) else TRUE
    if op == "empty":
        return TRUE if _is_empty(value) else FALSE
    if op == "eq":
        return TRUE if value == expected else FALSE
    if op == "ne":
        return TRUE if value != expected else FALSE
    if op == "in":
        if not isinstance(expected, (list, tuple, set)):
            raise ValidationError(
                "compliance.rule.predicate_value_must_be_list",
                params={"op": op},
            )
        return TRUE if value in expected else FALSE
    if op == "not_in":
        if not isinstance(expected, (list, tuple, set)):
            raise ValidationError(
                "compliance.rule.predicate_value_must_be_list",
                params={"op": op},
            )
        return FALSE if value in expected else TRUE
    if op in ("contains", "not_contains"):
        if isinstance(value, str):
            hit = isinstance(expected, str) and expected in value
        elif isinstance(value, (list, tuple, set)):
            hit = expected in value
        elif isinstance(value, Mapping):
            hit = expected in value
        else:
            return UNKNOWN_R
        if op == "contains":
            return TRUE if hit else FALSE
        return FALSE if hit else TRUE
    if op in ("gt", "gte", "lt", "lte"):
        left, right = _as_decimal(value), _as_decimal(expected)
        if left is None or right is None:
            # Sayı olmayan bir değeri sayısal işleçle kıyaslamak bir paket
            # hatasıdır; sessizce FALSE dönmek kuralı yanlış tarafa düşürür.
            return UNKNOWN_R
        if op == "gt":
            return TRUE if left > right else FALSE
        if op == "gte":
            return TRUE if left >= right else FALSE
        if op == "lt":
            return TRUE if left < right else FALSE
        return TRUE if left <= right else FALSE

    raise ValidationError(
        "compliance.rule.unsupported_operator", params={"op": op}
    )


SUPPORTED_OPERATORS: frozenset[str] = frozenset(
    {
        "is_true", "is_false", "eq", "ne", "in", "not_in",
        "contains", "not_contains", "gt", "gte", "lt", "lte",
        "exists", "missing", "is_unknown", "non_empty", "empty",
    }
)


def evaluate_predicate(
    node: Any, context: Mapping[str, Any], *, _depth: int = 0
) -> str:
    """
    Bir yüklem düğümünü üç değerli mantıkla değerlendirir.

    Yüklem yoksa (``None``) sonuç ``UNKNOWN``dır — "koşul tanımlanmamış" ile
    "koşul sağlandı" aynı şey değildir ve ikincisini varsaymak, boş bir kuralı
    otomatik uyumlu yapardı.
    """
    if node is None:
        return UNKNOWN_R
    if _depth > MAX_PREDICATE_DEPTH:
        raise ValidationError(
            "compliance.rule.predicate_too_deep",
            params={"max_depth": MAX_PREDICATE_DEPTH},
        )
    if isinstance(node, bool):
        return TRUE if node else FALSE
    if not isinstance(node, Mapping):
        raise ValidationError(
            "compliance.rule.invalid_predicate",
            params={"node_type": type(node).__name__},
        )

    if "const" in node:
        return TRUE if bool(node["const"]) else FALSE
    if "all" in node:
        return _kleene_and(
            evaluate_predicate(c, context, _depth=_depth + 1)
            for c in _as_children(node["all"])
        )
    if "any" in node:
        return _kleene_or(
            evaluate_predicate(c, context, _depth=_depth + 1)
            for c in _as_children(node["any"])
        )
    if "none" in node:
        return _negate(
            _kleene_or(
                evaluate_predicate(c, context, _depth=_depth + 1)
                for c in _as_children(node["none"])
            )
        )
    if "not" in node:
        return _negate(evaluate_predicate(node["not"], context, _depth=_depth + 1))

    field = node.get("field")
    if not isinstance(field, str) or not field:
        raise ValidationError("compliance.rule.predicate_missing_field")
    op = node.get("op", "is_true")
    if op not in SUPPORTED_OPERATORS:
        raise ValidationError("compliance.rule.unsupported_operator", params={"op": op})

    found, value = _resolve(context, field)
    return _compare(op, found, value, node.get("value"))


def _as_children(value: Any) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError("compliance.rule.empty_predicate_group")
    return value


def collect_fields(node: Any, *, _depth: int = 0) -> set[str]:
    """
    Bir yüklem ağacının okuduğu bağlam alanlarının tamamı.

    Yükleyici bunu, kuralların atıf yaptığı her alanın paketin ilan ettiği
    ``context_keys`` listesinde bulunduğunu doğrulamak için kullanır: yazım
    hatası taşıyan bir alan adı, aksi hâlde sessizce "bilinmiyor" üretir ve
    kural hiç çalışmamış olur.
    """
    if node is None or isinstance(node, bool) or _depth > MAX_PREDICATE_DEPTH:
        return set()
    if not isinstance(node, Mapping):
        return set()
    out: set[str] = set()
    for key in ("all", "any", "none"):
        if key in node and isinstance(node[key], (list, tuple)):
            for child in node[key]:
                out |= collect_fields(child, _depth=_depth + 1)
    if "not" in node:
        out |= collect_fields(node["not"], _depth=_depth + 1)
    field = node.get("field")
    if isinstance(field, str) and field:
        out.add(field)
    return out


def rule_fields(rule: Rule) -> set[str]:
    """Kuralın tüm yüklemlerinde geçen bağlam alanları."""
    out: set[str] = set()
    out |= collect_fields(loads(rule.applicability))
    out |= collect_fields(loads(rule.condition))
    for group in (loads(rule.exceptions, []) or [], loads(rule.review_triggers, []) or []):
        for item in group:
            if isinstance(item, Mapping):
                out |= collect_fields(item.get("predicate"))
    return out


# ===========================================================================
# Kanıt
# ===========================================================================
def _evidence_map(context: Mapping[str, Any]) -> Mapping[str, Any]:
    found, value = _resolve(context, "evidence")
    if found and isinstance(value, Mapping):
        return value
    return {}


def _evidence_ref(key: str, value: Any) -> dict[str, Any]:
    """
    Kanıttan yalnızca **referansı** alır, içeriğini değil.

    Kanıt gövdesi sözleşme metni, ekran görüntüsü yolu veya yazışma olabilir;
    bunları sonuç satırına kopyalamak, uyumluluk kaydını yeni bir kişisel veri
    yığınına çevirirdi.
    """
    ref: Any = None
    if isinstance(value, Mapping):
        for candidate in ("ref", "id", "url", "hash", "document_id"):
            if candidate in value and isinstance(value[candidate], (str, int)):
                ref = value[candidate]
                break
    elif isinstance(value, (str, int)):
        ref = value
    if isinstance(ref, str):
        ref = ref[:SNAPSHOT_TEXT_LIMIT]
    return {"key": key, "ref": ref}


def check_evidence(
    requirements: Any, context: Mapping[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """``(eksik_anahtarlar, sunulan_referanslar)``."""
    missing: list[str] = []
    refs: list[dict[str, Any]] = []
    if not isinstance(requirements, (list, tuple)):
        return missing, refs

    provided = _evidence_map(context)
    for req in requirements:
        if isinstance(req, str):
            key, optional = req, False
        elif isinstance(req, Mapping):
            key = req.get("key")
            optional = bool(req.get("optional", False))
        else:
            continue
        if not isinstance(key, str) or not key:
            continue
        value = provided.get(key)
        if key not in provided or _is_unknown_value(value) or _is_empty(value):
            if not optional:
                missing.append(key)
            continue
        refs.append(_evidence_ref(key, value))
    return missing, refs


# ===========================================================================
# İstisnalar ve inceleme tetikleyicileri
# ===========================================================================
def _match_group(group: Any, context: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """
    ``(kesin_eşleşenler, belirsizler)``.

    Belirsiz bir istisna yok sayılmaz: istisnanın uygulanıp uygulanmadığı
    bilinmiyorsa, kuralın sonucu da kesin olamaz.
    """
    matched: list[str] = []
    uncertain: list[str] = []
    if not isinstance(group, (list, tuple)):
        return matched, uncertain
    for item in group:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key") or item.get("id")
        if not isinstance(key, str) or not key:
            continue
        result = evaluate_predicate(item.get("predicate"), context)
        if result == TRUE:
            matched.append(key)
        elif result == UNKNOWN_R:
            uncertain.append(key)
    return matched, uncertain


# ===========================================================================
# Anlık görüntü
# ===========================================================================
def _snapshot_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return value[:SNAPSHOT_TEXT_LIMIT]
    # Yapısal değerler kanıt satırına kopyalanmaz; yalnızca türü kaydedilir.
    return f"<{type(value).__name__}>"


def build_snapshot(fields: Iterable[str], context: Mapping[str, Any]) -> dict[str, Any]:
    """Yalnızca kuralın okuduğu alanların ilkel değerleri."""
    out: dict[str, Any] = {}
    for field in sorted(set(fields)):
        found, value = _resolve(context, field)
        out[field] = _snapshot_value(value) if found else None
    return out


# ===========================================================================
# Sonuç türetme
# ===========================================================================
def _pack_is_binding(pack: RulePack | None) -> bool:
    """
    Paket, otomatik ``COMPLIANT`` üretmeye yetecek kadar olgun mu?

    Onaylanmamış bir paket değerlendirilebilir — taslakla prova yapmak
    gerekir — ama sonucu "uyumlu" diye raporlamak, onaylanmamış bir metni
    yürürlükteymiş gibi göstermek olurdu.
    """
    return pack is not None and pack.status == LifecycleStatus.ACTIVE


def derive_outcome(
    *,
    rule: Rule,
    pack: RulePack | None,
    applicability: str,
    condition: str,
    missing_evidence: Sequence[str],
    matched_exceptions: Sequence[str],
    uncertain_exceptions: Sequence[str],
    triggered_reviews: Sequence[str],
) -> tuple[str, list[str]]:
    """
    Ham sinyalleri tek bir sonuca indirger. ``(outcome, reason_kodları)``.

    Sıra önemlidir ve bilinçlidir: uygulanabilirlik, istisna, kanıt, koşul.
    Kanıt kontrolü koşuldan önce gelir; aksi hâlde kanıtsız bir bağlamda
    "koşul sağlandı" diyen bir yüklem, uyumluluk iddiası üretirdi.
    """
    reasons: list[str] = []

    if applicability == FALSE:
        return EvaluationOutcome.NOT_APPLICABLE, ["applicability_false"]
    if applicability == UNKNOWN_R:
        # Kuralın uygulanıp uygulanmadığı bile bilinmiyorsa, uyumluluk
        # hakkında hiçbir şey söylenemez.
        return EvaluationOutcome.INSUFFICIENT_EVIDENCE, ["applicability_unknown"]

    if matched_exceptions:
        return EvaluationOutcome.NOT_APPLICABLE, [
            "exception_matched:" + key for key in matched_exceptions
        ]

    if missing_evidence:
        return EvaluationOutcome.INSUFFICIENT_EVIDENCE, [
            "evidence_missing:" + key for key in missing_evidence
        ]

    if condition == FALSE:
        outcome = EvaluationOutcome.NON_COMPLIANT
        reasons.append("condition_false")
    elif condition == UNKNOWN_R:
        outcome = EvaluationOutcome.INSUFFICIENT_EVIDENCE
        reasons.append("condition_unknown")
    else:
        outcome = EvaluationOutcome.COMPLIANT
        reasons.append("condition_true")

    # --- Otomatik "uyumlu" kararını sınırlayan kapılar --------------------
    # Bunların hiçbiri NON_COMPLIANT sonucunu yumuşatmaz: bir ihlal
    # bulgusunun insan onayı beklemesi için sebep yoktur, tersi doğrudur.
    if outcome == EvaluationOutcome.COMPLIANT:
        if rule.requires_human_review:
            outcome = EvaluationOutcome.REVIEW_REQUIRED
            reasons.append("rule_requires_human_review")
        if not _pack_is_binding(pack):
            outcome = EvaluationOutcome.REVIEW_REQUIRED
            reasons.append("rulepack_not_active")
        if rule.confidence in (ConfidenceLevel.UNKNOWN, ConfidenceLevel.LOW):
            outcome = EvaluationOutcome.REVIEW_REQUIRED
            reasons.append("low_rule_confidence")
        if triggered_reviews:
            outcome = EvaluationOutcome.REVIEW_REQUIRED
            reasons.extend("review_trigger:" + key for key in triggered_reviews)

    # Belirsiz bir istisna hem "uyumlu" hem "ihlal" sonucunu şüpheli kılar:
    # istisna gerçekten uygulanıyorsa kural zaten geçerli değildi.
    if uncertain_exceptions and outcome in (
        EvaluationOutcome.COMPLIANT,
        EvaluationOutcome.NON_COMPLIANT,
    ):
        outcome = EvaluationOutcome.REVIEW_REQUIRED
        reasons.extend("exception_uncertain:" + key for key in uncertain_exceptions)

    if triggered_reviews and outcome == EvaluationOutcome.COMPLIANT:
        outcome = EvaluationOutcome.REVIEW_REQUIRED
        reasons.extend("review_trigger:" + key for key in triggered_reviews)

    return outcome, reasons


# ===========================================================================
# Zincirleme (audit_service deseni)
# ===========================================================================
def _payload(row: RuleEvaluation) -> str:
    """
    Bir sonucun yeniden üretilebilir gösterimi.

    Yalnızca **kalıcı sütunlardan** kurulur — saat okuması, rastgele değer
    yok — ki :func:`verify_chain` özeti sonradan yeniden hesaplayıp, bağı
    değil alanı düzenlenmiş bir satırı da yakalayabilsin.
    """
    return canonical_json(
        {
            "t": row.tenant_id,
            "r": row.rule_id,
            "rp": row.rule_pk,
            "pk": row.rulepack_key,
            "pv": row.rulepack_version,
            "ps": row.rulepack_status,
            "o": row.outcome,
            "sv": row.severity,
            "cf": row.confidence,
            # Sütun varsayılanları flush anında uygulanır; özet flush'tan önce
            # hesaplandığı için None gelen bir bayrak, satır geri okunduğunda
            # False'a dönüşür ve zincir haksız yere kırık görünürdü.
            "hr": bool(row.requires_human_review),
            "ar": row.applicability_result,
            "cr": row.condition_result,
            "me": row.missing_evidence,
            "mx": row.matched_exceptions,
            "tv": row.triggered_reviews,
            "rs": row.reasons,
            "ch": row.context_hash,
            "ev": row.evaluator,
            "en": row.engine_version,
            "ho": bool(row.is_human_override),
            "pe": row.previous_evaluation_id,
            "at": row.evaluated_at.isoformat() if row.evaluated_at else None,
        }
    )


def _compute_checksum(previous: str | None, payload: str) -> str:
    return sha256_hex(f"{previous or ''}|{payload}")


def _last_checksum(db: Session, tenant_id: int) -> str | None:
    """
    Kiracının zincirindeki son özet.

    Zincir kiracı bazındadır: bir kiracının kayıtları başka bir kiracının
    kayıtlarına bağlı olsaydı, tek bir kiracının verisini dışa aktarmak
    diğerlerinin zincirini kırardı.
    """
    return db.execute(
        select(RuleEvaluation.checksum)
        .where(RuleEvaluation.tenant_id == tenant_id)
        .order_by(RuleEvaluation.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def verify_chain(
    db: Session, *, tenant_id: int, limit: int | None = None
) -> dict[str, Any]:
    """
    Kiracının değerlendirme zincirini yürür ve ilk kırık halkayı bildirir.

    ``{"valid", "checked", "broken_at", "reason"}`` döner. ``reason`` değerleri
    :class:`EvidenceIntegrity` üyeleridir.
    """
    stmt = (
        select(RuleEvaluation)
        .where(RuleEvaluation.tenant_id == tenant_id)
        .order_by(RuleEvaluation.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)

    previous: str | None = None
    checked = 0
    for row in db.execute(stmt).scalars():
        checked += 1
        if not row.checksum:
            return _broken(checked, row.id, EvidenceIntegrity.MISSING_HASH)
        if row.previous_checksum != previous:
            return _broken(checked, row.id, EvidenceIntegrity.BROKEN_CHAIN)
        if _compute_checksum(previous, _payload(row)) != row.checksum:
            # Saklanan içerikten yeniden hesaplanır: özet sütununu da yeniden
            # yazarak zinciri görünüşte tutarlı tutan bir düzenlemeyi yakalar.
            return _broken(checked, row.id, EvidenceIntegrity.CONTENT_MISMATCH)
        previous = row.checksum
    return {
        "valid": True,
        "checked": checked,
        "broken_at": None,
        "reason": str(EvidenceIntegrity.OK),
    }


def _broken(checked: int, row_id: int, reason: str) -> dict[str, Any]:
    log.error(
        "Compliance evaluation chain broken at id=%s reason=%s", row_id, reason
    )
    return {
        "valid": False,
        "checked": checked,
        "broken_at": row_id,
        "reason": str(reason),
    }


# ===========================================================================
# Genel API
# ===========================================================================
def _tenant_of(context: Mapping[str, Any], explicit: int | None) -> int:
    tenant_id = explicit if explicit is not None else context.get("tenant_id")
    if tenant_id is None:
        raise ValidationError("compliance.evaluation.tenant_required")
    try:
        return int(tenant_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "compliance.evaluation.tenant_invalid",
            params={"tenant_id": str(tenant_id)},
        ) from exc


def evaluate(
    db: Session,
    *,
    rule: Rule,
    context: Mapping[str, Any],
    tenant_id: int | None = None,
    pack: RulePack | None = None,
    persist: bool = True,
    store_snapshot: bool = True,
    previous_evaluation_id: int | None = None,
) -> RuleEvaluation:
    """
    Tek bir kuralı bir bağlam üzerinde değerlendirir.

    Sonuç satırı oluşturulur, zincirlenir ve (``persist``) oturuma eklenerek
    ``flush`` edilir — **commit edilmez**. Çağıranın işlemi sonucu sahiplenir;
    böylece değerlendirme ile onu tetikleyen iş ya birlikte kalıcı olur ya da
    birlikte geri alınır.

    ``tenant_id`` açıkça verilmezse ``context["tenant_id"]``den okunur; ikisi
    de yoksa hata fırlatılır. Kiracısız bir uyumluluk sonucu yanlış müşteriye
    raporlanabilir.
    """
    if rule is None:
        raise ValidationError("compliance.evaluation.rule_required")
    if not isinstance(context, Mapping):
        raise ValidationError("compliance.evaluation.context_must_be_mapping")

    tenant = _tenant_of(context, tenant_id)
    pack = pack if pack is not None else rule.rulepack

    applicability = evaluate_predicate(loads(rule.applicability), context)
    exceptions = loads(rule.exceptions, []) or []
    matched_exc, uncertain_exc = _match_group(exceptions, context)
    triggers = loads(rule.review_triggers, []) or []
    triggered, uncertain_triggers = _match_group(triggers, context)
    # Belirsiz bir inceleme tetikleyicisi de insanı çağırmak için yeterlidir:
    # "tetiklendi mi bilmiyoruz" hâli, tetiklenmemiş sayılamaz.
    triggered = triggered + uncertain_triggers

    missing_evidence, evidence_refs = check_evidence(
        loads(rule.evidence_requirements, []) or [], context
    )

    # Kanıt eksikse koşul hiç değerlendirilmez; UNKNOWN olarak kaydedilir ki
    # sonuç satırı "koşul sağlanmıştı ama kanıt yoktu" gibi okunmasın.
    if missing_evidence or applicability != TRUE or matched_exc:
        condition = UNKNOWN_R
    else:
        condition = evaluate_predicate(loads(rule.condition), context)

    outcome, reasons = derive_outcome(
        rule=rule,
        pack=pack,
        applicability=applicability,
        condition=condition,
        missing_evidence=missing_evidence,
        matched_exceptions=matched_exc,
        uncertain_exceptions=uncertain_exc,
        triggered_reviews=triggered,
    )

    fields = rule_fields(rule)
    row = RuleEvaluation(
        tenant_id=tenant,
        rule_pk=rule.id,
        rulepack_id=rule.rulepack_id,
        rule_id=rule.rule_id,
        rulepack_key=pack.pack_key if pack is not None else None,
        rulepack_version=rule.rulepack_version,
        rulepack_status=pack.status if pack is not None else None,
        jurisdiction=rule.jurisdiction,
        regulation_code=rule.regulation_code,
        article_ref=rule.article_ref,
        outcome=str(outcome),
        severity=rule.severity,
        confidence=rule.confidence,
        requires_human_review=bool(rule.requires_human_review),
        applicability_result=str(applicability),
        condition_result=str(condition),
        missing_evidence=dumps(missing_evidence) if missing_evidence else None,
        matched_exceptions=dumps(matched_exc) if matched_exc else None,
        triggered_reviews=dumps(triggered) if triggered else None,
        reasons=dumps(reasons),
        evidence_refs=dumps(evidence_refs) if evidence_refs else None,
        context_hash=hash_object(context),
        context_snapshot=(
            dumps(build_snapshot(fields, context)) if store_snapshot else None
        ),
        evaluator=str(EvaluatorKind.ENGINE),
        engine_version=ENGINE_VERSION,
        is_human_override=False,
        previous_evaluation_id=previous_evaluation_id,
    )
    row.evaluated_at = utcnow()

    if persist:
        row.previous_checksum = _last_checksum(db, tenant)
        row.checksum = _compute_checksum(row.previous_checksum, _payload(row))
        db.add(row)
        db.flush()
    else:
        # Kalıcı olmayan sonuçta da özet hesaplanır; çağıran isterse aynı
        # değeri kendi kanıt paketine yazabilsin diye.
        row.checksum = _compute_checksum(None, _payload(row))

    log.info(
        "rule evaluated tenant=%s rule=%s outcome=%s applicability=%s condition=%s",
        tenant, rule.rule_id, outcome, applicability, condition,
    )
    return row


#: ``evaluate_pack`` varsayılan olarak arşivlenmiş kuralları atlar; geri
#: çekilmiş bir kuralı değerlendirmek, yürürlükten kalkmış bir yükümlülüğü
#: rapora sokar.
DEFAULT_SKIPPED_RULE_STATUSES: frozenset[str] = frozenset(
    {LifecycleStatus.SUPERSEDED, LifecycleStatus.WITHDRAWN}
)


def evaluate_pack(
    db: Session,
    *,
    pack: RulePack,
    context: Mapping[str, Any],
    tenant_id: int | None = None,
    persist: bool = True,
    store_snapshot: bool = True,
    skip_statuses: Iterable[str] = DEFAULT_SKIPPED_RULE_STATUSES,
) -> list[RuleEvaluation]:
    """
    Paketteki her kuralı aynı bağlam üzerinde değerlendirir.

    Kurallar ``rule_id`` sırasıyla işlenir; zincirlenmiş sonuç satırlarının
    sırası böylece aynı girdi için yeniden üretilebilir olur.
    """
    if pack is None:
        raise ValidationError("compliance.evaluation.rulepack_required")

    skip = {str(s) for s in skip_statuses}
    rules = sorted(
        (r for r in pack.rules if r.status not in skip),
        key=lambda r: r.rule_id,
    )
    return [
        evaluate(
            db,
            rule=rule,
            context=context,
            tenant_id=tenant_id,
            pack=pack,
            persist=persist,
            store_snapshot=store_snapshot,
        )
        for rule in rules
    ]


def summarise(evaluations: Sequence[RuleEvaluation]) -> dict[str, Any]:
    """
    Sonuç listesinin sayımı.

    ``compliance_score`` gibi tek bir yüzde üretilmez. Kanıtı eksik bir kuralı
    uyumlu bir kuralla aynı paydada toplayan bir skor, eksikliği sayısal
    olarak seyreltir ve raporu okuyanı yanıltır.
    """
    counts = {str(o): 0 for o in EvaluationOutcome}
    for row in evaluations:
        counts[row.outcome] = counts.get(row.outcome, 0) + 1
    needs_attention = (
        counts[str(EvaluationOutcome.NON_COMPLIANT)]
        + counts[str(EvaluationOutcome.INSUFFICIENT_EVIDENCE)]
        + counts[str(EvaluationOutcome.REVIEW_REQUIRED)]
    )
    return {
        "total": len(evaluations),
        "counts": counts,
        "needs_attention": needs_attention,
        "engine_version": ENGINE_VERSION,
    }


__all__ = [
    "ENGINE_VERSION",
    "MAX_PREDICATE_DEPTH",
    "SUPPORTED_OPERATORS",
    "build_snapshot",
    "canonical_json",
    "check_evidence",
    "collect_fields",
    "derive_outcome",
    "evaluate",
    "evaluate_pack",
    "evaluate_predicate",
    "hash_object",
    "rule_fields",
    "sha256_hex",
    "summarise",
    "verify_chain",
]
