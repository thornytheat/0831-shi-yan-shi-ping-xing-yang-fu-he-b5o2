from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from hashlib import sha256

from app.business import DomainIssue
from app.release.domain import (
    basis_digest,
    canonical_measurement,
    canonical_side,
    invalidation_evidence,
    resolve_release,
)

SAMPLE_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS sample_reviews (
        run_id TEXT NOT NULL,
        sample_key TEXT NOT NULL,
        case_id TEXT NOT NULL,
        sample_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'HELD',
        revision INTEGER NOT NULL DEFAULT 0,
        basis_revision INTEGER NOT NULL DEFAULT 0,
        eligible INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '',
        basis_digest TEXT NOT NULL DEFAULT '',
        board_json TEXT NOT NULL DEFAULT '{}',
        adopted_json TEXT,
        invalidation_json TEXT,
        note TEXT NOT NULL DEFAULT '',
        reviewed TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(run_id,sample_key)
    )''',
    '''CREATE TABLE IF NOT EXISTS retest_reports (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        sample_key TEXT NOT NULL,
        round_id TEXT NOT NULL,
        side TEXT NOT NULL,
        record_id TEXT NOT NULL DEFAULT '',
        measurement_json TEXT NOT NULL,
        created TEXT NOT NULL,
        updated TEXT NOT NULL,
        UNIQUE(run_id,sample_key,round_id,side)
    )''',
    '''CREATE TABLE IF NOT EXISTS measurement_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        correction_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL,
        sample_key TEXT NOT NULL,
        report_id TEXT NOT NULL DEFAULT '',
        target TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        created TEXT NOT NULL
    )''',
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def ensure_schema(db):
    for statement in SAMPLE_SCHEMA:
        db.execute(statement)


def _json(value, default=None):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default
    return value if value is not None else default


def _descriptor(run, key):
    report = run.get('report', {}) if isinstance(run, dict) else {}
    for item in report.get('samples', []) or []:
        if str(item.get('key')) == key:
            return item
    # 兼容尚未重建 sample index 的调用方/测试夹具。
    for case in report.get('cases', []) or []:
        for sample in (case.get('samples') or []):
            if str(sample.get('key')) == key:
                return sample
    return None


def _all_descriptors(run):
    report = run.get('report', {}) if isinstance(run, dict) else {}
    return list(report.get('samples', []) or [])


def split_ref(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = re.split(r'[:/#|]+', text, maxsplit=1)
    if len(parts) == 2:
        return parts
    # 样品编号按字段字典会归一为整数，因此 0004-1 / 0004_1 也可解析。
    match = re.fullmatch(r'(.+?)[-_](\d+)', text)
    if match:
        return match.group(1), match.group(2)
    return None


def _value(values, *keys):
    for key in keys:
        if isinstance(values, dict) and values.get(key) not in (None, ''):
            return values[key]
    return None


def resolve_identity(store, run_id, values=None, ref=None):
    values = values if isinstance(values, dict) else {}
    case = _value(values, 'case', 'case_id', 'case_code', 'group', 'business_case')
    sample = _value(values, 'sample', 'sample_id', 'sample_code', 'sample_no')
    key = _value(values, 'key', 'sample_key', 'sample_ref') or ref

    split = split_ref(key)
    if split and (case is None or sample is None):
        case, sample = split
    elif key and not split and case is not None and sample is None:
        sample = key

    run = None
    if run_id:
        run = store.load(run_id)

    if case is not None and sample is not None:
        case, sample = str(case), str(sample)
        return run_id, f'{case}:{sample}', case, sample

    if key is not None and run is not None:
        text = str(key)
        descriptors = _all_descriptors(run)
        exact = [item for item in descriptors if item.get('key') == text]
        if not exact:
            exact = [item for item in descriptors
                     if text in (item.get('sample'), item.get('sample_id'))
                     or text in (item.get('record_ids') or [])]
        if len(exact) == 1:
            item = exact[0]
            return run_id, item['key'], str(item['case']), str(item['sample'])
        if len(exact) > 1:
            raise DomainIssue('SAMPLE_IDENTIFIER_AMBIGUOUS')

    if run_id is None and values.get('run_id'):
        return resolve_identity(store, values['run_id'], values, ref)

    raise DomainIssue('SAMPLE_IDENTIFIER_REQUIRED')


def _ensure_review_row(db, run_id, key, case, sample):
    db.execute(
        '''INSERT INTO sample_reviews(run_id,sample_key,case_id,sample_id,updated)
           VALUES(?,?,?,?,?) ON CONFLICT(run_id,sample_key) DO NOTHING''',
        (run_id, key, case, sample, now()),
    )


def _load_rounds(db, run_id, key):
    rows = db.execute(
        'SELECT * FROM retest_reports WHERE run_id=? AND sample_key=? ORDER BY rowid',
        (run_id, key),
    ).fetchall()
    rounds = {}
    order = []
    for row in rows:
        round_id = row['round_id']
        if round_id not in rounds:
            rounds[round_id] = {'round_id': round_id, 'measurements': [], 'reports': []}
            order.append(round_id)
        measurement = _json(row['measurement_json'], {})
        measurement['report_id'] = row['id']
        rounds[round_id]['measurements'].append(measurement)
        rounds[round_id]['reports'].append({
            'id': row['id'], 'side': row['side'], 'record_id': row['record_id'],
            'measurement': measurement, 'created': row['created'], 'updated': row['updated'],
        })
    return [rounds[round_id] for round_id in order]


def _load_corrections(db, run_id, key):
    rows = db.execute(
        'SELECT * FROM measurement_corrections WHERE run_id=? AND sample_key=? ORDER BY id',
        (run_id, key),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['payload'] = _json(item.pop('payload_json'), {})
        result.append(item)
    return result


def build_board_item(store, run_id, key, db=None):
    run = store.load(run_id)
    descriptor = _descriptor(run, key) or {'key': key, 'case': key.split(':', 1)[0],
                                           'sample': key.split(':', 1)[-1]}
    close = False
    if db is None:
        context = store.connect()
        db = context.__enter__()
        close = True
    try:
        ensure_schema(db)
        row = db.execute(
            'SELECT * FROM sample_reviews WHERE run_id=? AND sample_key=?', (run_id, key)
        ).fetchone()
        rounds = _load_rounds(db, run_id, key)
        corrections = _load_corrections(db, run_id, key)
    finally:
        if close:
            context.__exit__(None, None, None)

    invalidation = _json(row['invalidation_json']) if row else None
    original_measurements = descriptor.get('measurements') or []
    original_lods = {item.get('lod') for item in original_measurements if item.get('lod') is not None}
    original_loqs = {item.get('loq') for item in original_measurements if item.get('loq') is not None}
    fallback_lod = next(iter(original_lods)) if len(original_lods) == 1 else None
    fallback_loq = next(iter(original_loqs)) if len(original_loqs) == 1 else None
    conclusion = resolve_release(
        descriptor, rounds, invalidation, descriptor.get('control'),
        fallback_lod=fallback_lod, fallback_loq=fallback_loq,
    )
    digest = basis_digest(conclusion)

    item = {
        'run_id': run_id,
        'run_revision': run.get('revision', 0),
        'key': key,
        'sample_key': key,
        'case': descriptor.get('case'),
        'case_id': descriptor.get('case', key.split(':', 1)[0]),
        'sample': descriptor.get('sample', key.split(':', 1)[-1]),
        'sample_id': descriptor.get('sample', key.split(':', 1)[-1]),
        'revision': row['revision'] if row else 0,
        'basis_revision': row['basis_revision'] if row else 0,
        'basis_digest': digest,
        'eligible': conclusion['eligible'],
        'issuable': conclusion['issuable'],
        'releasable': conclusion['releasable'],
        'can_release': conclusion['can_release'],
        'current_eligible': conclusion['eligible'],
        'reason': conclusion['reason'],
        'reason_codes': conclusion['reason_codes'],
        'quality_status': conclusion['original'].get('status'),
        'original': conclusion['original'],
        'retests': conclusion['retests'],
        'rounds': rounds,
        'corrections': corrections,
        'invalidation': conclusion['invalidation'],
        'adopted': conclusion['adopted'],
        'adopted_records': conclusion['adopted_records'],
        'measurement_records': conclusion['adopted']['measurements'] if conclusion['adopted'] else None,
        'note': row['note'] if row else '',
        'reviewed': row['reviewed'] if row and row['reviewed'] else None,
        'updated': row['updated'] if row else None,
        'status': conclusion['status'],
        'conclusion': conclusion['conclusion'],
    }

    if row:
        item['stored_status'] = row['status']
        if row['status'] == 'APPROVED':
            adopted = _json(row['adopted_json'], conclusion['adopted'])
            item.update({
                'status': 'APPROVED',
                'conclusion': 'APPROVED',
                'adopted': adopted,
                'adopted_records': (adopted or {}).get('record_ids', []),
                'measurement_records': (adopted or {}).get('measurements'),
                'basis_changed': bool(row['basis_digest'] and row['basis_digest'] != digest),
                'issued': True,
            })
        elif row['status'] == 'HELD':
            item.update({'status': 'HELD', 'conclusion': 'HELD', 'issued': False,
                         'manual_hold': bool(row['note'])})
        else:
            item.update({'status': conclusion['status'], 'conclusion': conclusion['conclusion'],
                         'issued': False})
    else:
        item.update({'issued': False, 'basis_changed': False})

    item['state'] = item['status']
    item['decision'] = item['status']
    if item['status'] == 'APPROVED':
        workflow_status = 'APPROVED'
    elif item['status'] == 'HELD' or (row and row['status'] == 'HELD'):
        workflow_status = 'HELD'
    else:
        workflow_status = 'PENDING'
    item['workflow_status'] = workflow_status
    item['pending'] = workflow_status == 'PENDING'
    item['release_status'] = 'RELEASED' if item['status'] == 'APPROVED' else item['status']
    item['issuance'] = {
        'status': workflow_status,
        'conclusion': item['status'],
        'issuable': item['eligible'] and item['status'] != 'APPROVED',
        'revision': item['revision'],
    }
    item['used_records'] = item.get('adopted_records', [])
    item['adopted_record_ids'] = item.get('adopted_records', [])
    item['adopted_measurements'] = item.get('measurement_records')
    item['used_measurement_records'] = item.get('measurement_records')
    return item


def sample_board(store, run_id):
    run = store.load(run_id)
    keys = [item['key'] for item in _all_descriptors(run)]
    with store.connect() as db:
        ensure_schema(db)
        for row in db.execute('SELECT sample_key FROM sample_reviews WHERE run_id=?', (run_id,)):
            if row['sample_key'] not in keys:
                keys.append(row['sample_key'])
    return [build_board_item(store, run_id, key) for key in keys]


# 常见命名别名。
board = sample_board
list_samples = sample_board
workbench = sample_board


def get_sample(store, run_id, ref=None, values=None):
    _, key, _, _ = resolve_identity(store, run_id, values or {}, ref)
    return build_board_item(store, run_id, key)


def _base_report(values):
    base = {key: values.get(key) for key in
            ('run_id', 'case', 'case_id', 'sample', 'sample_id', 'key', 'sample_key',
             'round', 'round_id', 'retest_id', 'retest_round', 'batch_id', 'pair_id')
            if values.get(key) is not None}
    return base


def _report_payloads(values):
    if isinstance(values, list):
        for value in values:
            yield from _report_payloads(value)
        return
    if not isinstance(values, dict):
        return

    base = _base_report(values)
    shared_limits = {key: values[key] for key in
                     ('lod', 'loq', 'limit', 'absolute_limit', 'rpd_limit_bp')
                     if values.get(key) is not None}
    collection = _value(values, 'reports', 'items', 'measurements', 'retests', 'rounds')
    if isinstance(collection, list):
        for item in collection:
            if isinstance(item, dict) and any(
                key in item for key in
                ('primary', 'duplicate', 'left', 'right', 'a', 'b', 'measurements', 'reports')
            ):
                yield from _report_payloads({**base, **shared_limits, **item})
            elif isinstance(item, dict):
                yield {**base, **shared_limits, **item}
        return
    nested_pair = _value(values, 'retest', 'retest_pair', 'pair', 'measurements')
    if isinstance(nested_pair, dict):
        yield from _report_payloads({**base, **shared_limits, **nested_pair})
        return
    pair_fields = {
        'primary': ('primary', 'left', 'a', 'side_a', 'first'),
        'duplicate': ('duplicate', 'right', 'b', 'side_b', 'second'),
    }
    present_pair_fields = [key for keys in pair_fields.values() for key in keys
                           if isinstance(values.get(key), (dict, int, float, str))]
    if present_pair_fields:
        for canonical_side_name, aliases in pair_fields.items():
            nested = _value(values, *aliases)
            if nested is not None:
                body = nested if isinstance(nested, dict) else {'concentration': nested}
                yield {**base, **shared_limits, **body, 'side': canonical_side_name}
        return
    yield values


def _report_id(run_id, key, round_id, side, explicit=None):
    if explicit:
        return str(explicit)
    raw = f'{run_id}:{key}:{round_id}:{side}'
    return 'RR-' + sha256(raw.encode()).hexdigest()[:24]


def record_retest(store, run_id=None, values=None, **kwargs):
    # 兼容 record_retest(store, payload) / record_retest(store, run_id, payload)。
    if values is None and isinstance(run_id, dict):
        values, run_id = run_id, None
    if run_id is None:
        run_id = (values or {}).get('run_id') or kwargs.get('run_id')
    if not run_id or not isinstance(values, dict):
        raise DomainIssue('RETEST_FIELDS')

    submitted = list(_report_payloads(values))
    if not submitted:
        raise DomainIssue('RETEST_FIELDS')

    identities = set()
    changed = False
    correction_posted = False
    affected_runs = set()
    with store.connect() as db:
        ensure_schema(db)
        for body in submitted:
            _, key, case, sample = resolve_identity(store, run_id, body)
            identities.add((key, case, sample))
            round_id = str(_value(body, 'round_id', 'round', 'retest_round', 'retest_round_id',
                                  'retest_no', 'round_no', 'cycle', 'attempt', 'session',
                                  'retest_id', 'batch_id', 'pair_id') or 'default')
            measurement = canonical_measurement(body)
            side = measurement.get('side') or canonical_side(body.get('side'))
            if side not in ('primary', 'duplicate'):
                # 没有 side 且本批只有一条时，若该轮已有一侧则补另一侧，否则记 primary。
                existing = db.execute(
                    'SELECT side FROM retest_reports WHERE run_id=? AND sample_key=? AND round_id=?',
                    (run_id, key, round_id),
                ).fetchall()
                sides = {row['side'] for row in existing}
                side = 'duplicate' if 'primary' in sides and 'duplicate' not in sides else 'primary'
            measurement['side'] = side
            explicit_id = _value(body, 'report_id', 'measurement_id', 'id')
            report_id = _report_id(run_id, key, round_id, side, explicit_id)
            record_id = measurement.get('record') or (str(explicit_id) if explicit_id else report_id)
            measurement['record'] = record_id
            timestamp = now()

            old_row = db.execute(
                'SELECT * FROM retest_reports WHERE run_id=? AND sample_key=? AND round_id=? AND side=?',
                (run_id, key, round_id, side),
            ).fetchone()
            if old_row is None:
                db.execute(
                    '''INSERT INTO retest_reports
                       (id,run_id,sample_key,round_id,side,record_id,measurement_json,created,updated)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (report_id, run_id, key, round_id, side, record_id,
                     json.dumps(measurement, ensure_ascii=False, sort_keys=True), timestamp, timestamp),
                )
                changed = True
            else:
                old_measurement = _json(old_row['measurement_json'], {})
                if old_measurement != measurement:
                    correction_posted = True
                    affected_runs.add(run_id)
                    db.execute(
                        'UPDATE retest_reports SET measurement_json=?,record_id=?,updated=? WHERE id=?',
                        (json.dumps(measurement, ensure_ascii=False, sort_keys=True),
                         record_id, timestamp, old_row['id']),
                    )
                    db.execute(
                        '''INSERT INTO measurement_corrections
                           (correction_id,run_id,sample_key,report_id,target,payload_json,created)
                           VALUES(?,?,?,?,?,?,?)''',
                        (str(_value(body, 'correction_id') or 'RESUBMISSION'), run_id, key,
                         old_row['id'], record_id,
                         json.dumps({'source': 'retest_resubmission', 'body': body},
                                    ensure_ascii=False, default=str), timestamp),
                    )
                    changed = True
            _ensure_review_row(db, run_id, key, case, sample)

        if changed:
            for key, case, sample in identities:
                db.execute(
                    '''UPDATE sample_reviews
                       SET revision=revision+1,basis_revision=basis_revision+1,updated=?
                       WHERE run_id=? AND sample_key=?''',
                    (now(), run_id, key),
                )
        if correction_posted:
            for changed_run in affected_runs:
                db.execute('UPDATE runs SET revision=revision+1 WHERE id=?', (changed_run,))

    # 一次提交通常只针对一个样品；若批量跨样品，返回列表。
    keys = [item[0] for item in sorted(identities)]
    if len(keys) == 1:
        return build_board_item(store, run_id, keys[0])
    return [build_board_item(store, run_id, key) for key in keys]


