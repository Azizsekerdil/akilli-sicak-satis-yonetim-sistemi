"""
The demo dataset must not contain anything that could be mistaken for a real
person's contact details.

Demo rows travel further than demo databases: they end up in screenshots, in
exported reports and in the presentation deck.  A random but *well-formed*
Turkish mobile number is indistinguishable from a real one at that distance, so
the generator masks contact details at the point of creation rather than
relying on somebody masking the screenshot afterwards.

These tests fix that property in place.  They check the generators directly and
then re-read the generator's own source, so a future edit that reintroduces a
plausible number fails here instead of on a slide.
"""

from __future__ import annotations

import random
import re

import pytest

from scripts import seed_demo_data as seed

#: A Turkish number a phone would actually dial: 10-11 digits, no letters.
DIALABLE = re.compile(r"(?:\+?90[\s-]?)?0?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b")


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
class TestMaskedContactDetails:
    def test_mobile_numbers_are_masked(self, rng):
        for _ in range(200):
            value = seed.demo_mobile(rng)
            assert "X" in value, value
            assert not DIALABLE.match(value.replace(" ", "")), value

    def test_landline_numbers_are_masked(self, rng):
        for _ in range(200):
            value = seed.demo_landline(rng)
            assert "X" in value, value
            assert sum(c.isdigit() for c in value) <= 5, value

    def test_no_generated_number_has_a_full_subscriber_block(self, rng):
        """
        A Turkish number carries 10 subscriber digits.  Nothing the generator
        produces may contain that many digits in total.
        """
        for maker in (seed.demo_mobile, seed.demo_landline):
            for _ in range(200):
                value = maker(rng)
                assert sum(c.isdigit() for c in value) < 10, value

    def test_tax_numbers_are_obviously_synthetic(self, rng):
        for _ in range(200):
            value = seed.demo_tax_number(rng)
            assert value.startswith("0000"), value
            assert len(value) == 10, value

    def test_emails_use_a_reserved_unresolvable_domain(self, rng):
        value = seed.demo_email(rng, "ahmet-yilmaz")
        assert value.endswith("@demo.invalid")
        # RFC 2606 reserves .invalid; .local is an mDNS name and can resolve.
        assert not value.endswith(".local")


# ---------------------------------------------------------------------------
# Source-level guard
# ---------------------------------------------------------------------------
class TestGeneratorSourceStaysClean:
    @pytest.fixture
    def source(self) -> str:
        with open(seed.__file__, encoding="utf-8") as fh:
            return fh.read()

    def test_no_inline_phone_number_construction(self, source):
        """
        Contact details must come from the masking helpers, never be assembled
        inline.  These are the exact shapes that were removed.
        """
        for pattern in (
            'f"05{rng.randint',
            'f"0{rng.randint(212',
            "randint(300000000",
            "randint(1000000, 9999999)",
        ):
            assert pattern not in source, f"inline contact generation is back: {pattern}"

    def test_phone_fields_are_assigned_from_the_helpers(self, source):
        assignments = re.findall(r"phone=([^,\n]+)", source)
        assert assignments, "no phone assignments found — has the seeder changed shape?"
        for value in assignments:
            value = value.strip()
            assert value.startswith(("demo_mobile", "demo_landline", "user.", "sp.")), value

    def test_no_dot_local_addresses_remain(self, source):
        assert "@demo.local" not in source


# ---------------------------------------------------------------------------
# End-to-end: what actually lands in the database
# ---------------------------------------------------------------------------
class TestSeededRowsAreMasked:
    """
    Runs the real generators the way the seeder calls them and checks the
    values that would be written.  A full seed run is far too slow for the unit
    suite; the source guard above is what stops the call sites drifting.
    """

    def test_a_batch_of_generated_contacts_is_uniformly_masked(self):
        r = random.Random(seed.SEED)
        produced = [seed.demo_mobile(r) for _ in range(50)] + [
            seed.demo_landline(r) for _ in range(50)
        ]
        assert all("X" in p for p in produced)
        assert all(p.startswith("+90 ") for p in produced)
        # Variety is preserved — the masking is not collapsing everything to
        # one string, which would make list screens look broken.
        assert len(set(produced)) > 20
