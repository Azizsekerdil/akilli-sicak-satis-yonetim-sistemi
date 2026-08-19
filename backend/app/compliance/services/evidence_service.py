"""
Kanıt zinciri servisi.

``app.compliance.models.evidence`` kanıt satırının *şeklini* ve mühürleme
kurallarını tanımlar; bu modül onu veritabanı gerçekliğiyle buluşturur: sıra
numarasının kiracı içinde nasıl tahsis edildiği, zincirin nasıl uzatıldığı ve
doğrulamanın nasıl çalıştırıldığı.

Üç davranış özellikle dikkat ister:

*   **Sıra numarası kiracı içinde tekildir** ve bunu veritabanı kısıtı garanti
    eder. İki eşzamanlı yazma aynı numarayı almaya çalışırsa ikincisi
    ``ConflictError`` ile reddedilir. Sessizce ikinci bir zincir başlatmaktansa
    isteği reddetmek doğrudur: çatallanmış bir kanıt zinciri, kırık bir
    zincirden daha zor fark edilir.
*   **İçeriksiz kanıt yazılamaz.** Ne satır içi yük ne de dışarıdan verilmiş
    bir özet varsa kayıt reddedilir; hiçbir şeyi bağlamayan bir özet, kanıt
    görünümlü boşluktur.
*   **Doğrulama satırı değiştirmez.** Sonuç yalnızca dönüş değeridir.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.compliance.enums import EvidenceKind
from app.compliance.models.evidence import EvidenceArtifact, content_digest
from app.compliance.models.evidence import verify_chain as _verify_rows
from app.core.exceptions import BusinessRuleError, ConflictError
from app.core.logging_config import get_logger
from app.core.utils import dumps

log = get_logger("app.compliance.evidence")

#: Kanıtı kimin ürettiği. İnsan beyanı ile otomat çıktısı aynı ağırlıkta
#: değildir; makbuz ve raporlarda ayrı görünmeleri gerekir.
COLLECTOR_HUMAN = "MANUAL"
COLLECTOR_SYSTEM = "SYSTEM"


def append(
    db: Session,
    *,
    tenant_id: int,
    kind: str | EvidenceKind,
    title: str,
    description: str | None = None,
    subject_type: str | None = None,
    subject_id: int | None = None,
    subject_ref: str | None = None,
    payload: Any = None,
    content_hash: str | None = None,
    source: str | None = None,
    source_uri: str | None = None,
    media_type: str | None = None,
    byte_size: int | None = None,
    storage_path: str | None = None,
    collector_kind: str = COLLECTOR_HUMAN,
    actor_user_id: int | None = None,
    actor_label: str | None = None,
    supersedes_id: int | None = None,
    retention_note: str | None = None,
    commit: bool = False,
) -> EvidenceArtifact:
    """
    Zincire bir kanıt ekle.

    Varsayılan olarak commit etmez: kanıt, anlattığı işlemle aynı işlemde
    kalmalıdır. İşlem geri alınırsa kanıt da geri alınır; aksi hâlde
    gerçekleşmemiş bir olayın kanıtı elimizde kalırdı.
    """
    payload_json = dumps(payload) if payload is not None else None

    digest = content_hash
    if digest is None and payload_json is not None:
        digest = content_digest(payload_json)
    if digest is None:
        raise BusinessRuleError(
            "compliance.evidence.content_required",
            detail="Evidence needs either an inline payload or an explicit content_hash.",
        )

    previous = db.execute(
        select(EvidenceArtifact)
        .where(EvidenceArtifact.tenant_id == tenant_id)
        .order_by(EvidenceArtifact.sequence_no.desc())
        .limit(1)
    ).scalar_one_or_none()

    artifact = EvidenceArtifact(
        tenant_id=tenant_id,
        kind=str(kind),
        title=title[:255],
        description=description,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_ref=subject_ref,
        collected_by_user_id=actor_user_id,
        collected_by_label=actor_label,
        collector_kind=collector_kind,
        source=source,
        source_uri=source_uri,
        media_type=media_type,
        byte_size=byte_size if byte_size is not None else _payload_size(payload_json),
        storage_path=storage_path,
        payload=payload_json,
        content_hash=digest,
        supersedes_id=supersedes_id,
        retention_note=retention_note,
    )
    artifact.seal(
        sequence_no=(previous.sequence_no + 1) if previous else 1,
        previous_hash=previous.chain_hash if previous else None,
    )

    db.add(artifact)
    try:
        db.flush()
    except IntegrityError as exc:
        # Tek olası ihlal (tenant_id, sequence_no) tekilliğidir: başka bir
        # işlem aynı anda zinciri uzatmış demektir.
        db.rollback()
        raise ConflictError(
            "compliance.evidence.chain_conflict",
            params={"tenant_id": tenant_id},
            detail="Concurrent evidence append; retry the operation.",
        ) from exc

    log.info(
        "evidence appended tenant=%s seq=%s kind=%s subject=%s#%s",
        tenant_id, artifact.sequence_no, artifact.kind, subject_type, subject_id,
    )
    if commit:
        db.commit()
    return artifact


def _payload_size(payload_json: str | None) -> int | None:
    return len(payload_json.encode("utf-8")) if payload_json else None


def list_artifacts(
    db: Session,
    *,
    tenant_id: int,
    kind: str | None = None,
    subject_type: str | None = None,
    subject_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[EvidenceArtifact], int]:
    """Kanıt listesi ve toplam sayısı (en yeni önce)."""
    conds = [EvidenceArtifact.tenant_id == tenant_id]
    if kind:
        conds.append(EvidenceArtifact.kind == kind)
    if subject_type:
        conds.append(EvidenceArtifact.subject_type == subject_type)
    if subject_id is not None:
        conds.append(EvidenceArtifact.subject_id == subject_id)

    total = int(
        db.execute(select(func.count(EvidenceArtifact.id)).where(*conds)).scalar_one() or 0
    )
    rows = (
        db.execute(
            select(EvidenceArtifact)
            .where(*conds)
            .order_by(EvidenceArtifact.sequence_no.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def verify(db: Session, *, tenant_id: int) -> dict[str, Any]:
    """
    Bir kiracının kanıt zincirini baştan sona doğrula.

    Tüm satırlar sıra numarasına göre okunur; kısmi doğrulama yapılmaz, çünkü
    zincirin ortasından başlayan bir kontrol, öncesinde silinmiş bir halkayı
    göremez.
    """
    rows = (
        db.execute(
            select(EvidenceArtifact)
            .where(EvidenceArtifact.tenant_id == tenant_id)
            .order_by(EvidenceArtifact.sequence_no.asc())
        )
        .scalars()
        .all()
    )
    result = dict(_verify_rows(list(rows)))
    if not result.get("valid"):
        log.error(
            "evidence chain broken tenant=%s at=%s status=%s",
            tenant_id, result.get("broken_at"), result.get("status"),
        )
    return result


def count_for(db: Session, *, tenant_id: int, subject_type: str) -> int:
    """Belirli bir kayıt türü için kaç kanıt var?"""
    return int(
        db.execute(
            select(func.count(EvidenceArtifact.id)).where(
                EvidenceArtifact.tenant_id == tenant_id,
                EvidenceArtifact.subject_type == subject_type,
            )
        ).scalar_one()
        or 0
    )


def subjects_with_evidence(
    db: Session, *, tenant_id: int, subject_type: str
) -> set[int]:
    """
    Kanıtı olan kayıtların kimlikleri.

    Genel durum tablosu "kaç kaydın kanıtı eksik" sorusunu bununla cevaplar;
    kayıt başına ayrı sorgu atmak, kayıt sayısı büyüdüğünde raporu kullanılmaz
    hâle getirirdi.
    """
    rows = db.execute(
        select(EvidenceArtifact.subject_id).where(
            EvidenceArtifact.tenant_id == tenant_id,
            EvidenceArtifact.subject_type == subject_type,
            EvidenceArtifact.subject_id.is_not(None),
        )
    ).scalars()
    return {int(r) for r in rows if r is not None}


def to_dict(artifact: EvidenceArtifact) -> dict[str, Any]:
    """API çıkışı için düz sözlük — yük gövdesi taşınmaz."""
    return {
        "id": artifact.id,
        "tenant_id": artifact.tenant_id,
        "sequence_no": artifact.sequence_no,
        "kind": artifact.kind,
        "title": artifact.title,
        "description": artifact.description,
        "subject_type": artifact.subject_type,
        "subject_id": artifact.subject_id,
        "subject_ref": artifact.subject_ref,
        "collected_at": artifact.collected_at,
        "collected_by_user_id": artifact.collected_by_user_id,
        "collected_by_label": artifact.collected_by_label,
        "collector_kind": artifact.collector_kind,
        "source": artifact.source,
        "source_uri": artifact.source_uri,
        "byte_size": artifact.byte_size,
        "storage_path": artifact.storage_path,
        "content_hash": artifact.content_hash,
        "previous_hash": artifact.previous_hash,
        "chain_hash": artifact.chain_hash,
        "supersedes_id": artifact.supersedes_id,
        "created_at": artifact.created_at,
    }
