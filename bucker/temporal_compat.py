"""Temporal SDK compatibility layer.

bucker's full stack runs on Temporal; lite mode does not. The lite import
graph (``bucker.cli`` -> ``bucker.lite`` -> ``bucker.activities.*`` and
``bucker.workflows.task_workflow``) touches modules that decorate functions
with ``@activity.defn`` / ``@workflow.defn`` at import time.

When the Temporal SDK is installed (``bucker[full]``) this module re-exports
the real objects, so full-stack behaviour is byte-for-byte unchanged. When it
is not (plain ``bucker``, i.e. lite mode), it provides inert stand-ins so
those modules import cleanly: decorators become identity functions, and
anything that would actually talk to Temporal raises a clear error if called
(which lite mode never does — it has its own in-process runner).

The lite CI job installs base deps only and boots the whole lite platform,
which is the regression test for this file.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

try:  # pragma: no cover - exercised by the lite CI job (no temporalio installed)
    from temporalio import activity as _activity  # type: ignore[import-untyped]
    from temporalio import workflow as _workflow  # type: ignore[import-untyped]
    from temporalio.common import RetryPolicy as _RetryPolicy  # type: ignore[import-untyped]

    activity = _activity
    workflow = _workflow
    RetryPolicy = _RetryPolicy
    HAS_TEMPORAL = True
except ImportError:  # lite mode: temporalio not installed
    HAS_TEMPORAL = False

    def _identity(*args, **kwargs):
        # Supports both bare (@activity.defn) and parameterised
        # (@activity.defn(name=...)) decorator forms.
        if args and len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def deco(f):
            return f

        return deco

    class _ActivityStub:
        logger = logging.getLogger("bucker")

        def defn(self, *args, **kwargs):
            return _identity(*args, **kwargs)

    class _WorkflowStub:
        def defn(self, *args, **kwargs):
            return _identity(*args, **kwargs)

        def run(self, *args, **kwargs):
            return _identity(*args, **kwargs)

        def query(self, *args, **kwargs):
            return _identity(*args, **kwargs)

        class unsafe:
            @staticmethod
            @contextmanager
            def imports_passed_through():
                yield

        @staticmethod
        def execute_activity(*args, **kwargs):
            raise RuntimeError(
                "Temporal is not installed (install bucker[full]); this code "
                "path belongs to the full stack, not lite mode"
            )

    class RetryPolicy:
        """Import-time stand-in; the real policy only matters inside a workflow."""

        def __init__(self, *args, **kwargs):
            pass

    activity = _ActivityStub()
    workflow = _WorkflowStub()
