"""Batch EM calibration of shared basic-event probability groups (schema v5).

The v5 entry point turns repeated partial node readings into a traceable
calibration parameter table by reusing the original 16-event fault-tree
parser, the shared AND/OR DAG and the whole-graph validation.  See
docs/contract.md for the wire contract.
"""
from .model import calibrate, parse_calibration_input

__all__ = ['calibrate', 'parse_calibration_input']
