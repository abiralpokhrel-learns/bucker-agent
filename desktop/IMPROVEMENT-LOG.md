# Improvement log (sequential verified cycles)

## Cycle 1 — actionable errors for rejected keys and rate limits (2026-09-18)
- **Research:** OpenRouter limits docs confirm 401 invalid key, 402 insufficient credits, 429 free-model caps; errors after 200 arrive as SSE events.
- **Change:** `bucker/desktop_gateway/diagnostics.py` now maps every closed-vocabulary category to a status code plus a Settings-pointing hint, with a single-reason collapse for all-providers-failed. `protocol.py` uses it on the non-streaming path too.
- **Test:** tests/test_desktop_stream_errors.py::test_rejected_key_is_not_reported_as_stream_interruption (both stream and non-stream) passes, plus inline-error and truncated-stream cases.
- **Verified:** 6/6 pass; upstream bodies never surface.
- **Live proof (real gateway + real Groq, invalid key):** curl over SSE now returns
  `{"type":"authentication_error","message":"Provider rejected the API key. Replace it in Settings → Providers.","code":401}`
  instead of the old `stream interrupted`. Full gateway suite 25/25. Hermes was the caller
  that retried 3x on the old opaque message; the fix removes the trigger, and Hermes'
  own retry loop still guards transient network errors.
