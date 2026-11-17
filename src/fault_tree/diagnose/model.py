"""Exact optimal adaptive diagnosis over the logical worlds of a fault tree.

A "world" is one full true/false assignment of the declared basic events; gate
values follow the existing AND/OR DAG contract. Every paid test reads the exact
boolean of a node in the *same* static world, so the conditional relevance of
tests is derived by splitting the joint world set rather than by multiplying
marginal node probabilities. Worlds whose prior mass is zero remain full
logical citizens: they keep branches open and can force extra tests.

The planner solves one subproblem at a time. A subproblem is the triple
(observed node readings, used test_id set, remaining budget), represented here
by (support mask of still-feasible worlds, used-test bit mask, remaining
budget). On positive-mass subproblems it minimizes exact conditional expected
follow-up cost; on logically non-empty zero-mass subproblems it minimizes the
worst follow-up cost. Ties resolve by the UTF-8 byte order of test_id.
"""
from fractions import Fraction

from ..common import ident, number, require, rows
from ..model import parse_tree

MAX_EVIDENCE = 116
MAX_TESTS = 10
MAX_COST = 10 ** 9


def parse_diagnosis_input(data):
    """Strictly validate the {tree, evidence, tests, budget} envelope.

    The tree is handed to the existing parse_tree contract unchanged. Evidence
    nodes must be declared events or gates with JSON-boolean readings and no
    duplicate node; tests reuse the ident rules, keep unique ids, and carry
    integer costs in 1..10**9. Budget is an integer in 0..10**9.
    """
    require(data, 'tree evidence tests budget')
    tree = data['tree']
    if not isinstance(tree, dict):
        raise ValueError('tree must be an object')
    events, gates, order, root = parse_tree(tree)
    known = set(events) | set(gates)

    evidence_rows = rows(data['evidence'], MAX_EVIDENCE)
    evidence = []
    seen_nodes = set()
    for item in evidence_rows:
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

    test_rows = rows(data['tests'], MAX_TESTS)
    tests = []
    seen_ids = set()
    for item in test_rows:
        require(item, 'id node cost')
        test_id = ident(item['id'])
        if test_id in seen_ids:
            raise ValueError('duplicate test id: ' + test_id)
        node = ident(item['node'])
        if node not in known:
            raise ValueError('test node is not a declared event or gate: ' + node)
        cost = number(item['cost'], 1, MAX_COST)
        seen_ids.add(test_id)
        tests.append({'id': test_id, 'node': node, 'cost': cost})

    budget = number(data['budget'], 0, MAX_COST)
    return {'events': events, 'gates': gates, 'order': order, 'root': root,
            'evidence': evidence, 'tests': tests, 'budget': budget}


def _fraction_text(value):
    """Reduced Fraction wire text: integers render without a denominator."""
    if value.denominator == 1:
        return str(value.numerator)
    return str(value.numerator) + '/' + str(value.denominator)


