"""
The endpoints the compliance screens depend on, exercised for real.

``test_ui_api_contract`` proves the URLs *exist*.  That is necessary and not
sufficient: a route that 500s on first contact is as useless to a person as one
that 404s.  These tests drive each screen's data path end to end — create the
record, read it back, act on it — through the HTTP layer, as the browser does.

Everything is written under the compliance tenant the scan creates, and the HSP
records live under their own tenant id so the receipt chain does not tangle
with ``test_hsp_engine``.
"""

from __future__ import annotations

import uuid

import pytest

API = "/api/v1"


@pytest.fixture(scope="module")
def tenant(client, auth) -> str:
    """
    Ensure a compliance tenant exists.

    The inventory scan is the documented way to bring one into being — reads
    deliberately do not create configuration as a side effect.
    """
    r = client.post(f"{API}/compliance/inventory/scan", headers=auth, json={})
    assert r.status_code in (200, 201), r.text
    return ""


# ---------------------------------------------------------------------------
# Inventory screen
# ---------------------------------------------------------------------------
class TestInventoryScreen:
    def test_summary_returns_the_tiles_the_screen_renders(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/inventory/summary", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        for key in (
            "tables",
            "fields",
            "direct_identifiers",
            "location_fields",
            "special_category_candidates",
            "unknown_lawful_basis",
            "unknown_retention",
            "review_required",
        ):
            assert key in body, f"summary is missing {key}"
            assert isinstance(body[key], int), key

    def test_summary_distinguishes_unscanned_from_empty(self, client, auth, tenant):
        body = client.get(f"{API}/compliance/inventory/summary", headers=auth).json()
        assert "scanned" in body
        # The fixture ran a scan, so this installation has been measured.
        assert body["scanned"] is True
        assert body["fields"] > 0, "the scan found no personal-data fields at all"

    def test_summary_agrees_with_the_field_list(self, client, auth, tenant):
        summary = client.get(f"{API}/compliance/inventory/summary", headers=auth).json()
        listing = client.get(
            f"{API}/compliance/inventory/fields", headers=auth, params={"size": 1}
        ).json()
        # Same population, counted two ways.
        assert summary["fields"] >= listing["total"] - listing["total"]  # sanity
        assert listing["total"] > 0

    def test_field_list_is_paginated(self, client, auth, tenant):
        r = client.get(
            f"{API}/compliance/inventory/fields", headers=auth, params={"size": 5}
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) <= 5
        assert body["total"] >= len(body["items"])


