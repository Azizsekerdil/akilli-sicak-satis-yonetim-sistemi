"""
Kural motoru ve paket yaşam döngüsü testleri.

Bu dosyanın koruduğu şey bir fonksiyon değil, bir duruş: **bilinmeyen uyumlu
değildir ve insan onayı atlanamaz.** Motorun her "iyileştirmesi" bu iki
değişmezi sessizce delme riski taşır, çünkü ikisi de sistemi daha "yardımsever"
göstermenin en kolay yoludur — bir alan eksikse varsayalım, bir onay yoksa
devam edelim. Aşağıdaki testler tam olarak bunu engeller.

Geliştirme sırasında iki gerçek hata bu testlerin öncüsü tarafından yakalandı:

*   Kural, yetki alanını paketten devralıyordu; ancak içerik özeti devralma
    ÖNCESİ değerlerden hesaplanıyordu. Sonuç: dosyadan hesaplanan imza ile
    satırlardan yeniden hesaplanan imza hiçbir zaman tutmuyor ve her aktivasyon
    "içerik değişmiş" diye reddediliyordu.
*   ``is_human_override`` sütun varsayılanı flush anında uygulanıyor, özet ise
    flush'tan önce hesaplanıyordu. ``None`` ile yazılan bayrak ``False`` olarak
    geri okununca kanıt zinciri haksız yere kırık görünüyordu.
"""

from __future__ import annotations

import json
import re

import pytest

from app.compliance.models.rules import RulePack
from app.compliance.models.tenant import Tenant
from app.compliance.rule_enums import (
    ApprovalDecision,
    EvaluationOutcome,
    LifecycleStatus,
    PredicateResult,
)
from app.compliance.services import rule_engine as engine
from app.compliance.services import rulepack_loader as loader
from app.core.exceptions import BusinessRuleError, ValidationError

TRUE = PredicateResult.TRUE
FALSE = PredicateResult.FALSE
UNKNOWN = PredicateResult.UNKNOWN

SUBMITTER = 9101
APPROVER = 9202


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _kvkk_file():
    return next(p for p in loader.discover_pack_files() if "kvkk" in p.name)


def _gdpr_file():
    return next(p for p in loader.discover_pack_files() if "gdpr" in p.name)


@pytest.fixture
def tenant(db):
    row = Tenant(code="CMP-TEST", name="Uyumluluk Test Kiracısı")
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def kvkk_pack(db):
    return loader.import_from_file(db, _kvkk_file(), imported_by_id=SUBMITTER)


@pytest.fixture
def approved_kvkk(db, kvkk_pack):
    loader.submit_for_review(db, kvkk_pack, user_id=SUBMITTER)
    loader.record_decision(
        db, kvkk_pack, approver_id=APPROVER, approver_name="DPO",
        decision=ApprovalDecision.APPROVED,
    )
    return kvkk_pack


#: KVKK paketinin tüm kontrollerini karşıladığını beyan eden bağlam.
FULL_CONTEXT = {
    "processes_personal_data": True,
    "data_controller_in_turkey": True,
    "processing_activities_inventoried": True,
    "purposes_documented": True,
    "field_level_inventory": True,
    "data_minimisation_reviewed": True,
    "retention_schedule_defined": True,
    "retention_enforced_technically": True,
    "accuracy_process_defined": True,
    "legal_basis_recorded": True,
    "legal_basis_per_activity": True,
    "processes_special_category_data": False,
    "privacy_notice_published": True,
    "privacy_notice_delivered_at_collection": True,
    "data_subject_request_channel": True,
    "data_subject_request_log": True,
    "security_measures_documented": True,
    "encryption_at_rest": True,
    "access_control_enforced": True,
    "personal_data_stored_outside_turkey": False,
    "audit_log_enabled": True,
    "audit_log_tamper_evident": True,
    "verbis_registration_required": False,
    "evidence": {
        "purpose_register": {"ref": "DOC-1"},
        "data_inventory": {"ref": "SCAN-1"},
        "retention_policy": {"ref": "POL-1"},
        "accuracy_procedure": {"ref": "PRC-1"},
        "legal_basis_register": {"ref": "DOC-2"},
        "privacy_notice_document": {"ref": "NOT-1"},
        "dsr_procedure": {"ref": "PRC-2"},
        "security_policy": {"ref": "POL-2"},
        "access_control_matrix": {"ref": "CFG-1"},
        "audit_log_sample": {"ref": "LOG-1"},
    },
}


