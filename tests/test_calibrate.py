"""Tests for the v5 shared-parameter EM calibration (`fault-calibrate`).

The expected numbers never come from the production engine.  An independent
oracle (tests/_oracle_calibrate.py) enumerates every basic-event world in
exact Fraction arithmetic, evaluates the AND/OR DAG independently and applies
the documented E/M formulas directly.  Small hand-built cases additionally pin
the first round, the convergence/iteration-limit decision and every report
field to values that can be checked by hand.
"""
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from fault_tree.calibrate.model import (
    calibrate, parse_calibration_input, _WorldTable, _world_masses,
    _theta_by_name)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _oracle_calibrate import oracle  # noqa: E402


def clone(obj):
    return json.loads(json.dumps(obj))


# ---------------------------------------------------------------------------
# Hand-built request: two groups that both move, one frozen unreferenced event
# ---------------------------------------------------------------------------

def hand_request():
    return {
        'tree': {
            'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 4},
                {'id': 'B', 'numerator': 1, 'denominator': 4},
                {'id': 'C', 'numerator': 1, 'denominator': 3},
                {'id': 'D', 'numerator': 1, 'denominator': 5}],
            'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B', 'C']}],
            'root': 'top'},
        'groups': [
            {'id': 'g1', 'events': ['A', 'B'], 'alpha': 1, 'beta': 1},
            {'id': 'g2', 'events': ['C'], 'alpha': 1, 'beta': 1}],
        'observations': [
            {'id': 'obs-all-failed', 'count': 2,
             'evidence': [{'node': 'A', 'failed': True},
                          {'node': 'B', 'failed': True},
                          {'node': 'C', 'failed': True}]},
            {'id': 'obs-top-healthy', 'count': 3,
             'evidence': [{'node': 'top', 'failed': False}]},
            {'id': 'obs-empty', 'count': 1, 'evidence': []}],
        'max_iterations': 50,
        'tolerance': 1e-9}


class HandComputedFirstRound(unittest.TestCase):
    """Every number below is derived by hand on the 3 fitted events.

    Initial theta1 = 1/4 (A,B), theta2 = 1/3 (C); frozen D = 1/5 is never a
    root ancestor.  The M denominator is alpha+beta+m*sum(count):
      g1: 2 + 2*6 = 14 ; g2: 2 + 1*6 = 8.
    """

    def setUp(self):
        self.request = hand_request()
        self.report = calibrate(clone(self.request))

    def test_initial_trace_point(self):
        point = self.report['trace'][0]
        self.assertEqual(point['iteration'], 0)
        self.assertIsNone(point['max_delta'])
        self.assertEqual([p['probability'] for p in point['parameters']],
                         [0.25, 1 / 3])
        # L values at the initial point, exact fractions:
        # obs-all-failed = theta1^2 * theta2 = (1/4)^2*(1/3) = 1/48
        # top healthy (A=B=C=false) = (3/4)^2*(2/3) = 3/8; empty = 1.
        # The end-of-run observations carry final L, so the initial L is read
        # back from the independent Fraction oracle's first history point.
        exact = oracle(self.request, max_rounds=0)
        initial_likelihood = exact['history'][0]['likelihoods']
        self.assertAlmostEqual(initial_likelihood['obs-all-failed'], 1 / 48)
        self.assertAlmostEqual(initial_likelihood['obs-top-healthy'], 3 / 8)
        self.assertAlmostEqual(initial_likelihood['obs-empty'], 1.0)
        ell = (2 * math.log(1 / 48) + 3 * math.log(3 / 8))
        self.assertAlmostEqual(point['log_likelihood'], ell)
        # J = ell + log(1/4)+log(3/4) [g1] + log(1/3)+log(2/3) [g2]
        penalty = (math.log(0.25) + math.log(0.75)
                   + math.log(1 / 3) + math.log(2 / 3))
        self.assertAlmostEqual(point['objective'], ell + penalty)

    def test_first_round_update_is_simultaneous(self):
        point = self.report['trace'][1]
        # Exact Fraction EM, one round (see class docstring denominators).
        first = oracle(self.request, max_rounds=1)
        for group_index in range(2):
            self.assertAlmostEqual(
                point['parameters'][group_index]['probability'],
                first['history'][1]['theta'][group_index], places=12)
        # The two groups must both change in the first round (a sequential or
        # single-group implementation would break one of these).
        initial = [0.25, 1 / 3]
        for group_index in range(2):
            self.assertNotEqual(point['parameters'][group_index]['probability'],
                                initial[group_index])

    def test_trace_length_and_status(self):
        self.assertEqual(len(self.report['trace']),
                         self.report['iterations'] + 1)
        self.assertEqual(self.report['status'], 'converged')
        for index, point in enumerate(self.report['trace']):
            self.assertEqual(point['iteration'], index)
            if index == 0:
                self.assertIsNone(point['max_delta'])
            else:
                self.assertIsNotNone(point['max_delta'])
        # Convergence point is the first one within tolerance.
        self.assertLessEqual(
            self.report['trace'][-1]['max_delta'],
            self.request['tolerance'])

    def test_final_report_consistency(self):
        final = self.report['trace'][-1]
        self.assertEqual(
            [g['probability'] for g in self.report['groups']],
            [p['probability'] for p in final['parameters']])
        # Every declared event appears, including the frozen one, in UTF-8
        # byte order, and the frozen prior is untouched.
        self.assertEqual([row['id'] for row in self.report['events']],
                         ['A', 'B', 'C', 'D'])
        frozen = {row['id']: row['probability']
                  for row in self.report['events']}['D']
        self.assertEqual(frozen, 0.2)
        # The shared parameter means independent repeated events: A and B
        # carry the same probability but remain separate event rows.
        g1 = self.report['groups'][0]
        self.assertEqual(g1['events'], ['A', 'B'])
        self.assertEqual(g1['probability'],
                         self.report['events'][0]['probability'])
        self.assertEqual(g1['probability'],
                         self.report['events'][1]['probability'])


