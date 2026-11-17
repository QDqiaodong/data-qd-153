import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OR_TREE = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                      {'id': 'B', 'numerator': 1, 'denominator': 2}],
           'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}], 'root': 'top'}
SHARED_TREE = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 10},
                          {'id': 'B', 'numerator': 1, 'denominator': 5},
                          {'id': 'C', 'numerator': 3, 'denominator': 10}],
               'gates': [{'id': 'AB', 'op': 'AND', 'inputs': ['A', 'B']},
                         {'id': 'AC', 'op': 'AND', 'inputs': ['A', 'C']},
                         {'id': 'top', 'op': 'OR', 'inputs': ['AB', 'AC']}], 'root': 'top'}
UNREF_TREE = {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                         {'id': 'B', 'numerator': 1, 'denominator': 2},
                         {'id': 'C', 'numerator': 1, 'denominator': 2}],
              'gates': [{'id': 'top', 'op': 'OR', 'inputs': ['A', 'B']}], 'root': 'top'}

def put(sample, revision, event, moment, failed):
    return {'sample_id': sample, 'revision': revision, 'op': 'put',
            'event_id': event, 'time': moment, 'failed': failed}

def withdraw(sample, revision):
    return {'sample_id': sample, 'revision': revision, 'op': 'withdraw'}

def batch(batch_id, *records):
    return {'batch_id': batch_id, 'records': list(records)}

class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.dataset = self.dir / 'ds'

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
        return path

    def tree(self, obj=OR_TREE):
        return self.write('tree.json', obj)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, '-m', 'fault_tree.history', *map(str, args)],
                              capture_output=True, text=True, env=env)

    def init(self, tree_path, horizon, code=0):
        result = self.cli('init', '--tree', tree_path, '--horizon', horizon, '--dataset', self.dataset)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def apply(self, source, code=0):
        result = self.cli('apply', '--dataset', self.dataset, '--source', source)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def query(self, day, name='query.json', code=0):
        out = self.dir / name
        result = self.cli('query', '--dataset', self.dataset, '--day', day, '--output', out)
        self.assertEqual(result.returncode, code, result.stderr)
        if code == 0:
            self.assertEqual(json.loads(result.stdout), json.loads(out.read_text()))
            return json.loads(out.read_text())
        return result

    def init_or(self, horizon=1000, tree=OR_TREE):
        self.init(self.tree(tree), horizon)