submit_retest = record_retest
add_retest = record_retest


def _find_report(db, store, run_id, target, payload):
    if target is None:
        target = _value(payload, 'report_id', 'measurement_id', 'retest_report_id',
                        'retest_id', 'corrects', 'corrects_record', 'target', 'target_record',
                        'corrected_record', 'record', 'record_id')
    if target is None:
        raise DomainIssue('CORRECTION_TARGET_REQUIRED')
    text = str(target)
    if run_id:
        rows = db.execute(
            '''SELECT * FROM retest_reports
               WHERE run_id=? AND (id=? OR record_id=?)''',
            (run_id, text, text),
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM retest_reports WHERE id=? OR record_id=?', (text, text)
        ).fetchall()
    if rows:
        return rows

    # target 可能是 retest round id：需要 payload.side 指明更正哪一侧。
    side = canonical_side(_value(payload, 'side', 'kind', 'position'))
    if run_id and side:
        _, key, _, _ = resolve_identity(store, run_id, payload)
        rows = db.execute(
            'SELECT * FROM retest_reports WHERE run_id=? AND sample_key=? AND round_id=? AND side=?',
            (run_id, key, text, side),
        ).fetchall()
    elif run_id:
        _, key, _, _ = resolve_identity(store, run_id, payload)
        rows = db.execute(
            'SELECT * FROM retest_reports WHERE run_id=? AND sample_key=? AND round_id=?',
            (run_id, key, text),
        ).fetchall()
    if not rows:
        raise DomainIssue('MEASUREMENT_NOT_FOUND')
    return rows


