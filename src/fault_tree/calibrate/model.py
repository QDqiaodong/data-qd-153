"""EM calibration of shared basic-event parameter groups (schema v5).

The reliability data team only has partial node readings from a handful of
independent trials, each repeated ``count`` times.  Rows never pool evidence:
each trial is evaluated against the *same* full basic-event vector and every
declared reading must hold in that vector.

Basic events partition into frozen events (kept at their exact tree prior,
which may be 0 or 1) and fitted parameter groups.  Every member of a group
shares one failure probability ``theta_g`` while the events stay conditionally
independent Bernoulli variables: equal parameters, not a common cause.  Group
members must start from one common interior rational probability in (0, 1).

The estimator is a fixed-point EM iteration with Beta(alpha, beta)
pseudo-counts (one pair per group, not per member).  The E step sums the
conditional expected number of failed group members over *every* logical
basic-event world matching a row, using the whole AND/OR DAG -- including
events and gates that never reach the root; missing nodes are unobserved,
never healthy.  The M step replaces every group simultaneously from one old
parameter vector.  This module performs exactly that documented iteration; it
neither identifies a unique true parameter nor searches for a global optimum.
"""
import math
from array import array

from ..common import ident, number, require, rows
from ..model import parse_tree

MAX_GROUPS = 16
MAX_OBSERVATIONS = 128
MAX_EVIDENCE = 116
MAX_ITERATIONS = 100
MAX_PSEUDO = 10 ** 6
MAX_COUNT = 10 ** 6
MIN_TOLERANCE = 1e-12
MAX_TOLERANCE = 1e-3


def _utf8(text):
    return text.encode('utf-8')


# ---------------------------------------------------------------------------
# Strict input parsing
# ---------------------------------------------------------------------------

def parse_calibration_input(data):
    """Validate the {tree,groups,observations,max_iterations,tolerance}
    envelope and return the normalized computation plan.

    The tree is handed to the original parse_tree contract unchanged, so the
    1..16 event cap, identifier, reference, gate-input and whole-DAG
    acyclicity rules are reused verbatim.
    """
    require(data, 'tree groups observations max_iterations tolerance')
    if not isinstance(data['tree'], dict):
        raise ValueError('tree must be an object')
    events, gates, order, root = parse_tree(data['tree'])
    known = set(events) | set(gates)

    groups = _parse_groups(rows(data['groups'], MAX_GROUPS), events)
    fitted = {name for group in groups for name in group['events']}
    observations = _parse_observations(
        rows(data['observations'], MAX_OBSERVATIONS), known)
    max_iterations = number(data['max_iterations'], 1, MAX_ITERATIONS)
    tolerance = _tolerance(data['tolerance'])

    return {'events': events, 'gates': gates, 'order': order, 'root': root,
            'groups': groups, 'fitted': fitted,
            'observations': observations,
            'max_iterations': max_iterations, 'tolerance': tolerance}


def _parse_groups(group_rows, events):
    if not group_rows:
        raise ValueError('groups must list 1..%d parameter groups' % MAX_GROUPS)
    groups = []
    seen_ids = set()
    covered = set()
    for item in group_rows:
        require(item, 'id events alpha beta')
        group_id = ident(item['id'])
        if group_id in seen_ids:
            raise ValueError('duplicate group id: ' + group_id)
        alpha = number(item['alpha'], 1, MAX_PSEUDO)
        beta = number(item['beta'], 1, MAX_PSEUDO)
        event_rows = rows(item['events'], len(events))
        if not event_rows:
            raise ValueError('group must contain at least one basic event')
        member_ids = [ident(name) for name in event_rows]
        if len(set(member_ids)) != len(member_ids):
            raise ValueError('group events must be distinct')
        shared = None
        for name in member_ids:
            if name not in events:
                raise ValueError('group member is not a declared basic event: ' + name)
            if name in covered:
                raise ValueError('basic event belongs to more than one group: ' + name)
            probability = events[name]
            if probability <= 0 or probability >= 1:
                raise ValueError('group members must start strictly inside (0,1): ' + name)
            # Exact rational comparison: binary floats never enter this check.
            if shared is None:
                shared = probability
            elif probability != shared:
                raise ValueError('group members must share one equal initial probability')
            covered.add(name)
        seen_ids.add(group_id)
        groups.append({'id': group_id, 'events': tuple(member_ids),
                       'alpha': alpha, 'beta': beta,
                       'initial': shared, 'm': len(member_ids)})
    return groups


