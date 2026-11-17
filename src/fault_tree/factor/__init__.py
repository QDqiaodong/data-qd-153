"""Joint conditional risk tables over shared fault-tree structure (schema v4).

The v4 entry point combines per-event Bernoulli priors, cross-event potential
factors, shared AND/OR gate topology and node readings, then eliminates them in
a caller-specified order to produce one exact conditional joint probability
table over a requested node group. See docs/contract.md for the wire contract.
"""
