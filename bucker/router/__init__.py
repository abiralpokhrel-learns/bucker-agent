"""Model router (BUILD_PLAN step 14).

Thin LiteLLM wrapper. The model name comes from config/env ONLY — this package
is the mechanism behind "the LLM is a replaceable plugin". Every request and
response is stored verbatim to blob storage so replay can answer from the
archive instead of re-invoking the model.
"""