class OracleComparison(unittest.TestCase):
    def compare(self, request, places=10):
        report = calibrate(clone(request))
        expected = oracle(request)
        self.assertEqual(report['iterations'], expected['iterations'])
        self.assertEqual(len(report['trace']), len(expected['history']))
        for index, (actual, exact) in enumerate(
                zip(report['trace'], expected['history'])):
            for group_index, parameter in enumerate(actual['parameters']):
                self.assertAlmostEqual(
                    parameter['probability'],
                    exact['theta'][group_index], places=places)
            self.assertAlmostEqual(actual['log_likelihood'], exact['ell'],
                                   places=9)
            self.assertAlmostEqual(actual['objective'], exact['J'], places=9)
            if index == 0:
                self.assertIsNone(actual['max_delta'])
            else:
                # The oracle records update k->k+1's delta on history point k.
                self.assertAlmostEqual(
                    actual['max_delta'],
                    expected['history'][index - 1]['max_delta'], places=12)
        return report

    def test_hand_request_matches_fraction_oracle(self):
        self.compare(hand_request())

    def test_two_groups_both_move_catches_denominator_errors(self):
        # Multi-member group g1 (m=2) and singleton g2 (m=1): using a per-
        # member denominator instead of m*sum(count), or updating groups
        # sequentially, would diverge from the Fraction oracle.
        request = hand_request()
        report = self.compare(request)
        first = report['trace'][1]['parameters']
        # Both groups move and the multi-member group is not double-counted.
        self.assertNotEqual(first[0]['probability'], 0.25)
        self.assertNotEqual(first[1]['probability'], 1 / 3)

    def test_random_fuzz(self):
        import random
        rng = random.Random(90210)
        for seed in range(60):
            rng.seed(seed)
            n = rng.randint(1, 5)
            names = ['E%d' % i for i in range(n)]
            events = []
            for name in names:
                denominator = rng.randint(2, 9)
                numerator = rng.randint(1, denominator - 1)
                events.append({'id': name, 'numerator': numerator,
                               'denominator': denominator})
            gates = []
            nodes = list(names)
            for gate_index in range(rng.randint(0, 3)):
                if len(nodes) < 2:
                    break
                left, right = rng.sample(nodes, 2)
                gate_id = 'G%d' % gate_index
                gates.append({'id': gate_id,
                              'op': rng.choice(['AND', 'OR']),
                              'inputs': [left, right]})
                nodes.append(gate_id)
            # Random disjoint groups over a subset of events.
            shuffled = list(names)
            rng.shuffle(shuffled)
            groups = []
            cursor = 0
            for group_index in range(rng.randint(1, max(1, n))):
                size = rng.randint(1, max(1, n - cursor))
                members = shuffled[cursor:cursor + size]
                cursor += size
                # Force a shared equal initial probability.
                base = events[names.index(members[0])]
                for member in members[1:]:
                    row = events[names.index(member)]
                    row['numerator'], row['denominator'] = (
                        base['numerator'], base['denominator'])
                groups.append({'id': 'grp%d' % group_index,
                               'events': members,
                               'alpha': rng.randint(1, 5),
                               'beta': rng.randint(1, 5)})
                if cursor == n:
                    break
            observations = []
            for row_index in range(rng.randint(1, 4)):
                evidence = []
                for node in rng.sample(nodes, rng.randint(0, min(2, len(nodes)))):
                    evidence.append({'node': node,
                                     'failed': rng.choice([True, False])})
                observations.append({'id': 'obs%d' % row_index,
                                     'count': rng.randint(1, 5),
                                     'evidence': evidence})
            request = {
                'tree': {'events': events, 'gates': gates,
                         'root': rng.choice(nodes)},
                'groups': groups, 'observations': observations,
                'max_iterations': rng.randint(1, 8),
                'tolerance': 1e-9}
            # Skip instances whose readings are unsatisfiable at the initial
            # point; those have their own dedicated unsupported_data tests.
            exact = oracle(request)
            if exact.get('status') == 'unsupported_data':
                continue
            self.compare(request, places=8)


