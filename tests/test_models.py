"""Model catalog + provider layer + setup wizard tests.

Pure tests: catalog integrity, deterministic chain suggestion, the .env
writer, and tier/parse helpers. The live probes (Ollama/OpenRouter) are
covered by the CLI and API integration tests instead.
"""

from __future__ import annotations

from pathlib import Path

from bucker.models import (
    CATALOG,
    by_id,
    provider_of,
    suggest_chain,
    tier_of,
)
from bucker.providers import (
    deepseek_key_status,
    openrouter_key_status,
    parse_model_chain,
)
from bucker.setup import apply_env, propose_env

# ------------------------------------------------------------ catalog ----


def test_catalog_ids_are_router_ids():
    """Every id is exactly what BUCKER_MODEL accepts."""
    for m in CATALOG:
        assert m.id.startswith(("ollama/", "openrouter/", "deepseek/")), m.id
        assert "/" in m.id and not m.id.endswith("/")


def test_catalog_tiers_are_known():
    for m in CATALOG:
        assert m.tier in ("local", "free", "paid"), m.id
        assert m.context > 0, m.id
        assert m.name and m.notes, m.id


def test_catalog_has_no_duplicate_ids():
    ids = [m.id for m in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_covers_all_three_tiers():
    assert {m.tier for m in CATALOG} == {"local", "free", "paid"}


def test_free_models_have_documented_daily_limits():
    for m in CATALOG:
        if m.tier == "free":
            assert m.daily_limit and m.daily_limit > 0, m.id
        else:
            assert m.daily_limit is None, m.id


def test_free_tier_rows_compute_remaining():
    from bucker.models import free_tier_rows

    rows = free_tier_rows({
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free": 47,
    })
    nemotron = next(r for r in rows
                    if r["model"] == "openrouter/nvidia/nemotron-3-super-120b-a12b:free")
    assert nemotron["limit"] == 50
    assert nemotron["calls_today"] == 47
    assert nemotron["remaining"] == 3
    assert nemotron["pct"] == 94.0


def test_free_tier_rows_never_go_negative():
    from bucker.models import free_tier_rows

    rows = free_tier_rows({
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free": 200,
    })
    assert rows[0]["remaining"] == 0
    assert rows[0]["pct"] == 100.0


def test_free_tier_rows_only_cover_catalogued_free_models():
    from bucker.models import CATALOG, free_tier_rows

    rows = free_tier_rows({"ollama/qwen2.5-coder:7b": 999, "some/unknown": 5})
    # Every row is a catalogued FREE model; paid/local/unknown counts in the
    # input never create rows.
    assert rows
    known_free = {m.id for m in CATALOG if m.tier == "free"}
    assert {r["model"] for r in rows} == known_free
    # The unknown + local models contributed nothing.
    assert all(r["calls_today"] == 0 for r in rows)


def test_tier_of_known_and_unknown():
    assert tier_of("ollama/qwen2.5-coder:7b") == "local"
    assert tier_of("openrouter/nvidia/nemotron-3-super-120b-a12b:free") == "free"
    assert tier_of("openrouter/anthropic/claude-sonnet-4.5") == "paid"
    # Any ollama model is local even if uncatalogued; unknown otherwise.
    assert tier_of("ollama/anything-else") == "local"
    assert tier_of("openrouter/unknown/x") == "unknown"


def test_by_id_and_provider_of():
    assert by_id("ollama/qwen2.5-coder:7b").name == "Qwen 2.5 Coder 7B"
    assert by_id("nope/nothing") is None
    assert provider_of("openrouter/x") == "openrouter"
    assert provider_of("nope") == "unknown"


# ------------------------------------------------------ chain suggestion ----


def test_suggest_chain_free_first_with_local():
    chain = suggest_chain(
        ollama_models=["qwen2.5-coder:3b", "qwen2.5-coder:7b"],
        openrouter_key_ok=True,
        deepseek_key_ok=True,
    )
    assert chain[0] == "ollama/qwen2.5-coder:7b"  # best local first
    assert "openrouter/nvidia/nemotron-3-super-120b-a12b:free" in chain
    # Paid OpenRouter models are NEVER suggested — free tier only.
    assert not any(":free" not in m and m.startswith("openrouter/") for m in chain)
    assert chain[-1] == "deepseek/deepseek-v4-flash"  # DeepSeek is the paid step


def test_suggest_chain_skips_hosted_without_key():
    chain = suggest_chain(ollama_models=["qwen2.5-coder:7b"], openrouter_key_ok=False)
    assert len(chain) == 1
    assert chain[0] == "ollama/qwen2.5-coder:7b"


def test_suggest_chain_without_ollama_uses_free_only():
    chain = suggest_chain(ollama_models=[], openrouter_key_ok=True)
    assert chain[0] == "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    # No DeepSeek without its own key.
    assert "deepseek/" not in chain


def test_suggest_chain_requires_deepseek_key_for_deepseek():
    chain = suggest_chain(ollama_models=[], openrouter_key_ok=False,
                          deepseek_key_ok=True)
    assert chain == ["deepseek/deepseek-v4-flash"]
    no_ds = suggest_chain(ollama_models=[], openrouter_key_ok=False,
                          deepseek_key_ok=False)
    assert no_ds == []


def test_suggest_chain_is_deterministic():
    a = suggest_chain(
        ollama_models=["qwen2.5-coder:7b", "qwen2.5-coder:3b"],
        openrouter_key_ok=True,
    )
    b = suggest_chain(
        ollama_models=["qwen2.5-coder:7b", "qwen2.5-coder:3b"],
        openrouter_key_ok=True,
    )
    assert a == b


def test_suggest_chain_picks_any_local_model_as_last_resort():
    chain = suggest_chain(ollama_models=["mistral:7b"], openrouter_key_ok=False)
    assert chain == ["ollama/mistral:7b"]


# --------------------------------------------------------- setup wizard ----


def test_propose_env_reasoning_is_honest():
    p = propose_env(ollama_models=[], openrouter_key_ok=False)
    assert p["chain"]  # always a usable default
    assert any("ollama pull" in r for r in p["reasoning"])
    assert any("OPENROUTER_API_KEY" in r for r in p["reasoning"])


def test_propose_env_unchanged_when_matching():
    p = propose_env(
        ollama_models=["qwen2.5-coder:7b"],
        openrouter_key_ok=True,
        deepseek_key_ok=True,
        current_model="ollama/qwen2.5-coder:7b",
        current_fallbacks=["openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                           "deepseek/deepseek-v4-flash"],
    )
    assert p["unchanged"] is True


def test_apply_env_writes_only_model_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nOPENROUTER_API_KEY=sk-or-v1-secret\n"
        "BUCKER_MODEL=openrouter/old-model\n"
        "BUCKER_MODEL_FALLBACKS=openrouter/old-fallback\n"
        "BUCKER_MAX_TOKENS_WORKER=3000\n",
        encoding="utf-8",
    )
    proposal = {
        "primary": "ollama/qwen2.5-coder:7b",
        "fallbacks": ["openrouter/nvidia/nemotron-3-super-120b-a12b:free"],
    }
    changed = apply_env(proposal, env)
    text = env.read_text(encoding="utf-8")
    assert "BUCKER_MODEL=ollama/qwen2.5-coder:7b" in text
    assert "BUCKER_MODEL_FALLBACKS=openrouter/nvidia/nemotron-3-super-120b-a12b:free" in text
    assert "OPENROUTER_API_KEY=sk-or-v1-secret" in text  # untouched
    assert "BUCKER_MAX_TOKENS_WORKER=3000" in text       # untouched
    assert "# comment" in text                            # untouched
    assert "old-model" not in text
    assert changed[0] == "BUCKER_MODEL=ollama/qwen2.5-coder:7b"


def test_apply_env_appends_when_missing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SOME_KEY=1\n", encoding="utf-8")
    changed = apply_env(
        {"primary": "ollama/qwen2.5-coder:7b", "fallbacks": []}, env
    )
    text = env.read_text(encoding="utf-8")
    assert "SOME_KEY=1" in text
    assert "BUCKER_MODEL=ollama/qwen2.5-coder:7b" in text
    assert len(changed) == 1


# ---------------------------------------------------------- providers ----


def test_openrouter_key_status_shape_only(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert openrouter_key_status()["ok"] is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "x" * 64)
    status = openrouter_key_status()
    assert status["ok"] is True
    assert "x" * 64 not in status["detail"]  # never leaks the value
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-key")
    assert openrouter_key_status()["ok"] is False


def test_deepseek_key_status_shape_only(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert deepseek_key_status()["ok"] is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "x" * 30)
    assert deepseek_key_status()["ok"] is True
    monkeypatch.setenv("DEEPSEEK_API_KEY", "short")
    assert deepseek_key_status()["ok"] is False


def test_parse_model_chain_annotates_tiers():
    chain = parse_model_chain(
        "ollama/qwen2.5-coder:7b",
        ["openrouter/nvidia/nemotron-3-super-120b-a12b:free"],
    )
    assert chain[0] == {"id": "ollama/qwen2.5-coder:7b",
                        "provider": "ollama", "tier": "local"}
    assert chain[1]["tier"] == "free"
    assert parse_model_chain("", []) == []
