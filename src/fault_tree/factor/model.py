"""Exact factor-graph joint conditional risk table (schema v4).

A request combines

* a wide-but-shallow fault tree (1..64 basic events, 0..100 AND/OR gates),
* cross-event potential factors over ordered basic-event scopes,
* node readings, a query node group and a full elimination order.

The unnormalized mass of one basic-event world ``x`` is the product of each
event's Bernoulli prior mass and every potential factor's value on ``x``; gate
values follow the same static AND/OR DAG.  ``Z`` sums that mass over every
world, ``N`` sums it over the worlds matching the readings, and ``Q(q)`` sums it
over the worlds matching both the readings and a query assignment ``q``.

``Z``/``N``/the joint table are obtained by exact bucket elimination in the
caller-supplied order (local factor multiply-and-sum), never by enumerating all
``2**n`` basic-event worlds.  A separate *structural* audit replays the same
order over factor scopes, before any evidence reduction or numeric
simplification, and bounds every initial, bucket, residual and final scope to at
most 12 variables.
"""
from fractions import Fraction
from itertools import product

from ..common import ident, number, require, rows

MAX_EVENTS = 64
MAX_GATES = 100
MAX_GATE_INPUTS = 5
MAX_FACTORS = 128
MAX_SCOPE = 8
MAX_EVIDENCE = 164
MAX_QUERY = 8
MAX_SCOPE_VARIABLES = 12
MAX_RATIO = 10 ** 9


def _utf8(text):
    return text.encode('utf-8')


def _canonical(names):
    """Scope canonical form: unique IDs in UTF-8 byte order."""
    return tuple(sorted(set(names), key=_utf8))


def _fraction_text(value):
    """Reduced Fraction wire text: integers render without a denominator."""
    if value.denominator == 1:
        return str(value.numerator)
    return str(value.numerator) + '/' + str(value.denominator)


# ---------------------------------------------------------------------------
# Strict input parsing
# ---------------------------------------------------------------------------

def parse_wide_tree(data):
    """Parse the 1..64-event tree contract; all other rules match the v1 tree.

    The old ``fault-tree`` parser keeps its 16-event acceptance cap: this
    widened parser is reachable only through the v4 envelope.
    """
    require(data, 'events gates root')
    events = {}
    event_rows = rows(data['events'], MAX_EVENTS)
    if not event_rows:
        raise ValueError('tree must declare 1..%d basic events' % MAX_EVENTS)
    for item in event_rows:
        require(item, 'id numerator denominator')
        event_id = ident(item['id'])
        numerator = number(item['numerator'], 0, MAX_RATIO)
        denominator = number(item['denominator'], 1, MAX_RATIO)
        if event_id in events or numerator > denominator:
            raise ValueError('duplicate event or invalid probability')
        events[event_id] = Fraction(numerator, denominator)
    gates = {}
    for item in rows(data['gates'], MAX_GATES):
        require(item, 'id op inputs')
        gate_id = ident(item['id'])
        op = ident(item['op'])
        inputs = [ident(x) for x in rows(item['inputs'], MAX_GATE_INPUTS)]
        if gate_id in events or gate_id in gates or op not in ('AND', 'OR') \
                or not inputs or len(inputs) > MAX_GATE_INPUTS \
                or len(set(inputs)) != len(inputs):
            raise ValueError('invalid gate')
        gates[gate_id] = {'op': op, 'inputs': inputs}
    root = ident(data['root'])
    known = set(events) | set(gates)
    if root not in known or any(x not in known for g in gates.values() for x in g['inputs']):
        raise ValueError('unknown node')
    order = []
    pending = set(gates)
    resolved = set(events)
    while pending:
        ready = sorted(g for g in pending if set(gates[g]['inputs']) <= resolved)
        if not ready:
            raise ValueError('cyclic gates')
        order.extend(ready)
        resolved.update(ready)
        pending.difference_update(ready)
    return events, gates, order, root


