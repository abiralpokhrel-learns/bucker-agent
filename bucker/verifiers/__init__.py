"""Verifier plugins (BUILD_PLAN steps 20-21, 35).

There is no universal "is this good?" function. Each domain registers its own
objective check: code -> tests/lint, research -> citation consistency. The
registry maps a verifier name (from the Task contract) to an implementation.

A verifier never asks a model. That rule is enforced by a test rather than
remembered (see tests/test_verifiers.py).
"""

from bucker.verifiers.base import (
    VerificationResult,
    Verifier,
    VerifierNotFound,
    available,
    for_task_type,
    get,
    register,
)
from bucker.verifiers.python_test_runner import (
    NoopVerifier,
    PythonTestRunner,
    register_builtins,
)

__all__ = [
    "NoopVerifier",
    "PythonTestRunner",
    "VerificationResult",
    "Verifier",
    "VerifierNotFound",
    "available",
    "for_task_type",
    "get",
    "register",
    "register_builtins",
]