class Planner:
    """Memoized exact solver over the feasible-world bit masks."""

    def __init__(self, spec):
        self.events = spec['events']
        self.gates = spec['gates']
        self.order = spec['order']
        self.root = spec['root']
        self.evidence = spec['evidence']
        # Canonical test order for deterministic exploration; tie-breaking
        # itself compares UTF-8 bytes explicitly when values coincide.
        self.tests = sorted(spec['tests'], key=lambda t: t['id'].encode('utf-8'))
        self.budget = spec['budget']
        self.names = sorted(self.events)
        self._build_worlds()
        self._mass_cache = {}
        self._solve_cache = {}
        self.infeasible = object()

    def _build_worlds(self):
        """Enumerate all 2**n logical worlds and apply the initial evidence.

        Node values are stored as bit masks over the world index instead of
        per-world dicts: every AND/OR gate is then one mask operation, and the
        initial evidence is an intersection of masks rather than a per-world
        re-evaluation of the whole DAG.
        """
        n = len(self.names)
        world_count = 1 << n
        # World indices span 0..world_count-1, so the all-worlds mask needs
        # world_count bits, not n bits.
        full_world = (1 << world_count) - 1

        def periodic_true_mask(step):
            """World-index bits where the product() bit of period 2*step is 1.

            With product((False, True), repeat=n) the last variable flips
            fastest; an event at distance step from the end is false on
            [2k*step, (2k+1)*step) and true on [(2k+1)*step, (2k+2)*step).
            """
            mask = 0
            start = step
            while start < world_count:
                mask |= ((1 << step) - 1) << start
                start += step << 1
            return mask

        node_masks = {}
        for position, name in enumerate(self.names):
            node_masks[name] = periodic_true_mask(1 << (n - 1 - position))
        for gid in self.order:
            gate = self.gates[gid]
            operands = [node_masks[name] for name in gate['inputs']]
            if gate['op'] == 'AND':
                value = full_world
                for operand in operands:
                    value &= operand
            else:
                value = 0
                for operand in operands:
                    value |= operand
            node_masks[gid] = value

        support_mask = full_world
        for node, failed in self.evidence:
            support_mask &= node_masks[node] if failed else full_world ^ node_masks[node]

        # Compress surviving worlds into consecutive support indices 0..size-1.
        support_indices = []
        remaining = support_mask
        while remaining:
            low = remaining & -remaining
            support_indices.append(low.bit_length() - 1)
            remaining ^= low
        self.size = len(support_indices)
        full = (1 << self.size) - 1
        self.full_mask = full
        # Mapping from an original world-index bit to its compressed support bit.
        compressed_bit = {}
        self.root_true_mask = 0
        root_world_mask = node_masks[self.root]
        for support_index, world_index in enumerate(support_indices):
            world_bit = 1 << world_index
            compressed_bit[world_bit] = 1 << support_index
            # Planning reads node values only through the integer node masks,
            # so no per-world value dictionaries are retained.
            if root_world_mask & world_bit:
                self.root_true_mask |= 1 << support_index
        self.root_false_mask = full ^ self.root_true_mask
        # Independent prior mass of each surviving world. We keep the Fraction
        # weights for exact reporting, and derive a shared integer unit scale
        # for the all-integer subset sums used during planning.
        event_factors = []
        for name in self.names:
            probability = self.events[name]
            event_factors.append((probability, 1 - probability))
        self.weights = []
        for world_index in support_indices:
            weight = Fraction(1)
            for position, (fail_probability, healthy_probability) in enumerate(event_factors):
                bit = (world_index >> (n - 1 - position)) & 1
                weight *= fail_probability if bit else healthy_probability
            self.weights.append(weight)
        # One common denominator for every weight; products stay exact because
        # each event probability already uses a divisor of this product.
        self.common_denominator = 1
        for probability in self.events.values():
            self.common_denominator *= probability.denominator
        self.weight_units = [int(weight * self.common_denominator) for weight in self.weights]
        self.zero_mass_mask = 0
        for index, units in enumerate(self.weight_units):
            if units == 0:
                self.zero_mass_mask |= 1 << index
        # Per-test masks over the surviving support worlds. Compress the
        # original-world node mask by mapping each surviving bit.
        self.test_true = []
        self.test_false = []
        for test in self.tests:
            true_mask = 0
            node_world_mask = node_masks[test['node']] & support_mask
            leftover = node_world_mask
            while leftover:
                low = leftover & -leftover
                true_mask |= compressed_bit[low]
                leftover ^= low
            self.test_true.append(true_mask)
            self.test_false.append(full ^ true_mask)

    def mass_int(self, mask):
        """Sum of integer weight units of the worlds in a mask (memoized)."""
        cached = self._mass_cache.get(mask)
        if cached is not None:
            return cached
        total = 0
        remaining = mask
        units = self.weight_units
        while remaining:
            low = remaining & -remaining
            total += units[low.bit_length() - 1]
            remaining ^= low
        self._mass_cache[mask] = total
        return total

    def mass(self, mask):
        """Total prior mass of the worlds in a mask as a Fraction."""
        return Fraction(self.mass_int(mask), self.common_denominator)

    def branch_mass_ints(self, parent_mask, true_mask, false_mask):
        """Integer unit masses of the two test-split branches.

        The branches partition the parent, so only the smaller branch is summed
        from scratch; the other follows by integer subtraction.
        """
        parent_units = self.mass_int(parent_mask)
        if true_mask == 0:
            return 0, parent_units
        if false_mask == 0:
            return parent_units, 0
        if true_mask.bit_count() <= false_mask.bit_count():
            true_units = self.mass_int(true_mask)
            return true_units, parent_units - true_units
        false_units = self.mass_int(false_mask)
        return parent_units - false_units, false_units

    def solve(self, support_mask, used_mask, remaining_budget):
        """Return (policy node, scaled expected follow-up units, worst cost).

        Expectations are carried as integer "scaled units":
        scaled_expected = conditional_expected * mass_int(support), so all
        candidate values share one implicit denominator and compare exactly
        without Fraction arithmetic. On zero-mass subproblems the scaled value
        is zero, as such a subproblem optimizes worst-case cost only. The
        sentinel self.infeasible marks an unsolvable subproblem.
        """
        if support_mask == 0:
            raise AssertionError('empty subproblems are never solved')
        key = (support_mask, used_mask, remaining_budget)
        cached = self._solve_cache.get(key)
        if cached is not None:
            return cached
        # Root already constant across every logically feasible world: stop.
        if support_mask & self.root_true_mask == 0:
            result = ({'kind': 'leaf', 'failed': False}, 0, 0)
        elif support_mask & self.root_false_mask == 0:
            result = ({'kind': 'leaf', 'failed': True}, 0, 0)
        else:
            result = self._choose_action(support_mask, used_mask, remaining_budget)
        self._solve_cache[key] = result
        return result

    def _choose_action(self, support_mask, used_mask, remaining_budget):
        # A subproblem has positive mass iff it contains any positive-mass world.
        positive = bool(support_mask & ~self.zero_mass_mask)
        parent_units = self.mass_int(support_mask)
        chosen = None
        for index, test in enumerate(self.tests):
            bit = 1 << index
            if used_mask & bit or test['cost'] > remaining_budget:
                continue
            true_mask = support_mask & self.test_true[index]
            false_mask = support_mask & self.test_false[index]
            # A test that cannot distinguish any two feasible worlds is strictly
            # dominated by not taking it: it would add cost while one branch is
            # logically impossible, so it can never be part of an optimal policy.
            if true_mask == 0 or false_mask == 0:
                continue
            child_budget = remaining_budget - test['cost']
            children = {}
            feasible = True
            for reading, branch_mask in ((True, true_mask), (False, false_mask)):
                child = self.solve(branch_mask, used_mask | bit, child_budget)
                if child is self.infeasible:
                    feasible = False
                    break
                children[reading] = child
            if not feasible:
                continue
            worst = test['cost'] + max(children[reading][2] for reading in (True, False))
            if positive:
                true_units, false_units = self.branch_mass_ints(
                    support_mask, true_mask, false_mask)
                # E_parent = cost + sum_r (units_r / parent_units) * E_r
                # scaled by parent_units:
                #   scaled = cost*parent_units
                #            + sum_r units_r * scaled_r / units_r
                #          = cost*parent_units + sum_r scaled_r
                # where scaled_r already equals E_r * units_r.
                scaled = test['cost'] * parent_units
                for reading in (True, False):
                    scaled += children[reading][1]
                value = scaled
            else:
                # Zero-mass subproblem: normative objective is worst-case cost.
                value = worst
                scaled = 0
            candidate = (value, test['id'].encode('utf-8'), index, children, scaled, worst)
            if chosen is None or candidate[0] < chosen[0] or (
                    candidate[0] == chosen[0] and candidate[1] < chosen[1]):
                chosen = candidate
        if chosen is None:
            # solve() records the infeasible sentinel in its own cache.
            return self.infeasible
        _, _, index, children, scaled, worst = chosen
        policy = {
            'kind': 'test',
            'test_id': self.tests[index]['id'],
            'on_false': children[False][0],
            'on_true': children[True][0],
        }
        return policy, scaled, worst

    def run(self):
        """Produce the full v3 result body (without schema envelope)."""
        logical_worlds = self.size
        if logical_worlds == 0:
            return {'status': 'inconsistent_evidence', 'logical_worlds': 0,
                    'evidence_probability': None, 'root_failure_probability': None,
                    'expected_cost': None, 'worst_cost': None, 'policy': None}
        evidence_mass = self.mass(self.full_mask)
        if evidence_mass == 0:
            return {'status': 'zero_mass_evidence', 'logical_worlds': logical_worlds,
                    'evidence_probability': _fraction_text(evidence_mass),
                    'root_failure_probability': None, 'expected_cost': None,
                    'worst_cost': None, 'policy': None}
        root_failure_mass = self.mass(self.full_mask & self.root_true_mask)
        root_failure_probability = root_failure_mass / evidence_mass
        outcome = self.solve(self.full_mask, 0, self.budget)
        if outcome is self.infeasible:
            return {'status': 'infeasible', 'logical_worlds': logical_worlds,
                    'evidence_probability': _fraction_text(evidence_mass),
                    'root_failure_probability': _fraction_text(root_failure_probability),
                    'expected_cost': None, 'worst_cost': None, 'policy': None}
        policy, scaled_expected, worst_cost = outcome
        # The root scaled value is conditional expectation * evidence mass units;
        # divide by the evidence unit count to recover the exact expectation.
        evidence_units = self.mass_int(self.full_mask)
        expected_cost = Fraction(scaled_expected, evidence_units)
        return {'status': 'optimal', 'logical_worlds': logical_worlds,
                'evidence_probability': _fraction_text(evidence_mass),
                'root_failure_probability': _fraction_text(root_failure_probability),
                'expected_cost': _fraction_text(expected_cost),
                'worst_cost': worst_cost, 'policy': policy}


def diagnose(data):
    """Validate one diagnosis request and compute the normative report result."""
    spec = parse_diagnosis_input(data)
    return Planner(spec).run()