def _parse_factor(item, event_ids, seen_ids):
    require(item, 'id scope values')
    factor_id = ident(item['id'])
    if factor_id in seen_ids:
        raise ValueError('duplicate factor id: ' + factor_id)
    seen_ids.add(factor_id)
    scope_rows = rows(item['scope'], MAX_SCOPE)
    if not scope_rows:
        raise ValueError('factor scope must list 1..%d basic events' % MAX_SCOPE)
    scope = [ident(name) for name in scope_rows]
    if len(set(scope)) != len(scope):
        raise ValueError('factor scope must be distinct basic events')
    for name in scope:
        if name not in event_ids:
            raise ValueError('factor scope entry is not a declared basic event: ' + name)
    value_rows = item['values']
    if not isinstance(value_rows, list) or len(value_rows) != (1 << len(scope)):
        raise ValueError('factor values must have exactly 2**len(scope) rows')
    values = []
    for row in value_rows:
        require(row, 'numerator denominator')
        numerator = number(row['numerator'], 0, MAX_RATIO)
        denominator = number(row['denominator'], 1, MAX_RATIO)
        values.append(Fraction(numerator, denominator))
    return {'id': factor_id, 'scope': tuple(scope), 'values': tuple(values)}


def parse_request(data):
    """Strictly validate the {tree,factors,evidence,query,elimination_order}
    envelope and return the normalized computation plan."""
    require(data, 'tree factors evidence query elimination_order')
    if not isinstance(data['tree'], dict):
        raise ValueError('tree must be an object')
    events, gates, order, root = parse_wide_tree(data['tree'])
    known = set(events) | set(gates)

    seen_ids = set()
    factors = [_parse_factor(item, set(events), seen_ids)
               for item in rows(data['factors'], MAX_FACTORS)]

    evidence = []
    seen_nodes = set()
    for item in rows(data['evidence'], MAX_EVIDENCE):
        require(item, 'node failed')
        node = ident(item['node'])
        if node not in known:
            raise ValueError('evidence node is not a declared event or gate: ' + node)
        if node in seen_nodes:
            raise ValueError('duplicate evidence node: ' + node)
        failed = item['failed']
        if type(failed) is not bool:
            raise ValueError('evidence failed must be a JSON boolean')
        seen_nodes.add(node)
        evidence.append((node, failed))

    query_rows = rows(data['query'], MAX_QUERY)
    if not query_rows:
        raise ValueError('query must list 1..%d declared nodes' % MAX_QUERY)
    query = [ident(name) for name in query_rows]
    if len(set(query)) != len(query):
        raise ValueError('query must list distinct nodes')
    for name in query:
        if name not in known:
            raise ValueError('query node is not declared: ' + name)

    elimination_rows = data['elimination_order']
    if not isinstance(elimination_rows, list) or len(elimination_rows) > len(known):
        raise ValueError('elimination_order must be a bounded array of nodes')
    elimination_order = [ident(name) for name in elimination_rows]
    if len(set(elimination_order)) != len(elimination_order):
        raise ValueError('elimination_order must not repeat a variable')
    for name in elimination_order:
        if name not in known:
            raise ValueError('elimination_order entry is not declared: ' + name)
    if set(elimination_order) != known - set(query):
        raise ValueError('elimination_order must list every non-query event and gate exactly once')

    return {'events': events, 'gates': gates, 'order': order, 'root': root,
            'factors': factors, 'evidence': evidence, 'query': query,
            'elimination_order': elimination_order}


# ---------------------------------------------------------------------------
# Structural audit: scopes only, independent of evidence and numeric values
# ---------------------------------------------------------------------------

def _initial_scopes(spec):
    """Every audit factor scope: event priors, potential scopes, gate deltas."""
    scopes = [(name,) for name in spec['events']]
    scopes.extend(_canonical(factor['scope']) for factor in spec['factors'])
    for gate_id, gate in spec['gates'].items():
        scopes.append(_canonical((gate_id,) + tuple(gate['inputs'])))
    return scopes