class Init(Base):
    def test_zero_state(self):
        tree_path = self.tree()
        result = self.init(tree_path, 200000)
        self.assertEqual(json.loads(result.stdout), {'generation': 0})
        import hashlib
        digest = hashlib.sha256(tree_path.read_bytes()).hexdigest()
        for day, (start, end) in enumerate([(0, 86400), (86400, 172800), (172800, 200000)]):
            report = self.query(day)
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(report['tree_sha256'], digest)
            self.assertEqual(report['root'], 'top')
            self.assertEqual(report['horizon'], 200000)
            self.assertEqual(report['generation'], 0)
            self.assertEqual(report['current_samples'], [])
            self.assertEqual(report['summary'], {'incident_count': 0, 'downtime_seconds': 0})
            self.assertEqual(report['day'], {'index': day, 'start': start, 'end': end,
                                             'downtime_seconds': 0, 'segments': []})
        self.assertEqual(self.query(3, code=2).returncode, 2)

    def test_existing_target_rejected(self):
        tree_path = self.tree()
        self.init(tree_path, 100)
        self.assertEqual(self.init(tree_path, 100, code=2).returncode, 2)
        other = self.write('file.json', OR_TREE)
        result = self.cli('init', '--tree', tree_path, '--horizon', 100, '--dataset', other)
        self.assertEqual(result.returncode, 2)

    def test_invalid_tree_leaves_nothing(self):
        bad = [dict(OR_TREE, gates=[{'id': 'top', 'op': 'OR', 'inputs': ['top']}]),
               dict(OR_TREE, root='missing'),
               dict(OR_TREE, gates=[{'id': 'top', 'op': 'OR', 'inputs': ['A', 'ghost']}]),
               {'events': [{'id': 'A', 'numerator': 3, 'denominator': 2}], 'gates': [], 'root': 'A'},
               {'events': [{'id': 'A', 'numerator': 1, 'denominator': 2},
                           {'id': 'A', 'numerator': 1, 'denominator': 3}], 'gates': [], 'root': 'A'},
               dict(OR_TREE, extra=1)]
        for index, broken in enumerate(bad):
            dataset = self.dir / ('ds%d' % index)
            result = self.cli('init', '--tree', self.write('bad%d.json' % index, broken),
                              '--horizon', 100, '--dataset', dataset)
            self.assertEqual(result.returncode, 2, broken)
            self.assertFalse(dataset.exists(), broken)

    def test_horizon_bounds(self):
        tree_path = self.tree()
        for bad in ['0', '7776001', '-5', '1.5', 'abc']:
            result = self.cli('init', '--tree', tree_path, '--horizon', bad, '--dataset', self.dir / 'ds-bad')
            self.assertEqual(result.returncode, 2, bad)
            self.assertFalse((self.dir / 'ds-bad').exists())
        self.init(tree_path, 1)
        self.assertEqual(self.query(0)['day'], {'index': 0, 'start': 0, 'end': 1,
                                                'downtime_seconds': 0, 'segments': []})
        big = self.dir / 'ds-big'
        result = self.cli('init', '--tree', tree_path, '--horizon', 7776000, '--dataset', big)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.dir / 'big.json'
        result = self.cli('query', '--dataset', big, '--day', 89, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text())['day']['end'], 7776000)
        result = self.cli('query', '--dataset', big, '--day', 90, '--output', out)
        self.assertEqual(result.returncode, 2)

    def test_tree_file_deleted_after_init(self):
        tree_path = self.tree()
        self.init(tree_path, 1000)
        tree_path.unlink()
        source = self.write('b1.json', batch('b1', put('s1', 1, 'A', 10, True)))
        self.apply(source)
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 990})
        source.unlink()
        self.assertEqual(self.query(0)['generation'], 1)