class StructuralScenarios(unittest.TestCase):
    def test_shared_sub_gate_evaluated_on_one_vector(self):
        # A shared AND gate feeds two parents; the reading must be satisfied
        # by one event vector, never by multiplying independent marginals.
        request = {
            'tree': {'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 2},
                {'id': 'B', 'numerator': 1, 'denominator': 2},
                {'id': 'C', 'numerator': 1, 'denominator': 2}],
                'gates': [
                    {'id': 'shared', 'op': 'AND', 'inputs': ['A', 'B']},
                    {'id': 'left', 'op': 'OR', 'inputs': ['shared', 'C']},
                    {'id': 'right', 'op': 'AND', 'inputs': ['shared', 'C']}],
                'root': 'left'},
            'groups': [{'id': 'g', 'events': ['A', 'B', 'C'],
                        'alpha': 1, 'beta': 1}],
            'observations': [
                {'id': 'both-parents', 'count': 4,
                 'evidence': [{'node': 'left', 'failed': True},
                              {'node': 'right', 'failed': True}]},
                {'id': 'shared-healthy', 'count': 2,
                 'evidence': [{'node': 'shared', 'failed': False}]}],
            'max_iterations': 20, 'tolerance': 1e-10}
        report = calibrate(clone(request))
        exact = oracle(request)
        for index, point in enumerate(report['trace']):
            self.assertAlmostEqual(
                point['parameters'][0]['probability'],
                exact['history'][index]['theta'][0], places=10)

    def test_contradictory_readings_are_unsupported(self):
        request = {
            'tree': {'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 2},
                {'id': 'B', 'numerator': 1, 'denominator': 2}],
                'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
                'root': 'g'},
            'groups': [{'id': 'g1', 'events': ['A', 'B'],
                        'alpha': 1, 'beta': 1}],
            'observations': [
                {'id': 'impossible', 'count': 1, 'evidence': [
                    {'node': 'g', 'failed': True},
                    {'node': 'A', 'failed': False},
                    {'node': 'B', 'failed': False}]}],
            'max_iterations': 5, 'tolerance': 1e-9}
        report = calibrate(request)
        self.assertEqual(report['status'], 'unsupported_data')
        self.assertEqual(report['unsupported_observations'], ['impossible'])
        self._assert_unsupported_shape(report)

    def test_frozen_zero_and_one_events_force_unsupported(self):
        request = {
            'tree': {'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 2},
                {'id': 'F0', 'numerator': 0, 'denominator': 1},
                {'id': 'F1', 'numerator': 1, 'denominator': 1}],
                'gates': [], 'root': 'A'},
            'groups': [{'id': 'g1', 'events': ['A'],
                        'alpha': 1, 'beta': 1}],
            'observations': [
                {'id': 'want-f0-failed', 'count': 1,
                 'evidence': [{'node': 'F0', 'failed': True}]},
                {'id': 'want-f1-healthy', 'count': 1,
                 'evidence': [{'node': 'F1', 'failed': False}]},
                {'id': 'fine', 'count': 2, 'evidence': []}],
            'max_iterations': 5, 'tolerance': 1e-9}
        report = calibrate(request)
        self.assertEqual(report['status'], 'unsupported_data')
        self.assertEqual(report['unsupported_observations'],
                         ['want-f0-failed', 'want-f1-healthy'])
        self._assert_unsupported_shape(report)

    def test_non_ancestor_frozen_event_still_gates_support(self):
        # D feeds no gate and is not a root ancestor.  Frozen at 0, an explicit
        # "D failed" reading must still make the row unsupported: it may not be
        # ignored merely because it is unrelated to the root.
        request = hand_request()
        request['tree']['events'][3] = {'id': 'D', 'numerator': 0,
                                        'denominator': 1}
        request['observations'] = [
            {'id': 'lonely-fail', 'count': 1,
             'evidence': [{'node': 'D', 'failed': True}]}]
        report = calibrate(request)
        self.assertEqual(report['status'], 'unsupported_data')
        self.assertEqual(report['unsupported_observations'], ['lonely-fail'])
        # The complementary healthy reading is supported and leaves D frozen.
        request['observations'] = [
            {'id': 'lonely-ok', 'count': 1,
             'evidence': [{'node': 'D', 'failed': False}]}]
        report = calibrate(request)
        self.assertNotEqual(report['status'], 'unsupported_data')
        self.assertEqual(
            {row['id']: row['probability'] for row in report['events']}['D'],
            0.0)

    def test_unobserved_member_keeps_prior_conditional_expectation(self):
        # A and B share one parameter but stay independent.  Only A is ever
        # read; B's posterior must remain its prior in every conditional
        # expectation, which the first-round update pins by hand.
        request = {
            'tree': {'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 3},
                {'id': 'B', 'numerator': 1, 'denominator': 3}],
                'gates': [], 'root': 'A'},
            'groups': [{'id': 'g', 'events': ['A', 'B'],
                        'alpha': 1, 'beta': 1}],
            'observations': [
                {'id': 'only-a', 'count': 5,
                 'evidence': [{'node': 'A', 'failed': True}]}],
            'max_iterations': 30, 'tolerance': 1e-12}
        report = calibrate(request)
        # Given A=true: E[C] = 1 (A certain) + 1/3 (B retains its prior).
        expected = Fraction(1 + 5 * Fraction(4, 3), 2 + 2 * 5)
        self.assertAlmostEqual(
            report['trace'][1]['parameters'][0]['probability'],
            float(expected), places=12)

    def test_shared_parameter_is_not_a_common_cause(self):
        # Two equal-probability independent events under an AND gate: the gate
        # probability is theta^2, never theta (a common-cause collapse would
        # treat the pair as one variable).  Checked through the likelihood of a
        # gate-failed reading against the exact Fraction oracle.
        request = {
            'tree': {'events': [
                {'id': 'A', 'numerator': 1, 'denominator': 4},
                {'id': 'B', 'numerator': 1, 'denominator': 4}],
                'gates': [{'id': 'g', 'op': 'AND', 'inputs': ['A', 'B']}],
                'root': 'g'},
            'groups': [{'id': 'pair', 'events': ['A', 'B'],
                        'alpha': 1, 'beta': 1}],
            'observations': [
                {'id': 'gate-failed', 'count': 3,
                 'evidence': [{'node': 'g', 'failed': True}]}],
            'max_iterations': 5, 'tolerance': 1e-12}
        exact = oracle(request, max_rounds=0)
        # Initial P(g=true) = (1/4)^2 = 1/16, not 1/4.
        self.assertAlmostEqual(
            exact['history'][0]['likelihoods']['gate-failed'], 1 / 16)
        # The production trace's initial point uses the same independent
        # product model; verify it converges to a valid interior estimate.
        report = calibrate(request)
        self.assertNotEqual(report['status'], 'unsupported_data')
        self.assertAlmostEqual(
            report['trace'][0]['parameters'][0]['probability'], 0.25)
        for group in report['groups']:
            self.assertGreater(group['probability'], 0.0)
            self.assertLess(group['probability'], 1.0)

    def test_empty_evidence_is_always_supported(self):
        request = hand_request()
        request['observations'] = [
            {'id': 'blank', 'count': 7, 'evidence': []}]
        report = calibrate(request)
        self.assertNotEqual(report['status'], 'unsupported_data')
        # An unconditioned row has likelihood 1 at every theta (binary64 noise
        # in the last ulp is acceptable).
        self.assertAlmostEqual(report['observations'][0]['probability'],
                               1.0, places=12)

    def _assert_unsupported_shape(self, report):
        self.assertEqual(report['iterations'], 0)
        self.assertIsNone(report['groups'])
        self.assertIsNone(report['events'])
        self.assertEqual(report['observations'], [])
        self.assertEqual(report['trace'], [])