def _parse_observations(observation_rows, known):
    if not observation_rows:
        raise ValueError('observations must list 1..%d trials' % MAX_OBSERVATIONS)
    observations = []
    seen_ids = set()
    for item in observation_rows:
        require(item, 'id count evidence')
        observation_id = ident(item['id'])
        if observation_id in seen_ids:
            raise ValueError('duplicate observation id: ' + observation_id)
        count = number(item['count'], 1, MAX_COUNT)
        evidence_rows = rows(item['evidence'], MAX_EVIDENCE)
        evidence = []
        seen_nodes = set()
        for reading in evidence_rows:
            require(reading, 'node failed')
            node = ident(reading['node'])
            if node not in known:
                raise ValueError('evidence node is not a declared event or gate: ' + node)
            if node in seen_nodes:
                raise ValueError('duplicate evidence node in one observation: ' + node)
            failed = reading['failed']
            if type(failed) is not bool:
                raise ValueError('evidence failed must be a JSON boolean')
            seen_nodes.add(node)
            evidence.append((node, failed))
        seen_ids.add(observation_id)
        observations.append({'id': observation_id, 'count': count,
                             'evidence': tuple(evidence)})
    return observations


def _tolerance(value):
    # bool is a subclass of int; reject it explicitly alongside non-numbers.
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError('tolerance must be a JSON number')
    result = float(value)
    if not math.isfinite(result) or not MIN_TOLERANCE <= result <= MAX_TOLERANCE:
        raise ValueError('tolerance must be a finite number in [1e-12,1e-3]')
    return result


# ---------------------------------------------------------------------------
# Logical worlds: one boolean vector over every declared basic event
# ---------------------------------------------------------------------------

class _WorldTable:
    """All 2**n basic-event worlds, with precomputed support memberships.

    World index ``w`` follows ``itertools.product((False, True), repeat=n)``
    over UTF-8-sorted event names, so the last name flips fastest.  Every
    declared gate is evaluated along the parse_tree DAG order on the same
    event vector; shared sub-gates share one state mask.

    The set of worlds matching a reading depends only on the DAG and the
    evidence, never on theta, so the per-support world lists are built once.
    Per-event intersections are precomputed too: the conditional expectation
    for a group is the sum of its member events' posterior failure
    expectations, which is exactly E[C_g | evidence].
    """

    def __init__(self, spec):
        events = spec['events']
        self.names = sorted(events, key=_utf8)
        n = len(self.names)
        self.n = n
        world_count = 1 << n
        self.world_count = world_count
        # One bit per world: the all-worlds mask spans 2**n bits, not n bits.
        full = (1 << world_count) - 1

        def periodic_true_mask(step):
            mask = 0
            start = step
            while start < world_count:
                mask |= ((1 << step) - 1) << start
                start += step << 1
            return mask

        node_masks = {}
        for position, name in enumerate(self.names):
            node_masks[name] = periodic_true_mask(1 << (n - 1 - position))
        for gate_id in spec['order']:
            gate = spec['gates'][gate_id]
            operands = [node_masks[name] for name in gate['inputs']]
            if gate['op'] == 'AND':
                value = full
                for operand in operands:
                    value &= operand
            else:
                value = 0
                for operand in operands:
                    value |= operand
            node_masks[gate_id] = value

        # Rows with the same evidence constrain the same world set.  Counts are
        # aggregated by support so splitting one evidence block into several
        # ids cannot change any float; supports are visited in ascending
        # integer order, independent of input row order.
        row_support = []
        support_count = {}
        for observation in spec['observations']:
            support = full
            for node, failed in observation['evidence']:
                mask = node_masks[node]
                support &= mask if failed else full ^ mask
            row_support.append((observation['id'], observation['count'], support))
            support_count[support] = support_count.get(support, 0) \
                + observation['count']
        self.row_support = row_support
        self.supports = sorted(support_count)
        self.support_count = support_count
        self.total_count = sum(count for _row_id, count, _support in row_support)

        groups = []
        for group in sorted(spec['groups'], key=lambda g: _utf8(g['id'])):
            event_positions = tuple(
                self.names.index(name)
                for name in sorted(group['events'], key=_utf8))
            groups.append({'id': group['id'],
                           'events': tuple(sorted(group['events'], key=_utf8)),
                           'alpha': group['alpha'], 'beta': group['beta'],
                           'm': group['m'], 'positions': event_positions,
                           'initial': float(group['initial'])})
        self.groups = groups
        frozen = [name for name in self.names if name not in spec['fitted']]
        self.frozen_probability = {name: float(spec['events'][name])
                                   for name in frozen}

        # support_worlds[s]: ascending world indices matching support s.
        # event_worlds[s][e]: the subset where fitted event e is failed.
        # Frozen events never need a posterior expectation, so only fitted
        # event intersections are materialized.
        fitted_positions = sorted(
            {position for group in groups for position in group['positions']})
        self.fitted_positions = fitted_positions
        # Per-world-index bit that records an event's failure: the first
        # product() variable (position 0) occupies the most significant bit.
        self.support_worlds = []
        self.event_worlds = []
        for support in self.supports:
            worlds = []
            remaining = support
            while remaining:
                low = remaining & -remaining
                worlds.append(low.bit_length() - 1)
                remaining ^= low
            self.support_worlds.append(array('I', worlds))
            # One intersection per fitted event, derived from the same world
            # list so every subset is an exact mask intersection.
            intersections = {}
            for position in fitted_positions:
                bit = 1 << (n - 1 - position)
                intersections[position] = array(
                    'I', (w for w in worlds if w & bit))
            self.event_worlds.append(intersections)


