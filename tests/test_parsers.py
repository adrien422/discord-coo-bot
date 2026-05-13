"""Marker parsers and small text-handling helpers."""
from __future__ import annotations


def test_parse_kv_quoted_and_bare(bot_module):
    m = bot_module
    result = m._parse_kv(
        'slug="customer-onboarding" kind=factsheet-team owner_person_id=12345 '
        'name="On-boarding flow"'
    )
    assert result == {
        "slug": "customer-onboarding",
        "kind": "factsheet-team",
        "owner_person_id": "12345",
        "name": "On-boarding flow",
    }


def test_normalize_message_text_collapses_wrap(bot_module):
    raw = (
        "Got it — channel manager for short-term\n"
        "  rentals, you and Adrien. Two quick ones to anchor the\n"
        " map: how does the team split?"
    )
    out = bot_module.normalize_message_text(raw)
    assert "\n" not in out
    assert "  " not in out


def test_normalize_message_text_strips_tui_artifact(bot_module):
    raw = "Message body ⎿ Interrupted · What should Claude do instead? more text"
    out = bot_module.normalize_message_text(raw)
    assert "Interrupted" not in out
    assert "Message body" in out
    assert "more text" in out


def test_normalize_preserves_paragraph_breaks(bot_module):
    raw = "First paragraph here.\n\nSecond paragraph here."
    out = bot_module.normalize_message_text(raw)
    assert "\n\n" in out


def test_match_action_exact(bot_module):
    assert bot_module._match_action("GET:/contacts", "GET:/contacts")
    assert not bot_module._match_action("POST:/contacts", "GET:/contacts")


def test_match_action_wildcard(bot_module):
    assert bot_module._match_action("GET:/contacts/123", "GET:/contacts/*")
    assert not bot_module._match_action(
        "GET:/contacts/123/extra", "GET:/contacts/*"
    )


def test_phase_approval_re(bot_module):
    assert bot_module.PHASE_APPROVAL_RE.search("I approve phase 2")
    assert bot_module.PHASE_APPROVAL_RE.search("APPROVE Phase 3 now")
    assert bot_module.PHASE_APPROVAL_RE.search("approve phase 2 right now")
    assert not bot_module.PHASE_APPROVAL_RE.search("phase 2 approved later")
    assert not bot_module.PHASE_APPROVAL_RE.search("disapprove phase 2")


def test_decision_parser_accepts_field_aliases(bot_module):
    m = bot_module
    # Canonical form
    a = m._parse_decision_fields(
        'title="A" text="B" rationale="C"'
    )
    assert a == ("A", "B", "C", None)
    # Claude's variant: subject/description
    b = m._parse_decision_fields(
        'subject="X" description="Y"'
    )
    assert b == ("X", "Y", None, None)


def test_coo_to_extracts_target_and_body(bot_module):
    text = (
        "[[COO_TO user_id=12345]] Hi there, this is the body.\n"
        "[[COO_NEXT_CONTACT user_id=12345 in_seconds=3600 reason=test]]"
    )
    matches = list(bot_module.COO_TO_RE.finditer(text))
    assert len(matches) == 1
    assert matches[0].group(1) == "12345"
    body = bot_module.normalize_message_text(matches[0].group(2))
    assert body.startswith("Hi there, this is the body.")
    # The lookahead should have stopped before the next [[COO_ marker
    assert "[[COO_NEXT_CONTACT" not in body


def test_http_call_re_captures_components(bot_module):
    text = "[[COO_HTTP_CALL slug=mycrm method=POST path=/contacts body='{\"x\":1}']]"
    m = bot_module.COO_HTTP_CALL_RE.search(text)
    assert m, "regex didn't match"
    assert m.group(1) == "mycrm"
    assert m.group(2) == "POST"
    assert m.group(3) == "/contacts"
    assert m.group(4) == '{"x":1}'


def test_fact_marker_extracts_three_fields(bot_module):
    text = '[[COO_FACT subject="company" predicate="founded" object="2025"]]'
    m = bot_module.COO_FACT_RE.search(text)
    assert m.group(1) == "company"
    assert m.group(2) == "founded"
    assert m.group(3) == "2025"


def test_commitment_marker_optional_due(bot_module):
    text_with_due = (
        '[[COO_COMMITMENT person_id=1234 description="ship feature" due="2026-06-01"]]'
    )
    text_no_due = (
        '[[COO_COMMITMENT person_id=1234 description="ship feature"]]'
    )
    m1 = bot_module.COO_COMMITMENT_RE.search(text_with_due)
    m2 = bot_module.COO_COMMITMENT_RE.search(text_no_due)
    assert m1.group(3) == "2026-06-01"
    assert m2.group(3) is None
