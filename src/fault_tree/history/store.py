"""Dataset layout, atomic generation publication and pinned snapshot reads.

Layout (public contract, see docs/contract.md):

    DIR/meta.json                    immutable tree parameters + horizon
    DIR/tree.json                    byte-exact copy of the init tree file
    DIR/current                      commit pointer: {"generation": N}
    DIR/generations/<N>/facts.json   revision memory + batch reception identity
    DIR/generations/<N>/summary.json global intervals for generation N
    DIR/generations/<N>/days/<D>.json day partition for generation N

Commit protocol: a new generation is built file by file inside
generations/<N>/ and only becomes visible when the "current" pointer is
atomically replaced (write temp file + os.replace). Readers pin the pointer
once and then read every file from that single generation directory, so they
always observe one complete old or new generation. Generation directories are
immutable once published and are never reused or garbage collected; a
half-written directory left by a killed writer is unreachable because nothing
points at it, and it is deleted before the next publish of the same number.
"""
import hashlib, json, os, shutil, time, uuid
from pathlib import Path
from ..common import loads as parse_input, read as read_input, save as save_json, ident, number
from ..model import parse_tree
from . import model

SCHEMA = 2
DAY_SECONDS = 86400
MAX_HORIZON = 7_776_000

def _barrier(label):
    """Test hook: when FAULT_HISTORY_BARRIER_DIR is set, announce <label> and
    wait for its acknowledgement file, so tests can interrupt or interleave a
    writer at exact persistence boundaries. Inert otherwise."""
    root = os.environ.get('FAULT_HISTORY_BARRIER_DIR')
    if not root: return
    directory = Path(root); directory.mkdir(parents=True, exist_ok=True)
    (directory / (label + '.ready')).write_text(label, encoding='utf-8')
    ack = directory / (label + '.ack')
    for _ in range(6000):
        if ack.exists(): return
        time.sleep(0.01)
    raise TimeoutError('barrier acknowledgement timeout: ' + label)

def _read_json(path):
    with Path(path).open('rb') as stream: return json.load(stream)

def _gen_dir(root, generation): return Path(root) / 'generations' / str(generation)

