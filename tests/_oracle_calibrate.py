"""Independent exact-rational EM oracle for cross-checking calibrate.

Deliberately written in a different style than the production engine: it
enumerates every basic-event world with Fraction arithmetic, evaluates the
DAG independently, and applies the documented EM formulas directly.  This is
a verification helper, never imported by the package.
"""
import json
import math
import sys
from fractions import Fraction
from itertools import product


def _gate_order(events, gates):
    pending = set(gates)
    resolved = set(events)
    order = []
    while pending:
        ready = sorted(g for g in pending
                       if set(gates[g]['inputs']) <= resolved)
        if not ready:
            raise ValueError('cycle')
        order.extend(ready)
        resolved.update(ready)
        pending.difference_update(ready)
    return order


def oracle(data, max_rounds=None):
    """Run the documented EM in exact Fraction arithmetic.

    Returns a dict with the full float trace (logs only become floats) and the
    final Fraction parameters, or the unsupported row list at the initial
    point.
    """
    tree = data['tree']
    events = {e['id']: Fraction(e['numerator'], e['denominator'])
              for e in tree['events']}
    gates = {g['id']: g for g in tree['gates']}
    order = _gate_order(events, gates)
    names = sorted(events)
    n = len(names)

    group_specs = sorted(data['groups'], key=lambda g: g['id'].encode('utf-8'))
    theta = [events[g['events'][0]] for g in group_specs]
    observations = data['observations']
    total_count = sum(o['count'] for o in observations)
    rounds = data['max_iterations'] if max_rounds is None else max_rounds

    def worlds_under(theta_vec):
        thet = dict(events)
        for value, spec in zip(theta_vec, group_specs):
            for nm in spec['events']:
                thet[nm] = value
        out = []
        for bits in product((False, True), repeat=n):
            vals = dict(zip(names, bits))
            for gid in order:
                gate = gates[gid]
                ops = [vals[x] for x in gate['inputs']]
                vals[gid] = all(ops) if gate['op'] == 'AND' else any(ops)
            weight = Fraction(1)
            for nm in names:
                p = thet[nm]
                weight *= p if vals[nm] else 1 - p
            out.append((vals, weight))
        return out

    def matches(vals, obs):
        return all(vals[reading['node']] == reading['failed']
                   for reading in obs['evidence'])

    history = []
    stop_round = rounds
    for round_index in range(rounds + 1):
        worlds = worlds_under(theta)
        likelihoods = {}
        for obs in observations:
            likelihoods[obs['id']] = sum(
                (weight for vals, weight in worlds if matches(vals, obs)),
                Fraction(0))
        if round_index == 0:
            unsupported = sorted(
                (obs['id'] for obs in observations
                 if likelihoods[obs['id']] == 0),
                key=lambda s: s.encode('utf-8'))
            if unsupported:
                return {'status': 'unsupported_data',
                        'unsupported_observations': unsupported}
        ell = sum(obs['count'] * math.log(float(likelihoods[obs['id']]))
                  for obs in observations)
        penalty = 0.0
        for value, spec in zip(theta, group_specs):
            penalty += spec['alpha'] * math.log(float(value))
            penalty += spec['beta'] * math.log1p(-float(value))
        point = {'iteration': round_index,
                 'theta': [float(value) for value in theta],
                 'ell': ell, 'J': ell + penalty,
                 'likelihoods': {key: float(value)
                                 for key, value in likelihoods.items()}}
        history.append(point)
        if round_index == rounds:
            break

        sufficient = []
        for spec in group_specs:
            statistic = Fraction(0)
            for obs in observations:
                matching = [(vals, weight) for vals, weight in worlds
                            if matches(vals, obs)]
                likelihood = sum((weight for _, weight in matching), Fraction(0))
                conditional = Fraction(0)
                for vals, weight in matching:
                    failed = sum(1 for nm in spec['events'] if vals[nm])
                    conditional += weight * failed / likelihood
                statistic += Fraction(obs['count']) * conditional
            sufficient.append(statistic)
        updated = []
        for spec, statistic in zip(group_specs, sufficient):
            denominator = (Fraction(spec['alpha']) + Fraction(spec['beta'])
                           + Fraction(len(spec['events']) * total_count))
            updated.append((Fraction(spec['alpha']) + statistic) / denominator)
        max_delta = max(abs(float(updated[i]) - float(theta[i]))
                        for i in range(len(theta)))
        point['max_delta'] = max_delta
        theta = updated
        if max_delta <= data['tolerance']:
            # Evaluate and record the final point before stopping.
            worlds = worlds_under(theta)
            likelihoods = {}
            for obs in observations:
                likelihoods[obs['id']] = sum(
                    (weight for vals, weight in worlds if matches(vals, obs)),
                    Fraction(0))
            ell = sum(obs['count'] * math.log(float(likelihoods[obs['id']]))
                      for obs in observations)
            penalty = 0.0
            for value, spec in zip(theta, group_specs):
                penalty += spec['alpha'] * math.log(float(value))
                penalty += spec['beta'] * math.log1p(-float(value))
            history.append({'iteration': round_index + 1,
                            'theta': [float(value) for value in theta],
                            'ell': ell, 'J': ell + penalty,
                            'max_delta': max_delta,
                            'likelihoods': {key: float(value)
                                            for key, value in likelihoods.items()}})
            stop_round = round_index + 1
            break
    return {'status': 'finished', 'history': history,
            'theta_fraction': theta, 'iterations': stop_round}


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8') as stream:
        data = json.load(stream)
    print(json.dumps(oracle(data), indent=1, default=str))