def _ctx(tenant, **overrides):
    ctx = dict(FULL_CONTEXT)
    ctx["tenant_id"] = tenant.id
    ctx.update(overrides)
    return ctx


def _by_id(results, rule_id):
    return next(r for r in results if r.rule_id == rule_id)


# ---------------------------------------------------------------------------
# Üç değerli mantık
# ---------------------------------------------------------------------------
class TestPredicateLogic:
    def test_eksik_alan_false_degil_unknown(self):
        node = {"field": "yok", "op": "is_true"}
        assert engine.evaluate_predicate(node, {}) == UNKNOWN

    def test_unknown_dizgesi_bilinmeyen_sayilir(self):
        node = {"field": "x", "op": "eq", "value": "SCC"}
        assert engine.evaluate_predicate(node, {"x": "UNKNOWN"}) == UNKNOWN
        assert engine.evaluate_predicate(node, {"x": "SCC"}) == TRUE
        assert engine.evaluate_predicate(node, {"x": "NONE"}) == FALSE

    def test_kleene_ve(self):
        f = {"field": "a", "op": "is_true"}
        t = {"field": "b", "op": "is_true"}
        u = {"field": "yok", "op": "is_true"}
        ctx = {"a": False, "b": True}
        # Bir FALSE her şeyi düşürür, bilinmeyen olsa bile.
        assert engine.evaluate_predicate({"all": [f, u]}, ctx) == FALSE
        assert engine.evaluate_predicate({"all": [t, u]}, ctx) == UNKNOWN
        assert engine.evaluate_predicate({"all": [t, t]}, ctx) == TRUE

    def test_kleene_veya(self):
        t = {"field": "b", "op": "is_true"}
        f = {"field": "a", "op": "is_true"}
        u = {"field": "yok", "op": "is_true"}
        ctx = {"a": False, "b": True}
        assert engine.evaluate_predicate({"any": [t, u]}, ctx) == TRUE
        assert engine.evaluate_predicate({"any": [f, u]}, ctx) == UNKNOWN
        assert engine.evaluate_predicate({"any": [f, f]}, ctx) == FALSE

    def test_degilleme_bilinmeyeni_korur(self):
        u = {"field": "yok", "op": "is_true"}
        assert engine.evaluate_predicate({"not": u}, {}) == UNKNOWN

    def test_varlik_isleci_iki_degerlidir(self):
        """"Bu bilgi var mı?" sorusunun cevabı bilinmez olamaz."""
        assert engine.evaluate_predicate({"field": "x", "op": "exists"}, {}) == FALSE
        assert engine.evaluate_predicate({"field": "x", "op": "missing"}, {}) == TRUE
        assert (
            engine.evaluate_predicate({"field": "x", "op": "is_unknown"}, {"x": None})
            == TRUE
        )

    def test_sayisal_karsilastirma(self):
        node = {"field": "d", "op": "lte", "value": 30}
        assert engine.evaluate_predicate(node, {"d": 30}) == TRUE
        assert engine.evaluate_predicate(node, {"d": 31}) == FALSE
        # Sayı olmayan bir değeri sessizce FALSE saymak kuralı yanlış tarafa
        # düşürürdü; sonuç bilinmeyendir.
        assert engine.evaluate_predicate(node, {"d": "yakinda"}) == UNKNOWN

    def test_bool_sayisal_isleçte_kabul_edilmez(self):
        node = {"field": "d", "op": "gt", "value": 0}
        assert engine.evaluate_predicate(node, {"d": True}) == UNKNOWN

    def test_bilinmeyen_islec_reddedilir(self):
        with pytest.raises(ValidationError):
            engine.evaluate_predicate({"field": "x", "op": "regex"}, {"x": "a"})

    def test_asiri_derin_yuklem_reddedilir(self):
        node = {"field": "x", "op": "is_true"}
        for _ in range(engine.MAX_PREDICATE_DEPTH + 2):
            node = {"not": node}
        with pytest.raises(ValidationError):
            engine.evaluate_predicate(node, {"x": True})

    def test_alan_toplama(self):
        node = {"all": [
            {"field": "a", "op": "is_true"},
            {"any": [{"field": "b", "op": "is_true"},
                     {"not": {"field": "c", "op": "is_true"}}]},
        ]}
        assert engine.collect_fields(node) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Sevk edilen paketlerin bütünlüğü
