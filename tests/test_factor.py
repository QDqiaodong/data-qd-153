"""Tests for the v4 joint conditional risk table product (`fault-factor`).

The expected numbers here never come from the production bucket-elimination
code.  A separately written oracle enumerates every basic-event world, applies
the priors, every potential factor (rows interpreted in the *declared* scope
order) and the deterministic AND/OR DAG, then filters by evidence and query to
produce Z, N and the joint table directly.

The structural audit is checked both by hand on targeted scenarios and by an
independent scope-only replay, plus the pair of inputs whose elimination orders
widen or narrow a bucket.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path

from fault_tree.factor.model import analyze, parse_request, structural_audit

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Independent brute-force oracle
# ---------------------------------------------------------------------------

def _oracle_envelope(request):
    """Recompute the DAG order and node values independently of production."""
    tree = request['tree']
    events = {e['id']: Fraction(e['numerator'], e['denominator']) for e in tree['events']}
    gates = {g['id']: {'op': g['op'], 'inputs': list(g['inputs'])} for g in tree['gates']}
    pending = set(gates)
    resolved = set(events)
    order = []
    while pending:
        ready = [g for g in pending if set(gates[g]['inputs']) <= resolved]
        if not ready:
            raise ValueError('cycle')
        for g in ready:
            order.append(g)
        resolved.update(ready)
        pending -= set(ready)
    return events, gates, order


def _oracle_worlds(request):
    events, gates, order = _oracle_envelope(request)
    names = sorted(events)
    worlds = []
    for bits in product((False, True), repeat=len(names)):
        values = dict(zip(names, bits))
        for gid in order:
            gate = gates[gid]
            operands = [values[x] for x in gate['inputs']]
            values[gid] = all(operands) if gate['op'] == 'AND' else any(operands)
        weight = Fraction(1)
        for name in names:
            p = events[name]
            weight *= p if values[name] else 1 - p
        for factor in request['factors']:
            scope = factor['scope']
            row = 0
            for name in scope:
                row = (row << 1) | int(values[name])
            value = factor['values'][row]
            weight *= Fraction(value['numerator'], value['denominator'])
        worlds.append({'values': values, 'weight': weight})
    return worlds


def _fraction_text(value):
    return str(value.numerator) if value.denominator == 1 else '%d/%d' % (value.numerator, value.denominator)


def oracle_result(request):
    worlds = _oracle_worlds(request)
    evidence = request['evidence']
    query = sorted(request['query'], key=lambda s: s.encode('utf-8'))
    total = sum((world['weight'] for world in worlds), Fraction(0))
    variables = query
    if total == 0:
        return {'status': 'zero_model', 'normalizer': '0', 'evidence_probability': None,
                'variables': variables, 'joint': []}
    matching = [world for world in worlds
                if all(world['values'][item['node']] == item['failed']
                       for item in evidence)]
    evidence_mass = sum((world['weight'] for world in matching), Fraction(0))
    if evidence_mass == 0:
        return {'status': 'zero_evidence', 'normalizer': _fraction_text(total),
                'evidence_probability': '0', 'variables': variables, 'joint': []}
    joint = []
    for bits in product((False, True), repeat=len(variables)):
        mass = sum((world['weight'] for world in matching
                    if all(world['values'][node] == bit
                           for node, bit in zip(variables, bits))), Fraction(0))
        joint.append({'failed': list(bits), 'probability': _fraction_text(mass / evidence_mass)})
    return {'status': 'ok', 'normalizer': _fraction_text(total),
            'evidence_probability': _fraction_text(evidence_mass / total),
            'variables': variables, 'joint': joint}


def oracle_audit(request):
    """Independent scope-only replay of the elimination order."""
    tree = request['tree']
    scopes = [(e['id'],) for e in tree['events']]
    scopes += [tuple(f['scope']) for f in request['factors']]
    scopes += [tuple([g['id']] + list(g['inputs'])) for g in tree['gates']]

    def canon(names):
        return tuple(sorted(set(names), key=lambda s: s.encode('utf-8')))

    scopes = [canon(scope) for scope in scopes]
    peak = max((len(scope) for scope in scopes), default=0)
    steps = []
    for variable in request['elimination_order']:
        bucket = [scope for scope in scopes if variable in scope]
        bucket_scope = canon(name for scope in bucket for name in scope)
        residual = tuple(name for name in bucket_scope if name != variable)
        scopes = [scope for scope in scopes if variable not in scope] + [residual]
        peak = max(peak, len(bucket_scope), len(residual))
        steps.append({'variable': variable, 'bucket_scope': list(bucket_scope),
                      'residual_scope': list(residual)})
    final = canon(name for scope in scopes for name in scope)
    peak = max(peak, len(final))
    return {'steps': steps, 'final_scope': list(final), 'peak_variables': peak}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def two_event_request():
    return {
        'tree': {
            'events': [{'id': 'A', 'numerator': 1, 'denominator': 3},
                       {'id': 'B', 'numerator': 1, 'denominator': 4}],
            'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
            'root': 'g'},
        'factors': [
            {'id': 'fBA', 'scope': ['B', 'A'], 'values': [
                {'numerator': 1, 'denominator': 1},
                {'numerator': 2, 'denominator': 1},
                {'numerator': 1, 'denominator': 2},
                {'numerator': 3, 'denominator': 1}]},
        ],
        'evidence': [{'node': 'B', 'failed': True}],
        'query': ['g', 'A'],
        'elimination_order': ['B'],
    }


SHARED_TREE = {
    'events': [{'id': 'A', 'numerator': 1, 'denominator': 10},
               {'id': 'B', 'numerator': 1, 'denominator': 5},
               {'id': 'C', 'numerator': 3, 'denominator': 10},
               {'id': 'D', 'numerator': 1, 'denominator': 2}],
    'gates': [{'id': 'AB', 'op': 'AND', 'inputs': ['A', 'B']},
              {'id': 'AC', 'op': 'AND', 'inputs': ['A', 'C']},
              {'id': 'top', 'op': 'OR', 'inputs': ['AB', 'AC', 'D']},
              {'id': 'side', 'op': 'OR', 'inputs': ['D', 'B']}],
    'root': 'top',
}

SHARED_REQUEST = {
    'tree': SHARED_TREE,
    'factors': [
        {'id': 'ab_coupling', 'scope': ['A', 'B'], 'values': [
            {'numerator': 1, 'denominator': 1},
            {'numerator': 3, 'denominator': 1},
            {'numerator': 1, 'denominator': 2},
            {'numerator': 4, 'denominator': 1}]},
        {'id': 'cd_coupling', 'scope': ['C', 'D'], 'values': [
            {'numerator': 2, 'denominator': 1},
            {'numerator': 1, 'denominator': 3},
            {'numerator': 1, 'denominator': 1},
            {'numerator': 5, 'denominator': 2}]},
    ],
    'evidence': [{'node': 'B', 'failed': True}],
    'query': ['top', 'AB'],
    'elimination_order': ['A', 'C', 'D', 'B', 'AC', 'side'],
}


def clone(obj):
    return json.loads(json.dumps(obj))


class OracleComparison(unittest.TestCase):
    def compare(self, request):
        spec = parse_request(request)
        expected_audit = oracle_audit(request)
        actual = analyze(request)
        expected = oracle_result(request)
        self.assertEqual(actual['audit'], expected_audit)
        for key in expected:
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(actual['audit'], structural_audit(spec))
        return actual

    def test_shared_gates_and_overlapping_potentials(self):
        report = self.compare(SHARED_REQUEST)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['variables'], ['AB', 'top'])
        self.assertEqual(len(report['joint']), 4)
        self.assertAlmostEqual(sum(Fraction(row['probability']) for row in report['joint']), 1)

    def test_two_event_ordered_scope(self):
        self.compare(two_event_request())

    def test_random_fuzz(self):
        import random
        rng = random.Random(4711)
        for seed in range(200):
            rng.seed(seed)
            n = rng.randint(1, 4)
            probs = [Fraction(0), Fraction(1), Fraction(1, 2), Fraction(1, 3), Fraction(3, 5)]
            events = []
            for i in range(n):
                p = rng.choice(probs)
                events.append({'id': 'E%d' % i, 'numerator': p.numerator,
                               'denominator': p.denominator})
            nodes = [e['id'] for e in events]
            gates = []
            for g in range(rng.randint(0, 3)):
                if len(nodes) < 2:
                    break
                inputs = rng.sample(nodes, 2)
                gates.append({'id': 'G%d' % g, 'op': rng.choice(['AND', 'OR']),
                              'inputs': inputs})
                nodes.append('G%d' % g)
            tree = {'events': events, 'gates': gates, 'root': rng.choice(nodes)}
            factors = []
            for f in range(rng.randint(0, 2)):
                size = rng.randint(1, min(3, n))
                scope = rng.sample([e['id'] for e in events], size)
                values = []
                for _ in range(1 << size):
                    values.append({'numerator': rng.randint(0, 6),
                                   'denominator': rng.randint(1, 5)})
                factors.append({'id': 'F%d' % f, 'scope': scope, 'values': values})
            evidence = []
            for node in rng.sample(nodes, rng.randint(0, min(2, len(nodes)))):
                evidence.append({'node': node, 'failed': rng.choice([True, False])})
            query = rng.sample(nodes, rng.randint(1, min(3, len(nodes))))
            rest = [node for node in nodes if node not in query]
            rng.shuffle(rest)
            request = {'tree': tree, 'factors': factors, 'evidence': evidence,
                       'query': query, 'elimination_order': rest}
            self.compare(request)


class TargetedScenarios(unittest.TestCase):
    def test_scope_column_order_is_semantic(self):
        base = clone(SHARED_REQUEST)
        # Reordered scope with the matching value reshuffle is invariant.
        swapped = clone(SHARED_REQUEST)
        swapped['factors'][0]['scope'] = ['B', 'A']
        original_values = swapped['factors'][0]['values']
        # rows for [A,B] = (F,F),(F,T),(T,F),(T,T); for [B,A] the columns are
        # reversed, so the row index bits swap.
        swapped['factors'][0]['values'] = [original_values[0], original_values[2],
                                           original_values[1], original_values[3]]
        self.assertEqual(analyze(base)['joint'], analyze(swapped)['joint'])
        # Only swapping the scope and keeping values samples different rows.
        reordered_only = clone(SHARED_REQUEST)
        reordered_only['factors'][0]['scope'] = ['B', 'A']
        self.assertNotEqual(analyze(reordered_only)['joint'], analyze(base)['joint'])

    def test_factor_id_does_not_affect_numbers(self):
        first = clone(SHARED_REQUEST)
        second = clone(SHARED_REQUEST)
        second['factors'][0]['id'] = 'renamed'
        self.assertEqual(analyze(first)['joint'], analyze(second)['joint'])
        self.assertEqual(analyze(first)['normalizer'], analyze(second)['normalizer'])

    def test_scalar_residual_factor_propagates(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 4},
                                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [], 'root': 'A'},
            'factors': [],
            'evidence': [],
            'query': ['A'],
            'elimination_order': ['B'],
        }
        report = analyze(request)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['normalizer'], '1')
        self.assertEqual(report['joint'][0]['failed'], [False])
        self.assertEqual(report['joint'][0]['probability'], '3/4')
        self.assertEqual(report['joint'][1]['probability'], '1/4')
        self.assertEqual(report['audit']['steps'][0]['residual_scope'], [])
        self.assertEqual(report['audit']['final_scope'], ['A'])

    def test_query_gate_and_multiple_related_gates(self):
        request = clone(SHARED_REQUEST)
        request['query'] = ['top', 'side', 'AB']
        request['elimination_order'] = ['A', 'C', 'D', 'B', 'AC']
        report = analyze(request)
        self.assertEqual(report['variables'], ['AB', 'side', 'top'])
        self.assertEqual(len(report['joint']), 8)
        self.assertEqual(report['normalizer'], oracle_result(request)['normalizer'])
        self.assertEqual(report['joint'], oracle_result(request)['joint'])

    def test_independent_factor_changes_normalizer(self):
        base = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'Z', 'numerator': 1, 'denominator': 2}],
                     'gates': [], 'root': 'A'},
            'factors': [],
            'evidence': [],
            'query': ['A'],
            'elimination_order': ['Z'],
        }
        plain = analyze(base)
        self.assertEqual(plain['normalizer'], '1')
        weighted = clone(base)
        weighted['factors'] = [{'id': 'boost', 'scope': ['Z'], 'values': [
            {'numerator': 1, 'denominator': 1},
            {'numerator': 3, 'denominator': 1}]}]
        report = analyze(weighted)
        # Z now weighs 0.5*1 + 0.5*3 = 2, which the normalizer must include.
        self.assertEqual(report['normalizer'], '2')
        self.assertEqual(report['evidence_probability'], '1')
        self.assertEqual(report['joint'][0]['probability'], '1/2')

    def test_non_normalized_potential_values(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2}],
                     'gates': [], 'root': 'A'},
            'factors': [{'id': 'heavy', 'scope': ['A'], 'values': [
                {'numerator': 7, 'denominator': 2},
                {'numerator': 9, 'denominator': 2}]}],
            'evidence': [],
            'query': ['A'],
            'elimination_order': [],
        }
        report = analyze(request)
        # Z = 0.5*7/2 + 0.5*9/2 = 4.
        self.assertEqual(report['normalizer'], '4')
        self.assertEqual(report['joint'][0]['probability'], '7/16')
        self.assertEqual(report['joint'][1]['probability'], '9/16')

    def test_gate_evidence_breaks_probability_mass(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
                     'root': 'g'},
            'factors': [],
            'evidence': [{'node': 'g', 'failed': True}, {'node': 'A', 'failed': True}],
            'query': ['B'],
            'elimination_order': ['A', 'g'],
        }
        report = analyze(request)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['normalizer'], '1')
        self.assertEqual(report['evidence_probability'], '1/4')
        # Given g and A, B must be true: the false row is present with mass 0.
        self.assertEqual(report['joint'], [
            {'failed': [False], 'probability': '0'},
            {'failed': [True], 'probability': '1'}])

    def test_query_contains_observed_node(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 3},
                                {'id': 'B', 'numerator': 1, 'denominator': 3}],
                     'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                     'root': 'top'},
            'factors': [],
            'evidence': [{'node': 'A', 'failed': True}],
            'query': ['A', 'B'],
            'elimination_order': ['top'],
        }
        report = analyze(request)
        self.assertEqual(report['variables'], ['A', 'B'])
        self.assertEqual([row['failed'] for row in report['joint']],
                         [[False, False], [False, True], [True, False], [True, True]])
        self.assertEqual([row['probability'] for row in report['joint']],
                         ['0', '0', '2/3', '1/3'])
        self.assertEqual(report['evidence_probability'], '1/3')

    def test_unreferenced_event_and_gate_still_weigh_in(self):
        # B feeds no gate and is not an ancestor of root; its prior and its
        # potential still change Z.  The side gate is likewise declared but
        # unreferenced by root, so it becomes a real query variable.
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 2},
                                {'id': 'C', 'numerator': 1, 'denominator': 2}],
                     'gates': [{'id': 'side', 'op': 'OR', 'inputs': ['B', 'C']}],
                     'root': 'A'},
            'factors': [{'id': 'boost', 'scope': ['B'], 'values': [
                {'numerator': 1, 'denominator': 1},
                {'numerator': 4, 'denominator': 1}]}],
            'evidence': [],
            'query': ['A', 'side'],
            'elimination_order': ['B', 'C'],
        }
        report = analyze(request)
        # Z = A prior sums to 1 times B-weighted 1 and side-gate constraint.
        self.assertEqual(report['normalizer'], '5/2')
        self.assertEqual(report['audit']['final_scope'], ['A', 'side'])
        self.assertEqual(report['variables'], ['A', 'side'])
        # Brute-force Z includes the unreferenced B prior and boost potential.
        self.assertEqual(report['joint'], [
            {'failed': [False, False], 'probability': '1/20'},
            {'failed': [False, True], 'probability': '9/20'},
            {'failed': [True, False], 'probability': '1/20'},
            {'failed': [True, True], 'probability': '9/20'}])

    def test_factor_row_unknown_fields(self):
        request = clone(Validation.BASE)
        request['factors'][0]['values'][0]['extra'] = 1
        with self.assertRaises(ValueError):
            analyze(request)
        request = clone(Validation.BASE)
        request['factors'][0]['values'][0]['numerator'] = True
        with self.assertRaises(ValueError):
            analyze(request)

    def test_variables_sorted_by_utf8_bytes(self):
        request = clone(Validation.BASE)
        request['tree'] = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                      {'id': 'B', 'numerator': 1, 'denominator': 2},
                                      {'id': 'z', 'numerator': 1, 'denominator': 2},
                                      {'id': 'é', 'numerator': 1, 'denominator': 2}],
                           'gates': [{'id': 'top', 'op': 'OR',
                                      'inputs': ['A', 'B', 'z', 'é']}],
                           'root': 'top'}
        request['factors'] = []
        request['evidence'] = []
        request['query'] = ['é', 'top', 'A']
        request['elimination_order'] = ['B', 'z']
        report = analyze(request)
        self.assertEqual(report['variables'], ['A', 'top', 'é'])
        self.assertEqual(len(report['joint']), 8)
        first = report
        request['query'] = ['A', 'top', 'é']
        self.assertEqual(analyze(request), first)

    def test_zero_model(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2}],
                     'gates': [], 'root': 'A'},
            'factors': [{'id': 'annihilate', 'scope': ['A'], 'values': [
                {'numerator': 0, 'denominator': 1},
                {'numerator': 0, 'denominator': 1}]}],
            'evidence': [{'node': 'A', 'failed': True}],
            'query': ['A'],
            'elimination_order': [],
        }
        report = analyze(request)
        self.assertEqual(report['status'], 'zero_model')
        self.assertEqual(report['normalizer'], '0')
        self.assertIsNone(report['evidence_probability'])
        self.assertEqual(report['joint'], [])
        self.assertEqual(report['variables'], ['A'])
        self.assertEqual(report['audit']['final_scope'], ['A'])
        # A contradictory reading is irrelevant: zero model wins.
        self.assertEqual(report['status'], 'zero_model')

    def test_zero_evidence_only(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
                     'root': 'g'},
            'factors': [],
            'evidence': [{'node': 'g', 'failed': True}, {'node': 'A', 'failed': False}],
            'query': ['B'],
            'elimination_order': ['A', 'g'],
        }
        report = analyze(request)
        self.assertEqual(report['status'], 'zero_evidence')
        self.assertEqual(report['normalizer'], '1')
        self.assertEqual(report['evidence_probability'], '0')
        self.assertEqual(report['joint'], [])
        self.assertEqual(report['variables'], ['B'])

    def test_no_evidence_probability_is_one(self):
        request = clone(SHARED_REQUEST)
        request['evidence'] = []
        report = analyze(request)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['evidence_probability'], '1')

    def test_elimination_order_changes_bucket_width_not_result(self):
        wide = clone(SHARED_REQUEST)
        wide['elimination_order'] = ['A', 'B', 'C', 'D', 'AC', 'side']
        narrow = clone(SHARED_REQUEST)
        narrow['elimination_order'] = ['D', 'C', 'B', 'side', 'AC', 'A']
        first = analyze(wide)
        second = analyze(narrow)
        self.assertEqual(first['joint'], second['joint'])
        self.assertEqual(first['normalizer'], second['normalizer'])
        self.assertEqual(first['evidence_probability'], second['evidence_probability'])
        self.assertNotEqual(first['audit']['steps'], second['audit']['steps'])
        self.assertNotEqual(first['audit']['peak_variables'],
                            second['audit']['peak_variables'])
        # Hand-checked first buckets: eliminating A merges both shared sub-gates
        # and the A-prior, giving {A,AB,AC,B,C}; eliminating D first merges the
        # D-prior, the cd potential and the two gates that use D.
        self.assertEqual(first['audit']['steps'][0],
                         {'variable': 'A', 'bucket_scope': ['A', 'AB', 'AC', 'B', 'C'],
                          'residual_scope': ['AB', 'AC', 'B', 'C']})
        self.assertEqual(first['audit']['steps'][1],
                         {'variable': 'B',
                          'bucket_scope': ['AB', 'AC', 'B', 'C', 'D', 'side'],
                          'residual_scope': ['AB', 'AC', 'C', 'D', 'side']})
        self.assertEqual(first['audit']['peak_variables'], 6)
        self.assertEqual(first['audit']['final_scope'], ['AB', 'top'])
        self.assertEqual(second['audit']['steps'][0]['variable'], 'D')
        # D's own prior, the cd potential, and the top/side gate plates.
        self.assertEqual(second['audit']['steps'][0]['bucket_scope'],
                         ['AB', 'AC', 'B', 'C', 'D', 'side', 'top'])
        self.assertEqual(second['audit']['steps'][0]['residual_scope'],
                         ['AB', 'AC', 'B', 'C', 'side', 'top'])
        self.assertEqual(second['audit']['peak_variables'], 7)
        self.assertEqual(second['audit']['final_scope'], ['AB', 'top'])
        # Both audits match the independent scope-only replay exactly.
        self.assertEqual(first['audit'], oracle_audit(wide))
        self.assertEqual(second['audit'], oracle_audit(narrow))

    def test_scalar_factor_chain_and_empty_scope_boundary(self):
        # Eliminating a lone variable whose plate has exactly one variable
        # produces an empty residual scope that must survive as a scalar.
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A']}],
                     'root': 'g'},
            'factors': [], 'evidence': [], 'query': ['g', 'B'],
            'elimination_order': ['A'],
        }
        report = analyze(request)
        self.assertEqual(report['audit']['steps'],
                         [{'variable': 'A',
                           'bucket_scope': ['A', 'g'],
                           'residual_scope': ['g']}])
        self.assertEqual(report['audit']['final_scope'], ['B', 'g'])
        self.assertEqual(report['audit']['peak_variables'], 2)
        self.assertEqual(report['joint'], oracle_result(request)['joint'])

        # A scoped potential over a single event is a one-column table and,
        # once eliminated, leaves a scalar whose empty scope is audited.
        scalar = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [], 'root': 'A'},
            'factors': [{'id': 'only_a', 'scope': ['A'], 'values': [
                {'numerator': 5, 'denominator': 1},
                {'numerator': 1, 'denominator': 5}]}],
            'evidence': [], 'query': ['B'],
            'elimination_order': ['A'],
        }
        report = analyze(scalar)
        self.assertEqual(report['audit']['steps'],
                         [{'variable': 'A',
                           'bucket_scope': ['A'],
                           'residual_scope': []}])
        self.assertEqual(report['audit']['final_scope'], ['B'])
        # Z = A prior times the single-column potential = 5/2 + 1/10 = 13/5;
        # the B prior sums to 1, so the eliminated A factor is a scalar.
        self.assertEqual(report['normalizer'], '13/5')
        self.assertEqual(report['evidence_probability'], '1')
        self.assertEqual(report['joint'],
                         [{'failed': [False], 'probability': '1/2'},
                          {'failed': [True], 'probability': '1/2'}])

    def test_audit_ignores_evidence_and_zero_values(self):
        base = clone(SHARED_REQUEST)
        base['evidence'] = []
        base['factors'] = []
        fixed = clone(SHARED_REQUEST)
        fixed['evidence'] = [{'node': node, 'failed': False}
                             for node in ('A', 'B', 'C', 'D')]
        fixed['factors'] = [{'id': 'zero', 'scope': ['A', 'B'],
                             'values': [{'numerator': 0, 'denominator': 1}] * 4}]
        self.assertEqual(analyze(base)['audit']['peak_variables'],
                         analyze(fixed)['audit']['peak_variables'])
        self.assertEqual(len(analyze(base)['audit']['steps']),
                         len(analyze(fixed)['audit']['steps']))


class WidthLimit(unittest.TestCase):
    def _wide_structure(self):
        events = [{'id': 'E%02d' % i, 'numerator': 1, 'denominator': 2}
                  for i in range(16)]
        gates = [
            {'id': 'g1', 'op': 'AND', 'inputs': ['E00', 'E01', 'E02', 'E03', 'E04']},
            {'id': 'g2', 'op': 'OR', 'inputs': ['E00', 'E05', 'E06', 'E07', 'E08']},
        ]
        scope = ['E00', 'E09', 'E10', 'E11', 'E12', 'E13', 'E14', 'E15']
        values = [{'numerator': 1, 'denominator': 1}] * (1 << len(scope))
        return {
            'tree': {'events': events, 'gates': gates, 'root': 'g1'},
            'factors': [{'id': 'wide', 'scope': scope, 'values': values}],
            'evidence': [],
            'query': ['g1'],
            'elimination_order': ['E00', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06',
                                  'E07', 'E08', 'E09', 'E10', 'E11', 'E12', 'E13',
                                  'E14', 'E15', 'g2'],
        }

    def test_thirteen_variable_bucket_rejected(self):
        request = self._wide_structure()
        # Eliminating E00 merges the priors and both gate plates with the wide
        # potential: E00..E08 plus g1,g2 plus E09..E15 is 18 variables.
        with self.assertRaises(ValueError):
            analyze(request)

    def test_rejected_even_when_evidence_fixes_everything(self):
        request = self._wide_structure()
        request['evidence'] = [{'node': event['id'], 'failed': False}
                               for event in request['tree']['events']]
        request['evidence'] += [{'node': 'g1', 'failed': False},
                                {'node': 'g2', 'failed': False}]
        with self.assertRaises(ValueError):
            analyze(request)

    def test_twelve_variables_accepted(self):
        request = self._wide_structure()
        # Drop one event from the wide potential: bucket for E00 becomes
        # E00..E08 + g1,g2 + E09..E14 = 17; instead split the gates so the
        # union stays at the boundary.  The narrow chain below is the clean
        # accepted-at-12 case.
        events = [{'id': 'E%02d' % i, 'numerator': 1, 'denominator': 2}
                  for i in range(12)]
        gates = [{'id': 'g1', 'op': 'AND',
                  'inputs': ['E00', 'E01', 'E02', 'E03']}]
        scope = ['E00', 'E04', 'E05', 'E06', 'E07', 'E08', 'E09', 'E10']
        values = [{'numerator': 1, 'denominator': 1}] * (1 << len(scope))
        chain = {
            'tree': {'events': events, 'gates': gates, 'root': 'g1'},
            'factors': [{'id': 'wide', 'scope': scope, 'values': values}],
            'evidence': [],
            'query': ['g1'],
            'elimination_order': ['E00', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06',
                                  'E07', 'E08', 'E09', 'E10', 'E11'],
        }
        report = analyze(chain)
        # Bucket for E00 = the prior, the g1 plate (E00..E03) and the wide
        # potential (E00, E04..E10): exactly 12 variables.
        self.assertEqual(report['audit']['steps'][0]['bucket_scope'],
                         ['E00', 'E01', 'E02', 'E03', 'E04', 'E05',
                          'E06', 'E07', 'E08', 'E09', 'E10', 'g1'])
        self.assertEqual(report['audit']['peak_variables'], 12)
        self.assertEqual(report['status'], 'ok')


class LargeShallowInstances(unittest.TestCase):
    def test_sixty_four_event_chain(self):
        import time
        events = [{'id': 'E%02d' % i, 'numerator': 1, 'denominator': 3}
                  for i in range(64)]
        gates = []
        previous = 'E00'
        for index in range(1, 64):
            gate_id = 'G%02d' % index
            gates.append({'id': gate_id,
                          'op': 'AND' if index % 2 else 'OR',
                          'inputs': [previous, 'E%02d' % index]})
            previous = gate_id
        tree = {'events': events, 'gates': gates, 'root': previous}
        query = [previous, 'E63']
        elimination = [e['id'] for e in events] + [g['id'] for g in gates]
        elimination = [name for name in elimination if name not in query]
        request = {'tree': tree, 'factors': [], 'evidence': [],
                   'query': query, 'elimination_order': elimination}
        started = time.monotonic()
        report = analyze(request)
        elapsed = time.monotonic() - started
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['joint']), 4)
        self.assertEqual(report['audit']['peak_variables'], 3)
        self.assertLess(elapsed, 20.0)
        # Independent oracle over 2**64 worlds is impossible; instead check the
        # chain against the equivalent v1 semantics root probability through a
        # direct recursive reliability computation on the chain.
        marginal = _chain_root_marginal(events, gates, previous)
        root_position = report['variables'].index(previous)
        joint_true = sum(Fraction(row['probability']) for row in report['joint']
                         if row['failed'][root_position])
        self.assertEqual(joint_true, marginal)

    def test_shared_gate_used_by_two_parents(self):
        request = {
            'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                {'id': 'B', 'numerator': 1, 'denominator': 3},
                                {'id': 'C', 'numerator': 1, 'denominator': 4}],
                     'gates': [{'id': 'shared', 'op': 'AND', 'inputs': ['A', 'B']},
                               {'id': 'left', 'op': 'OR', 'inputs': ['shared', 'C']},
                               {'id': 'right', 'op': 'AND', 'inputs': ['shared', 'C']},
                               {'id': 'top', 'op': 'OR', 'inputs': ['left', 'right']}],
                     'root': 'top'},
            'factors': [],
            'evidence': [],
            'query': ['left', 'right'],
            'elimination_order': ['A', 'B', 'C', 'shared', 'top'],
        }
        report = analyze(request)
        expected = oracle_result(request)
        self.assertEqual(report['joint'], expected['joint'])
        # left and right are dependent through the shared sub-gate.
        joint = {(tuple(row['failed'])): Fraction(row['probability'])
                 for row in report['joint']}
        left_marginal = joint[(True, False)] + joint[(True, True)]
        right_marginal = joint[(False, True)] + joint[(True, True)]
        both = joint[(True, True)]
        self.assertNotEqual(both, left_marginal * right_marginal)


def _chain_root_marginal(events, gates, root):
    """Independently propagate exact event marginals through the chain DAG.

    Each gate consumes disjoint fresh events along the chain, so the gate
    formulas apply to exact marginals; this is a separate reliability
    computation, not world enumeration.
    """
    p = {e['id']: Fraction(e['numerator'], e['denominator']) for e in events}
    for gate in gates:
        values = [p[name] for name in gate['inputs']]
        if gate['op'] == 'AND':
            product_ = Fraction(1)
            for value in values:
                product_ *= value
            p[gate['id']] = product_
        else:
            healthy = Fraction(1)
            for value in values:
                healthy *= 1 - value
            p[gate['id']] = 1 - healthy
    return p[root]


if __name__ == '__main__':
    unittest.main()


class Validation(unittest.TestCase):
    BASE = {
        'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                            {'id': 'B', 'numerator': 1, 'denominator': 2}],
                 'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
                 'root': 'g'},
        'factors': [{'id': 'f', 'scope': ['A', 'B'], 'values': [
            {'numerator': 1, 'denominator': 1},
            {'numerator': 2, 'denominator': 1},
            {'numerator': 3, 'denominator': 2},
            {'numerator': 1, 'denominator': 3}]}],
        'evidence': [],
        'query': ['g'],
        'elimination_order': ['A', 'B'],
    }

    def bad(self, mutate):
        request = clone(self.BASE)
        mutate(request)
        with self.assertRaises((ValueError, TypeError)):
            analyze(request)

    def test_envelope_fields(self):
        self.bad(lambda r: r.pop('factors'))
        self.bad(lambda r: r.update(extra=1))
        self.bad(lambda r: r['tree'].update(extra=1))
        self.bad(lambda r: r['factors'][0].update(extra=1))
        self.bad(lambda r: r['evidence'].append({'node': 'A', 'failed': True, 'x': 1}))

    def test_tree_contract(self):
        self.bad(lambda r: r['tree']['events'].append(
            {'id': 'C', 'numerator': 1, 'denominator': 2, 'extra': 1}))
        self.bad(lambda r: r['tree']['gates'][0].update(op='XOR'))
        self.bad(lambda r: r['tree']['gates'][0].update(inputs=['A', 'A']))
        self.bad(lambda r: r['tree']['gates'][0].update(inputs=['A', 'nope']))
        self.bad(lambda r: r['tree'].update(root='nope'))
        self.bad(lambda r: r['tree']['events'][0].update(numerator=3, denominator=2))
        self.bad(lambda r: r['tree'].update(events=[]))
        self.bad(lambda r: r['tree']['gates'][0].update(inputs=['A', 'B', 'A', 'B', 'A', 'B']))

    def test_a_wide_tree_is_kept_off_the_old_entry_point(self):
        tree = {'events': [{'id': 'E%02d' % i, 'numerator': 1, 'denominator': 2}
                           for i in range(17)], 'gates': [], 'root': 'E00'}
        from fault_tree.model import parse_tree
        with self.assertRaises(ValueError):
            parse_tree(tree)

    def test_factor_rules(self):
        self.bad(lambda r: r['factors'][0]['values'].pop())
        self.bad(lambda r: r['factors'][0]['values'].append(
            {'numerator': 1, 'denominator': 1}))
        self.bad(lambda r: r['factors'][0].update(scope=['A', 'A']))
        self.bad(lambda r: r['factors'][0].update(scope=['A', 'nope']))
        self.bad(lambda r: r['factors'][0].update(scope=[]))
        self.bad(lambda r: r['factors'][0]['values'][0].update(numerator=-1))
        self.bad(lambda r: r['factors'][0]['values'][0].update(denominator=0))
        self.bad(lambda r: r['factors'][0]['values'][0].update(numerator=10 ** 9 + 1))
        self.bad(lambda r: r['factors'][0]['values'][0].update(numerator=True))
        self.bad(lambda r: r['factors'].append(clone(r['factors'][0])))
        self.bad(lambda r: r['factors'].append({'id': 'g', 'scope': ['A'], 'values': []}))
        self.bad(lambda r: r.update(factors=[{'id': 'f%d' % i, 'scope': ['A'],
                                              'values': [{'numerator': 1, 'denominator': 1},
                                                         {'numerator': 1, 'denominator': 1}]}
                                             for i in range(129)]))

    def test_evidence_and_query_rules(self):
        self.bad(lambda r: r['evidence'].append({'node': 'nope', 'failed': True}))
        self.bad(lambda r: r['evidence'].append({'node': 'A', 'failed': 1}))
        self.bad(lambda r: r['evidence'].extend(
            [{'node': 'A', 'failed': True}, {'node': 'A', 'failed': False}]))
        self.bad(lambda r: r.update(evidence=[{'node': 'A', 'failed': True}] * 165))
        self.bad(lambda r: r.update(query=[]))
        self.bad(lambda r: r.update(query=['nope']))
        self.bad(lambda r: r.update(query=['g', 'g']))
        self.bad(lambda r: r.update(query=['A', 'B', 'g', 'A', 'B', 'g', 'A', 'B', 'g']))

    def test_elimination_order_rules(self):
        self.bad(lambda r: r.update(elimination_order=[]))
        self.bad(lambda r: r.update(elimination_order=['A']))
        self.bad(lambda r: r.update(elimination_order=['A', 'A']))
        self.bad(lambda r: r.update(elimination_order=['A', 'B', 'g']))
        self.bad(lambda r: r.update(elimination_order=['A', 'B', 'nope']))
        self.bad(lambda r: r.update(elimination_order=['A', 'B', 1]))

    def test_elimination_may_not_include_query(self):
        self.bad(lambda r: r.update(elimination_order=['A', 'B', 'g']))
        request = clone(self.BASE)
        request['query'] = ['A', 'B']
        request['elimination_order'] = ['A']
        with self.assertRaises(ValueError):
            analyze(request)

    def test_gate_arity_and_query_width(self):
        nine = {'events': [{'id': 'N%d' % i, 'numerator': 1, 'denominator': 2}
                           for i in range(9)], 'gates': [], 'root': 'N0'}
        request = clone(self.BASE)
        request['tree'] = nine
        request['factors'] = []
        request['query'] = ['N%d' % i for i in range(9)]
        with self.assertRaises(ValueError):
            analyze(request)
        six = clone(self.BASE)
        six['tree']['gates'][0]['inputs'] = ['A', 'B', 'A2', 'B2', 'A3', 'B3']
        with self.assertRaises(ValueError):
            analyze(six)

    def test_factor_scope_must_be_events(self):
        self.bad(lambda r: r['factors'][0].update(scope=['A', 'g']))

    def test_allowed_shapes(self):
        request = clone(self.BASE)
        request['query'] = ['A']
        request['elimination_order'] = ['B', 'g']
        report = analyze(request)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['variables'], ['A'])

    def test_array_reorder_invariance(self):
        first = analyze(clone(self.BASE))
        request = clone(self.BASE)
        request['tree']['events'].reverse()
        request['tree']['gates'].reverse()
        self.assertEqual(analyze(request), first)
        # Evidence row order and factor row order also do not matter.
        request = clone(self.BASE)
        request['evidence'] = [{'node': 'A', 'failed': True}]
        first = analyze(clone(request))
        swapped = clone(request)
        swapped['factors'].reverse()
        self.assertEqual(analyze(swapped), first)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def cli(self, source, output):
        return subprocess.run([sys.executable, '-m', 'fault_tree.factor',
                               '--source', str(source), '--output', str(output)],
                              capture_output=True, text=True, cwd=ROOT)

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj), encoding='utf-8')
        return path

    def test_cli_success_envelope_and_stdout(self):
        source = self.write('in.json', SHARED_REQUEST)
        output = self.dir / 'out.json'
        run = self.cli(source, output)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(set(report), {'schema_version', 'source_sha256', 'result'})
        self.assertEqual(report['schema_version'], 4)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(set(report['result']),
                         {'status', 'normalizer', 'evidence_probability',
                          'variables', 'joint', 'audit'})
        self.assertEqual(set(report['result']['audit']),
                         {'steps', 'final_scope', 'peak_variables'})
        self.assertEqual(json.loads(run.stdout), report)

    def test_fixture_round_trip(self):
        source = ROOT / 'fixtures/factor/request.json'
        output = self.dir / 'out.json'
        run = self.cli(source, output)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), json.loads(output.read_text()))
        self.assertEqual(json.loads(output.read_text())['result']['status'], 'ok')

    def test_replay_byte_stable(self):
        source = self.write('in.json', SHARED_REQUEST)
        output = self.dir / 'out.json'
        first_run = self.cli(source, output)
        self.assertEqual(first_run.returncode, 0)
        first = output.read_bytes()
        self.assertEqual(self.cli(source, output).returncode, 0)
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual(first_run.stdout, self.cli(source, output).stdout)

    def test_semantic_reorder_changes_sha_not_result(self):
        source = self.write('one.json', SHARED_REQUEST)
        one = self.dir / 'one.out.json'
        self.assertEqual(self.cli(source, one).returncode, 0)
        shuffled = clone(SHARED_REQUEST)
        shuffled['tree']['events'].reverse()
        shuffled['tree']['gates'].reverse()
        shuffled['evidence'].reverse()
        shuffled['query'].reverse()
        shuffled['factors'].reverse()
        two_source = self.write('two.json', shuffled)
        two = self.dir / 'two.out.json'
        self.assertEqual(self.cli(two_source, two).returncode, 0)
        first = json.loads(one.read_text())
        second = json.loads(two.read_text())
        self.assertEqual(first['result'], second['result'])
        self.assertNotEqual(first['source_sha256'], second['source_sha256'])

    def test_normal_zero_states_exit_zero(self):
        zero_model = clone(Validation.BASE)
        zero_model['factors'] = [{'id': 'z', 'scope': ['A'], 'values': [
            {'numerator': 0, 'denominator': 1},
            {'numerator': 0, 'denominator': 1}]}]
        source = self.write('zm.json', zero_model)
        run = self.cli(source, self.dir / 'zm.out.json')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['result']['status'], 'zero_model')
        zero_evidence = clone(Validation.BASE)
        zero_evidence['evidence'] = [{'node': 'g', 'failed': True},
                                     {'node': 'A', 'failed': False}]
        source = self.write('ze.json', zero_evidence)
        run = self.cli(source, self.dir / 'ze.out.json')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['result']['status'], 'zero_evidence')

    def test_failure_exits_two_and_preserves_output(self):
        output = self.dir / 'out.json'
        output.write_text('prior')
        cases = ['{}', '{"a":1,"a":2}', 'NaN', '{"tree":{}}',
                 json.dumps({'tree': {'events': [{'id': 'A', 'numerator': 1,
                                                 'denominator': 1}],
                                      'gates': [], 'root': 'A'},
                            'factors': [], 'evidence': [],
                            'query': ['A'], 'elimination_order': []})[:-2] + ',}']
        for body in cases:
            source = self.dir / 'bad.json'
            source.write_text(body)
            run = self.cli(source, output)
            self.assertEqual(run.returncode, 2, body)
            self.assertEqual(output.read_text(), 'prior')
        # A wide 13-variable structure must also exit 2 and preserve output.
        source = self.write('wide.json', WidthLimit()._wide_structure())
        self.assertEqual(self.cli(source, output).returncode, 2)
        self.assertEqual(output.read_text(), 'prior')

    def test_output_safety(self):
        source = self.write('same.json', Validation.BASE)
        self.assertEqual(self.cli(source, source).returncode, 2)
        link = self.dir / 'link.json'
        link.symlink_to(source)
        self.assertEqual(self.cli(source, link).returncode, 2)
        self.assertEqual(json.loads(source.read_text()), Validation.BASE)

    def test_size_limit(self):
        big = clone(Validation.BASE)
        big['factors'][0]['id'] = 'x' * 8_000_000
        source = self.write('big.json', big)
        self.assertGreater(len(source.read_bytes()), 8_000_000)
        run = self.cli(source, self.dir / 'big.out.json')
        self.assertEqual(run.returncode, 2)


class WideChainWithSharedGate(unittest.TestCase):
    """64 basic events, a shared sub-gate and an alternating low-width chain.

    Exact verification conditions on the 8-event shared core (256 worlds) and
    then evaluates the 56-event tail chain analytically with an independent
    two-node DP, so no 2**64 enumeration is ever performed even in the test.
    """

    def build(self):
        core_events = ['C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7']
        tail_events = ['E%02d' % i for i in range(8, 64)]
        events = [{'id': name, 'numerator': (index % 5) + 1,
                   'denominator': (index % 7) + 6}
                  for index, name in enumerate(core_events + tail_events)]
        gates = [
            {'id': 'q1', 'op': 'AND', 'inputs': ['C0', 'C1', 'C2']},
            {'id': 'q2', 'op': 'OR', 'inputs': ['C3', 'C4']},
            {'id': 'q3', 'op': 'AND', 'inputs': ['q1', 'q2']},      # shares q1
            {'id': 'q4', 'op': 'OR', 'inputs': ['q1', 'C5']},
            {'id': 'core', 'op': 'AND', 'inputs': ['q3', 'q4', 'C6', 'C7']},
        ]
        previous = 'core'
        for index, name in enumerate(tail_events):
            gate_id = 'T%02d' % index
            gates.append({'id': gate_id,
                          'op': 'AND' if index % 2 else 'OR',
                          'inputs': [previous, name]})
            previous = gate_id
        tree = {'events': events, 'gates': gates, 'root': previous}
        return tree, tail_events, previous

    def test_joint_and_audit(self):
        tree, tail_events, root = self.build()
        query = [root, 'core', 'T30']
        elimination = [e['id'] for e in tree['events']] + [g['id'] for g in tree['gates']]
        elimination = [name for name in elimination if name not in query]
        request = {'tree': tree, 'factors': [], 'evidence': [],
                   'query': query, 'elimination_order': elimination}
        report = analyze(request)
        audit = oracle_audit(request)
        self.assertEqual(report['audit'], audit)
        self.assertLessEqual(report['audit']['peak_variables'], 12)
        self.assertEqual(report['audit']['final_scope'], ['T30', 'T55', 'core'])
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['evidence_probability'], '1')
        self.assertEqual(len(report['joint']), 8)

        expected = self._independent_joint(tree, tail_events, root)
        variables = report['variables']
        index_of = {name: index for index, name in enumerate(variables)}
        for row in report['joint']:
            key = (row['failed'][index_of[root]], row['failed'][index_of['core']],
                   row['failed'][index_of['T30']])
            self.assertEqual(Fraction(row['probability']), expected[key], key)
        self.assertEqual(sum(expected.values()), 1)

    def _independent_joint(self, tree, tail_events, root):
        events = {e['id']: Fraction(e['numerator'], e['denominator'])
                  for e in tree['events']}
        gates = {g['id']: g for g in tree['gates']}
        core_names = ['C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7']
        core_gate_order = ['q1', 'q2', 'q3', 'q4', 'core']
        tail_order = ['T%02d' % i for i in range(len(tail_events))]

        def value(name, assignment):
            if name in assignment:
                return assignment[name]
            gate = gates[name]
            operands = [value(x, assignment) for x in gate['inputs']]
            return all(operands) if gate['op'] == 'AND' else any(operands)

        # Joint distribution over (T30, root) as a function of the core gate
        # value, computed by a forward DP over the tail chain.  tail_state maps
        # (chain gate value, T30 value) -> probability mass.
        def tail_joint(core_value):
            state = {(core_value, core_value): Fraction(1)}
            for index, event_name in enumerate(tail_events):
                gate = gates['T%02d' % index]
                p = events[event_name]
                nxt = {}
                for (chain_value, t30_value), mass in state.items():
                    for event_value, factor in ((False, 1 - p), (True, p)):
                        if not factor:
                            continue
                        operands = [chain_value, event_value]
                        new_chain = (all(operands) if gate['op'] == 'AND'
                                     else any(operands))
                        new_t30 = new_chain if index == 30 else t30_value
                        key = (new_chain, new_t30)
                        nxt[key] = nxt.get(key, Fraction(0)) + mass * factor
                state = nxt
            joint = {}
            for (chain_value, t30_value), mass in state.items():
                joint[(chain_value, t30_value)] = mass
            return joint

        result = {}
        for bits in product((False, True), repeat=len(core_names)):
            assignment = dict(zip(core_names, bits))
            weight = Fraction(1)
            for name in core_names:
                p = events[name]
                weight *= p if assignment[name] else 1 - p
            if not weight:
                continue
            for name in core_gate_order:
                assignment[name] = value(name, assignment)
            core_value = assignment['core']
            for (tail_root, t30), mass in tail_joint(core_value).items():
                if not mass:
                    continue
                key = (tail_root, core_value, t30)
                result[key] = result.get(key, Fraction(0)) + weight * mass
        return result


class RejectionWidths(unittest.TestCase):
    def exact_thirteen_structure(self):
        events = [{'id': 'E%02d' % i, 'numerator': 1, 'denominator': 2}
                  for i in range(12)]
        gates = [{'id': 'g1', 'op': 'AND',
                  'inputs': ['E00', 'E01', 'E02', 'E03', 'E04']}]
        scope = ['E00', 'E05', 'E06', 'E07', 'E08', 'E09', 'E10', 'E11']
        values = [{'numerator': 1, 'denominator': 1}] * (1 << len(scope))
        return {
            'tree': {'events': events, 'gates': gates, 'root': 'g1'},
            'factors': [{'id': 'wide', 'scope': scope, 'values': values}],
            'evidence': [],
            'query': ['g1'],
            'elimination_order': ['E00', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06',
                                  'E07', 'E08', 'E09', 'E10', 'E11'],
        }

    def test_peak_is_exactly_thirteen(self):
        request = self.exact_thirteen_structure()
        audit = oracle_audit(request)
        self.assertEqual(audit['peak_variables'], 13)
        self.assertEqual(audit['steps'][0]['bucket_scope'],
                         ['E00', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06',
                          'E07', 'E08', 'E09', 'E10', 'E11', 'g1'])
        with self.assertRaises(ValueError):
            analyze(request)

    def test_still_rejected_when_evidence_fixes_all_nodes(self):
        request = self.exact_thirteen_structure()
        request['evidence'] = [{'node': event['id'], 'failed': False}
                               for event in request['tree']['events']]
        request['evidence'].append({'node': 'g1', 'failed': False})
        self.assertEqual(oracle_audit(request)['peak_variables'], 13)
        with self.assertRaises(ValueError):
            analyze(request)


class FixtureMatchesOracle(unittest.TestCase):
    def test_shipped_fixture(self):
        request = json.loads((ROOT / 'fixtures/factor/request.json').read_text())
        report = analyze(request)
        expected = oracle_result(request)
        for key in ('status', 'normalizer', 'evidence_probability', 'variables', 'joint'):
            self.assertEqual(report[key], expected[key], key)
        self.assertEqual(report['evidence_probability'], '10829/65664')
        self.assertEqual([row['probability'] for row in report['joint']],
                         ['112/221', '70/221', '24/221', '15/221'])


class OldEntriesRegression(unittest.TestCase):
    """The v4 addition must not change the three shipped entry points."""

    def run_cli(self, args):
        return subprocess.run([sys.executable, *args], capture_output=True,
                              text=True, cwd=ROOT)

    def test_fault_tree_still_v1(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'out.json'
            run = self.run_cli(['-m', 'fault_tree.cli', '--source',
                                'fixtures/input.json', '--output', str(out)])
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['result']['failure_probability'], '11/250')

    def test_fault_diagnose_still_v3(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'out.json'
            run = self.run_cli(['-m', 'fault_tree.diagnose', '--source',
                                'fixtures/diagnose/request.json', '--output', str(out)])
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['schema_version'], 3)
            self.assertEqual(report['result']['status'], 'optimal')
            self.assertEqual(report['result']['root_failure_probability'], '11/250')

    def test_fault_history_still_v2(self):
        with tempfile.TemporaryDirectory() as temp:
            dataset = Path(temp) / 'ds'
            out = Path(temp) / 'day.json'
            init = self.run_cli(['-m', 'fault_tree.history', 'init', '--tree',
                                 'fixtures/history/tree.json', '--horizon', '200000',
                                 '--dataset', str(dataset)])
            self.assertEqual(init.returncode, 0, init.stderr)
            applied = self.run_cli(['-m', 'fault_tree.history', 'apply', '--dataset',
                                    str(dataset), '--source', 'fixtures/history/batch1.json'])
            self.assertEqual(applied.returncode, 0, applied.stderr)
            query = self.run_cli(['-m', 'fault_tree.history', 'query', '--dataset',
                                  str(dataset), '--day', '2', '--output', str(out)])
            self.assertEqual(query.returncode, 0, query.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(report['day']['downtime_seconds'], 2200)


if __name__ == '__main__':
    unittest.main()