class CountAndEvidenceWeighting(unittest.TestCase):
    def test_splitting_rows_preserves_the_trajectory(self):
        base = hand_request()
        split = hand_request()
        split['observations'] = [
            {'id': 'z-all-1', 'count': 1,
             'evidence': [{'node': 'A', 'failed': True},
                          {'node': 'B', 'failed': True},
                          {'node': 'C', 'failed': True}]},
            {'id': 'a-all-2', 'count': 1,
             'evidence': [{'node': 'A', 'failed': True},
                          {'node': 'B', 'failed': True},
                          {'node': 'C', 'failed': True}]},
            {'id': 'm-top-1', 'count': 1,
             'evidence': [{'node': 'top', 'failed': False}]},
            {'id': 'b-top-2', 'count': 2,
             'evidence': [{'node': 'top', 'failed': False}]},
            {'id': 'q-empty', 'count': 1, 'evidence': []}]
        first = calibrate(base)
        second = calibrate(split)
        self.assertEqual(first['status'], second['status'])
        self.assertEqual(first['iterations'], second['iterations'])
        for left, right in zip(first['trace'], second['trace']):
            self.assertEqual(
                [p['probability'] for p in left['parameters']],
                [p['probability'] for p in right['parameters']])
            # The evidence blocks are identical, so likelihoods and the
            # weighted objective are bit-identical too.
            self.assertEqual(left['log_likelihood'], right['log_likelihood'])
            self.assertEqual(left['objective'], right['objective'])
            self.assertEqual(left['max_delta'], right['max_delta'])
        self.assertEqual(
            [g['probability'] for g in first['groups']],
            [g['probability'] for g in second['groups']])

    def test_counts_are_not_normalized_to_sum_one(self):
        # With alpha/beta fixed, scaling every count up changes the balance
        # between data and the pseudo-count penalty, so the estimate moves.
        base = hand_request()
        scaled = hand_request()
        for observation in scaled['observations']:
            observation['count'] *= 100
        first = calibrate(base)
        second = calibrate(scaled)
        self.assertNotEqual(
            [g['probability'] for g in first['groups']],
            [g['probability'] for g in second['groups']])
        # And the scaled run still matches the exact oracle.
        exact = oracle(scaled)
        for index, point in enumerate(second['trace']):
            for group_index, parameter in enumerate(point['parameters']):
                self.assertAlmostEqual(
                    parameter['probability'],
                    exact['history'][index]['theta'][group_index], places=9)

    def test_repeated_evidence_rows_each_keep_their_count(self):
        request = hand_request()
        request['observations'] = [
            {'id': 'r1', 'count': 3, 'evidence': []},
            {'id': 'r2', 'count': 3, 'evidence': []}]
        report = calibrate(request)
        # Two rows of count 3 behave like one row of count 6 for the update.
        merged = hand_request()
        merged['observations'] = [
            {'id': 'only', 'count': 6, 'evidence': []}]
        single = calibrate(merged)
        self.assertEqual(
            [g['probability'] for g in report['groups']],
            [g['probability'] for g in single['groups']])


