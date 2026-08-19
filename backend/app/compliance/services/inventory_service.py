"""
Kişisel veri envanteri servisi.

Keşif tarayıcısı (``app.compliance.scanners.discovery``) repoyu ölçer; bu
modül ölçümü kalıcı, karşılaştırılabilir ve kanıtlanabilir hâle getirir.

Tasarımın özü: **tarayıcı aday üretir, karar üretmez.** Yeni bulunan her alan
``REVIEW_REQUIRED`` durumunda doğar. Bir alanın özel nitelikli aday olarak
işaretlenmesi onu özel nitelikli yapmaz; incelenmemiş olması da onu sıradan
kişisel veri yapmaz.

Tarama **birleştiricidir, silici değildir**. Bir alan artık şemada
görünmüyorsa satırı silinmez; ``is_present`` yanlışa döner ve bulgu insan
incelemesine düşer. Silinen bir sütun, envanterden sessizce kaybolması gereken
bir şey değil, açıklanması gereken bir değişikliktir.

İnsan onaylı bir sınıflandırma tarayıcı tarafından ezilmez. Tarayıcının
sonucu onaylanmış sınıflandırmadan farklıysa alan yeniden incelemeye alınır
ve fark not olarak yazılır — sessizce ne eski karar korunur ne de yeni tahmin
dayatılır.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.enums import (
    ComplianceState,
    DataSensitivity,
    DiscoverySource,
    EvidenceKind,
    IdentifiabilityLevel,
    ReviewStatus,
    TransferMechanism,
)
from app.compliance.models.inventory import DataField, Transfer
from app.compliance.scanners import discovery
from app.compliance.services import evidence_service
from app.core.deps import Ctx
from app.core.exceptions import NotFoundError
from app.core.logging_config import get_logger
from app.core.utils import slugify
from app.models.base import utcnow
from app.services import audit_service

log = get_logger("app.compliance.inventory")

SUBJECT_FIELD = "cmp_data_field"
SUBJECT_SCAN = "cmp_inventory_scan"
SUBJECT_TRANSFER = "cmp_transfer"

#: Tarayıcının Türkçe kategorileri ile hassasiyet sözlüğü arasındaki eşleme.
#: Tarayıcı sözlüğü genişler de burada karşılığı bulunmazsa sonuç ``UNKNOWN``
#: olur; hiçbir koşulda "kişisel değil" varsayılmaz.
_SENSITIVITY_BY_CATEGORY: dict[str, str] = {
    "KISISEL": DataSensitivity.PERSONAL,
    "OZEL_NITELIKLI": DataSensitivity.SPECIAL_CATEGORY,
    "KONUM": DataSensitivity.LOCATION,
}

#: İnsan kararının verildiği kabul edilen durumlar — tarayıcı bunların
#: üzerine yazmaz.
_HUMAN_SETTLED = frozenset({ReviewStatus.ACCEPTED, ReviewStatus.REJECTED})


def _identifiability(is_direct: bool, category: str) -> str:
    """
    Alanın kimliği ne ölçüde belirlediği.

    Konum alanı doğrudan tanımlayıcı sayılmaz ama anonim de değildir: tek bir
    koordinat kişiyi göstermez, koordinat dizisi çoğu zaman gösterir.
    """
    if is_direct:
        return IdentifiabilityLevel.DIRECT
    if category in _SENSITIVITY_BY_CATEGORY:
        return IdentifiabilityLevel.INDIRECT
    return IdentifiabilityLevel.UNKNOWN


def run_scan(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    full: bool = False,
    seed_transfers: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Keşif taramasını çalıştır ve envanteri güncelle.

    ``full=False`` yalnızca ORM alanlarını okur ve hızlıdır. ``full=True``
    bağımlılık lisanslarını, AI sağlayıcılarını ve otomasyon noktalarını da
    ölçer; kurulu paketleri sorguladığı için belirgin biçimde yavaştır ve bu
    yüzden varsayılan değildir.

    Tarayıcı çökerse sonuç ``FAILED`` olarak kanıtlanır. Başarısız bir taramayı
    hiç kaydetmemek, envanteri "değişmedi" göstererek en tehlikeli yanlış
    izlenimi yaratırdı.
    """
    started = utcnow()
    result: dict[str, Any] = {
        "status": "OK",
        "scope": "FULL" if full else "FIELDS",
        "started_at": started,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "disappeared": 0,
        "reclassified": 0,
        "personal": 0,
        "special_candidates": 0,
        "location": 0,
        "direct_identifiers": 0,
        "tables": 0,
        "review_notes": [],
        "summary": {},
        "transfer_candidates": 0,
        "error": None,
    }

    try:
        if full:
            envanter = discovery.envanter_cikar()
            found = list(envanter.personal_data)
            result["review_notes"] = list(envanter.review_required)
            result["summary"] = dict(envanter.ozet)
        else:
            envanter = None
            found, notes = discovery.tara_kisisel_veri()
            result["review_notes"] = list(notes)
    except Exception as exc:
        result["status"] = "FAILED"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = utcnow()
        log.exception("Discovery scan failed for tenant=%s", tenant_id)
        _record_scan_evidence(db, ctx, tenant_id=tenant_id, result=result, note=note)
        return result

    existing = {
        (row.table_name, row.column_name): row
        for row in db.execute(
            select(DataField).where(DataField.tenant_id == tenant_id)
        ).scalars()
    }

    now = utcnow()
    seen: set[tuple[str, str]] = set()
    tables: set[str] = set()

    for item in found:
        key = (item.tablo, item.alan)
        if key in seen:
            # Aynı alan iki model dosyasında görünebilir; envanterde bir kez
            # sayılmalıdır, yoksa "kaç kişisel veri alanı var" sorusu şişer.
            continue
        seen.add(key)
        tables.add(item.tablo)

        sensitivity = _SENSITIVITY_BY_CATEGORY.get(item.kategori, DataSensitivity.UNKNOWN)
        identifiability = _identifiability(item.tanimlayici, item.kategori)
        if sensitivity == DataSensitivity.PERSONAL:
            result["personal"] += 1
        elif sensitivity == DataSensitivity.SPECIAL_CATEGORY:
            result["special_candidates"] += 1
        elif sensitivity == DataSensitivity.LOCATION:
            result["location"] += 1
        if item.tanimlayici:
            result["direct_identifiers"] += 1

        row = existing.get(key)
        if row is None:
            db.add(
                DataField(
                    tenant_id=tenant_id,
                    table_name=item.tablo[:128],
                    column_name=item.alan[:128],
                    column_type=(item.tip or None),
                    source_module=item.dosya,
                    source_line=item.satir,
                    sensitivity=sensitivity,
                    identifiability=identifiability,
                    is_special_category=(sensitivity == DataSensitivity.SPECIAL_CATEGORY),
                    discovered_by=DiscoverySource.SCANNER,
                    discovered_at=now,
                    review_status=ReviewStatus.REVIEW_REQUIRED,
                    is_present=True,
                    last_seen_at=now,
                    created_by_id=ctx.user_id,
                )
            )
            result["created"] += 1
            continue

        row.last_seen_at = now
        row.source_module = item.dosya
        row.source_line = item.satir
        row.column_type = item.tip or row.column_type
        row.updated_by_id = ctx.user_id
        was_absent = not row.is_present
        row.is_present = True

        if row.review_status in _HUMAN_SETTLED:
            if row.sensitivity != sensitivity:
                # İnsan kararı ile ölçüm ayrışıyor: ne kararı ezeriz ne de
                # ölçümü yok sayarız; alan yeniden incelemeye alınır.
                row.review_status = ReviewStatus.REVIEW_REQUIRED
                row.notes = _append_note(
                    row.notes,
                    f"Tarayıcı {sensitivity} bekliyor, onaylı sınıflandırma "
                    f"{row.sensitivity}. Yeniden inceleme gerekiyor.",
                )
                result["reclassified"] += 1
                result["updated"] += 1
            elif was_absent:
                row.review_status = ReviewStatus.REVIEW_REQUIRED
                result["updated"] += 1
            else:
                result["unchanged"] += 1
            continue

        changed = (
            row.sensitivity != sensitivity
            or row.identifiability != identifiability
            or was_absent
        )
        row.sensitivity = sensitivity
        row.identifiability = identifiability
        row.is_special_category = sensitivity == DataSensitivity.SPECIAL_CATEGORY
        if changed:
            result["updated"] += 1
        else:
            result["unchanged"] += 1

    for key, row in existing.items():
        if key in seen or not row.is_present:
            continue
        if row.discovered_by != DiscoverySource.SCANNER:
            # Elle girilmiş satırlar tarayıcı görmediği için kaybolmuş
            # sayılmaz; tarayıcı yalnızca kendi bulduklarına tanıklık eder.
            continue
        row.is_present = False
        row.review_status = ReviewStatus.REVIEW_REQUIRED
        row.notes = _append_note(row.notes, "Son taramada bulunamadı.")
        row.updated_by_id = ctx.user_id
        result["disappeared"] += 1

    result["tables"] = len(tables)
    result["finished_at"] = utcnow()
    db.flush()

    if full and seed_transfers and envanter is not None:
        result["transfer_candidates"] = _seed_transfer_candidates(
            db, ctx, tenant_id=tenant_id, env=envanter
        )

    artifact = _record_scan_evidence(db, ctx, tenant_id=tenant_id, result=result, note=note)
    result["evidence_id"] = artifact.id
    audit_service.record(
        db,
        "EXECUTE",
        entity_type=SUBJECT_SCAN,
        entity_id=artifact.id,
        entity_label=result["scope"],
        summary=(
            f"Inventory scan: +{result['created']} new, {result['updated']} updated, "
            f"{result['disappeared']} disappeared"
        ),
        new_values={
            k: result[k]
            for k in ("created", "updated", "disappeared", "reclassified", "status")
        },
        **ctx.audit_kwargs(),
    )
    return result