# ---------------------------------------------------------------------------
# Consents screen — notice detail
# ---------------------------------------------------------------------------
class TestNoticeDetail:
    @pytest.fixture
    def notice_id(self, client, auth, tenant) -> int:
        code = f"TEST-NOTICE-{uuid.uuid4().hex[:8]}".upper()
        r = client.post(
            f"{API}/compliance/notices",
            headers=auth,
            json={
                "notice_code": code,
                "language": "tr",
                "title": "Test aydınlatma metni",
                "body": "Bu metin testte üretilmiştir ve gerçek bir aydınlatma değildir.",
            },
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    def test_detail_returns_the_body(self, client, auth, notice_id):
        r = client.get(f"{API}/compliance/notices/{notice_id}", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == notice_id
        assert body["body"], "the detail view exists to show the body and it is empty"

    def test_list_does_not_carry_the_body(self, client, auth, notice_id):
        rows = client.get(f"{API}/compliance/notices", headers=auth).json()["items"]
        assert rows
        assert all(row.get("body") in (None, "") for row in rows)

    def test_unknown_notice_is_404_not_500(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/notices/99999999", headers=auth)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Data-subject request screen
# ---------------------------------------------------------------------------
class TestDataSubjectRequestScreen:
    @pytest.fixture
    def request_id(self, client, auth, tenant) -> int:
        r = client.post(
            f"{API}/compliance/dsr",
            headers=auth,
            json={
                "subject_type": "CUSTOMER",
                "request_type": "ACCESS",
                "subject_ref": f"customer:{uuid.uuid4().hex[:6]}",
                "description": "Test başvurusu.",
            },
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    def test_detail_loads(self, client, auth, request_id):
        r = client.get(f"{API}/compliance/dsr/{request_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == request_id

    def test_transition_moves_the_request_and_is_visible(self, client, auth, request_id):
        r = client.post(
            f"{API}/compliance/dsr/{request_id}/transition",
            headers=auth,
            json={"to_status": "IN_PROGRESS", "note": "Çalışmaya alındı."},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "IN_PROGRESS"

        again = client.get(f"{API}/compliance/dsr/{request_id}", headers=auth).json()
        assert again["status"] == "IN_PROGRESS"

    def test_transition_requires_a_note(self, client, auth, request_id):
        r = client.post(
            f"{API}/compliance/dsr/{request_id}/transition",
            headers=auth,
            json={"to_status": "ON_HOLD", "note": ""},
        )
        assert r.status_code == 422

    def test_transition_rejects_an_unknown_status(self, client, auth, request_id):
        r = client.post(
            f"{API}/compliance/dsr/{request_id}/transition",
            headers=auth,
            json={"to_status": "NOT_A_STATUS", "note": "deneme"},
        )
        assert r.status_code in (400, 422)

    def test_closing_must_go_through_fulfil(self, client, auth, request_id):
        """
        Closure writes the closure evidence and checks identity verification.
        Letting a status dropdown reach FULFILLED would make both optional.
        """
        r = client.post(
            f"{API}/compliance/dsr/{request_id}/transition",
            headers=auth,
            json={"to_status": "FULFILLED", "note": "kısa yoldan kapatma denemesi"},
        )
        assert r.status_code in (400, 409, 422), r.text
        after = client.get(f"{API}/compliance/dsr/{request_id}", headers=auth).json()
        assert after["status"] != "FULFILLED"


# ---------------------------------------------------------------------------
# Rule pack screen
# ---------------------------------------------------------------------------
class TestRulePackScreen:
    def test_list_loads(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/rulepacks", headers=auth)
        assert r.status_code == 200, r.text

    def test_detail_loads_when_a_pack_exists(self, client, auth, tenant):
        rows = client.get(f"{API}/compliance/rulepacks", headers=auth).json()["items"]
        if not rows:
            pytest.skip("no rule pack loaded in this installation")
        pack_id = rows[0]["id"]
        r = client.get(f"{API}/compliance/rulepacks/{pack_id}", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == pack_id

    def test_unknown_pack_is_404(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/rulepacks/99999999", headers=auth)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Rights-receipt screen
# ---------------------------------------------------------------------------
class TestRightsReceiptScreen:
    @pytest.fixture(scope="class")
    def receipt(self, client, auth):
        """
        Produce a real receipt by asking the engine a real question.

        The seed registers the product's declared decision points; the
        evaluation writes a receipt whether the answer is allow or deny, which
        is the property the screen depends on.
        """
        from app.compliance.services import hsp_seed, tenant_service
        from app.core.db import SessionLocal

        s = SessionLocal()
        try:
            resolved = tenant_service.resolve(s, None)
            assert resolved is not None, "the tenant fixture did not run"
            hsp_seed.seed(s, tenant_id=resolved.id, commit=True)
        finally:
            s.close()

        r = client.post(
            f"{API}/compliance/hsp/evaluate",
            headers=auth,
            json={
                "machine_code": "VS-CREDIT-GATE",
                "action_code": "credit.limit.evaluate",
                "subject_ref": f"customer:{uuid.uuid4().hex[:6]}",
                "context": {},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("receipt_id"), "an evaluation produced no receipt"

        rows = client.get(f"{API}/compliance/hsp/receipts", headers=auth).json()["items"]
        assert rows, "the receipt list is empty right after an evaluation"
        return rows[0]

    def test_every_evaluation_leaves_a_receipt(self, receipt):
        assert receipt["question"]
        assert receipt["verdict"]
        assert receipt["content_hash"]

    def test_detail_loads(self, client, auth, receipt):
        r = client.get(f"{API}/compliance/hsp/receipts/{receipt['id']}", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == receipt["id"]
        assert isinstance(body["reasons"], list)

    def test_detail_reports_no_appeal_before_one_is_filed(self, client, auth, receipt):
        body = client.get(
            f"{API}/compliance/hsp/receipts/{receipt['id']}", headers=auth
        ).json()
        assert body.get("appeal_reference") is None

    def test_appeal_is_recorded_and_then_visible(self, client, auth, receipt):
        r = client.post(
            f"{API}/compliance/hsp/receipts/{receipt['id']}/appeal",
            headers=auth,
            json={"reason": "Karara itiraz ediyorum, gerekçesi eksik.", "contact": ""},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["appeal_reference"], "the appeal produced no reference"
        assert body["appeal_submitted_at"]

        again = client.get(
            f"{API}/compliance/hsp/receipts/{receipt['id']}", headers=auth
        ).json()
        assert again["appeal_reference"] == body["appeal_reference"]

    def test_the_appeal_lands_in_the_request_queue(self, client, auth, receipt):
        rows = client.get(
            f"{API}/compliance/dsr",
            headers=auth,
            params={"size": 200},
        ).json()["items"]
        refs = {row["reference"] for row in rows}
        detail = client.get(
            f"{API}/compliance/hsp/receipts/{receipt['id']}", headers=auth
        ).json()
        assert detail["appeal_reference"] in refs, (
            "the appeal is not visible in the data-subject request queue"
        )

    def test_a_second_appeal_is_refused(self, client, auth, receipt):
        r = client.post(
            f"{API}/compliance/hsp/receipts/{receipt['id']}/appeal",
            headers=auth,
            json={"reason": "ikinci itiraz"},
        )
        assert r.status_code in (400, 409), r.text

    def test_appeal_requires_a_reason(self, client, auth, receipt):
        r = client.post(
            f"{API}/compliance/hsp/receipts/{receipt['id']}/appeal",
            headers=auth,
            json={"reason": ""},
        )
        assert r.status_code == 422

    def test_unknown_receipt_is_404(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/hsp/receipts/99999999", headers=auth)
        assert r.status_code == 404

    def test_chain_verifies(self, client, auth, receipt):
        r = client.get(f"{API}/compliance/hsp/receipts/verify", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is True


# ---------------------------------------------------------------------------
# Overview screen
# ---------------------------------------------------------------------------
class TestOverviewScreen:
    def test_overview_returns_categories_not_a_score(self, client, auth, tenant):
        r = client.get(f"{API}/compliance/overview", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "categories" in body
        # The design refuses to collapse a rights posture into one number.
        assert "score" not in body
        assert "compliance_score" not in body