def _write_pointer(root, generation):
    root = Path(root)
    temp = root / ('.current-' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps({'generation': generation}, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temp, root / 'current')

def _load_meta(root):
    meta = _read_json(Path(root) / 'meta.json')
    if not isinstance(meta, dict) or meta.get('kind') != 'meta' or meta.get('schema_version') != SCHEMA:
        raise ValueError('dataset meta is missing or invalid')
    horizon = meta.get('horizon')
    if type(horizon) is not int or not 1 <= horizon <= MAX_HORIZON:
        raise ValueError('dataset meta horizon is invalid')
    if meta.get('num_days') != (horizon + DAY_SECONDS - 1) // DAY_SECONDS:
        raise ValueError('dataset meta day count is invalid')
    ident(meta.get('tree_sha256')); ident(meta.get('root'))
    if not isinstance(meta.get('events'), list) or not all(isinstance(e, str) for e in meta['events']):
        raise ValueError('dataset meta events are invalid')
    if not isinstance(meta.get('gates'), dict) or not isinstance(meta.get('order'), list):
        raise ValueError('dataset meta gates are invalid')
    return meta

def _load_current(root):
    pointer = _read_json(Path(root) / 'current')
    if not isinstance(pointer, dict) or type(pointer.get('generation')) is not int or pointer['generation'] < 0:
        raise ValueError('dataset commit pointer is missing or invalid')
    return pointer['generation']

def _check_envelope(obj, kind, generation, meta):
    if not isinstance(obj, dict) or obj.get('kind') != kind or obj.get('schema_version') != SCHEMA \
            or obj.get('generation') != generation or obj.get('tree_sha256') != meta['tree_sha256']:
        raise ValueError('dataset generation %d has an incomplete or inconsistent %s file' % (generation, kind))

def _check_content(content, event_ids, horizon):
    if not isinstance(content, dict): raise ValueError('facts memory is invalid')
    op = content.get('op')
    if op == 'put':
        if set(content) != {'op', 'event_id', 'time', 'failed'}: raise ValueError('facts memory is invalid')
        if content['event_id'] not in event_ids: raise ValueError('facts memory is invalid')
        number(content['time'], 0, horizon - 1)
        if type(content['failed']) is not bool: raise ValueError('facts memory is invalid')
    elif op != 'withdraw' or set(content) != {'op'}:
        raise ValueError('facts memory is invalid')

def _load_facts(root, generation, meta):
    facts = _read_json(_gen_dir(root, generation) / 'facts.json')
    _check_envelope(facts, 'facts', generation, meta)
    samples = facts.get('samples'); batches = facts.get('batches')
    if not isinstance(samples, dict) or not isinstance(batches, dict):
        raise ValueError('facts memory is invalid')
    event_ids = set(meta['events']); memory = {}
    for sample_id, revisions in samples.items():
        ident(sample_id)
        if not isinstance(revisions, dict) or not revisions: raise ValueError('facts memory is invalid')
        memory[sample_id] = {}
        for key, content in revisions.items():
            if not isinstance(key, str) or not key.isdigit() or str(int(key)) != key:
                raise ValueError('facts memory is invalid')
            revision = number(int(key), 1, model.MAX_REVISION)
            _check_content(content, event_ids, meta['horizon'])
            memory[sample_id][revision] = content
    for batch_id, saved in batches.items():
        ident(batch_id)
        if not isinstance(saved, dict) or type(saved.get('generation')) is not int \
                or not isinstance(saved.get('fingerprint'), str):
            raise ValueError('facts batch memory is invalid')
    return memory, batches

def init(tree, horizon, dataset):
    """Create a generation-0 dataset. The target directory must not exist; the
    dataset appears atomically via one directory rename, so a failed init never
    leaves a queryable dataset behind."""
    dataset = Path(dataset)
    with Path(tree).open('rb') as stream: raw = stream.read(8_000_001)
    data = parse_input(raw)
    digest = hashlib.sha256(raw).hexdigest()
    events, gates, order, root = parse_tree(data)
    if os.path.lexists(dataset): raise ValueError('dataset path already exists: ' + str(dataset))
    dataset.parent.mkdir(parents=True, exist_ok=True)
    staging = dataset.parent / (dataset.name + '.staging-' + uuid.uuid4().hex)
    num_days = (horizon + DAY_SECONDS - 1) // DAY_SECONDS
    meta = {'schema_version': SCHEMA, 'kind': 'meta', 'tree_sha256': digest, 'root': root,
            'horizon': horizon, 'num_days': num_days, 'events': sorted(events),
            'gates': {gid: {'op': gates[gid]['op'], 'inputs': list(gates[gid]['inputs'])} for gid in sorted(gates)},
            'order': list(order)}
    envelope = {'schema_version': SCHEMA, 'tree_sha256': digest}
    try:
        (_gen_dir(staging, 0) / 'days').mkdir(parents=True)
        save_json(staging / 'meta.json', meta)
        (staging / 'tree.json').write_bytes(raw)
        save_json(_gen_dir(staging, 0) / 'facts.json',
                  {**envelope, 'kind': 'facts', 'generation': 0, 'samples': {}, 'batches': {}})
        save_json(_gen_dir(staging, 0) / 'summary.json',
                  {**envelope, 'kind': 'summary', 'generation': 0, 'incident_count': 0, 'downtime_seconds': 0, 'incidents': []})
        for index in range(num_days):
            save_json(_gen_dir(staging, 0) / 'days' / (str(index) + '.json'),
                      {**envelope, 'kind': 'day', 'generation': 0, 'index': index,
                       'start': index * DAY_SECONDS, 'end': min((index + 1) * DAY_SECONDS, horizon),
                       'downtime_seconds': 0, 'segments': []})
        _write_pointer(staging, 0)
        _barrier('init.pre_rename')
        os.rename(staging, dataset)
    finally:
        if staging.exists(): shutil.rmtree(staging, ignore_errors=True)
    return {'generation': 0}

def _publish(root, generation, facts, summary, days, meta):
    envelope = {'schema_version': SCHEMA, 'tree_sha256': meta['tree_sha256']}
    directory = _gen_dir(root, generation)
    if directory.exists(): shutil.rmtree(directory)
    (directory / 'days').mkdir(parents=True)
    _barrier('publish.begin')
    save_json(directory / 'facts.json', facts)
    _barrier('publish.facts')
    save_json(directory / 'summary.json', {**envelope, 'kind': 'summary', 'generation': generation, **summary})
    _barrier('publish.summary')
    for day in days:
        save_json(directory / 'days' / (str(day['index']) + '.json'),
                  {**envelope, 'kind': 'day', 'generation': generation, **day})
    _barrier('publish.days')
    _barrier('publish.pre_commit')
    _write_pointer(root, generation)
    _barrier('publish.post_commit')

def apply(dataset, source):
    """Settle one batch and publish exactly one new generation, or return the
    first receipt of an identical replay. Rejections persist nothing."""
    dataset = Path(dataset)
    meta = _load_meta(dataset)
    generation = _load_current(dataset)
    samples, batches = _load_facts(dataset, generation, meta)
    data, _ = read_input(source)
    batch_id, canonical = model.validate_batch(data, set(meta['events']), meta['horizon'])
    fingerprint = model.batch_fingerprint(batch_id, canonical)
    if batch_id in batches:
        saved = batches[batch_id]
        if saved['fingerprint'] != fingerprint:
            raise ValueError('batch_id already received with different content: ' + batch_id)
        return {'batch_id': batch_id, 'generation': saved['generation']}
    merged = model.settle(samples, canonical)
    observations = model.effective_observations(merged)
    intervals = model.evaluate_intervals(meta, observations)
    summary, days = model.materialize(meta, intervals)
    new_generation = generation + 1
    facts = {'schema_version': SCHEMA, 'tree_sha256': meta['tree_sha256'], 'kind': 'facts',
             'generation': new_generation,
             'samples': {sid: {str(rev): content for rev, content in sorted(revs.items())}
                         for sid, revs in sorted(merged.items())},
             'batches': {**batches, batch_id: {'generation': new_generation, 'fingerprint': fingerprint}}}
    _publish(dataset, new_generation, facts, summary, days, meta)
    return {'batch_id': batch_id, 'generation': new_generation}

def query(dataset, day_index, output):
    """Read one pinned committed generation and write the day report. All data
    fields come from the single generation named by the commit pointer at read
    time; the dataset is self-contained and needs no external file."""
    dataset = Path(dataset); output = Path(output)
    if type(day_index) is not int or day_index < 0:
        raise ValueError('day must be a non-negative integer')
    real_dataset = os.path.realpath(dataset); real_output = os.path.realpath(output)
    if real_output == real_dataset or real_output.startswith(real_dataset + os.sep):
        raise ValueError('output must be outside the dataset directory')
    meta = _load_meta(dataset)
    if day_index >= meta['num_days']: raise ValueError('day is outside the dataset horizon')
    generation = _load_current(dataset)
    samples, _ = _load_facts(dataset, generation, meta)
    summary = _read_json(_gen_dir(dataset, generation) / 'summary.json')
    _check_envelope(summary, 'summary', generation, meta)
    day = _read_json(_gen_dir(dataset, generation) / 'days' / (str(day_index) + '.json'))
    _check_envelope(day, 'day', generation, meta)
    if day.get('index') != day_index: raise ValueError('day partition is inconsistent')
    report = {'schema_version': SCHEMA, 'tree_sha256': meta['tree_sha256'], 'root': meta['root'],
              'horizon': meta['horizon'], 'generation': generation,
              'current_samples': model.current_samples(samples),
              'summary': {'incident_count': summary['incident_count'],
                          'downtime_seconds': summary['downtime_seconds']},
              'day': {'index': day['index'], 'start': day['start'], 'end': day['end'],
                      'downtime_seconds': day['downtime_seconds'], 'segments': day['segments']}}
    save_json(output, report)
    return report