# ---------------------------------------------------------------------------
class TestShippedPacks:
    @pytest.mark.parametrize("path_fn", [_kvkk_file, _gdpr_file])
    def test_paket_dogrulamadan_gecer(self, path_fn):
        data = json.loads(path_fn().read_text(encoding="utf-8"))
        assert loader.validate_pack(data) == []

    @pytest.mark.parametrize("path_fn", [_kvkk_file, _gdpr_file])
    def test_kural_sayisi_makul(self, path_fn):
        data = json.loads(path_fn().read_text(encoding="utf-8"))
        assert 8 <= len(data["rules"]) <= 15

    @pytest.mark.parametrize("path_fn", [_kvkk_file, _gdpr_file])
    def test_her_kural_insan_incelemesi_ister(self, path_fn):
        """Bu paketler TASLAKTIR; hiçbiri otomatik uyumluluk üretemez."""
        data = json.loads(path_fn().read_text(encoding="utf-8"))
        for rule in data["rules"]:
            assert rule["requires_human_review"] is True, rule["rule_id"]

    @pytest.mark.parametrize("path_fn", [_kvkk_file, _gdpr_file])
    def test_madde_referansi_bos_birakilmaz(self, path_fn):
        data = json.loads(path_fn().read_text(encoding="utf-8"))
        for rule in data["rules"]:
            assert rule["article_ref"].strip(), rule["rule_id"]

    @pytest.mark.parametrize("path_fn", [_kvkk_file, _gdpr_file])
    def test_para_cezasi_tutari_yazilmaz(self, path_fn):
        """
        Tutarlar yıllık değişir; pakete gömülen bir rakam sessizce eskir.

        Aranan şey para birimi sözcüğü değil, **sayı + para birimi** kalıbıdır.
        Düz alt dize araması bu testin ilk sürümünde "İHTİYATLI" içindeki "TL"
        hecesine takıldı — keşif tarayıcısında da aynı hata yapılmıştı.
        """
        raw = path_fn().read_text(encoding="utf-8")
        tutar = re.compile(
            r"\d[\d.,\s]*\s*(TL|TRY|EUR|EURO|AVRO|₺|€)\b|"
            r"[₺€]\s*\d|"
            r"\b(milyon|million)\s+(TL|EUR|avro)",
            re.IGNORECASE,
        )
        hit = tutar.search(raw)
        assert hit is None, f"para tutarı bulundu: {hit.group(0)!r}"

    def test_kaynak_izin_listesi_disi_adres_reddedilir(self):
        data = json.loads(_kvkk_file().read_text(encoding="utf-8"))
        data["legal_sources"][0]["official_url"] = "https://ornek-blog.example/kvkk"
        problems = loader.validate_pack(data)
        assert any("allowlist" in p for p in problems)

    def test_ilan_edilmemis_baglam_alani_reddedilir(self):
        """Yazım hatası taşıyan bir alan sessizce 'bilinmiyor' üretmemeli."""
        data = json.loads(_kvkk_file().read_text(encoding="utf-8"))
        data["rules"][0]["condition"] = {"field": "yaziim_hatasi", "op": "is_true"}
        problems = loader.validate_pack(data)
        assert any("not declared in pack context_keys" in p for p in problems)

    def test_requires_human_review_alani_zorunlu(self):
        data = json.loads(_kvkk_file().read_text(encoding="utf-8"))
        del data["rules"][0]["requires_human_review"]
        assert loader.validate_pack(data)


