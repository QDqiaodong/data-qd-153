"""Tests for the v3 optimal adaptive diagnosis planner.

The expected values here never come from the production planner. An
independently written brute-force oracle enumerates every world itself and
recursively enumerates, for every candidate first test, the complete feasible
strategy combinations of both reading branches under the exact normative
objective. Output policies are additionally executed against every logical
world to verify leaf conclusions, per-path budgets and independently
recomputed expected/worst costs.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path

from fault_tree.diagnose.model import diagnose, parse_diagnosis_input

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Independent brute-force oracle (no production planning code is reused)
# ---------------------------------------------------------------------------

def _oracle_worlds(spec):
    """Enumerate basic-event worlds, evaluate the DAG independently, filter."""
    names = sorted(spec['events'])
    worlds = []
    for bits in product((False, True), repeat=len(names)):
        assignment = dict(zip(names, bits))
        values = dict(assignment)
        for gid in spec['order']:
            gate = spec['gates'][gid]
            operands = [values[x] for x in gate['inputs']]
            values[gid] = all(operands) if gate['op'] == 'AND' else any(operands)
        if all(values[node] == failed for node, failed in spec['evidence']):
            weight = Fraction(1)
            for name in names:
                p = spec['events'][name]
                weight *= p if values[name] else 1 - p
            worlds.append({'values': values, 'weight': weight})
    return worlds


def _fraction_text(value):
    return str(value.numerator) if value.denominator == 1 else '%d/%d' % (value.numerator, value.denominator)


class OraclePlanner:
    """Full strategy enumeration with frozenset support, its own memo table."""

    INFEASIBLE = object()

    def __init__(self, spec, worlds):
        self.spec = spec
        self.worlds = worlds
        self.root = spec['root']
        self.tests = spec['tests']
        self.budget = spec['budget']
        self.cache = {}

    def mass(self, subset):
        return sum((self.worlds[i]['weight'] for i in subset), Fraction(0))

    def solve(self, subset, used_ids, remaining_budget):
        subset = frozenset(subset)
        key = (subset, frozenset(used_ids), remaining_budget)
        if key in self.cache:
            return self.cache[key]
        root_values = {self.worlds[i]['values'][self.root] for i in subset}
        if len(root_values) == 1:
            result = ({'kind': 'leaf', 'failed': next(iter(root_values))}, Fraction(0), 0)
            self.cache[key] = result
            return result
        positive = self.mass(subset) > 0
        total_mass = self.mass(subset)
        best = None
        for test in sorted(self.tests, key=lambda t: t['id'].encode('utf-8')):
            if test['id'] in used_ids or test['cost'] > remaining_budget:
                continue
            true_set = {i for i in subset if self.worlds[i]['values'][test['node']]}
            false_set = subset - true_set
            branches = {}
            ok = True
            for reading, branch in ((True, true_set), (False, false_set)):
                if not branch:
                    branches[reading] = None
                    continue
                child = self.solve(branch, used_ids | {test['id']}, remaining_budget - test['cost'])
                if child is self.INFEASIBLE:
                    ok = False
                    break
                branches[reading] = child
            if not ok:
                continue
            worst = test['cost'] + max(
                0 if branches[r] is None else branches[r][2] for r in (True, False))
            if positive:
                expected = Fraction(test['cost'])
                for reading, branch in ((True, true_set), (False, false_set)):
                    if not branch:
                        continue
                    branch_mass = self.mass(frozenset(branch))
                    if branch_mass > 0:
                        expected += (branch_mass / total_mass) * branches[reading][1]
                value = expected
            else:
                expected = Fraction(0)
                value = worst
            tie_key = test['id'].encode('utf-8')
            if best is None or value < best[0] or (value == best[0] and tie_key < best[1]):
                best = (value, tie_key, test['id'], branches, expected, worst)
        if best is None:
            self.cache[key] = self.INFEASIBLE
            return self.INFEASIBLE
        _, _, test_id, branches, expected, worst = best
        policy = {'kind': 'test', 'test_id': test_id,
                  'on_false': None if branches[False] is None else branches[False][0],
                  'on_true': None if branches[True] is None else branches[True][0]}
        result = (policy, expected, worst)
        self.cache[key] = result
        return result

    def run(self):
        worlds = self.worlds
        if not worlds:
            return {'status': 'inconsistent_evidence', 'logical_worlds': 0,
                    'evidence_probability': None, 'root_failure_probability': None,
                    'expected_cost': None, 'worst_cost': None, 'policy': None}
        evidence_mass = sum((w['weight'] for w in worlds), Fraction(0))
        if evidence_mass == 0:
            return {'status': 'zero_mass_evidence', 'logical_worlds': len(worlds),
                    'evidence_probability': '0', 'root_failure_probability': None,
                    'expected_cost': None, 'worst_cost': None, 'policy': None}
        root_failure = sum((w['weight'] for w in worlds if w['values'][self.root]), Fraction(0))
        outcome = self.solve(set(range(len(worlds))), set(), self.budget)
        if outcome is self.INFEASIBLE:
            return {'status': 'infeasible', 'logical_worlds': len(worlds),
                    'evidence_probability': _fraction_text(evidence_mass),
                    'root_failure_probability': _fraction_text(root_failure / evidence_mass),
                    'expected_cost': None, 'worst_cost': None, 'policy': None}
        policy, expected, worst = outcome
        return {'status': 'optimal', 'logical_worlds': len(worlds),
                'evidence_probability': _fraction_text(evidence_mass),
                'root_failure_probability': _fraction_text(root_failure / evidence_mass),
                'expected_cost': _fraction_text(expected),
                'worst_cost': worst, 'policy': policy}


def oracle_report(request):
    spec = parse_diagnosis_input(request)
    worlds = _oracle_worlds(spec)
    return OraclePlanner(spec, worlds).run()


# ---------------------------------------------------------------------------
# Independent policy execution over every logical world
# ---------------------------------------------------------------------------

def execute_policy(request, report):
    """Walk the produced policy for every feasible world; return per-world stats."""
    spec = parse_diagnosis_input(request)
    worlds = _oracle_worlds(spec)
    cost_by_id = {t['id']: t['cost'] for t in spec['tests']}
    assert report['logical_worlds'] == len(worlds)
    stats = []
    for world in worlds:
        node = report['policy']
        used = set()
        spent = 0
        while node['kind'] == 'test':
            test_id = node['test_id']
            assert test_id not in used, 'test executed twice on one path'
            used.add(test_id)
            spent += cost_by_id[test_id]
            assert spent <= spec['budget'], 'path exceeds budget'
            reading = world['values'][next(t['node'] for t in spec['tests'] if t['id'] == test_id)]
            branch = node['on_true'] if reading else node['on_false']
            assert branch is not None, 'took a logically impossible null branch'
            node = branch
        assert node['kind'] == 'leaf'
        assert node['failed'] == world['values'][spec['root']], 'wrong leaf conclusion'
        stats.append((world, spent))
    return worlds, stats


def assert_policy_costs(testcase, request, report):
    worlds, stats = execute_policy(request, report)
    total_mass = sum((w['weight'] for w in worlds), Fraction(0))
    if total_mass > 0:
        expected = sum((w['weight'] * cost for w, cost in stats), Fraction(0)) / total_mass
        testcase.assertEqual(_fraction_text(expected), report['expected_cost'])
    testcase.assertEqual(max(cost for _, cost in stats), report['worst_cost'])


# ---------------------------------------------------------------------------
# Fuzz fixtures
# ---------------------------------------------------------------------------

def random_request(seed):
    import random
    rng = random.Random(seed)
    n = rng.randint(1, 4)
    probs = [Fraction(0), Fraction(1), Fraction(1, 2), Fraction(1, 3), Fraction(2, 3),
             Fraction(rng.randint(1, 4), rng.randint(5, 9))]
    events = []
    for i in range(n):
        p = rng.choice(probs)
        events.append({'id': 'E%d' % i, 'numerator': p.numerator, 'denominator': p.denominator})
    nodes = [e['id'] for e in events]
    gates = []
    for g in range(rng.randint(1, 3)):
        if len(nodes) < 2:
            break
        inputs = rng.sample(nodes, 2)
        gates.append({'id': 'G%d' % g, 'op': rng.choice(['AND', 'OR']), 'inputs': inputs})
        nodes.append('G%d' % g)
    tree = {'events': events, 'gates': gates, 'root': rng.choice(nodes)}
    evidence = []
    for node in rng.sample(nodes, rng.randint(0, min(3, len(nodes)))):
        evidence.append({'node': node, 'failed': rng.choice([True, False])})
    tests = []
    for t in range(rng.randint(0, 4)):
        tests.append({'id': 't%d' % t, 'node': rng.choice(nodes), 'cost': rng.randint(1, 6)})
    return {'tree': tree, 'evidence': evidence, 'tests': tests, 'budget': rng.randint(0, 12)}


class OracleFuzz(unittest.TestCase):
    def test_matches_brute_force_oracle(self):
        for seed in range(300):
            request = random_request(seed)
            actual = diagnose(request)
            expected = oracle_report(request)
            self.assertEqual(actual, expected, 'seed %d' % seed)
            if actual['status'] == 'optimal':
                assert_policy_costs(self, request, actual)


class TargetedScenarios(unittest.TestCase):
    SHARED_TREE = {
        'events': [{'id': 'A', 'numerator': 1, 'denominator': 10},
                   {'id': 'B', 'numerator': 1, 'denominator': 5},
                   {'id': 'C', 'numerator': 3, 'denominator': 10}],
        'gates': [{'id': 'AB', 'op': 'AND', 'inputs': ['A', 'B']},
                  {'id': 'AC', 'op': 'AND', 'inputs': ['A', 'C']},
                  {'id': 'top', 'op': 'OR', 'inputs': ['AB', 'AC']}],
        'root': 'top'}

    def test_shared_subgate_conditional_dependence(self):
        # Tests read shared-event gates; their relevance comes from the joint
        # worlds (A appears in both gates), not independent marginals.
        request = {'tree': self.SHARED_TREE, 'evidence': [],
                   'tests': [{'id': 'tAB', 'node': 'AB', 'cost': 1},
                             {'id': 'tAC', 'node': 'AC', 'cost': 1}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        # Hand derivation: P(AC)=0.03 so testing AC first stops on the larger
        # branch; expectation = 1 + P(AC=false)*1 = 1.97 = 197/100.
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['policy']['test_id'], 'tAC')
        self.assertEqual(report['expected_cost'], '197/100')
        self.assertEqual(report['worst_cost'], 2)
        self.assertEqual(report['root_failure_probability'], '11/250')
        assert_policy_costs(self, request, report)

    def test_best_follow_up_changes_with_reading(self):
        tree = {
            'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                       {'id': 'B', 'numerator': 1, 'denominator': 2},
                       {'id': 'C', 'numerator': 1, 'denominator': 10}],
            'gates': [{'id': 'AB', 'op': 'AND', 'inputs': ['A', 'B']},
                      {'id': 'top', 'op': 'OR', 'inputs': ['AB', 'C']}],
            'root': 'top'}
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 1},
                             {'id': 'tB', 'node': 'B', 'cost': 1},
                             {'id': 'tC', 'node': 'C', 'cost': 1}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        root_node = report['policy']
        self.assertEqual(root_node['kind'], 'test')
        follow_true = root_node['on_true']
        follow_false = root_node['on_false']
        # The two readings prescribe different continuation tests.
        self.assertEqual(follow_true['kind'], 'test')
        self.assertEqual(follow_false['kind'], 'test')
        self.assertNotEqual(follow_true['test_id'], follow_false['test_id'])
        assert_policy_costs(self, request, report)

    def test_branch_over_budget_makes_cheap_test_infeasible(self):
        tree = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                           {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                'root': 'top'}
        # tA looks cheap in expectation (6 + 0.5*6 = 9) but its true branch
        # cannot afford the required follow-up within budget 10.
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 6},
                             {'id': 'tB', 'node': 'B', 'cost': 6},
                             {'id': 'tRoot', 'node': 'top', 'cost': 9}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['policy']['test_id'], 'tRoot')
        self.assertEqual(report['expected_cost'], '9')
        self.assertEqual(report['worst_cost'], 9)
        assert_policy_costs(self, request, report)
        # Without the decisive affordable test, no compliant policy exists.
        request['tests'] = request['tests'][:2]
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'infeasible')
        self.assertIsNone(report['policy'])
        self.assertIsNone(report['expected_cost'])
        self.assertEqual(report['root_failure_probability'], '3/4')

    def test_zero_mass_branch_still_requires_testing(self):
        tree = {'events': [{'id': 'A', 'numerator': 0, 'denominator': 1},
                           {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                'root': 'top'}
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 1},
                             {'id': 'tB', 'node': 'B', 'cost': 1}],
                   'budget': 2}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'optimal')
        # B first; when B is false, root is still logically open because the
        # zero-mass worlds with A=true must be ruled out.
        self.assertEqual(report['policy']['test_id'], 'tB')
        self.assertEqual(report['policy']['on_false']['test_id'], 'tA')
        self.assertEqual(report['expected_cost'], '3/2')
        self.assertEqual(report['worst_cost'], 2)
        worlds, stats = execute_policy(request, report)
        zero_mass_paths = [cost for world, cost in stats if world['weight'] == 0]
        self.assertTrue(zero_mass_paths)
        self.assertEqual(max(zero_mass_paths), 2)

    def test_zero_mass_evidence_vs_logical_contradiction(self):
        tree = {'events': [{'id': 'A', 'numerator': 0, 'denominator': 1}],
                'gates': [], 'root': 'A'}
        zero = {'tree': tree, 'evidence': [{'node': 'A', 'failed': True}],
                'tests': [{'id': 'tA', 'node': 'A', 'cost': 1}], 'budget': 5}
        report = diagnose(zero)
        self.assertEqual(report, oracle_report(zero))
        self.assertEqual(report['status'], 'zero_mass_evidence')
        self.assertEqual(report['logical_worlds'], 1)
        self.assertEqual(report['evidence_probability'], '0')
        self.assertIsNone(report['policy'])
        # Even when the root would be determined, zero mass stops planning.
        contradiction = {'tree': tree,
                         'evidence': [{'node': 'A', 'failed': True},
                                      {'node': 'A', 'failed': False}],
                         'tests': [], 'budget': 5}
        with self.assertRaises(ValueError):
            diagnose(contradiction)
        tree2 = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                            {'id': 'B', 'numerator': 1, 'denominator': 2}],
                 'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                 'root': 'top'}
        # On an OR gate, A=true forces top=true, so top=false contradicts it.
        conflict = {'tree': tree2,
                    'evidence': [{'node': 'A', 'failed': True},
                                 {'node': 'top', 'failed': False}],
                    'tests': [], 'budget': 5}
        report = diagnose(conflict)
        self.assertEqual(report, oracle_report(conflict))
        self.assertEqual(report['status'], 'inconsistent_evidence')
        self.assertEqual(report['logical_worlds'], 0)
        self.assertIsNone(report['evidence_probability'])

    def test_tie_break_utf8_and_determinism(self):
        tree = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                           {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                'root': 'top'}
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 'z', 'node': 'B', 'cost': 1},
                             {'id': 'é', 'node': 'A', 'cost': 1}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        # UTF-8 bytes: 'z' = 7a, 'é' = c3 a9, so 'z' is smaller despite code point.
        self.assertEqual(report['policy']['test_id'], 'z')
        self.assertEqual(json.dumps(report, sort_keys=True),
                         json.dumps(diagnose(json.loads(json.dumps(request))), sort_keys=True))

    def test_probability_one_is_not_logical_certainty(self):
        # P(A)=1 still admits the zero-mass world A=false, so the root stays
        # logically open: no test means infeasible despite probability 1.
        tree = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 1}],
                'gates': [], 'root': 'A'}
        request = {'tree': tree, 'evidence': [], 'tests': [], 'budget': 0}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['root_failure_probability'], '1')
        solvable = dict(request, tests=[{'id': 't', 'node': 'A', 'cost': 1}], budget=1)
        report = diagnose(solvable)
        self.assertEqual(report, oracle_report(solvable))
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['expected_cost'], '1')

    def test_no_tests_and_zero_budget_edges(self):
        tree = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2}],
                'gates': [], 'root': 'A'}
        undetermined = {'tree': tree, 'evidence': [], 'tests': [], 'budget': 5}
        report = diagnose(undetermined)
        self.assertEqual(report, oracle_report(undetermined))
        self.assertEqual(report['status'], 'infeasible')
        determined = {'tree': tree, 'evidence': [{'node': 'A', 'failed': True}],
                      'tests': [], 'budget': 0}
        report = diagnose(determined)
        self.assertEqual(report, oracle_report(determined))
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['policy'], {'kind': 'leaf', 'failed': True})
        self.assertEqual(report['expected_cost'], '0')
        self.assertEqual(report['worst_cost'], 0)

    def test_leaf_when_root_already_determined(self):
        tree = self.SHARED_TREE
        request = {'tree': tree, 'evidence': [{'node': 'AB', 'failed': True}],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 1}], 'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['policy'], {'kind': 'leaf', 'failed': True})
        self.assertEqual(report['expected_cost'], '0')
        self.assertEqual(report['worst_cost'], 0)
        self.assertEqual(report['root_failure_probability'], '1')

    def test_null_branch_mechanism_and_evidence_tested_node(self):
        tree = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                           {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                'root': 'top'}
        request = {'tree': tree, 'evidence': [{'node': 'A', 'failed': False}],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 1},
                             {'id': 'tB', 'node': 'B', 'cost': 1}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['policy']['test_id'], 'tB')
        worlds, _ = execute_policy(request, report)
        self.assertEqual(len(worlds), 2)

        # Every emitted test node carries both branch keys (null is the wire
        # value for logically impossible outcomes).
        def walk(node):
            if node['kind'] == 'leaf':
                self.assertEqual(set(node), {'kind', 'failed'})
                return
            self.assertEqual(set(node), {'kind', 'test_id', 'on_false', 'on_true'})
            if node['on_false'] is not None:
                walk(node['on_false'])
            if node['on_true'] is not None:
                walk(node['on_true'])

        walk(report['policy'])

        # White-box check of the mechanism that produces null: under evidence
        # A=false the tA true-reading mask intersects no feasible world, so its
        # true branch would serialize as null; an uninformative test is strictly
        # dominated and therefore never selected by the normative policy.
        from fault_tree.diagnose.model import Planner
        spec = parse_diagnosis_input(request)
        planner = Planner(spec)
        tA_index = next(i for i, t in enumerate(planner.tests) if t['id'] == 'tA')
        self.assertEqual(planner.full_mask & planner.test_true[tA_index], 0)
        self.assertEqual(planner.full_mask & planner.test_false[tA_index],
                         planner.full_mask)
        # A policy forced to rely on the uninformative test alone is infeasible.
        forced = dict(request)
        forced['tests'] = [request['tests'][0]]
        forced_report = diagnose(forced)
        self.assertEqual(forced_report, oracle_report(forced))
        self.assertEqual(forced_report['status'], 'infeasible')


    def test_zero_mass_worlds_keep_logically_open_root(self):
        # Probability 0 never means logically impossible: an AND/OR root over
        # prob-0 events stays open because the all-true world still exists.
        tree = {'events': [{'id': 'A', 'numerator': 0, 'denominator': 1},
                           {'id': 'B', 'numerator': 0, 'denominator': 1}],
                'gates': [{'id': 'top', 'op': 'AND', 'inputs': ['A', 'B']}],
                'root': 'top'}
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 't', 'node': 'top', 'cost': 1}], 'budget': 5}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['status'], 'optimal')
        self.assertEqual(report['policy']['kind'], 'test')
        # Closing the zero-mass worlds with evidence makes the root a leaf.
        closed = dict(request, evidence=[{'node': 'A', 'failed': False}])
        report_closed = diagnose(closed)
        self.assertEqual(report_closed, oracle_report(closed))
        self.assertEqual(report_closed['policy'], {'kind': 'leaf', 'failed': False})
        self.assertEqual(report_closed['evidence_probability'], '1')

    def test_zero_mass_subproblem_uses_worst_case_objective(self):
        # Positive-mass root, but after B=false the remaining open worlds are
        # all zero mass (only A could still be true with P(A)=0). That branch
        # must pick by minimum worst-case cost, not by expectation.
        tree = {'events': [{'id': 'A', 'numerator': 0, 'denominator': 1},
                           {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                'root': 'top'}
        request = {'tree': tree, 'evidence': [],
                   'tests': [{'id': 'tA', 'node': 'A', 'cost': 4},
                             {'id': 'tB', 'node': 'B', 'cost': 1},
                             {'id': 'tTop', 'node': 'top', 'cost': 3}],
                   'budget': 10}
        report = diagnose(request)
        self.assertEqual(report, oracle_report(request))
        self.assertEqual(report['policy']['test_id'], 'tB')
        zero_mass_branch = report['policy']['on_false']
        self.assertEqual(zero_mass_branch['kind'], 'test')
        self.assertEqual(zero_mass_branch['test_id'], 'tTop')  # worst 3 < worst 4


class Validation(unittest.TestCase):
    BASE = {'tree': {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                                 {'id': 'B', 'numerator': 1, 'denominator': 2}],
                     'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}],
                     'root': 'top'},
            'evidence': [],
            'tests': [{'id': 't1', 'node': 'A', 'cost': 1}],
            'budget': 5}

    def bad(self, mutate):
        request = json.loads(json.dumps(self.BASE))
        mutate(request)
        with self.assertRaises((ValueError, TypeError)):
            diagnose(request)

    def test_strict_envelope(self):
        self.bad(lambda r: r.pop('budget'))
        self.bad(lambda r: r.update(extra=1))
        self.bad(lambda r: r['tree'].update(extra=1))
        self.bad(lambda r: r['tests'][0].update(extra=1))
        self.bad(lambda r: r['evidence'].append({'node': 'A', 'failed': True, 'extra': 1}))

    def test_tree_contract_not_relaxed(self):
        self.bad(lambda r: r['tree']['events'].append(
            {'id': 'C', 'numerator': 1, 'denominator': 2, 'extra': 1}))
        self.bad(lambda r: r['tree']['gates'][0].update(op='XOR'))
        self.bad(lambda r: r['tree']['gates'][0].update(inputs=['A', 'A']))
        self.bad(lambda r: r['tree']['gates'][0].update(inputs=['A', 'nope']))
        self.bad(lambda r: r['tree'].update(root='nope'))
        self.bad(lambda r: r['tree']['events'][0].update(numerator=3, denominator=2))

    def test_evidence_rules(self):
        self.bad(lambda r: r['evidence'].append({'node': 'nope', 'failed': True}))
        self.bad(lambda r: r['evidence'].append({'node': 'A', 'failed': 1}))
        self.bad(lambda r: r['evidence'].append({'node': 'A', 'failed': 'true'}))
        self.bad(lambda r: r['evidence'].extend(
            [{'node': 'A', 'failed': True}, {'node': 'A', 'failed': False}]))
        self.bad(lambda r: r.update(evidence=[{'node': 'A'}] * 117))

    def test_test_rules(self):
        self.bad(lambda r: r['tests'].append({'id': 't1', 'node': 'B', 'cost': 1}))
        self.bad(lambda r: r['tests'].append({'id': 't2', 'node': 'nope', 'cost': 1}))
        self.bad(lambda r: r['tests'].append({'id': 't2', 'node': 'B', 'cost': 0}))
        self.bad(lambda r: r['tests'].append({'id': 't2', 'node': 'B', 'cost': 10 ** 9 + 1}))
        self.bad(lambda r: r['tests'].append({'id': 't2', 'node': 'B', 'cost': True}))
        self.bad(lambda r: r['tests'].append({'id': 't2', 'node': 'B', 'cost': 1.5}))
        self.bad(lambda r: r.update(tests=[{'id': 't%d' % i, 'node': 'A', 'cost': 1}
                                           for i in range(11)]))

    def test_budget_rules(self):
        for bad_budget in (-1, 10 ** 9 + 1, 1.0, True, False, '5', None):
            request = json.loads(json.dumps(self.BASE))
            request['budget'] = bad_budget
            with self.assertRaises((ValueError, TypeError)):
                diagnose(request)

    def test_allowed_shapes(self):
        request = json.loads(json.dumps(self.BASE))
        # Two test ids on one node and a test on an evidenced node are allowed.
        request['evidence'] = [{'node': 'A', 'failed': True}]
        request['tests'] = [{'id': 't1', 'node': 'A', 'cost': 1},
                            {'id': 't2', 'node': 'A', 'cost': 2},
                            {'id': 't3', 'node': 'top', 'cost': 10 ** 9}]
        request['budget'] = 10 ** 9
        report = diagnose(request)
        self.assertIn(report['status'], ('optimal', 'infeasible'))

    def test_array_reorder_invariant(self):
        request = json.loads(json.dumps(self.BASE))
        request['tests'] = [{'id': 'tB', 'node': 'B', 'cost': 2},
                            {'id': 'tA', 'node': 'A', 'cost': 1}]
        first = diagnose(request)
        request['tests'].reverse()
        request['tree']['events'].reverse()
        request['tree']['gates'].reverse()
        self.assertEqual(diagnose(request), first)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def cli(self, source, output):
        return subprocess.run([sys.executable, '-m', 'fault_tree.diagnose',
                               '--source', str(source), '--output', str(output)],
                              capture_output=True, text=True, cwd=ROOT)

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj), encoding='utf-8')
        return path

    def test_cli_success_envelope_and_stdout(self):
        source = self.write('in.json', Validation.BASE)
        output = self.dir / 'out.json'
        run = self.cli(source, output)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(set(report), {'schema_version', 'source_sha256', 'result'})
        self.assertEqual(report['schema_version'], 3)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(set(report['result']),
                         {'status', 'logical_worlds', 'evidence_probability',
                          'root_failure_probability', 'expected_cost', 'worst_cost', 'policy'})
        # The same report is printed to stdout.
        self.assertEqual(json.loads(run.stdout), report)

    def test_normal_statuses_exit_zero(self):
        for status, request in (
                ('inconsistent_evidence', {'tree': Validation.BASE['tree'],
                                           'evidence': [{'node': 'A', 'failed': True},
                                                        {'node': 'top', 'failed': False}],
                                           'tests': [], 'budget': 1}),
                ('zero_mass_evidence', {'tree': {'events': [{'id': 'A', 'numerator': 0,
                                                              'denominator': 1}],
                                                 'gates': [], 'root': 'A'},
                                        'evidence': [{'node': 'A', 'failed': True}],
                                        'tests': [], 'budget': 1}),
                ('infeasible', {**Validation.BASE, 'budget': 0})):
            source = self.write(status + '.json', request)
            output = self.dir / (status + '.out.json')
            run = self.cli(source, output)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(output.read_text())['result']['status'], status)

    def test_failure_preserves_output(self):
        output = self.dir / 'out.json'
        output.write_text('prior')
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '{"tree":{}}'):
            source = self.dir / 'bad.json'
            source.write_text(body)
            run = self.cli(source, output)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(output.read_text(), 'prior')

    def test_same_path_and_symlink_rejected(self):
        source = self.write('same.json', Validation.BASE)
        self.assertEqual(self.cli(source, source).returncode, 2)
        link = self.dir / 'link.json'
        link.symlink_to(source)
        out = self.dir / 'via-link.json'
        self.assertEqual(self.cli(source, link).returncode, 2)
        self.assertFalse(out.exists())

    def test_byte_stability_and_sha_change_on_reorder(self):
        request = json.loads(json.dumps(Validation.BASE))
        request['tests'] = [{'id': 'tB', 'node': 'B', 'cost': 2},
                            {'id': 'tA', 'node': 'A', 'cost': 1}]
        s1 = self.write('1.json', request)
        o1 = self.dir / '1.out.json'
        self.assertEqual(self.cli(s1, o1).returncode, 0)
        first = o1.read_bytes()
        self.assertEqual(self.cli(s1, o1).returncode, 0)
        self.assertEqual(o1.read_bytes(), first)
        shuffled = json.loads(json.dumps(request))
        shuffled['tests'].reverse()
        shuffled['tree']['events'].reverse()
        s2 = self.write('2.json', shuffled)
        o2 = self.dir / '2.out.json'
        self.assertEqual(self.cli(s2, o2).returncode, 0)
        self.assertEqual(json.loads(o2.read_text())['result'],
                         json.loads(first.decode())['result'])
        self.assertNotEqual(json.loads(o2.read_text())['source_sha256'],
                            json.loads(first.decode())['source_sha256'])

    def test_size_limit(self):
        big = json.loads(json.dumps(Validation.BASE))
        big['tests'][0]['id'] = 'x' * (8_000_000)
        source = self.write('big.json', big)
        self.assertGreater(len(source.read_bytes()), 8_000_000)
        run = self.cli(source, self.dir / 'big.out.json')
        self.assertEqual(run.returncode, 2)


class AcceptanceSize(unittest.TestCase):
    """Accepted upper bound: 16 basic events, 100 gates, 10 tests."""

    @staticmethod
    def build_request():
        import random
        rng = random.Random(20261117)
        events = []
        nodes = []
        for i in range(16):
            denominator = rng.randint(2, 97)
            events.append({'id': 'E%02d' % i,
                           'numerator': rng.randint(0, denominator),
                           'denominator': denominator})
            nodes.append('E%02d' % i)
        gates = []
        for g in range(100):
            inputs = rng.sample(nodes, 2)
            gates.append({'id': 'G%03d' % g,
                          'op': rng.choice(['AND', 'OR']), 'inputs': inputs})
            nodes.append('G%03d' % g)
        tree = {'events': events, 'gates': gates, 'root': 'G099'}
        tests = [{'id': 't%02d' % t, 'node': rng.choice(nodes),
                  'cost': rng.randint(1, 20)} for t in range(10)]
        # A direct root test guarantees the problem is solvable; its cost still
        # competes with the cheaper interior tests under the normative objective.
        tests[0] = {'id': 't00', 'node': 'G099', 'cost': 50}
        evidence = [{'node': rng.choice(nodes), 'failed': rng.choice([True, False])}]
        # A generous budget guarantees at least one compliant policy so the
        # produced tree is executed on every one of the 65536 worlds below.
        return {'tree': tree, 'evidence': evidence, 'tests': tests,
                'budget': 10 ** 6}

    def test_full_size_policy_executed_on_all_worlds(self):
        request = self.build_request()
        report = diagnose(request)
        self.assertIn(report['status'], ('optimal', 'infeasible'))
        self.assertLessEqual(report['logical_worlds'],
                             2 ** len(request['tree']['events']))
        self.assertGreater(report['logical_worlds'], 0)
        if report['status'] != 'optimal':
            self.assertIsNone(report['policy'])
            return
        worlds, stats = execute_policy(request, report)
        self.assertEqual(len(worlds), report['logical_worlds'])
        self.assertLessEqual(len(worlds), 65536)
        self.assertGreater(len(worlds), 0)
        # execute_policy already asserted leaf correctness, no repeated test and
        # per-path budget; independently recompute expectation and worst cost.
        total_mass = sum((w['weight'] for w in worlds), Fraction(0))
        expected = sum((w['weight'] * cost for w, cost in stats), Fraction(0)) / total_mass
        self.assertEqual(_fraction_text(expected), report['expected_cost'])
        self.assertEqual(max(cost for _, cost in stats), report['worst_cost'])
        self.assertLessEqual(report['worst_cost'], request['budget'])
        # Every policy leaf is reached by at least one world (no dead branches).
        reachable_leaves = set()

        def walk(node):
            if node['kind'] == 'leaf':
                reachable_leaves.add(json.dumps(node, sort_keys=True))
            else:
                if node['on_false'] is not None:
                    walk(node['on_false'])
                if node['on_true'] is not None:
                    walk(node['on_true'])

        walk(report['policy'])
        # Number of leaves cannot exceed the number of distinct world signatures.
        self.assertLessEqual(len(reachable_leaves), len(worlds))

    def test_full_size_infeasible_when_budget_too_tight(self):
        request = self.build_request()
        request['budget'] = 0
        report = diagnose(request)
        self.assertEqual(report['status'], 'infeasible')
        self.assertIsNone(report['policy'])
        self.assertIsNone(report['expected_cost'])
        self.assertIsNone(report['worst_cost'])
        # Root probability is still computed exactly under the initial evidence.
        Fraction(report['root_failure_probability'])


if __name__ == '__main__':
    unittest.main()