def _world_masses(table, theta_by_name):
    """Independent Bernoulli product mass of every logical world.

    Frozen events keep their tree prior (0 or 1 included); fitted events use
    the current shared group parameters.  Zero-mass worlds are retained as
    logical citizens.  Each event's healthy/failed blocks are contiguous
    runs, so the product is applied with bounded slice assignments.
    """
    n = table.n
    masses = [1.0] * table.world_count
    for position, name in enumerate(table.names):
        probability = theta_by_name[name]
        healthy = 1.0 - probability
        block = 1 << (n - 1 - position)
        index = 0
        while index < table.world_count:
            for offset in range(index, index + block):
                masses[offset] *= healthy
            index += block
            for offset in range(index, index + block):
                masses[offset] *= probability
            index += block
    return masses


def _e_step(table, masses):
    """One E step over every unique evidence support.

    Returns ``(likelihoods, event_expectation)``: ``likelihoods[s]`` is
    L_i(theta) for support ``s`` and ``event_expectation[s][e]`` is the exact
    posterior failure expectation of fitted event position ``e`` given the
    support.  Every value is a sum over all matching worlds -- no sampling, no
    hard attribution, no dropped zero-mass worlds.  The terms are positive and
    number at most 2**16, so a sequential binary64 sum stays within roughly
    2**16 * machine epsilon (~3e-12 relative, measured ~1e-14 here) of the
    exact total, comfortably inside the 1e-10 oracle tolerance; no clamping or
    epsilon floor is applied.  Group expectations follow by summing member
    events.
    """
    likelihoods = {}
    event_expectation = {}
    for s_index, support in enumerate(table.supports):
        worlds = table.support_worlds[s_index]
        likelihood = 0.0
        for index in range(len(worlds)):
            likelihood += masses[worlds[index]]
        likelihoods[support] = likelihood
        if likelihood <= 0.0:
            event_expectation[support] = {
                position: 0.0 for position in table.fitted_positions}
            continue
        expectations = {}
        intersections = table.event_worlds[s_index]
        for position in table.fitted_positions:
            subset = intersections[position]
            numerator = 0.0
            for index in range(len(subset)):
                numerator += masses[subset[index]]
            expectations[position] = numerator / likelihood
        event_expectation[support] = expectations
    return likelihoods, event_expectation


# ---------------------------------------------------------------------------
# EM iteration
# ---------------------------------------------------------------------------

def _parameter_vector(table, group_theta):
    return [{'id': group['id'], 'probability': group_theta[g]}
            for g, group in enumerate(table.groups)]


def _theta_by_name(table, group_theta):
    theta = dict(table.frozen_probability)
    for g, group in enumerate(table.groups):
        value = group_theta[g]
        for name in group['events']:
            theta[name] = value
    return theta


