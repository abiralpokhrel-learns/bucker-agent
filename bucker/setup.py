"""Setup wizard: propose and apply a free-first model configuration.

`bucker setup` walks the user through what this machine can actually use:
local Ollama models (free, private), OpenRouter free tiers (free, hosted),
and OpenRouter paid models. It proposes a deterministic free-first chain
(see bucker.models.suggest_chain) and can write it into .env.

The proposal is pure; writing .env is a small, line-preserving operation
(only BUCKER_MODEL / BUCKER_MODEL_FALLBACKS are touched — everything else
in .env survives byte-for-byte).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bucker.models import suggest_chain

_MODEL_RE = re.compile(r"^BUCKER_MODEL=")
_FALLBACKS_RE = re.compile(r"^BUCKER_MODEL_FALLBACKS=")


def propose_env(
    *,
    ollama_models: list[str],
    openrouter_key_ok: bool,
    current_model: str = "",
    current_fallbacks: list[str] | None = None,
) -> dict[str, Any]:
    """The wizard's proposal: a free-first chain + what changed.

    Returns a dict with:
      chain         — [primary, *fallbacks]
      reasoning     — human lines explaining each choice
      unchanged     — True when the proposal equals current config
    """
    chain = suggest_chain(
        ollama_models=ollama_models,
        openrouter_key_ok=openrouter_key_ok,
    )
    current_fallbacks = current_fallbacks or []

    reasoning: list[str] = []
    if ollama_models:
        reasoning.append(
            f"local Ollama has: {', '.join(ollama_models[:6])}"
            + (" (…)" if len(ollama_models) > 6 else "")
        )
    else:
        reasoning.append("Ollama is unreachable or has no models pulled — "
                         "run: ollama pull qwen2.5-coder:7b")

    if openrouter_key_ok:
        reasoning.append("OpenRouter key looks valid — free hosted tier is "
                         "available as a fallback")
    else:
        reasoning.append("no usable OpenRouter key — hosted models are "
                         "skipped (set OPENROUTER_API_KEY to add them)")

    if not chain:
        chain = ["ollama/qwen2.5-coder:7b"]  # explicit default, clearly marked
        reasoning.append("nothing detected — falling back to the documented "
                         "default (pull it with: ollama pull qwen2.5-coder:7b)")

    unchanged = (
        chain == [current_model, *current_fallbacks]
        or (chain and chain[0] == current_model
            and set(chain[1:]) == set(current_fallbacks))
    )

    return {
        "chain": chain,
        "primary": chain[0] if chain else "",
        "fallbacks": chain[1:] if chain else [],
        "reasoning": reasoning,
        "unchanged": unchanged,
    }


def apply_env(proposal: dict[str, Any], env_path: Path) -> list[str]:
    """Write the proposal's chain into .env (line-preserving).

    Only BUCKER_MODEL and BUCKER_MODEL_FALLBACKS lines are replaced; all
    other lines (keys, comments, blank lines) are kept byte-for-byte.
    Returns the list of changed settings ("BUCKER_MODEL=...", ...).
    """
    primary = proposal["primary"]
    fallbacks = proposal["fallbacks"]

    fallback_line = ""
    if fallbacks:
        fallback_line = f"BUCKER_MODEL_FALLBACKS={','.join(fallbacks)}\n"

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) \
        if env_path.exists() else []
    new_lines: list[str] = []
    changed: list[str] = []
    model_written = fallback_written = False

    for line in lines:
        if _MODEL_RE.match(line):
            new_lines.append(f"BUCKER_MODEL={primary}\n")
            changed.append(f"BUCKER_MODEL={primary}")
            model_written = True
        elif _FALLBACKS_RE.match(line):
            if fallback_line:
                new_lines.append(fallback_line)
                changed.append(fallback_line.rstrip("\n"))
            # else: drop the empty fallbacks line
            fallback_written = True
        else:
            new_lines.append(line)

    if not model_written:
        new_lines.append(f"BUCKER_MODEL={primary}\n")
        changed.append(f"BUCKER_MODEL={primary}")
    if fallback_line and not fallback_written:
        new_lines.append(fallback_line)
        changed.append(fallback_line.rstrip("\n"))

    env_path.write_text("".join(new_lines), encoding="utf-8")
    return changed