def _append_note(existing: str | None, note: str) -> str:
    stamped = f"[{utcnow().date().isoformat()}] {note}"
    return f"{existing}\n{stamped}" if existing else stamped


def _record_scan_evidence(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    result: dict[str, Any],
    note: str | None,
) -> Any:
    """
    Taramayı kanıt zincirine bağla.

    Kanıtın yükü sayımlar ve tarayıcının uyarı listesidir; alan adlarının
    tamamı değil. Kanıt uğruna envanterin ikinci bir kopyasını üretmek,
    uyumluluk katmanını yeni bir veri havuzuna çevirirdi.
    """
    payload = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in result.items()
    }
    if note:
        payload["operator_note"] = note

    return evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.SCAN_OUTPUT,
        title=f"Inventory scan ({result['scope']}) — {result['status']}",
        description=(
            "Kişisel veri keşif taraması çıktısı. Bulgular adaydır; "
            "sınıflandırma insan incelemesine tabidir."
        ),
        subject_type=SUBJECT_SCAN,
        payload=payload,
        source="app.compliance.scanners.discovery",
        collector_kind=evidence_service.COLLECTOR_SYSTEM,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )


def _seed_transfer_candidates(
    db: Session, ctx: Ctx, *, tenant_id: int, env: Any
) -> int:
    """
    Aktif bulut AI sağlayıcılarından **aday** aktarım kaydı üret.

    Aday satır, aktarımın hukuka uygun olduğunu söylemez; "burada
    değerlendirilmemiş bir aktarım var" der. Ülke ve mekanizma bilinmeyen
    doğar: uç noktanın alan adından ülke çıkarmak (".com" → belirli bir ülke
    gibi) tahmindir ve uyumluluk kaydında tahmine yer yoktur.

    Kapalı sağlayıcılar için kayıt üretilmez; kullanılmayan bir bileşen için
    aktarım kaydı açmak envanteri gürültüye boğar.
    """
    created = 0
    for provider in getattr(env, "ai_providers", []) or []:
        if not provider.get("aktif") or provider.get("yerel_mi"):
            continue
        name = str(provider.get("ad") or "UNKNOWN")
        code = f"AI-{slugify(name, sep='-').upper()}"[:64]
        exists = db.execute(
            select(func.count(Transfer.id)).where(
                Transfer.tenant_id == tenant_id, Transfer.code == code
            )
        ).scalar_one()
        if exists:
            continue

        db.add(
            Transfer(
                tenant_id=tenant_id,
                code=code,
                name=f"{name} (keşif adayı)",
                name_en=f"{name} (discovery candidate)",
                description=(
                    "Keşif taramasında bulunan aktif bulut AI sağlayıcısı. "
                    "Aktarımın kapsamı, alıcı ülkesi ve dayanağı insan "
                    "tarafından belirlenmelidir."
                ),
                destination_country=None,
                mechanism=TransferMechanism.UNKNOWN,
                status=ComplianceState.REVIEW_REQUIRED,
                adequacy_status=ReviewStatus.REVIEW_REQUIRED,
                tia_outcome=ComplianceState.UNKNOWN,
                data_categories_note=str(provider.get("base_url") or "")[:512] or None,
                created_by_id=ctx.user_id,
            )
        )
        created += 1

    if created:
        db.flush()
        log.info("seeded %d transfer candidates for tenant=%s", created, tenant_id)
    return created


