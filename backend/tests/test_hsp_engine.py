"""
HSP çekirdeği testleri.

Buradaki testler motorun *kolaylıklarını* değil, **zorunlu davranışlarını**
korur.  Her biri, ihlal edildiğinde sessizce yanlış izin üretecek bir kuralı
sabitler:

    varsayılan ret · süre dolması · geri alma · insan onayı · makbuz zinciri

Kayıtlar ayrı bir kiracı kimliği (:data:`TENANT`) altında yazılır, böylece
paralel çalışan diğer uyumluluk testleriyle ne veri ne de makbuz zinciri
karışır.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.compliance.models.hsp import (
    CapabilityToken,
    EmergencyOverride,
    GrantSource,
    GrantStatus,
    HspVerdict,
    ImpactLevel,
    MachineActionManifest,
    MachinePassport,
    PolicyEffect,
    RevocationTarget,
    RightsPolicy,
    RightsReceipt,
    SovereigntyDomain,
    SubjectKind,
)
from app.compliance.services import hsp_engine as engine
from app.compliance.services import hsp_seed as seed_module
from app.models.base import utcnow

#: Testlerin kendi kiracısı — üretim tohumu ve diğer testlerle çakışmasın.
TENANT = 9001
CUSTOMER = "customer:900001"
EMPLOYEE = "user:900002"


@pytest.fixture
def seeded(db):
    """Üç gerçek karar noktasını test kiracısına tohumlar."""
    summary = seed_module.seed(db, tenant_id=TENANT, commit=False)
    engine.ensure_node(
        db, subject_ref=CUSTOMER, subject_kind=SubjectKind.CUSTOMER, tenant_id=TENANT
    )
    engine.ensure_node(
        db, subject_ref=EMPLOYEE, subject_kind=SubjectKind.EMPLOYEE, tenant_id=TENANT
    )
    return summary


def _ctx(**extra):
    base = {"tenant_id": TENANT}
    base.update(extra)
    return base


class TestTohum:
    """Tohum verisi keşif taramasının bulduğu üç karar noktasını karşılar."""

    def test_uc_makine_tohumlanir(self, db, seeded):
        assert set(seeded["machines"]) == {
            seed_module.MACHINE_CREDIT_GATE,
            seed_module.MACHINE_RISK_SCORER,
            seed_module.MACHINE_FLEET_TRACKER,
        }

    def test_kredi_karari_hem_decide_hem_act_beyan_edilir(self, db, seeded):
        rows = (
            db.execute(
                select(MachineActionManifest).where(
                    MachineActionManifest.tenant_id == TENANT,
                    MachineActionManifest.machine_id
                    == seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
                )
            )
            .scalars()
            .all()
        )
        assert {r.domain for r in rows} == {
            SovereigntyDomain.DECIDE,
            SovereigntyDomain.ACT,
        }

    def test_hukuki_dayanak_uydurulmaz(self, db, seeded):
        """Dayanak bilinmiyorsa REVIEW_REQUIRED kalır; madde numarası yazılmaz."""
        rows = (
            db.execute(
                select(MachineActionManifest).where(
                    MachineActionManifest.tenant_id == TENANT
                )
            )
            .scalars()
            .all()
        )
        assert rows
        assert all(r.legal_basis_ref == "REVIEW_REQUIRED" for r in rows)

    def test_insana_birakilan_kararlar_sayilir(self, db, seeded):
        assert seeded["review_required"]

    def test_tohumlama_yinelenebilir(self, db, seeded):
        again = seed_module.seed(db, tenant_id=TENANT, commit=False)
        assert again["machines"] == seeded["machines"]
        assert again["policies"] == seeded["policies"]


class TestVarsayilanRet:
    """Açık izin yoksa cevap reddir — sessiz izin yoktur."""

    def test_bilinmeyen_makine_reddedilir(self, db, seeded):
        d = engine.evaluate(
            db, subject_ref=CUSTOMER, machine_id=10**9, manifest="x.y", context=_ctx()
        )
        assert d.verdict == HspVerdict.DENY
        assert d.allow is False
        assert engine.R_MACHINE_UNKNOWN in d.reasons

    def test_beyan_edilmemis_eylem_reddedilir(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest="beyan.edilmemis.eylem",
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.DENY
        assert engine.R_MANIFEST_UNDECLARED in d.reasons

    def test_politikasiz_beyan_reddedilir(self, db, seeded):
        machine_id = seeded["machines"][seed_module.MACHINE_CREDIT_GATE]
        db.add(
            MachineActionManifest(
                tenant_id=TENANT,
                machine_id=machine_id,
                action_code="credit.limit.raise",
                domain=SovereigntyDomain.DECIDE,
                title="politikası olmayan eylem",
                impact_level=ImpactLevel.LOW,
            )
        )
        db.flush()
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=machine_id,
            manifest="credit.limit.raise",
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.DENY
        assert engine.R_NO_POLICY in d.reasons

    def test_her_karar_makbuz_birakir(self, db, seeded):
        d = engine.evaluate(
            db, subject_ref=CUSTOMER, machine_id=10**9, manifest="x.y", context=_ctx()
        )
        # Reddedilen talep de kaydedilir; yalnızca izinleri kaydeden bir sistem
        # kaç kez reddettiğini bilemez.
        assert d.receipt_id is not None
        assert db.get(RightsReceipt, d.receipt_id) is not None

    def test_karar_tek_skora_indirgenmez(self, db, seeded):
        d = engine.evaluate(
            db, subject_ref=CUSTOMER, machine_id=10**9, manifest="x.y", context=_ctx()
        )
        assert isinstance(d.reasons, list) and d.reasons
        assert not hasattr(d, "score")


class TestKrediKarari:
    """check_credit: ekonomik etki, DECIDE ile ACT ayrı değerlendirilir."""

    def test_degerlendirme_izinli(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_EVALUATE,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.ALLOW
        assert d.policy_code == seed_module.POLICY_CREDIT_EVALUATE

    def test_satisin_reddi_insan_onayi_ister(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.REQUIRE_HUMAN_APPROVAL
        assert d.allow is False
        assert d.appeal_path

    def test_kayitli_insan_onayi_izne_cevirir(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(human_approval={"granted": True, "user_id": 7}),
        )
        assert d.verdict == HspVerdict.ALLOW

    def test_sahipsiz_onay_onay_sayilmaz(self, db, seeded):
        """Kime ait olduğu bilinmeyen onay, onay değildir."""
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(human_approval={"granted": True}),
        )
        assert d.verdict == HspVerdict.REQUIRE_HUMAN_APPROVAL

    def test_insan_reddi_ret_uretir(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(human_approval={"granted": False, "user_id": 7}),
        )
        assert d.verdict == HspVerdict.DENY
        assert engine.R_HUMAN_REFUSED in d.reasons

    def test_enforce_izin_yoksa_firlatir(self, db, seeded):
        from app.core.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            engine.enforce(
                db,
                subject_ref=CUSTOMER,
                machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
                manifest=seed_module.ACTION_CREDIT_BLOCK,
                context=_ctx(),
            )


class TestProfilleme:
    """Risk/churn skorlaması: KNOW süreli jetona, DECIDE insana bağlıdır."""

    def test_risk_profili_jetonla_izinli(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_RISK_SCORER],
            manifest=seed_module.ACTION_RISK_PROFILE,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.ALLOW

    def test_terk_sinifi_insan_incelemesi_ister(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_RISK_SCORER],
            manifest=seed_module.ACTION_CHURN_CLASSIFY,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.REQUIRE_HUMAN_APPROVAL

    def test_jeton_suresi_dolunca_expired(self, db, seeded):
        token = db.execute(
            select(CapabilityToken).where(
                CapabilityToken.tenant_id == TENANT,
                CapabilityToken.token_ref == seed_module.TOKEN_RISK_PROFILE,
            )
        ).scalar_one()
        token.expires_at = utcnow() - timedelta(minutes=1)
        db.flush()

        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_RISK_SCORER],
            manifest=seed_module.ACTION_RISK_PROFILE,
            context=_ctx(),
        )
        # Sessiz izin yok: süresi dolan yetki açıkça EXPIRED döner.
        assert d.verdict == HspVerdict.EXPIRED
        assert d.allow is False
        assert token.status == GrantStatus.EXPIRED

    def test_jeton_zorunluysa_ve_yoksa_ret(self, db, seeded):
        token = db.execute(
            select(CapabilityToken).where(
                CapabilityToken.tenant_id == TENANT,
                CapabilityToken.token_ref == seed_module.TOKEN_RISK_PROFILE,
            )
        ).scalar_one()
        db.delete(token)
        db.flush()

        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_RISK_SCORER],
            manifest=seed_module.ACTION_RISK_PROFILE,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.DENY
        assert engine.R_TOKEN_MISSING in d.reasons


class TestKonumTakibi:
    """gps_events: özerklik etkisi; izin mesai oturumuna bağlıdır."""

    def _track(self, db, seeded, **ctx):
        return engine.evaluate(
            db,
            subject_ref=EMPLOYEE,
            machine_id=seeded["machines"][seed_module.MACHINE_FLEET_TRACKER],
            manifest=seed_module.ACTION_LOCATION_TRACK,
            context=_ctx(**ctx),
        )

    def test_mesai_oturumu_aciksa_izinli(self, db, seeded):
        assert self._track(db, seeded, day_session_active=True).verdict == HspVerdict.ALLOW

    def test_mesai_kapaliysa_reddedilir(self, db, seeded):
        d = self._track(db, seeded, day_session_active=False)
        assert d.verdict == HspVerdict.DENY
        assert any(r.startswith(engine.R_CONDITION_UNMET) for r in d.reasons)

    def test_kosul_baglamda_yoksa_reddedilir(self, db, seeded):
        """Eksik olgu 'sorun yok' demek değildir."""
        d = self._track(db, seeded)
        assert d.verdict == HspVerdict.DENY
        assert any(r.startswith(engine.R_CONDITION_MISSING) for r in d.reasons)


class TestGeriAlmaVePasaport:
    def test_ozne_geri_alirsa_revoked(self, db, seeded):
        engine.revoke(
            db,
            tenant_id=TENANT,
            target_kind=RevocationTarget.SUBJECT_ALL,
            subject_ref=CUSTOMER,
            reason_code="SUBJECT_REQUEST",
            source=GrantSource.SUBJECT,
        )
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_RISK_SCORER],
            manifest=seed_module.ACTION_RISK_PROFILE,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.REVOKED

    def test_geri_alma_yalnizca_hedef_ozneyi_etkiler(self, db, seeded):
        engine.revoke(
            db,
            tenant_id=TENANT,
            target_kind=RevocationTarget.SUBJECT_ALL,
            subject_ref=CUSTOMER,
            reason_code="SUBJECT_REQUEST",
            source=GrantSource.SUBJECT,
        )
        d = engine.evaluate(
            db,
            subject_ref=EMPLOYEE,
            machine_id=seeded["machines"][seed_module.MACHINE_FLEET_TRACKER],
            manifest=seed_module.ACTION_LOCATION_TRACK,
            context=_ctx(day_session_active=True),
        )
        assert d.verdict == HspVerdict.ALLOW

    def test_suresi_dolan_pasaport_expired(self, db, seeded):
        machine_id = seeded["machines"][seed_module.MACHINE_CREDIT_GATE]
        passport = (
            db.execute(
                select(MachinePassport).where(
                    MachinePassport.tenant_id == TENANT,
                    MachinePassport.machine_id == machine_id,
                )
            )
            .scalars()
            .first()
        )
        passport.expires_at = utcnow() - timedelta(days=1)
        db.flush()

        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=machine_id,
            manifest=seed_module.ACTION_CREDIT_EVALUATE,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.EXPIRED


class TestOlaganustuHal:
    """Override, beklenen insan onayının yerine geçer — yasağı açmaz."""

    def _override(self, db, machine_id):
        row = EmergencyOverride(
            tenant_id=TENANT,
            code="OVR-TEST",
            machine_id=machine_id,
            action_code=seed_module.ACTION_CREDIT_BLOCK,
            domain=SovereigntyDomain.ACT,
            reason_code="OPERATIONAL",
            justification="test senaryosu",
            authorized_by_user_id=1,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(row)
        db.flush()
        return row

    def test_insan_onayinin_yerine_gecer(self, db, seeded):
        machine_id = seeded["machines"][seed_module.MACHINE_CREDIT_GATE]
        self._override(db, machine_id)
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=machine_id,
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.ALLOW
        assert any(r.startswith(engine.R_OVERRIDE_USED) for r in d.reasons)
        # Her kullanım sonradan insan incelemesi için işaretlenir.
        assert db.get(RightsReceipt, d.receipt_id).human_review_required is True

    def test_acik_yasagi_acamaz(self, db, seeded):
        machine_id = seeded["machines"][seed_module.MACHINE_CREDIT_GATE]
        self._override(db, machine_id)
        db.add(
            RightsPolicy(
                tenant_id=TENANT,
                code="HSP-TEST-DENY",
                title="test yasağı",
                domain=SovereigntyDomain.ACT,
                action_code=seed_module.ACTION_CREDIT_BLOCK,
                subject_kind=SubjectKind.CUSTOMER,
                machine_id=machine_id,
                effect=PolicyEffect.DENY,
                priority=900,
            )
        )
        db.flush()
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=machine_id,
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(),
        )
        assert d.verdict == HspVerdict.DENY
        assert engine.R_POLICY_DENY in d.reasons


class TestMakbuzZinciri:
    def test_zincir_dogrulanir(self, db, seeded):
        for _ in range(3):
            engine.evaluate(
                db,
                subject_ref=CUSTOMER,
                machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
                manifest=seed_module.ACTION_CREDIT_EVALUATE,
                context=_ctx(),
            )
        result = engine.verify_chain(db, tenant_id=TENANT)
        assert result["valid"] is True
        assert result["checked"] >= 3

    def test_degistirilen_makbuz_yakalanir(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_EVALUATE,
            context=_ctx(),
        )
        receipt = db.get(RightsReceipt, d.receipt_id)
        receipt.verdict = HspVerdict.DENY  # geçmişe dönük "düzeltme"
        db.flush()

        result = engine.verify_chain(db, tenant_id=TENANT)
        assert result["valid"] is False
        assert result["reason"] == "content_hash_mismatch"

    def test_makbuz_kararin_gerekcesini_tasir(self, db, seeded):
        d = engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_BLOCK,
            context=_ctx(),
        )
        receipt = db.get(RightsReceipt, d.receipt_id)
        assert receipt.question
        assert receipt.reasons()
        assert receipt.appeal_path
        assert receipt.policy_code == seed_module.POLICY_CREDIT_BLOCK

    def test_ozne_kendi_makbuzlarini_gorebilir(self, db, seeded):
        engine.evaluate(
            db,
            subject_ref=CUSTOMER,
            machine_id=seeded["machines"][seed_module.MACHINE_CREDIT_GATE],
            manifest=seed_module.ACTION_CREDIT_EVALUATE,
            context=_ctx(),
        )
        rows = engine.subject_receipts(db, subject_ref=CUSTOMER, tenant_id=TENANT)
        assert rows and all(r.subject_ref == CUSTOMER for r in rows)