def _correction_patch(payload):
    changes = _value(payload, 'changes', 'patch', 'correction_fields')
    if not isinstance(changes, dict):
        changes = {}
    # 顶层已知测量字段也视为更正内容。
    merged = dict(changes)
    for key in ('concentration', 'result', 'measured', 'reading', 'value',
                'lod', 'detection_limit', 'loq', 'quantification_limit',
                'limit', 'rpd_limit_bp', 'absolute_limit', 'record', 'record_id'):
        if payload.get(key) is not None:
            merged[key] = payload[key]
    return merged


def correct_retest(store, *args, **kwargs):
    payload = kwargs.pop('values', None)
    run_id = kwargs.pop('run_id', None)
    target = kwargs.pop('retest_id', None) or kwargs.pop('measurement_id', None) or kwargs.pop('target', None)
    positional = list(args)
    if positional and isinstance(positional[-1], (dict, list)):
        payload = positional.pop() if payload is None else {**payload, **positional.pop()}
    if not isinstance(payload, dict):
        payload = dict(kwargs)
    else:
        for key, value in kwargs.items():
            if key not in ('run_id', 'retest_id', 'measurement_id', 'target', 'values'):
                payload.setdefault(key, value)

    if positional:
        if len(positional) >= 2 and run_id is None:
            run_id, target = positional[0], target or positional[1]
        elif len(positional) == 1:
            value = positional[0]
            if isinstance(value, str):
                if run_id is None and payload.get('sample') is not None:
                    run_id = value
                else:
                    target = target or value
    run_id = run_id or payload.get('run_id')
    if isinstance(payload, list):
        results = [correct_retest(store, run_id, item) for item in payload]
        return results

    patch = _correction_patch(payload)
    if not patch:
        raise DomainIssue('CORRECTION_FIELDS')

    affected = []
    with store.connect() as db:
        ensure_schema(db)
        rows = _find_report(db, store, run_id, target, payload)
        timestamp = now()
        for row in rows:
            old = _json(row['measurement_json'], {})
            new = canonical_measurement({**old, **patch})
            # 更正不应因载荷省略而丢失身份。
            new['side'] = row['side']
            if not new.get('record'):
                new['record'] = row['record_id'] or row['id']
            if old == new:
                continue
            db.execute(
                'UPDATE retest_reports SET measurement_json=?,record_id=?,updated=? WHERE id=?',
                (json.dumps(new, ensure_ascii=False, sort_keys=True),
                 new.get('record') or row['record_id'], timestamp, row['id']),
            )
            correction_id = str(_value(payload, 'correction_id', 'id') or
                                 f'C{row["id"]}-{timestamp}')
            db.execute(
                '''INSERT INTO measurement_corrections
                   (correction_id,run_id,sample_key,report_id,target,payload_json,created)
                   VALUES(?,?,?,?,?,?,?)''',
                (correction_id, row['run_id'], row['sample_key'], row['id'],
                 row['record_id'], json.dumps({'patch': patch, 'before': old, 'after': new},
                                              ensure_ascii=False, sort_keys=True, default=str), timestamp),
            )
            _ensure_review_row(db, row['run_id'], row['sample_key'],
                               row['sample_key'].split(':', 1)[0],
                               row['sample_key'].split(':', 1)[-1])
            db.execute(
                '''UPDATE sample_reviews
                   SET revision=revision+1,basis_revision=basis_revision+1,updated=?
                   WHERE run_id=? AND sample_key=?''',
                (timestamp, row['run_id'], row['sample_key']),
            )
            affected.append((row['run_id'], row['sample_key']))

        if affected:
            for corrected_run in {run_id for run_id, _ in affected}:
                db.execute('UPDATE runs SET revision=revision+1 WHERE id=?', (corrected_run,))

    if not affected and rows:
        # 幂等无变化：直接返回当前结论。
        first = rows[0]
        return build_board_item(store, first['run_id'], first['sample_key'])
    if len(affected) == 1:
        return build_board_item(store, *affected[0])
    return [build_board_item(store, run_id, key) for run_id, key in affected]