# ---------------------------------------------------------------------------
# Okuma ve insan incelemesi
# ---------------------------------------------------------------------------
def list_fields(
    db: Session,
    *,
    tenant_id: int,
    sensitivity: str | None = None,
    review_status: str | None = None,
    table_name: str | None = None,
    term: str | None = None,
    present_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[DataField], int]:
    conds: list[Any] = [DataField.tenant_id == tenant_id]
    if sensitivity:
        conds.append(DataField.sensitivity == sensitivity)
    if review_status:
        conds.append(DataField.review_status == review_status)
    if table_name:
        conds.append(DataField.table_name == table_name)
    if present_only:
        conds.append(DataField.is_present.is_(True))
    if term:
        like = f"%{term.strip().lower()}%"
        conds.append(
            func.lower(DataField.column_name).like(like)
            | func.lower(DataField.table_name).like(like)
        )

    total = int(db.execute(select(func.count(DataField.id)).where(*conds)).scalar_one() or 0)
    rows = (
        db.execute(
            select(DataField)
            .where(*conds)
            .order_by(DataField.table_name, DataField.column_name)
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def review_field(
    db: Session,
    ctx: Ctx,
    *,
    tenant_id: int,
    field_id: int,
    review_status: str,
    sensitivity: str | None = None,
    identifiability: str | None = None,
    notes: str | None = None,
) -> DataField:
    """
    Bir envanter alanının insan incelemesini kaydet.

    Kararın kendisi kanıt zincirine yazılır: envanter satırındaki durum alanı
    yalnızca "karar verildi" göstergesidir, kararın ispatı değildir.
    """
    field = db.get(DataField, field_id)
    if field is None or field.tenant_id != tenant_id:
        raise NotFoundError("compliance.data_field.not_found", params={"id": field_id})

    before = {
        "review_status": field.review_status,
        "sensitivity": field.sensitivity,
        "identifiability": field.identifiability,
    }

    field.review_status = review_status
    if sensitivity:
        field.sensitivity = sensitivity
        field.is_special_category = sensitivity == DataSensitivity.SPECIAL_CATEGORY
    if identifiability:
        field.identifiability = identifiability
    if notes is not None:
        field.notes = notes
    if review_status in _HUMAN_SETTLED:
        field.confirmed_by_user_id = ctx.user_id
        field.confirmed_at = utcnow()
    field.updated_by_id = ctx.user_id
    db.flush()

    evidence_service.append(
        db,
        tenant_id=tenant_id,
        kind=EvidenceKind.DECISION_RECORD,
        title=f"Inventory review: {field.table_name}.{field.column_name}",
        description="Envanter alanı için insan sınıflandırma kararı.",
        subject_type=SUBJECT_FIELD,
        subject_id=field.id,
        payload={
            "field": f"{field.table_name}.{field.column_name}",
            "before": before,
            "after": {
                "review_status": field.review_status,
                "sensitivity": field.sensitivity,
                "identifiability": field.identifiability,
            },
            "notes": notes,
        },
        collector_kind=evidence_service.COLLECTOR_HUMAN,
        actor_user_id=ctx.user_id,
        actor_label=ctx.user.username,
    )
    audit_service.record(
        db,
        "UPDATE",
        entity_type=SUBJECT_FIELD,
        entity_id=field.id,
        entity_label=f"{field.table_name}.{field.column_name}",
        summary=f"Inventory field reviewed: {review_status}",
        old_values=before,
        new_values={"review_status": review_status, "sensitivity": field.sensitivity},
        **ctx.audit_kwargs(),
    )
    return field


def field_to_dict(field: DataField) -> dict[str, Any]:
    return {
        "id": field.id,
        "tenant_id": field.tenant_id,
        "table_name": field.table_name,
        "column_name": field.column_name,
        "column_type": field.column_type,
        "sensitivity": field.sensitivity,
        "identifiability": field.identifiability,
        "is_special_category": field.is_special_category,
        "source_module": field.source_module,
        "source_line": field.source_line,
        "discovered_by": field.discovered_by,
        "discovered_at": field.discovered_at,
        "review_status": field.review_status,
        "confirmed_at": field.confirmed_at,
        "is_present": field.is_present,
        "last_seen_at": field.last_seen_at,
        "notes": field.notes,
    }


def field_summary(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """
    Envanter ekranının üst şeridi — hepsi tek geçişte ölçülür.

    ``status_summary`` genel durum tablosu içindir ve inceleme kuyruğuna
    odaklanır; burada sorulan başka bir soru: *ne bulundu*. İki farklı ekranın
    aynı sözlüğü paylaşması, birine eklenen alanın diğerini bozmasına yol açar.

    "Bilinmiyor" ayrı sayılır ve sıfıra karıştırılmaz: hukuki dayanağı
    ölçülmemiş bir alan, dayanağı olmayan bir alan değildir — ikisi de rapor
    edilir ama farklı işlerdir.
    """
    rows = (
        db.execute(
            select(
                DataField.table_name,
                DataField.identifiability,
                DataField.sensitivity,
                DataField.is_special_category,
                DataField.review_status,
                DataField.category_id,
            ).where(DataField.tenant_id == tenant_id, DataField.is_present.is_(True))
        )
        .all()
    )

    tables: set[str] = set()
    fields = direct = location = special = unknown_basis = review_required = 0
    for table_name, identifiability, sensitivity, is_special, review_status, category_id in rows:
        tables.add(str(table_name))
        fields += 1
        if str(identifiability) == IdentifiabilityLevel.DIRECT:
            direct += 1
        if str(sensitivity) == DataSensitivity.LOCATION:
            location += 1
        if bool(is_special):
            special += 1
        if category_id is None:
            # No category means no lawful basis has been attached to the field.
            unknown_basis += 1
        if str(review_status) in (ReviewStatus.REVIEW_REQUIRED, ReviewStatus.IN_REVIEW):
            review_required += 1

    return {
        "tables": len(tables),
        "fields": fields,
        "direct_identifiers": direct,
        "location_fields": location,
        "special_category_candidates": special,
        "unknown_lawful_basis": unknown_basis,
        # Retention is modelled per policy, not per field; until a field is
        # attached to a category there is nothing to derive a period from.
        "unknown_retention": unknown_basis,
        "review_required": review_required,
        "scanned": fields > 0,
    }


def status_summary(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """
    Genel durum tablosunun envanter satırı.

    Hiç tarama yapılmamışsa sonuç "temiz" değil ``UNKNOWN``'dır: ölçülmemiş
    bir envanter, boş bir envanter değildir.
    """
    rows = db.execute(
        select(DataField.review_status, func.count(DataField.id))
        .where(DataField.tenant_id == tenant_id)
        .group_by(DataField.review_status)
    ).all()
    by_status = {str(status): int(count) for status, count in rows}
    total = sum(by_status.values())
    pending = by_status.get(ReviewStatus.REVIEW_REQUIRED, 0) + by_status.get(
        ReviewStatus.IN_REVIEW, 0
    )
    special = int(
        db.execute(
            select(func.count(DataField.id)).where(
                DataField.tenant_id == tenant_id,
                DataField.is_special_category.is_(True),
            )
        ).scalar_one()
        or 0
    )
    absent = int(
        db.execute(
            select(func.count(DataField.id)).where(
                DataField.tenant_id == tenant_id, DataField.is_present.is_(False)
            )
        ).scalar_one()
        or 0
    )
    last_seen = db.execute(
        select(func.max(DataField.last_seen_at)).where(DataField.tenant_id == tenant_id)
    ).scalar_one()

    return {
        "total": total,
        "pending_review": pending,
        "reviewed": total - pending,
        "special_candidates": special,
        "absent": absent,
        "last_activity_at": last_seen,
        "scanned": total > 0,
    }