# ---------------------------------------------------------------------------
# İçe alma ve yaşam döngüsü
# ---------------------------------------------------------------------------
class TestPackLifecycle:
    def test_dosya_kendi_durumunu_dayatamaz(self, db):
        data = json.loads(_kvkk_file().read_text(encoding="utf-8"))
        data["status"] = "ACTIVE"
        pack = loader.import_pack(db, data, imported_by_id=SUBMITTER)
        assert pack.status == LifecycleStatus.DRAFT

    def test_icerik_ozeti_satirlardan_yeniden_uretilebilir(self, db, kvkk_pack):
        assert loader.content_hash_of_stored_pack(kvkk_pack) == kvkk_pack.content_hash

    def test_ayni_paket_iki_kez_alinamaz(self, db, kvkk_pack):
        from app.core.exceptions import ConflictError

        with pytest.raises(ConflictError):
            loader.import_from_file(db, _kvkk_file(), imported_by_id=SUBMITTER)

    def test_onaysiz_aktivasyon_reddedilir(self, db, kvkk_pack):
        with pytest.raises(BusinessRuleError):
            loader.activate(db, kvkk_pack, activated_by_id=APPROVER)

    def test_servisi_atlayan_dogrudan_yazim_da_reddedilir(self, db, kvkk_pack):
        """İnsan onayı yardımcı bir fonksiyon değil, veri katmanı değişmezidir."""
        kvkk_pack.status = str(LifecycleStatus.ACTIVE)
        with pytest.raises(BusinessRuleError):
            db.flush()
        db.rollback()

    def test_gonderen_kendi_paketini_onaylayamaz(self, db, kvkk_pack):
        loader.submit_for_review(db, kvkk_pack, user_id=SUBMITTER)
        with pytest.raises(BusinessRuleError):
            loader.record_decision(db, kvkk_pack, approver_id=SUBMITTER)

    def test_taslak_dogrudan_onaya_gecemez(self, db, kvkk_pack):
        with pytest.raises(BusinessRuleError):
            loader.record_decision(db, kvkk_pack, approver_id=APPROVER)

    def test_onay_sonrasi_aktivasyon(self, db, approved_kvkk):
        assert approved_kvkk.status == LifecycleStatus.APPROVED
        loader.activate(db, approved_kvkk, activated_by_id=APPROVER)
        assert approved_kvkk.status == LifecycleStatus.ACTIVE
        assert all(r.status == LifecycleStatus.ACTIVE for r in approved_kvkk.rules)

    def test_icerik_degisirse_onay_kapsam_disi_kalir(self, db, approved_kvkk):
        """Kaynak ya da kural değişirse imza artık o paketi imzalamıyordur."""
        assert loader.effective_approvals(db, approved_kvkk)
        rule = sorted(approved_kvkk.rules, key=lambda r: r.rule_id)[0]
        rule.article_ref = "m. 99"
        db.flush()
        approved_kvkk.content_hash = loader.content_hash_of_stored_pack(approved_kvkk)
        assert loader.effective_approvals(db, approved_kvkk) == []
        with pytest.raises(BusinessRuleError):
            loader.activate(db, approved_kvkk, activated_by_id=APPROVER)

    def test_reddedilen_paket_taslaga_doner(self, db, kvkk_pack):
        loader.submit_for_review(db, kvkk_pack, user_id=SUBMITTER)
        loader.record_decision(
            db, kvkk_pack, approver_id=APPROVER,
            decision=ApprovalDecision.CHANGES_REQUESTED,
        )
        assert kvkk_pack.status == LifecycleStatus.DRAFT

    def test_onay_geri_alinirsa_paket_yururlukten_kalkar(self, db, approved_kvkk):
        approval = loader.effective_approvals(db, approved_kvkk)[0]
        loader.activate(db, approved_kvkk, activated_by_id=APPROVER)
        loader.revoke_approval(db, approval, user_id=APPROVER, reason="kaynak eskidi")
        assert approved_kvkk.status == LifecycleStatus.WITHDRAWN

    def test_onay_zinciri_dogrulanir(self, db, approved_kvkk):
        assert loader.verify_approval_chain(db)["valid"]