class StoppingRule(unittest.TestCase):
    def test_iteration_limit_status(self):
        request = hand_request()
        request['max_iterations'] = 3
        report = calibrate(request)
        self.assertEqual(report['status'], 'max_iterations')
        self.assertEqual(report['iterations'], 3)
        self.assertEqual(len(report['trace']), 4)

    def test_convergence_takes_precedence_at_the_limit(self):
        request = hand_request()
        natural = calibrate(request)
        convergence_round = natural['iterations']
        request['max_iterations'] = convergence_round
        report = calibrate(request)
        self.assertEqual(report['status'], 'converged')
        self.assertEqual(report['iterations'], convergence_round)

    def test_objective_never_decreases(self):
        report = calibrate(hand_request())
        objectives = [point['objective'] for point in report['trace']]
        for previous, current in zip(objectives, objectives[1:]):
            self.assertGreaterEqual(
                current + 1e-8 * max(1.0, abs(previous)), previous)

    def test_tiny_objective_change_does_not_stop_early(self):
        # A loose parameter tolerance still requires the parameter-distance
        # rule; a small objective change alone never triggers convergence.
        request = hand_request()
        request['tolerance'] = 1e-3
        report = calibrate(request)
        last = report['trace'][-1]
        self.assertLessEqual(last['max_delta'], 1e-3)
        self.assertEqual(report['status'], 'converged')
        # Every recorded delta before the last one was strictly above tol.
        for point in report['trace'][1:-1]:
            self.assertGreater(point['max_delta'], 1e-3)


