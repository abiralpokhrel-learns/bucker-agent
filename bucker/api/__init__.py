"""HTTP surface (BUILD_PLAN steps 24, 33).

JSON API: POST /tasks, GET /tasks, GET /tasks/{id}, GET /tasks/{id}/events,
POST /tasks/{id}/replay. Pages: GET /, GET /tasks/new,
GET /tasks/{id}/dashboard, GET /tasks/{id}/replay.
Bearer-token auth, per-project scoped keys for multi-tenant deployments.
"""

from bucker.api.app import app

__all__ = ["app"]