# ---------------------------------------------------------------------------
# Değerlendirme
# ---------------------------------------------------------------------------
class TestEvaluation:
    def test_bos_baglam_uyumlu_degil(self, db, tenant, kvkk_pack):
        results = engine.evaluate_pack(
            db, pack=kvkk_pack, context={"tenant_id": tenant.id}
        )
        counts = engine.summarise(results)["counts"]
        assert counts["COMPLIANT"] == 0
        assert counts["INSUFFICIENT_EVIDENCE"] == len(results)

    def test_tam_baglam_bile_otomatik_uyumlu_donmez(self, db, tenant, kvkk_pack):
        """requires_human_review=True olan kural en fazla REVIEW_REQUIRED döner."""
        results = engine.evaluate_pack(db, pack=kvkk_pack, context=_ctx(tenant))
        counts = engine.summarise(results)["counts"]
        assert counts["COMPLIANT"] == 0
        assert counts["REVIEW_REQUIRED"] > 0
        # Koşullar gerçekten sağlanmış olmalı; sonuç kanıt eksikliğinden değil,
        # yalnızca insan onayı kapısından dolayı beklemede.
        assert _by_id(results, "KVKK-12-01").condition_result == TRUE

    def test_kanit_eksikse_sonuc_insufficient_evidence(self, db, tenant, kvkk_pack):
        results = engine.evaluate_pack(
            db, pack=kvkk_pack, context=_ctx(tenant, evidence={})
        )
        counts = engine.summarise(results)["counts"]
        assert counts["COMPLIANT"] == 0
        assert counts["INSUFFICIENT_EVIDENCE"] >= 8
        row = _by_id(results, "KVKK-12-01")
        assert row.outcome == EvaluationOutcome.INSUFFICIENT_EVIDENCE
        # Kanıt yokken koşul hiç değerlendirilmemeli; aksi hâlde sonuç satırı
        # "koşul sağlanmıştı ama kanıt yoktu" gibi okunurdu.
        assert row.condition_result == UNKNOWN

    def test_unknown_deger_ihlale_donusmez(self, db, tenant, kvkk_pack):
        results = engine.evaluate_pack(
            db, pack=kvkk_pack, context=_ctx(tenant, encryption_at_rest="UNKNOWN")
        )
        assert (
            _by_id(results, "KVKK-12-01").outcome
            == EvaluationOutcome.INSUFFICIENT_EVIDENCE
        )

    def test_gercek_ihlal_non_compliant(self, db, tenant, kvkk_pack):
        results = engine.evaluate_pack(
            db, pack=kvkk_pack, context=_ctx(tenant, encryption_at_rest=False)
        )
        assert _by_id(results, "KVKK-12-01").outcome == EvaluationOutcome.NON_COMPLIANT

    def test_uygulanmayan_kural_not_applicable(self, db, tenant, kvkk_pack):
        results = engine.evaluate_pack(db, pack=kvkk_pack, context=_ctx(tenant))
        assert _by_id(results, "KVKK-06-01").outcome == EvaluationOutcome.NOT_APPLICABLE

    def test_istisna_kurali_devre_disi_birakir(self, db, tenant, kvkk_pack):
        ctx = _ctx(
            tenant,
            verbis_registration_required=True,
            verbis_registration_completed=False,
            verbis_exemption_documented=True,
        )
        ctx["evidence"] = dict(ctx["evidence"], verbis_registration_record={"ref": "X"})
        row = _by_id(engine.evaluate_pack(db, pack=kvkk_pack, context=ctx), "KVKK-16-01")
        assert row.outcome == EvaluationOutcome.NOT_APPLICABLE
        assert "documented_exemption" in (row.matched_exceptions or "")

    def test_inceleme_tetikleyicisi_uyumluyu_beklemeye_alir(self, db, tenant, kvkk_pack):
        ctx = _ctx(tenant, personal_data_stored_outside_turkey=True)
        row = _by_id(engine.evaluate_pack(db, pack=kvkk_pack, context=ctx), "KVKK-12-01")
        assert "data_stored_outside_turkey" in (row.triggered_reviews or "")

    def test_insan_onayi_gerektirmeyen_kural_uyumlu_donebilir(
        self, db, tenant, approved_kvkk
    ):
        """Kapı, motoru sakatlamadan yalnızca gerektiğinde kapanmalı."""
        loader.activate(db, approved_kvkk, activated_by_id=APPROVER)
        rule = _rule_of(approved_kvkk, "KVKK-04-01")
        rule.requires_human_review = False
        rule.confidence = "HIGH"
        db.flush()
        row = engine.evaluate(
            db, rule=rule, context=_ctx(tenant), pack=approved_kvkk, persist=False
        )
        assert row.outcome == EvaluationOutcome.COMPLIANT

    def test_onaysiz_paket_uyumlu_donduremez(self, db, tenant, kvkk_pack):
        """Taslak bir paket, kural izin verse bile 'uyumlu' raporlayamaz."""
        rule = _rule_of(kvkk_pack, "KVKK-04-01")
        rule.requires_human_review = False
        rule.confidence = "HIGH"
        db.flush()
        row = engine.evaluate(
            db, rule=rule, context=_ctx(tenant), pack=kvkk_pack, persist=False
        )
        assert row.outcome == EvaluationOutcome.REVIEW_REQUIRED
        assert "rulepack_not_active" in (row.reasons or "")

    def test_kiracisiz_degerlendirme_reddedilir(self, db, kvkk_pack):
        with pytest.raises(ValidationError):
            engine.evaluate(db, rule=kvkk_pack.rules[0], context={})

    def test_anlik_goruntu_yalnizca_ilkel_deger_saklar(self, db, tenant, kvkk_pack):
        ctx = _ctx(tenant, purposes_documented={"gizli": "kişisel veri"})
        row = _by_id(engine.evaluate_pack(db, pack=kvkk_pack, context=ctx), "KVKK-04-01")
        assert "kişisel veri" not in (row.context_snapshot or "")
        assert "<dict>" in (row.context_snapshot or "")