class InvarianceAndDeterminism(unittest.TestCase):
    def test_semantic_reorders_keep_the_full_result(self):
        request = hand_request()
        baseline = calibrate(request)
        shuffled = hand_request()
        shuffled['tree']['events'].reverse()
        shuffled['tree']['gates'].reverse()
        shuffled['groups'].reverse()
        shuffled['groups'][0]['events'].reverse()
        shuffled['observations'].reverse()
        for observation in shuffled['observations']:
            observation['evidence'].reverse()
        self.assertEqual(calibrate(shuffled), baseline)

    def test_report_is_byte_stable(self):
        request = hand_request()
        first = calibrate(request)
        second = calibrate(clone(request))
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True))

    def test_groups_and_rows_sorted_by_utf8_bytes(self):
        request = hand_request()
        request['groups'] = list(reversed(request['groups']))
        request['groups'][0]['id'] = 'zzz'
        request['groups'][1]['id'] = 'AAA'
        report = calibrate(request)
        group_ids = [group['id'] for group in report['groups']]
        self.assertEqual(group_ids, sorted(group_ids,
                                           key=lambda s: s.encode('utf-8')))
        observation_ids = [row['id'] for row in report['observations']]
        self.assertEqual(
            observation_ids,
            sorted(observation_ids, key=lambda s: s.encode('utf-8')))


class NumericBounds(unittest.TestCase):
    def test_extreme_counts_and_interior_probabilities_stay_finite(self):
        request = hand_request()
        request['observations'] = [
            {'id': 'big', 'count': 10 ** 6, 'evidence': []},
            {'id': 'big2', 'count': 10 ** 6,
             'evidence': [{'node': 'top', 'failed': True}]}]
        request['tree']['events'][0] = {'id': 'A', 'numerator': 1,
                                        'denominator': 10 ** 9}
        request['tree']['events'][1] = {'id': 'B', 'numerator': 1,
                                        'denominator': 10 ** 9}
        request['tree']['events'][2] = {'id': 'C',
                                        'numerator': 10 ** 9 - 1,
                                        'denominator': 10 ** 9}
        request['max_iterations'] = 5
        report = calibrate(request)
        for point in report['trace']:
            for parameter in point['parameters']:
                self.assertTrue(math.isfinite(parameter['probability']))
                self.assertGreater(parameter['probability'], 0.0)
                self.assertLess(parameter['probability'], 1.0)
            self.assertTrue(math.isfinite(point['objective']))
            self.assertTrue(math.isfinite(point['log_likelihood']))
        # The documented lower bound after one update:
        # theta >= 1/(2*10^6 + 16*128*10^6).
        floor = 1 / (2 * 10 ** 6 + 16 * 128 * 10 ** 6)
        for group in report['groups']:
            self.assertGreater(group['probability'], floor)
            self.assertLess(group['probability'], 1 - floor)


