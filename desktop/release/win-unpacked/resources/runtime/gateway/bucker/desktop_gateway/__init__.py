"""Isolated desktop inference gateway; never imports the full API app.

This process must be imported before other Bucker modules: config.py uses an
absolute repository .env path, so changing cwd is NOT sufficient isolation.
Requires python-dotenv with PYTHON_DOTENV_DISABLED support (>=1.2).
"""

import os

# Must precede even gateway package imports (its __init__ imports settings).
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# Imports below the dotenv guard are intentional: moving them breaks isolation.
# ruff: noqa: E402
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from bucker.gateway.adapters import OpenAICompatAdapter
from bucker.gateway.circuit import CircuitRegistry
from bucker.gateway.quota import QuotaManager
from bucker.gateway.registry import GatewayModel, ModelRegistry
from bucker.gateway.routing import RouterEngine

_stream_usage_reported = ContextVar("desktop_stream_usage_reported", default=False)

ALLOWED_PROVIDERS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "sambanova": "https://api.sambanova.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "huggingface": "https://router.huggingface.co/v1",
}
DESKTOP_AUTO_MODEL = "desktop-auto"


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: str
    base_url: str
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_./:@+-]+$")
    context: int = Field(gt=0)
    max_output: int = Field(gt=0)
    free: bool
    tools: bool

    @model_validator(mode="after")
    def validate_connection(self):
        if self.base_url != ALLOWED_PROVIDERS.get(self.provider):
            raise ValueError("provider endpoint is not allowlisted")
        if not self.free:
            raise ValueError("only user-confirmed free billing is allowed")
        if self.provider == "openrouter" and not self.model.endswith(":free"):
            raise ValueError("OpenRouter model must end in :free")
        if not self.api_key.get_secret_value().strip():
            raise ValueError("provider credential is required")
        return self


class DesktopAdapter(OpenAICompatAdapter):
    """Keep shared wire translation, but use only explicit credentials."""

    def __init__(self, connection):
        super().__init__(timeout_s=60)
        self.name = connection.provider
        self.base_url = connection.base_url
        self._credential = connection.api_key

    @property
    def api_key(self):
        return self._credential.get_secret_value()

    async def complete(self, req, model_id):
        from bucker.gateway.errors import ProviderUnavailableError

        try:
            result = await super().complete(req, model_id)
            if not isinstance(result.text, str):
                raise TypeError("invalid content")
            return result
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ProviderUnavailableError("malformed response", provider=self.name) from None

    def _payload(self, req, model_id):
        payload = super()._payload(req, model_id)
        if req.stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _normalize_chunk(data, text_parts, tool_acc):
        if data.get("usage"):
            _stream_usage_reported.set(True)
        return OpenAICompatAdapter._normalize_chunk(data, text_parts, tool_acc)

    async def stream(self, req, model_id):
        reset = _stream_usage_reported.set(False)
        try:
            async for event in super().stream(req, model_id):
                if event["type"] == "usage":
                    event = {**event, "reported": _stream_usage_reported.get()}
                yield event
        finally:
            _stream_usage_reported.reset(reset)

    def _client_for(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60,
                trust_env=False,
                follow_redirects=False,
            )
        return self._client


def error_response(status, category, message):
    return JSONResponse(
        status_code=status,
        content={
            "error": {"message": message, "type": category, "code": status},
        },
    )


def create_app(*, token: str, connections: list[dict]) -> FastAPI:
    """Validate parent config, construct ONLY its models and adapters."""
    if not isinstance(token, str) or not token.strip() or not token.isascii():
        raise ValueError("a nonempty ASCII desktop token is required")
    if not isinstance(connections, list) or not connections:
        raise ValueError("a nonempty connection array is required")
    try:
        configured = [Connection.model_validate(c) for c in connections]
    except ValidationError:
        # Pydantic errors include input values, potentially keys. Never emit them.
        raise ValueError("invalid desktop connection configuration") from None
    adapters, models = {}, {}
    for priority, c in enumerate(configured):
        canonical = f"{c.provider}/{c.model}"
        if canonical in models:
            raise ValueError("duplicate connection")
        if (
            c.provider in adapters
            and adapters[c.provider].api_key != c.api_key.get_secret_value()
        ):
            raise ValueError("conflicting provider credentials")
        adapters.setdefault(c.provider, DesktopAdapter(c))
        models[canonical] = GatewayModel(
            canonical_id=canonical,
            provider=c.provider,
            provider_model_id=c.model,
            family=c.model,
            context=c.context,
            max_output=c.max_output,
            capabilities=frozenset({"streaming"} | ({"tools"} if c.tools else set())),
            # User assertion of free billing isn't a verified price or quota.
            price_input_per_m=None,
            price_output_per_m=None,
            free=True,
            priority=priority,
        )
    engine = RouterEngine(
        registry=ModelRegistry(models),
        adapters=adapters,
        circuits=CircuitRegistry(threshold=3, open_for_s=30),
        quota=QuotaManager(),
        policy="free_only",
        deadline_s=90,
        timeout_s=60,
        max_retries=0,
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            for adapter in adapters.values():
                if adapter._client is not None:
                    await adapter._client.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path != "/health/live":
            scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                supplied.encode(), token.encode()
            ):
                return error_response(
                    401, "authentication_error", "invalid or missing bearer token"
                )
        return await call_next(request)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        return {"status": "ready", "provider_credentials_verified": False}

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": DESKTOP_AUTO_MODEL, "object": "model", "owned_by": "bucker"},
                *[
                    {
                        "id": m.canonical_id,
                        "object": "model",
                        "owned_by": m.provider,
                        "context_window": m.context,
                        "max_output_tokens": m.max_output,
                        "free": m.free,
                        "capabilities": sorted(m.capabilities),
                    }
                    for m in engine.registry.all()
                ],
            ],
        }

    from .protocol import install_routes

    install_routes(app, engine, error_response)
    return app
