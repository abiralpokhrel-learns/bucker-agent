"""Verifier plugins (BUILD_PLAN steps 20-21, 35).

There is no universal "is this good?" function. Each domain registers its own
objective check: code -> tests/lint, research -> citation consistency. The
registry maps a verifier name (from the Task contract) to an implementation.
"""