# ---------------------------------------------------------------------------
# Kanıt zinciri
# ---------------------------------------------------------------------------
class TestEvidenceChain:
    def test_zincir_dogrulanir(self, db, tenant, kvkk_pack):
        engine.evaluate_pack(db, pack=kvkk_pack, context=_ctx(tenant))
        result = engine.verify_chain(db, tenant_id=tenant.id)
        assert result["valid"], result
        assert result["checked"] == kvkk_pack.rule_count

    def test_sessiz_duzenleme_yakalanir(self, db, tenant, kvkk_pack):
        rows = engine.evaluate_pack(db, pack=kvkk_pack, context=_ctx(tenant))
        rows[0].outcome = str(EvaluationOutcome.COMPLIANT)
        db.flush()
        result = engine.verify_chain(db, tenant_id=tenant.id)
        assert not result["valid"]
        assert result["reason"] == "CONTENT_MISMATCH"

    def test_zincir_kiraci_bazindadir(self, db, tenant, kvkk_pack):
        """Bir kiracının verisini dışa aktarmak diğerinin zincirini kırmamalı."""
        other = Tenant(code="CMP-TEST-2", name="İkinci Kiracı")
        db.add(other)
        db.flush()
        engine.evaluate_pack(db, pack=kvkk_pack, context=_ctx(tenant))
        engine.evaluate_pack(
            db, pack=kvkk_pack, context=_ctx(tenant, tenant_id=other.id)
        )
        assert engine.verify_chain(db, tenant_id=tenant.id)["valid"]
        assert engine.verify_chain(db, tenant_id=other.id)["valid"]


def _rule_of(pack: RulePack, rule_id: str):
    return next(r for r in pack.rules if r.rule_id == rule_id)