correct_measurement = correct_retest
post_correction = correct_retest


def invalidate_original(store, run_id=None, sample_ref=None, values=None, **kwargs):
    if isinstance(run_id, dict):
        values, run_id = run_id, None
    if isinstance(sample_ref, dict) and values is None:
        values, sample_ref = sample_ref, None
    values = values if isinstance(values, dict) else {}
    values = {**kwargs, **values}
    run_id = run_id or values.get('run_id')
    _, key, case, sample = resolve_identity(store, run_id, values, sample_ref)
    evidence = invalidation_evidence(values.get('invalidation') or values.get('voidance') or values)
    if not evidence:
        raise DomainIssue('ORIGINAL_VOID_EVIDENCE_REQUIRED')
    evidence.setdefault('record', _value(values, 'original_record', 'original_record_id'))
    timestamp = now()
    with store.connect() as db:
        ensure_schema(db)
        _ensure_review_row(db, run_id, key, case, sample)
        db.execute(
            '''UPDATE sample_reviews
               SET invalidation_json=?, revision=revision+1, updated=?
               WHERE run_id=? AND sample_key=?''',
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), timestamp, run_id, key),
        )
    return build_board_item(store, run_id, key)


record_invalidation = invalidate_original
add_void_evidence = invalidate_original


def _normalize_status(value, default='APPROVED'):
    text = str(value or default).strip().upper().replace('-', '_')
    if text in ('APPROVED', 'APPROVE', 'RELEASED', 'RELEASE', 'ISSUED', 'ISSUE', 'PASS', 'PASSED'):
        return 'APPROVED'
    if text in ('HELD', 'HOLD', 'PENDING_HOLD', 'SUSPENDED'):
        return 'HELD'
    return text