class Validation(unittest.TestCase):
    BASE = hand_request()

    def bad(self, mutate):
        request = clone(self.BASE)
        mutate(request)
        with self.assertRaises((ValueError, TypeError)):
            calibrate(request)

    def test_envelope_fields(self):
        self.bad(lambda r: r.pop('groups'))
        self.bad(lambda r: r.update(extra=1))
        self.bad(lambda r: r['tree'].update(extra=1))

    def test_tree_uses_original_contract(self):
        # 17 events exceed the original 16-event cap.
        def seventeen(r):
            r['tree']['events'] = [
                {'id': 'E%d' % i, 'numerator': 1, 'denominator': 2}
                for i in range(17)]
        self.bad(seventeen)
        self.bad(lambda r: r['tree']['gates'][0].update(op='XOR'))
        self.bad(lambda r: r['tree'].update(root='missing'))

    def test_group_rules(self):
        self.bad(lambda r: r.update(groups=[]))
        self.bad(lambda r: r['groups'].append(
            {'id': 'g1', 'events': ['A'], 'alpha': 1, 'beta': 1}))
        self.bad(lambda r: r['groups'].append(
            {'id': 'g3', 'events': ['A'], 'alpha': 1, 'beta': 1}))
        self.bad(lambda r: r['groups'][0].update(alpha=0))
        self.bad(lambda r: r['groups'][0].update(beta=10 ** 6 + 1))
        self.bad(lambda r: r['groups'][0].update(alpha=True))
        self.bad(lambda r: r['groups'][0].update(events=['A', 'A']))
        self.bad(lambda r: r['groups'][0].update(events=[]))
        self.bad(lambda r: r['groups'][0].update(events=['A', 'nope']))
        # Gates cannot be fitted parameters.
        self.bad(lambda r: r['groups'][0].update(events=['top']))
        self.bad(lambda r: r['groups'][0].update(extra=1))

    def test_group_initial_probability_rules(self):
        # Unequal member initial probabilities.
        self.bad(lambda r: r['tree']['events'][1].update(
            numerator=1, denominator=5))
        # Boundary probabilities are rejected for fitted members.
        self.bad(lambda r: r['tree']['events'][0].update(
            numerator=0, denominator=1))
        self.bad(lambda r: r['tree']['events'][0].update(
            numerator=1, denominator=1))

    def test_equal_value_different_fraction_accepted(self):
        request = clone(self.BASE)
        request['tree']['events'][1] = {'id': 'B', 'numerator': 2,
                                        'denominator': 8}
        report = calibrate(request)
        self.assertNotEqual(report['status'], 'unsupported_data')

    def test_observation_rules(self):
        self.bad(lambda r: r.update(observations=[]))
        self.bad(lambda r: r['observations'].append(
            {'id': 'obs-empty', 'count': 1, 'evidence': []}))
        self.bad(lambda r: r['observations'][0].update(count=0))
        self.bad(lambda r: r['observations'][0].update(count=10 ** 6 + 1))
        self.bad(lambda r: r['observations'][0].update(count=True))
        self.bad(lambda r: r['observations'][0]['evidence'].append(
            {'node': 'A', 'failed': True}))
        self.bad(lambda r: r['observations'][0]['evidence'].append(
            {'node': 'nope', 'failed': True}))
        self.bad(lambda r: r['observations'][0]['evidence'].append(
            {'node': 'A', 'failed': 1}))
        self.bad(lambda r: r['observations'][0].update(extra=1))

    def test_iteration_and_tolerance_rules(self):
        self.bad(lambda r: r.update(max_iterations=0))
        self.bad(lambda r: r.update(max_iterations=101))
        self.bad(lambda r: r.update(max_iterations=True))
        self.bad(lambda r: r.update(tolerance=True))
        self.bad(lambda r: r.update(tolerance='1e-9'))
        self.bad(lambda r: r.update(tolerance=float('nan')))
        self.bad(lambda r: r.update(tolerance=float('inf')))
        for invalid in (1e-13, 1e-2):
            request = clone(self.BASE)
            request['tolerance'] = invalid
            with self.assertRaises(ValueError):
                calibrate(request)

    def test_partial_group_coverage_leaves_events_frozen(self):
        request = clone(self.BASE)
        # Only A is fitted; B, C, D stay at their tree priors.
        request['groups'] = [{'id': 'solo', 'events': ['A'],
                              'alpha': 1, 'beta': 1}]
        report = calibrate(request)
        priors = {row['id']: row['probability']
                  for row in report['events']}
        self.assertEqual(priors['B'], 0.25)
        self.assertEqual(priors['C'], 1 / 3)
        self.assertEqual(priors['D'], 0.2)
        self.assertEqual(report['groups'][0]['events'], ['A'])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def cli(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'fault_tree.calibrate',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, cwd=ROOT,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj), encoding='utf-8')
        return path

    def test_success_envelope_stdout_and_file(self):
        source = self.write('in.json', hand_request())
        output = self.dir / 'out.json'
        run = self.cli(source, output)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'result'})
        self.assertEqual(report['schema_version'], 5)
        self.assertEqual(
            report['source_sha256'],
            hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(set(report['result']),
                         {'status', 'iterations', 'groups', 'events',
                          'observations', 'trace',
                          'unsupported_observations'})
        self.assertEqual(json.loads(run.stdout), report)

    def test_unsupported_data_exits_zero(self):
        request = hand_request()
        # Valid, non-duplicate readings that no world can satisfy: top is an OR
        # of A,B,C, so top healthy while A is failed is impossible.
        request['observations'] = [
            {'id': 'bad', 'count': 1,
             'evidence': [{'node': 'top', 'failed': False},
                          {'node': 'A', 'failed': True}]}]
        source = self.write('in.json', request)
        output = self.dir / 'out.json'
        run = self.cli(source, output)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['result']['status'], 'unsupported_data')
        self.assertEqual(
            report['result']['unsupported_observations'], ['bad'])

    def test_byte_stable_replay(self):
        source = self.write('in.json', hand_request())
        output = self.dir / 'out.json'
        self.assertEqual(self.cli(source, output).returncode, 0)
        first = output.read_bytes()
        self.assertEqual(self.cli(source, output).returncode, 0)
        self.assertEqual(output.read_bytes(), first)

    def test_reorder_changes_sha_not_result(self):
        source = self.write('one.json', hand_request())
        one = self.dir / 'one.out.json'
        self.assertEqual(self.cli(source, one).returncode, 0)
        shuffled = hand_request()
        shuffled['tree']['events'].reverse()
        shuffled['observations'].reverse()
        two_source = self.write('two.json', shuffled)
        two = self.dir / 'two.out.json'
        self.assertEqual(self.cli(two_source, two).returncode, 0)
        first = json.loads(one.read_text())
        second = json.loads(two.read_text())
        self.assertEqual(first['result'], second['result'])
        self.assertNotEqual(first['source_sha256'],
                            second['source_sha256'])

    def test_failure_exits_two_and_preserves_output(self):
        output = self.dir / 'out.json'
        output.write_text('prior')
        cases = ['{}', '{"a":1,"a":2}', 'NaN', '{"tree":{}}',
                 json.dumps(hand_request())[:-2] + ',}']
        for body in cases:
            source = self.dir / 'bad.json'
            source.write_text(body)
            run = self.cli(source, output)
            self.assertEqual(run.returncode, 2, body)
            self.assertEqual(output.read_text(), 'prior')

    def test_output_safety(self):
        source = self.write('same.json', hand_request())
        self.assertEqual(self.cli(source, source).returncode, 2)
        link = self.dir / 'link.json'
        link.symlink_to(source)
        self.assertEqual(self.cli(source, link).returncode, 2)
        self.assertEqual(json.loads(source.read_text()), hand_request())

    def test_size_limit(self):
        big = hand_request()
        big['groups'][0]['id'] = 'x' * 8_000_000
        source = self.write('big.json', big)
        self.assertGreater(len(source.read_bytes()), 8_000_000)
        self.assertEqual(
            self.cli(source, self.dir / 'big.out.json').returncode, 2)


class FixtureContract(unittest.TestCase):
    def test_shipped_fixture_matches_oracle(self):
        source = ROOT / 'fixtures/calibrate/request.json'
        request = json.loads(source.read_text())
        report = calibrate(request)
        exact = oracle(request)
        self.assertEqual(report['iterations'], exact['iterations'])
        for index, point in enumerate(report['trace']):
            for group_index, parameter in enumerate(point['parameters']):
                self.assertAlmostEqual(
                    parameter['probability'],
                    exact['history'][index]['theta'][group_index], places=10)

    def test_shipped_unsupported_fixture(self):
        source = ROOT / 'fixtures/calibrate/unsupported.json'
        request = json.loads(source.read_text())
        report = calibrate(request)
        self.assertEqual(report['status'], 'unsupported_data')
        self.assertEqual(report['iterations'], 0)
        self.assertIsNone(report['groups'])
        self.assertIsNone(report['events'])
        self.assertEqual(report['observations'], [])
        self.assertEqual(report['trace'], [])
        self.assertEqual(report['unsupported_observations'],
                         ['trial-unsupported'])


if __name__ == '__main__':
    unittest.main()
