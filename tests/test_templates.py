"""Task templates: registry integrity and resolution."""

from __future__ import annotations

import pytest

from bucker.templates import TEMPLATES, UnknownTemplateError, list_templates, resolve_template


def test_templates_have_required_fields():
    """Every template is a complete preset, not a stub."""
    for tid, t in TEMPLATES.items():
        assert tid
        assert t["name"], tid
        assert t["description"], tid
        assert t["objective"], tid
        assert t["task_type"], tid
        # Limits are either a number or an explicit None (no default).
        assert t["default_budget_usd"] is None or t["default_budget_usd"] >= 0
        assert t["default_deadline_minutes"] is None or t["default_deadline_minutes"] >= 1
        assert t["default_max_retries"] >= 0


def test_demo_template_is_the_last_listed():
    """The five-step demo is the fallback starter, not the headline."""
    ids = [t["id"] for t in list_templates()]
    assert ids[0] == "code-fix"
    assert ids[-1] == "demo"


def test_resolve_template_returns_the_dict():
    t = resolve_template("code-fix")
    assert t["name"] == "Code fix"
    assert "test suite passes" in t["objective"]


def test_resolve_unknown_template_raises_with_known_list():
    with pytest.raises(UnknownTemplateError) as exc:
        resolve_template("nope")
    assert "code-fix" in str(exc.value)


def test_list_templates_is_stable():
    a = [t["id"] for t in list_templates()]
    b = [t["id"] for t in list_templates()]
    assert a == b
