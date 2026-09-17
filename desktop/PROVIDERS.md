# Provider entitlement snapshot — 2026-09-17

Documentation research, not authenticated inference certification. Six onboarding entries are present. Five have recurring free API access/credits useful for limited experiments; Hugging Face's small allowance is explicitly marginal. API account billing must remain free-only; selecting a provider here cannot guarantee that a paid account will never be charged.

| Provider | Documented allowance / restrictions | Official evidence |
|---|---|---|
| OpenRouter | Existing `:free` models only; 20 RPM, 50 requests/day without a qualifying credit purchase. No paid model fallback. | https://openrouter.ai/docs/api-reference/limits ; https://openrouter.ai/docs/guides/routing/model-variants/free |
| Google Gemini / AI Studio | Selected models have free API quotas. Project, model and region limits vary. Free-tier data may be used to improve products; assess code privacy. | https://ai.google.dev/gemini-api/docs/pricing ; https://ai.google.dev/gemini-api/docs/rate-limits ; https://ai.google.dev/gemini-api/docs/openai |
| Groq | Recurring free plan; token-per-minute caps often dominate context budget. Organization/model limits apply. | https://console.groq.com/docs/rate-limits ; https://console.groq.com/docs/tool-use |
| Mistral | Free API mode, no credit card required; current pricing advertises monthly credits. Not an unlimited Experiment plan. | https://mistral.ai/pricing ; https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key |
| SambaNova | Free tier without payment method; listed production models have 20 RPM / 20 RPD / 200,000 TPD. Preview models are not production guarantees. | https://docs.sambanova.ai/docs/en/models/rate-limits ; https://docs.sambanova.ai/docs/en/features/function-calling |
| Hugging Face | $0.10/month on free accounts, subject to change, HF-routed inference only. Too small to market as sustained coding capacity. | https://huggingface.co/docs/inference-providers/pricing ; https://huggingface.co/docs/inference-providers/guides/function-calling |

OpenRouter's public model catalog was fetched directly during verification. The old Kimi/Qwen free IDs in the scaffold were absent. `nvidia/nemotron-3-ultra-550b-a55b:free` was present with zero prompt/completion prices, tools support, 1,000,000 context and 65,536 max output: https://openrouter.ai/api/v1/models . This is a snapshot, not a future availability guarantee.

SambaNova's documented free production `gpt-oss-120b` replaced an unverified Qwen catalog entry. Other catalog model metadata must still be checked using each connected account before release. Tool-calling support in documentation does not prove preservation of provider-specific multi-turn metadata by our generic adapter.

## Researched but not included

- Cloudflare Workers AI: genuine recurring 10,000 Neurons/day; needs account ID plus token and model eligibility handling. Not implemented in this desktop. https://developers.cloudflare.com/workers-ai/platform/pricing
- Cerebras: current official docs describe time-limited new-account credit after adding a payment method, not a permanent free tier. Removed. https://inference-docs.cerebras.ai/support/rate-limits
- NVIDIA hosted Build/NIM: prototype access is not a verified renewing production entitlement. https://docs.api.nvidia.com/nim/docs/product
- DeepSeek direct: paid optional, not counted as free. https://api-docs.deepseek.com/quick_start/pricing

No API keys, account actions, real paid inference, or successful authenticated coding run were used to create this research.
