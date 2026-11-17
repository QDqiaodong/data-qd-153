"""Optimal adaptive fault-diagnosis policy generation (schema v3).

The planner enumerates the full joint space of basic-event assignments of the
static fault tree and derives the conditional relevance of every paid test from
those shared worlds, never from independent marginal products. See
docs/contract.md for the full wire and semantics contract.
"""