class Settlement(Base):
    def test_put_advances_and_query_reflects(self):
        self.init_or()
        result = self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        self.assertEqual(json.loads(result.stdout), {'batch_id': 'b1', 'generation': 1})
        report = self.query(0)
        self.assertEqual(report['generation'], 1)
        self.assertEqual(report['current_samples'],
                         [{'sample_id': 's1', 'revision': 1, 'op': 'put', 'event_id': 'A',
                           'time': 100, 'failed': True}])
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 900})
        self.assertEqual(report['day']['segments'], [{'incident': [100, 1000], 'start': 100, 'end': 1000}])

    def test_out_of_order_revisions(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 5, 'A', 100, True))))
        self.apply(self.write('b2.json', batch('b2', put('s1', 2, 'A', 50, True))))
        report = self.query(0)
        self.assertEqual(report['generation'], 2)
        self.assertEqual(report['current_samples'][0]['revision'], 5)
        self.assertEqual(report['day']['segments'], [{'incident': [100, 1000], 'start': 100, 'end': 1000}])
        # revision 2 is remembered: different content for it must fail
        self.apply(self.write('b3.json', batch('b3', put('s1', 2, 'A', 60, True))), code=2)
        self.assertEqual(self.query(0)['generation'], 2)

    def test_same_revision_different_content_rejected(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        self.apply(self.write('b2.json', batch('b2', put('s1', 1, 'A', 200, True))), code=2)
        report = self.query(0)
        self.assertEqual(report['generation'], 1)
        self.assertEqual(report['day']['segments'], [{'incident': [100, 1000], 'start': 100, 'end': 1000}])
        # the rejected batch registered nothing: its id stays usable
        result = self.apply(self.write('b3.json', batch('b2', put('s1', 1, 'A', 100, True))))
        self.assertEqual(json.loads(result.stdout), {'batch_id': 'b2', 'generation': 2})

    def test_withdraw_tombstone_blocks_old_put_revival(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 3, 'A', 100, True))))
        self.apply(self.write('b2.json', batch('b2', withdraw('s1', 5))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 0, 'downtime_seconds': 0})
        self.assertEqual(report['current_samples'],
                         [{'sample_id': 's1', 'revision': 5, 'op': 'withdraw',
                           'event_id': None, 'time': None, 'failed': None}])
        # resending the already known old put is accepted but cannot revive it
        self.apply(self.write('b3.json', batch('b3', put('s1', 3, 'A', 100, True))))
        # registering an unseen lower put cannot revive either
        self.apply(self.write('b4.json', batch('b4', put('s1', 2, 'A', 100, True))))
        report = self.query(0)
        self.assertEqual(report['generation'], 4)
        self.assertEqual(report['summary'], {'incident_count': 0, 'downtime_seconds': 0})
        self.assertEqual(report['current_samples'][0]['revision'], 5)
        # the old revisions stay remembered with their exact content
        self.apply(self.write('b5.json', batch('b5', put('s1', 3, 'A', 150, True))), code=2)
        self.apply(self.write('b6.json', batch('b6', put('s1', 2, 'A', 100, False))), code=2)

    def test_batch_replay_idempotent_and_conflict_rejected(self):
        self.init_or()
        source = self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True)))
        first = json.loads(self.apply(source).stdout)
        for _ in range(2):
            self.assertEqual(json.loads(self.apply(source).stdout), first)
        self.assertEqual(self.query(0)['generation'], 1)
        # same id, different content: rejected, state untouched
        self.apply(self.write('b2.json', batch('b1', put('s1', 1, 'A', 100, True), put('s2', 1, 'B', 5, True))), code=2)
        self.apply(self.write('b3.json', batch('b1', put('s9', 9, 'B', 1, True))), code=2)
        self.assertEqual(self.query(0)['generation'], 1)
        # key order and record order do not change batch identity
        reordered = self.dir / 'b1-reordered.json'
        reordered.write_text('{"records":[{"time":100,"sample_id":"s1","revision":1,"op":"put","failed":true,"event_id":"A"}],"batch_id":"b1"}')
        self.assertEqual(json.loads(self.apply(reordered).stdout), first)
        # replay after later batches moved the generation forward: first receipt, no reprocessing
        self.apply(self.write('b4.json', batch('b4', put('s2', 1, 'B', 700, True))))
        self.assertEqual(self.query(0)['generation'], 2)
        self.assertEqual(json.loads(self.apply(source).stdout), {'batch_id': 'b1', 'generation': 1})
        self.assertEqual(self.query(0)['generation'], 2)

    def test_in_batch_correction_resolves_same_second_conflict(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        # a second sensor contradicting s1 at the same second: whole batch rejected
        self.apply(self.write('b2.json', batch('b2', put('s2', 1, 'A', 100, False))), code=2)
        self.assertEqual(self.query(0)['generation'], 1)
        # one later batch corrects s1 and re-sends s2: the contradiction is gone
        result = self.apply(self.write('b3.json', batch('b3', put('s1', 2, 'A', 100, False),
                                                        put('s2', 1, 'A', 100, False))))
        self.assertEqual(json.loads(result.stdout), {'batch_id': 'b3', 'generation': 2})
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 0, 'downtime_seconds': 0})
        self.assertEqual([s['sample_id'] for s in report['current_samples']], ['s1', 's2'])

    def test_rejection_registers_no_versions(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        # conflicting batch also carries a brand-new, harmless sample s3
        self.apply(self.write('b2.json', batch('b2', put('s2', 1, 'A', 100, False),
                                               put('s3', 1, 'B', 500, True))), code=2)
        report = self.query(0)
        self.assertEqual([s['sample_id'] for s in report['current_samples']], ['s1'])
        # s2/s3 were never registered: a corrected batch may reuse the id and the slots
        result = self.apply(self.write('b3.json', batch('b2', put('s2', 1, 'A', 100, True),
                                                        put('s3', 1, 'B', 500, True))))
        self.assertEqual(json.loads(result.stdout), {'batch_id': 'b2', 'generation': 2})
        report = self.query(0)
        self.assertEqual(len(report['current_samples']), 3)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 900})

    def test_duplicate_record_in_batch_rejected(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 1, True), put('s1', 1, 'A', 1, True))), code=2)
        self.assertEqual(self.query(0)['generation'], 0)

    def test_validation_errors(self):
        self.init_or()
        good = put('s1', 1, 'A', 10, True)
        bad_batches = [
            {'records': [good]},                                  # missing batch_id
            {'batch_id': 'x', 'records': [good], 'extra': 1},     # unknown field
            {'batch_id': '', 'records': [good]},
            {'batch_id': 'x', 'records': []},
            {'batch_id': 'x', 'records': [good] * 5001},
            {'batch_id': 'x', 'records': ['not-an-object']},
            {'batch_id': 'x', 'records': [dict(good, extra=1)]},
            {'batch_id': 'x', 'records': [{k: v for k, v in good.items() if k != 'failed'}]},
            {'batch_id': 'x', 'records': [dict(withdraw('s1', 1), event_id='A')]},
            {'batch_id': 'x', 'records': [dict(good, op='noop')]},
            {'batch_id': 'x', 'records': [put('s1', 0, 'A', 10, True)]},
            {'batch_id': 'x', 'records': [put('s1', 10 ** 9 + 1, 'A', 10, True)]},
            {'batch_id': 'x', 'records': [put('s1', True, 'A', 10, True)]},
            {'batch_id': 'x', 'records': [put('s1', '1', 'A', 10, True)]},
            {'batch_id': 'x', 'records': [put('s1', 1, 'A', -1, True)]},
            {'batch_id': 'x', 'records': [put('s1', 1, 'A', 1000, True)]},   # time == horizon
            {'batch_id': 'x', 'records': [put('s1', 1, 'A', True, True)]},   # bool as time
            {'batch_id': 'x', 'records': [put('s1', 1, 'A', 10, 1)]},        # failed not boolean
            {'batch_id': 'x', 'records': [put('s1', 1, 'A', 10, 'true')]},
            {'batch_id': 'x', 'records': [put('s1', 1, 'top', 10, True)]},   # gate, not event
            {'batch_id': 'x', 'records': [put('s1', 1, 'ghost', 10, True)]},
            {'batch_id': 'x', 'records': [put('  ', 1, 'A', 10, True)]},
        ]
        for index, broken in enumerate(bad_batches):
            self.apply(self.write('bad%d.json' % index, broken), code=2)
            self.assertEqual(self.query(0, name='q%d.json' % index)['generation'], 0, broken)
        self.apply(self.write('dup.json', '{"batch_id":"x","batch_id":"y","records":[]}'), code=2)
        big = self.dir / 'big.json'
        big.write_text('{"batch_id":"x","records":[' + json.dumps(good) + ',' + ' ' * 8_000_000 + 'null]}')
        self.apply(big, code=2)
        self.assertEqual(self.query(0)['generation'], 0)

    def test_unreferenced_event_accepted_but_inert(self):
        self.init_or(tree=UNREF_TREE)
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'C', 100, True))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 0, 'downtime_seconds': 0})
        self.assertEqual(report['current_samples'][0]['event_id'], 'C')
        # conflicts are enforced even for events root never references
        self.apply(self.write('b2.json', batch('b2', put('s2', 1, 'C', 100, False))), code=2)
        self.apply(self.write('b3.json', batch('b3', put('s2', 1, 'C', 100, True))))
        self.assertEqual(self.query(0)['summary'], {'incident_count': 0, 'downtime_seconds': 0})

    def test_event_as_root(self):
        self.init_or(tree={'events': [{'id': 'A', 'numerator': 1, 'denominator': 2}], 'gates': [], 'root': 'A'})
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 7, True))))
        self.assertEqual(self.query(0)['summary'], {'incident_count': 1, 'downtime_seconds': 993})