def structural_audit(spec):
    """Replay the elimination order over scopes, before any simplification.

    The bucket of a variable is the union of the scopes of every current factor
    that contains it; those factors are replaced by one residual factor holding
    the bucket scope minus the variable.  An empty residual scope is kept as a
    scalar factor.  Scope widths are never shrunk by evidence, zero potentials
    or constant factors.
    """
    scopes = _initial_scopes(spec)
    peak = max((len(scope) for scope in scopes), default=0)
    steps = []
    for variable in spec['elimination_order']:
        bucket = [scope for scope in scopes if variable in scope]
        if not bucket:
            raise ValueError('elimination variable has no factor: ' + variable)
        bucket_scope = _canonical(name for scope in bucket for name in scope)
        residual_scope = tuple(name for name in bucket_scope if name != variable)
        scopes = [scope for scope in scopes if variable not in scope]
        scopes.append(residual_scope)
        peak = max(peak, len(bucket_scope), len(residual_scope))
        steps.append({'variable': variable,
                      'bucket_scope': list(bucket_scope),
                      'residual_scope': list(residual_scope)})
    final_scope = _canonical(name for scope in scopes for name in scope)
    peak = max(peak, len(final_scope))
    if peak > MAX_SCOPE_VARIABLES:
        raise ValueError('scope width %d exceeds the %d-variable limit'
                         % (peak, MAX_SCOPE_VARIABLES))
    return {'steps': steps, 'final_scope': list(final_scope), 'peak_variables': peak}


# ---------------------------------------------------------------------------
# Exact local factors and bucket elimination
# ---------------------------------------------------------------------------

class Factor:
    """A factor over UTF-8-sorted variables, rows in False-before-True order.

    Row index is the binary number whose most significant bit is the first
    variable's value, so False before True in lexicographic order.
    """

    __slots__ = ('variables', 'values')

    def __init__(self, variables, values):
        self.variables = variables
        self.values = values

    def __contains__(self, variable):
        return variable in self.variables


def _multiply(left, right):
    """Exact factor product; tables are small (at most 2**12 rows)."""
    if not left.variables:
        scalar = left.values[0]
        return Factor(right.variables, [scalar * value for value in right.values])
    if not right.variables:
        scalar = right.values[0]
        return Factor(left.variables, [scalar * value for value in left.values])
    if left.variables == right.variables:
        return Factor(left.variables,
                      [a * b for a, b in zip(left.values, right.values)])
    union = _canonical(left.variables + right.variables)
    width = len(union)
    position = {name: index for index, name in enumerate(union)}
    pos_left = [position[name] for name in left.variables]
    pos_right = [position[name] for name in right.variables]
    rows_total = 1 << width
    left_values = left.values
    right_values = right.values
    values = [None] * rows_total
    # Walk the union table once, mapping each row back to both sub-tables.
    for row in range(rows_total):
        left_index = 0
        for pos in pos_left:
            left_index = (left_index << 1) | ((row >> (width - 1 - pos)) & 1)
        right_index = 0
        for pos in pos_right:
            right_index = (right_index << 1) | ((row >> (width - 1 - pos)) & 1)
        values[row] = left_values[left_index] * right_values[right_index]
    return Factor(union, values)


def _sum_out(factor, variable):
    """Sum a factor over one variable, preserving row order of the remainder."""
    variables = factor.variables
    position = variables.index(variable)
    width = len(variables)
    remaining = tuple(name for name in variables if name != variable)
    other_positions = [pos for pos in range(width) if pos != position]
    rows_total = 1 << len(remaining)
    values = [Fraction(0)] * rows_total
    for row, value in enumerate(factor.values):
        target = 0
        for pos in other_positions:
            target = (target << 1) | ((row >> (width - 1 - pos)) & 1)
        values[target] += value
    return Factor(remaining, values)


def _table_factor(scope, values):
    """Build a canonical factor from rows interpreted in the *given* scope
    order, False before True.  The scope order is the column coordinate: only
    the values move when the scope order changes."""
    width = len(scope)
    canonical = _canonical(scope)
    source_position = {name: index for index, name in enumerate(scope)}
    result = [None] * (1 << width)
    for source_row in range(1 << width):
        canonical_row = 0
        for name in canonical:
            bit = (source_row >> (width - 1 - source_position[name])) & 1
            canonical_row = (canonical_row << 1) | bit
        result[canonical_row] = values[source_row]
    return Factor(canonical, result)