def _objective(table, group_theta, support_likelihood):
    """ell(theta) plus the Beta pseudo-count terms J(theta).

    Rows sharing one support pool only their counts; the weighted log mass is
    accumulated over canonical supports with a stable sum.
    """
    ell = math.fsum(
        table.support_count[support] * math.log(support_likelihood[support])
        for support in table.supports)
    penalty = 0.0
    for g, group in enumerate(table.groups):
        theta = group_theta[g]
        # Contract bounds keep theta strictly inside (0,1); log1p still
        # evaluates the healthy branch without a 1-theta rounding issue.
        penalty += group['alpha'] * math.log(theta)
        penalty += group['beta'] * math.log1p(-theta)
    return ell, ell + penalty


def _trace_point(iteration, table, group_theta, support_likelihood, max_delta):
    ell, objective = _objective(table, group_theta, support_likelihood)
    return {'iteration': iteration,
            'parameters': _parameter_vector(table, group_theta),
            'log_likelihood': ell, 'objective': objective,
            'max_delta': max_delta}


def calibrate(data):
    """Validate one v5 request and run the documented fixed-point EM.

    Returns the result body without the schema/SHA envelope.  All semantic
    validation finishes before any likelihood is evaluated, and a row with
    zero initial mass ends the run normally as ``unsupported_data`` without
    any parameter update.
    """
    spec = parse_calibration_input(data)
    table = _WorldTable(spec)
    group_theta = [group['initial'] for group in table.groups]

    initial_masses = _world_masses(table, _theta_by_name(table, group_theta))
    initial_likelihood, initial_expectation = _e_step(table, initial_masses)

    # Every zero-mass row is reported; the initial-point check happens before
    # any parameter update may start.
    unsupported = sorted(
        (row_id for row_id, _count, support in table.row_support
         if initial_likelihood[support] == 0.0), key=_utf8)
    if unsupported:
        return {'status': 'unsupported_data', 'iterations': 0,
                'groups': None, 'events': None, 'observations': [],
                'trace': [], 'unsupported_observations': unsupported}

    trace = [_trace_point(0, table, group_theta, initial_likelihood, None)]
    iterations = 0
    status = 'max_iterations'
    # The E step at the current parameter vector is shared by the recorded
    # trace point and the next M step, so each distinct theta is evaluated
    # exactly once.
    masses = initial_masses
    support_likelihood = initial_likelihood
    event_expectation = initial_expectation
    for _ in range(spec['max_iterations']):
        # Sufficient statistics T_g.  C_g(w) is the number of failed members,
        # so its conditional expectation is the members' posterior sum.
        # Canonical support and event orders make every sum independent of
        # input row order and of how evidence is split across observation ids.
        sufficient = []
        for group in table.groups:
            statistic = 0.0
            for support in table.supports:
                member_expectation = 0.0
                for position in group['positions']:
                    member_expectation += event_expectation[support][position]
                statistic += table.support_count[support] * member_expectation
            sufficient.append(statistic)

        # M step: every numerator is finished before any group is replaced.
        updated = []
        for g, group in enumerate(table.groups):
            denominator = (group['alpha'] + group['beta']
                           + group['m'] * table.total_count)
            updated.append((group['alpha'] + sufficient[g]) / denominator)
        max_delta = max(abs(updated[g] - group_theta[g])
                        for g in range(len(table.groups)))
        group_theta = updated
        iterations += 1

        # Re-evaluate at the new parameter vector before recording; the same
        # expectations feed the next M step.
        masses = _world_masses(table, _theta_by_name(table, group_theta))
        support_likelihood, event_expectation = _e_step(table, masses)
        trace.append(_trace_point(iterations, table, group_theta,
                                  support_likelihood, max_delta))
        # Only the parameter distance stops the loop, and this takes
        # precedence over reaching max_iterations in this same round.
        if max_delta <= spec['tolerance']:
            status = 'converged'
            break

    final_theta = _theta_by_name(table, group_theta)
    group_report = [
        {'id': group['id'], 'events': list(group['events']),
         'probability': group_theta[g]}
        for g, group in enumerate(table.groups)]
    event_report = [{'id': name, 'probability': final_theta[name]}
                    for name in table.names]
    observation_report = [
        {'id': row_id, 'probability': support_likelihood[support]}
        for row_id, _count, support in
        sorted(table.row_support, key=lambda row: _utf8(row[0]))]
    return {'status': status, 'iterations': iterations,
            'groups': group_report, 'events': event_report,
            'observations': observation_report, 'trace': trace,
            'unsupported_observations': []}