class Materialize(Base):
    def test_shared_events_witness_switch_single_interval(self):
        self.init_or(tree=SHARED_TREE)
        self.apply(self.write('b1.json', batch('b1',
            put('s1', 1, 'A', 100, True), put('s2', 1, 'B', 200, True),
            put('s3', 1, 'B', 300, False), put('s4', 1, 'C', 300, True),
            put('s5', 1, 'A', 400, False))))
        report = self.query(0)
        # (A&B) fails over to (A&C) at 300 without the root ever clearing
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 200})
        self.assertEqual(report['day']['segments'], [{'incident': [200, 400], 'start': 200, 'end': 400}])

    def test_same_second_switch_not_split(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1',
            put('s1', 1, 'A', 10, True), put('s2', 1, 'A', 20, False),
            put('s3', 1, 'B', 20, True), put('s4', 1, 'B', 30, False))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 20})
        self.assertEqual(report['day']['segments'], [{'incident': [10, 30], 'start': 10, 'end': 30}])

    def test_time_zero_and_horizon_end(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 0, True),
                                               put('s2', 1, 'A', 999, False))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 999})
        self.assertEqual(report['day']['segments'], [{'incident': [0, 999], 'start': 0, 'end': 999}])
        # B takes over at the exact second A recovers: one unbroken incident to the horizon
        self.apply(self.write('b2.json', batch('b2', put('s3', 1, 'B', 999, True))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 1000})
        self.assertEqual(report['day']['segments'], [{'incident': [0, 1000], 'start': 0, 'end': 1000}])
        # moving A's recovery earlier splits the span into two incidents
        self.apply(self.write('b3.json', batch('b3', put('s2', 2, 'A', 500, False))))
        report = self.query(0)
        self.assertEqual(report['summary'], {'incident_count': 2, 'downtime_seconds': 501})
        self.assertEqual(report['day']['segments'], [{'incident': [0, 500], 'start': 0, 'end': 500},
                                                     {'incident': [999, 1000], 'start': 999, 'end': 1000}])

    def test_cross_day_partitions_and_partial_last_day(self):
        self.init(self.tree(), 200000)
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 80000, True),
                                               put('s2', 1, 'A', 175000, False))))
        expected = {0: ([[80000, 86400]], 6400), 1: ([[86400, 172800]], 86400), 2: ([[172800, 175000]], 2200)}
        total = 0
        for day, (segments, seconds) in expected.items():
            report = self.query(day)
            self.assertEqual(report['day']['downtime_seconds'], seconds)
            self.assertEqual(report['day']['segments'],
                             [{'incident': [80000, 175000], 'start': s, 'end': e} for s, e in segments])
            self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 95000})
            total += report['day']['downtime_seconds']
        self.assertEqual(total, 95000)

    def test_empty_days_have_partitions(self):
        self.init(self.tree(), 200000)
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 10, True), put('s2', 1, 'A', 20, False))))
        for day in (1, 2):
            report = self.query(day)
            self.assertEqual(report['day']['segments'], [])
            self.assertEqual(report['day']['downtime_seconds'], 0)
            self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 10})