def _gate_factor(gate_id, gate):
    inputs = tuple(gate['inputs'])
    variables = _canonical((gate_id,) + inputs)
    width = len(variables)
    values = []
    for row in range(1 << width):
        assignment = {}
        for index, name in enumerate(variables):
            assignment[name] = bool((row >> (width - 1 - index)) & 1)
        operands = [assignment[name] for name in inputs]
        expected = all(operands) if gate['op'] == 'AND' else any(operands)
        values.append(Fraction(1) if assignment[gate_id] == expected else Fraction(0))
    return Factor(variables, values)


def _build_numeric_factors(spec, with_evidence):
    factors = []
    for name, probability in spec['events'].items():
        factors.append(Factor((name,), [Fraction(1) - probability, probability]))
    for factor in spec['factors']:
        factors.append(_table_factor(factor['scope'], factor['values']))
    for gate_id, gate in spec['gates'].items():
        factors.append(_gate_factor(gate_id, gate))
    if with_evidence:
        for node, failed in spec['evidence']:
            factors.append(Factor((node,), [Fraction(0), Fraction(1)] if failed
                                  else [Fraction(1), Fraction(0)]))
    return factors


def _eliminate(factors, elimination_order):
    """Bucket-eliminate in the given order and return the final factor."""
    factors = list(factors)
    for variable in elimination_order:
        bucket = [factor for factor in factors if variable in factor]
        if not bucket:
            raise ValueError('elimination variable has no factor: ' + variable)
        product_ = bucket[0]
        for factor in bucket[1:]:
            product_ = _multiply(product_, factor)
        residual = _sum_out(product_, variable)
        factors = [factor for factor in factors if variable not in factor]
        factors.append(residual)
    final = factors[0]
    for factor in factors[1:]:
        final = _multiply(final, factor)
    return final


def _reorder_to_scope(factor, target):
    """Return the factor's rows in the requested variable order."""
    if factor.variables == target:
        return factor.values
    if set(factor.variables) != set(target):
        raise AssertionError('final factor scope does not match the query')
    width = len(target)
    values = factor.values
    target_position = {name: index for index, name in enumerate(target)}
    result = [None] * (1 << width)
    for row in range(1 << width):
        source = 0
        for name in factor.variables:
            source = (source << 1) | ((row >> (width - 1 - target_position[name])) & 1)
        result[row] = values[source]
    return result


def _joint_rows(values, variables):
    """Enumerate joint rows in False-before-True order over ``variables``."""
    joint = []
    for row, bits in enumerate(product((False, True), repeat=len(variables))):
        joint.append({'failed': list(bits),
                      'probability': _fraction_text(values[row])})
    return joint


def analyze(data):
    """Validate one v4 request and return the full result body."""
    spec = parse_request(data)
    audit = structural_audit(spec)
    variables = _canonical(spec['query'])

    # Z: prior/gate/factor model only (all worlds weigh in).
    total = _eliminate(_build_numeric_factors(spec, False), spec['elimination_order'])
    normalizer = sum(total.values, Fraction(0))
    if normalizer == 0:
        return {'status': 'zero_model', 'normalizer': _fraction_text(normalizer),
                'evidence_probability': None, 'variables': list(variables),
                'joint': [], 'audit': audit}

    # N and Q: the same model restricted by evidence.
    conditioned = _eliminate(_build_numeric_factors(spec, True),
                             spec['elimination_order'])
    table = _reorder_to_scope(conditioned, variables)
    evidence_mass = sum(table, Fraction(0))
    if evidence_mass == 0:
        return {'status': 'zero_evidence', 'normalizer': _fraction_text(normalizer),
                'evidence_probability': '0', 'variables': list(variables),
                'joint': [], 'audit': audit}

    normalized = [mass / evidence_mass for mass in table]
    return {'status': 'ok', 'normalizer': _fraction_text(normalizer),
            'evidence_probability': _fraction_text(evidence_mass / normalizer),
            'variables': list(variables), 'joint': _joint_rows(normalized, variables),
            'audit': audit}