def release_sample(store, *args, **kwargs):
    values = kwargs.pop('values', None)
    run_id = kwargs.pop('run_id', None)
    sample_ref = kwargs.pop('sample_ref', None) or kwargs.pop('ref', None)
    positional = list(args)
    if positional and isinstance(positional[-1], dict):
        values = positional.pop() if values is None else {**values, **positional.pop()}
    values = values if isinstance(values, dict) else {}
    for key, value in kwargs.items():
        values.setdefault(key, value)

    if positional:
        if len(positional) >= 2 and run_id is None:
            run_id, sample_ref = positional[0], sample_ref or positional[1]
            if len(positional) >= 3 and isinstance(positional[2], int):
                values.setdefault('revision', positional[2])
        elif len(positional) == 1:
            value = positional[0]
            if isinstance(value, str):
                identity_in_payload = _value(values, 'sample', 'sample_id', 'sample_key', 'key', 'case')
                if run_id is None and identity_in_payload is not None:
                    run_id = value
                elif run_id is not None:
                    sample_ref = sample_ref or value
                else:
                    try:
                        store.load(value)
                        run_id = value
                    except DomainIssue:
                        sample_ref = value
            elif isinstance(value, int):
                values.setdefault('revision', value)
    run_id = run_id or values.get('run_id')
    if not run_id:
        raise DomainIssue('RUN_NOT_FOUND')

    _, key, case, sample = resolve_identity(store, run_id, values, sample_ref)
    status = _normalize_status(_value(values, 'status', 'decision', 'outcome'), 'APPROVED')
    if status not in ('APPROVED', 'HELD'):
        raise DomainIssue('REVIEW_FIELDS')
    note = values.get('note', '')
    if note is None:
        note = ''
    if not isinstance(note, str) or len(note) > 200:
        raise DomainIssue('REVIEW_FIELDS')
    note = note.strip()

    expected_revision = _value(values, 'revision', 'expected_revision', 'lock_revision')
    expected_basis = _value(values, 'basis_revision', 'expected_basis_revision')
    expected_run_revision = _value(values, 'run_revision', 'expected_run_revision')
    if expected_revision is not None and type(expected_revision) is not int:
        raise DomainIssue('REVIEW_FIELDS')
    if expected_basis is not None and type(expected_basis) is not int:
        raise DomainIssue('REVIEW_FIELDS')
    if expected_run_revision is not None and type(expected_run_revision) is not int:
        raise DomainIssue('REVIEW_FIELDS')

    timestamp = now()
    run_row = store.load(run_id)
    with store.connect() as db:
        ensure_schema(db)
        _ensure_review_row(db, run_id, key, case, sample)
        row = db.execute(
            'SELECT * FROM sample_reviews WHERE run_id=? AND sample_key=?', (run_id, key)
        ).fetchone()
        current_revision = row['revision']
        # revision 是样品工作台版本；run_revision 仅在调用方明确持有时校验批次版本。
        if expected_revision is not None and expected_revision != current_revision:
            raise DomainIssue('STALE_REVIEW')
        if expected_run_revision is not None and expected_run_revision != run_row['revision']:
            raise DomainIssue('STALE_REVIEW')
        if expected_basis is not None and expected_basis != row['basis_revision']:
            raise DomainIssue('STALE_REVIEW')
        if row['status'] == 'APPROVED':
            raise DomainIssue('RUN_FINALIZED')

        board_item = build_board_item(store, run_id, key, db=db)
        if status == 'APPROVED' and not board_item['eligible']:
            aliases = ['UNRESOLVED_ISSUES', 'SAMPLE_HELD', *board_item.get('reason_codes', [])]
            reason = board_item.get('reason') or 'UNRESOLVED_ISSUES'
            if 'INCOMPLETE' in reason or 'MISSING' in reason:
                aliases.extend(['SAMPLE_PAIR_MISSING', 'RETEST_SIDE_MISSING'])
            if 'METHOD' in reason:
                aliases.append('METHOD_MISMATCH')
            if 'VOID' in reason:
                aliases.extend(['VOID_EVIDENCE_REQUIRED', 'ORIGINAL_VOID_REQUIRED',
                                'ORIGINAL_INVALIDATION_REQUIRED'])
            if reason == 'QUALITY_LIMIT':
                aliases.append('QUALITY_LIMIT')
            raise DomainIssue(reason, aliases=aliases)

        adopted = board_item.get('adopted')
        cursor = db.execute(
            '''UPDATE sample_reviews
               SET status=?, revision=revision+1, eligible=?, reason=?, basis_digest=?,
                   board_json=?, adopted_json=?, invalidation_json=COALESCE(?, invalidation_json),
                   note=?, reviewed=?, updated=?
               WHERE run_id=? AND sample_key=? AND revision=?''',
            (status, 1 if board_item['eligible'] else 0, board_item['reason'],
             board_item['basis_digest'],
             json.dumps(board_item, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(adopted, ensure_ascii=False, sort_keys=True, default=str) if adopted else None,
             json.dumps(board_item.get('invalidation'), ensure_ascii=False, sort_keys=True)
             if board_item.get('invalidation') else None,
             note, timestamp if status == 'APPROVED' else '', timestamp,
             run_id, key, current_revision),
        )
        if cursor.rowcount != 1:
            raise DomainIssue('STALE_REVIEW')

    return build_board_item(store, run_id, key)


decide_sample = release_sample
review_sample = release_sample
issue_sample = release_sample
approve_sample = release_sample


def release_case(store, run_id, case_ref, values=None):
    """签发一个业务组内当前合格的样品；组内不合格样品保留暂缓。"""
    items = sample_board(store, run_id)
    refs = [item['key'] for item in items
            if str(item.get('case')) == str(case_ref) and item.get('eligible')
            and item.get('status') != 'APPROVED']
    if not refs:
        raise DomainIssue('UNRESOLVED_ISSUES', aliases=['SAMPLE_PAIR_MISSING', 'QUALITY_LIMIT'])
    return release_samples(store, run_id, refs, values or {})


decide_case = release_case


def release_samples(store, run_id, sample_refs=None, values=None):
    """批量签发选定样品；各样品使用独立乐观锁，不因同批其他样品暂缓而失败。"""
    values = dict(values) if isinstance(values, dict) else {}
    if sample_refs is None:
        sample_refs = values.pop('samples', None) or values.pop('sample_keys', None) \
            or values.pop('sample_ids', None) or values.pop('sample_refs', None) or []
    if isinstance(sample_refs, str):
        sample_refs = [part.strip() for part in re.split(r'[,，]', sample_refs) if part.strip()]
    if not isinstance(sample_refs, (list, tuple)) or not sample_refs:
        raise DomainIssue('SAMPLE_IDENTIFIER_REQUIRED')

    results, errors = [], []
    for ref in sample_refs:
        item_values = dict(values)
        if isinstance(ref, dict):
            item_values.update(ref)
            ref = item_values.pop('sample_ref', None) or item_values.pop('key', None) \
                or item_values.pop('sample_key', None)
        try:
            results.append(release_sample(store, run_id, ref, item_values))
        except DomainIssue as error:
            errors.append({'sample': str(ref), 'code': error.code, 'aliases': list(error.aliases)})
    if errors and not results:
        raise DomainIssue(errors[0]['code'], aliases=[alias for e in errors for alias in e['aliases']])
    run = store.load(run_id)
    run['samples'] = results
    run['items'] = results
    run['sample_errors'] = errors
    run['partial'] = bool(errors)
    return run