BARRIERS = ['publish.begin', 'publish.facts', 'publish.summary', 'publish.days',
            'publish.pre_commit', 'publish.post_commit']

class Barrier:
    """Drive a writer subprocess through the publish barriers one by one."""
    def __init__(self, test, barrier_dir):
        self.test = test
        self.dir = Path(barrier_dir)

    def wait_ready(self, proc, label):
        marker = self.dir / (label + '.ready')
        for _ in range(3000):
            if marker.exists(): return
            self.test.assertIsNone(proc.poll(), 'writer exited before barrier ' + label)
            time.sleep(0.01)
        self.test.fail('barrier never reached: ' + label)

    def drive_to(self, proc, label):
        for name in BARRIERS:
            self.wait_ready(proc, name)
            if name == label: return
            (self.dir / (name + '.ack')).write_text('1')
        self.test.fail('unknown barrier: ' + label)

    def ack(self, label):
        (self.dir / (label + '.ack')).write_text('1')

class Publish(Base):
    def barrier_apply(self, source, barrier_dir):
        env = dict(os.environ, FAULT_HISTORY_BARRIER_DIR=str(barrier_dir))
        return subprocess.Popen([sys.executable, '-m', 'fault_tree.history', 'apply',
                                 '--dataset', str(self.dataset), '--source', str(source)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_kill_at_every_write_boundary(self):
        for round_, label in enumerate(BARRIERS):
            dataset = self.dir / ('ds%d' % round_)
            self.dataset = dataset
            self.init_or()
            source = self.write('b%d.json' % round_, batch('b1', put('s1', 1, 'A', 100, True)))
            barrier = Barrier(self, self.dir / ('barrier%d' % round_))
            proc = self.barrier_apply(source, barrier.dir)
            barrier.drive_to(proc, label)
            proc.kill()
            proc.communicate()
            expect = 1 if label == 'publish.post_commit' else 0
            report = self.query(0, name='q%d.json' % round_)
            self.assertEqual(report['generation'], expect, label)
            self.assertEqual(report['summary']['downtime_seconds'], 900 if expect else 0)
            # replaying the original batch never advances the generation twice
            result = self.apply(source)
            self.assertEqual(json.loads(result.stdout), {'batch_id': 'b1', 'generation': 1})
            report = self.query(0, name='q2-%d.json' % round_)
            self.assertEqual(report['generation'], 1)
            self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 900})
            self.assertEqual(json.loads(self.apply(source).stdout), {'batch_id': 'b1', 'generation': 1})

    def test_interleaved_readers_observe_complete_generations(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        source = self.write('b2.json', batch('b2', put('s2', 1, 'A', 500, False)))
        barrier = Barrier(self, self.dir / 'barrier')
        proc = self.barrier_apply(source, barrier.dir)
        barrier.drive_to(proc, 'publish.pre_commit')
        for index in range(3):
            report = self.query(0, name='during%d.json' % index)
            self.assertEqual(report['generation'], 1)
            self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 900})
        barrier.ack('publish.pre_commit')
        barrier.drive_to(proc, 'publish.post_commit')
        report = self.query(0, name='after.json')
        self.assertEqual(report['generation'], 2)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 400})
        barrier.ack('publish.post_commit')
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(out), {'batch_id': 'b2', 'generation': 2})
        report = self.query(0, name='final.json')
        self.assertEqual(report['generation'], 2)
        self.assertEqual(report['day']['segments'], [{'incident': [100, 500], 'start': 100, 'end': 500}])

    def test_init_kill_leaves_no_dataset(self):
        tree_path = self.tree()
        barrier = self.dir / 'barrier-init'
        env = dict(os.environ, FAULT_HISTORY_BARRIER_DIR=str(barrier))
        proc = subprocess.Popen([sys.executable, '-m', 'fault_tree.history', 'init', '--tree',
                                 str(tree_path), '--horizon', '1000', '--dataset', str(self.dataset)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        marker = barrier / 'init.pre_rename.ready'
        for _ in range(3000):
            if marker.exists(): break
            self.assertIsNone(proc.poll())
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        proc.kill()
        proc.communicate()
        self.assertFalse(self.dataset.exists())
        self.init(tree_path, 1000)
        self.assertEqual(self.query(0)['generation'], 0)

class Linkage(Base):
    def test_correction_splits_merges_and_moves_intervals(self):
        self.init(self.tree(), 200000)
        # one cross-day incident over [1000, 150000)
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 1000, True),
                                               put('s2', 1, 'A', 150000, False))))
        self.assertEqual(self.query(0)['summary'], {'incident_count': 1, 'downtime_seconds': 149000})
        self.assertEqual(self.query(2)['day']['segments'], [])
        # withdraw the end observation, re-close later: incident moves into day 2
        self.apply(self.write('b2.json', batch('b2', withdraw('s2', 2), put('s3', 1, 'A', 180000, False))))
        report = self.query(2)
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 179000})
        self.assertEqual(report['day']['segments'], [{'incident': [1000, 180000], 'start': 172800, 'end': 180000}])
        self.assertEqual(report['current_samples'][1],
                         {'sample_id': 's2', 'revision': 2, 'op': 'withdraw',
                          'event_id': None, 'time': None, 'failed': None})
        # moving the end observation earlier shrinks day 0 and clears days 1-2;
        # an independent B failure becomes a second incident
        self.apply(self.write('b3.json', batch('b3', put('s3', 2, 'A', 40000, False),
                                               put('s4', 1, 'B', 50000, True), put('s5', 1, 'B', 60000, False))))
        for day, incidents, seconds in [(0, [[1000, 40000], [50000, 60000]], 49000), (1, [], 0), (2, [], 0)]:
            report = self.query(day)
            self.assertEqual(report['day']['downtime_seconds'], seconds)
            self.assertEqual([s['incident'] for s in report['day']['segments']], incidents)
            self.assertEqual(report['summary'], {'incident_count': 2, 'downtime_seconds': 49000})
        # A fails again across B's gap: the two incidents merge into one cross-day span
        source = self.write('b4.json', batch('b4', put('s6', 1, 'A', 45000, True),
                                             put('s3', 3, 'A', 180000, False)))
        barrier = Barrier(self, self.dir / 'barrier')
        env = dict(os.environ, FAULT_HISTORY_BARRIER_DIR=str(barrier.dir))
        proc = subprocess.Popen([sys.executable, '-m', 'fault_tree.history', 'apply',
                                 '--dataset', str(self.dataset), '--source', str(source)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        barrier.drive_to(proc, 'publish.pre_commit')
        proc.kill()
        proc.communicate()
        # interrupted before the commit: the previous generation is fully intact
        report = self.query(0, name='before.json')
        self.assertEqual(report['generation'], 3)
        self.assertEqual(report['summary'], {'incident_count': 2, 'downtime_seconds': 49000})
        result = self.apply(source)
        self.assertEqual(json.loads(result.stdout), {'batch_id': 'b4', 'generation': 4})
        report = self.query(0, name='merged.json')
        self.assertEqual(report['summary'], {'incident_count': 1, 'downtime_seconds': 179000})
        self.assertEqual(report['day']['segments'], [{'incident': [1000, 180000], 'start': 1000, 'end': 86400}])
        self.assertEqual(self.query(1, name='m1.json')['day']['segments'],
                         [{'incident': [1000, 180000], 'start': 86400, 'end': 172800}])
        self.assertEqual(self.query(2, name='m2.json')['day']['segments'],
                         [{'incident': [1000, 180000], 'start': 172800, 'end': 180000}])
        self.assertEqual(sum(self.query(d, name='md%d.json' % d)['day']['downtime_seconds'] for d in range(3)), 179000)
        self.assertEqual([s['revision'] for s in report['current_samples']], [1, 2, 3, 1, 1, 1])

class OutputSafety(Base):
    def test_output_must_stay_outside_dataset(self):
        self.init_or()
        for target in [self.dataset, self.dataset / 'inside.json', self.dataset / 'days']:
            result = self.cli('query', '--dataset', self.dataset, '--day', 0, '--output', target)
            self.assertEqual(result.returncode, 2, target)
            self.assertFalse((self.dataset / 'inside.json').exists())

    def test_symlink_and_failed_query_preserve_output(self):
        self.init_or()
        out = self.dir / 'out.json'
        out.write_text('prior')
        self.query(5, name='out.json', code=2)
        self.assertEqual(out.read_text(), 'prior')
        link = self.dir / 'link.json'
        link.symlink_to(out)
        result = self.cli('query', '--dataset', self.dataset, '--day', 0, '--output', link)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(link.is_symlink())
        self.assertEqual(out.read_text(), 'prior')

    def test_query_byte_deterministic(self):
        self.init_or()
        self.apply(self.write('b1.json', batch('b1', put('s1', 1, 'A', 100, True))))
        first = self.query(0, name='q1.json')
        second = self.query(0, name='q2.json')
        self.assertEqual((self.dir / 'q1.json').read_bytes(), (self.dir / 'q2.json').read_bytes())
        self.assertEqual(first, second)

    def test_broken_dataset_state_rejected(self):
        self.init_or()
        result = self.cli('apply', '--dataset', self.dir / 'missing', '--source', self.write('b1.json', batch('b1')))
        self.assertEqual(result.returncode, 2)
        (self.dataset / 'current').write_text('{"generation": 7}')
        self.query(0, code=2)
        self.apply(self.write('b2.json', batch('b2', put('s1', 1, 'A', 1, True))), code=2)

if __name__ == '__main__':
    unittest.main()
