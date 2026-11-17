"""Measured-state revision settlement, timeline evaluation and downtime materialization.

All functions are pure: they take already-parsed structures and return new
structures (or raise ValueError), so a rejected batch never mutates state that
could be persisted. Settlement order:
  1. validate_batch      - strict wire contract for one batch file
  2. batch_fingerprint   - canonical identity of a validated batch
  3. settle              - whole-batch merge into the per-sample revision memory
  4. effective_observations - final active puts per (event, time), conflict check
  5. evaluate_intervals  - maximal half-open root-true intervals over [0, horizon)
  6. materialize         - global summary + per-day partition segments
"""
import bisect, hashlib, json
from ..common import ident, number, require, rows

MAX_RECORDS = 5000
MAX_REVISION = 10 ** 9
DAY_SECONDS = 86400

def validate_batch(data, event_ids, horizon):
    """Check the strict batch contract; return (batch_id, canonical records).

    Each canonical record is {'sample_id', 'revision', 'content'} where content
    is {'op': 'put', 'event_id', 'time', 'failed'} or {'op': 'withdraw'}.
    """
    require(data, 'batch_id records')
    batch_id = ident(data['batch_id'])
    records = rows(data['records'], MAX_RECORDS)
    if not records: raise ValueError('records must contain 1..5000 items')
    seen = set(); canonical = []
    for rec in records:
        if not isinstance(rec, dict): raise ValueError('record must be an object')
        op = rec.get('op')
        if op == 'put': require(rec, 'sample_id revision op event_id time failed')
        elif op == 'withdraw': require(rec, 'sample_id revision op')
        else: raise ValueError('record op must be "put" or "withdraw"')
        sample_id = ident(rec['sample_id'])
        revision = number(rec['revision'], 1, MAX_REVISION)
        key = (sample_id, revision)
        if key in seen: raise ValueError('duplicate (sample_id, revision) in batch')
        seen.add(key)
        if op == 'put':
            event_id = ident(rec['event_id'])
            if event_id not in event_ids: raise ValueError('event_id is not a declared basic event: ' + event_id)
            moment = number(rec['time'], 0, horizon - 1)
            failed = rec['failed']
            if type(failed) is not bool: raise ValueError('failed must be a JSON boolean')
            content = {'op': 'put', 'event_id': event_id, 'time': moment, 'failed': failed}
        else:
            content = {'op': 'withdraw'}
        canonical.append({'sample_id': sample_id, 'revision': revision, 'content': content})
    return batch_id, canonical

def _wire(rec):
    """Canonical wire form of one record: exactly the fields the sender supplied."""
    return {'sample_id': rec['sample_id'], 'revision': rec['revision'], **rec['content']}

def batch_fingerprint(batch_id, canonical):
    """Canonical batch identity: every field counts; only object key order and
    records row order are ignored."""
    def dump(obj): return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    records = sorted(dump(_wire(rec)) for rec in canonical)
    return hashlib.sha256(dump({'batch_id': batch_id, 'records': records}).encode('utf-8')).hexdigest()

def settle(samples, canonical):
    """Merge a whole validated batch into the revision memory (a fresh copy).

    A previously received (sample_id, revision) must carry identical content,
    otherwise the entire batch is rejected. Unseen revisions register even when
    lower than the current maximum; they never demote it.
    """
    merged = {sid: dict(revs) for sid, revs in samples.items()}
    for rec in canonical:
        revisions = merged.setdefault(rec['sample_id'], {})
        revision = rec['revision']
        if revision in revisions:
            if revisions[revision] != rec['content']:
                raise ValueError('revision %d of sample %s already received with different content' % (revision, rec['sample_id']))
        else:
            revisions[revision] = rec['content']
    return merged

def effective_observations(samples):
    """Final active puts as {event_id: {time: failed}} from each sample's
    highest revision. A highest-revision withdraw is a tombstone and
    contributes nothing. Opposite failed values at the same (event, time)
    reject the whole batch, for every declared event."""
    observations = {}
    for revisions in samples.values():
        content = revisions[max(revisions)]
        if content['op'] != 'put': continue
        slot = observations.setdefault(content['event_id'], {})
        moment, failed = content['time'], content['failed']
        if moment in slot and slot[moment] != failed:
            raise ValueError('conflicting effective observations for event %s at time %d' % (content['event_id'], moment))
        slot[moment] = failed
    return observations

def _root_value(meta, vector):
    state = dict(vector)
    for gid in meta['order']:
        gate = meta['gates'][gid]
        values = [state[node] for node in gate['inputs']]
        state[gid] = all(values) if gate['op'] == 'AND' else any(values)
    return state[meta['root']]

def evaluate_intervals(meta, observations):
    """Maximal half-open intervals of [0, horizon) where root is true.

    Every event is healthy before its first observation; a put holds from its
    time until the next observation of the same event; the last one persists to
    the horizon. All changes at one second apply together before the shared
    basic-event vector is evaluated, so no transient or zero-length faults and
    no split of adjacent root-true spans can appear.
    """
    horizon = meta['horizon']
    series = {}; points = {0}
    for event in meta['events']:
        obs = observations.get(event)
        if obs:
            times = sorted(obs)
            series[event] = (times, [obs[t] for t in times])
            points.update(times)
    intervals = []; start = None
    for moment in sorted(points):
        vector = {}
        for event in meta['events']:
            pair = series.get(event)
            if pair is None: vector[event] = False
            else:
                times, values = pair
                index = bisect.bisect_right(times, moment) - 1
                vector[event] = values[index] if index >= 0 else False
        if _root_value(meta, vector):
            if start is None: start = moment
        elif start is not None:
            intervals.append([start, moment]); start = None
    if start is not None: intervals.append([start, horizon])
    return intervals

def materialize(meta, intervals):
    """Global summary plus one partition per day. Segments are the non-empty
    intersections of the global intervals with each day and carry the global
    [start, end] identity of their incident."""
    horizon = meta['horizon']
    summary = {'incident_count': len(intervals),
               'downtime_seconds': sum(end - start for start, end in intervals),
               'incidents': [list(interval) for interval in intervals]}
    days = []
    for index in range(meta['num_days']):
        start = index * DAY_SECONDS
        end = min((index + 1) * DAY_SECONDS, horizon)
        segments = [{'incident': [s, e], 'start': max(s, start), 'end': min(e, end)}
                    for s, e in intervals if max(s, start) < min(e, end)]
        days.append({'index': index, 'start': start, 'end': end,
                     'downtime_seconds': sum(seg['end'] - seg['start'] for seg in segments),
                     'segments': segments})
    return summary, days

def current_samples(samples):
    """Highest revision of every sample, sorted by sample_id; a withdraw
    tombstone keeps its identity and reports null event fields."""
    result = []
    for sample_id in sorted(samples):
        revisions = samples[sample_id]
        top = max(revisions); content = revisions[top]
        if content['op'] == 'put':
            result.append({'sample_id': sample_id, 'revision': top, 'op': 'put',
                           'event_id': content['event_id'], 'time': content['time'], 'failed': content['failed']})
        else:
            result.append({'sample_id': sample_id, 'revision': top, 'op': 'withdraw',
                           'event_id': None, 'time': None, 'failed': None})
    return result
