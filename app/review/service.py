from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

from app.business import DomainIssue
from app.config import RULE_VERSION
from app.modules.pipeline import run
from app.release.service import (
    board as sample_board,
    correct_retest,
    get_sample,
    invalidate_original,
    record_retest,
    release_case,
    release_sample,
    release_samples,
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def execute(store, data):
    start = monotonic()
    fingerprint = sha256(data).hexdigest()
    run_id = sha256(f'{RULE_VERSION}:{fingerprint}'.encode()).hexdigest()
    try:
        return store.load(run_id)
    except DomainIssue as error:
        if error.code != 'RUN_NOT_FOUND':
            raise
    report = run(data)
    report['manifest'] = dict(sha256=fingerprint, version=RULE_VERSION, created=now(),
                              duration_ms=round((monotonic()-start)*1000,3), bytes=len(data), run_id=run_id)
    return store.register(run_id,report)


def _is_sample_review(values):
    has_sample = any(values.get(key) not in (None, '') for key in
                     ('sample', 'sample_id', 'sample_key', 'sample_ref', 'key'))
    action = str(values.get('action', '')).lower()
    return has_sample and (
        values.get('status') in ('APPROVED', 'HELD', 'RELEASED', 'ISSUED')
        or values.get('decision') in ('APPROVED', 'HELD', 'RELEASED', 'ISSUED')
        or action in ('release', 'approve', 'hold', 'issue_sample', 'release_sample')
    )


def _dispatch_sample_action(store, run_id, values):
    action = str(values.get('action', '')).lower()
    if action in ('retest', 'record_retest', 'submit_retest') or any(
        key in values for key in ('retest', 'retests', 'reports')
    ):
        payload = values.get('retest') or values.get('retests') or values.get('reports') or values
        if isinstance(payload, dict):
            payload.setdefault('run_id', run_id)
        return record_retest(store, run_id, payload)
    if action in ('correction', 'correct_retest') or any(
        key in values for key in ('correction', 'corrections', 'patch')
    ):
        payload = values.get('correction') or values.get('corrections') or values
        return correct_retest(store, run_id=run_id, values=payload)
    if action in ('invalidation', 'void', 'void_evidence') or any(
        key in values for key in ('invalidation', 'voidance', 'void_evidence')
    ):
        payload = values.get('invalidation') or values.get('voidance') or values
        return invalidate_original(store, run_id, None, payload if isinstance(payload, dict) else values)
    return release_sample(store, run_id, values=values)


def review(store, run_id, values):
    if not isinstance(values, dict):
        raise DomainIssue('REVIEW_FIELDS')
    if values.get('action') or any(key in values for key in ('retest', 'retests', 'correction', 'corrections', 'invalidation', 'voidance')):
        return _dispatch_sample_action(store, run_id, values)
    if any(values.get(key) is not None for key in ('samples', 'sample_keys', 'sample_ids', 'sample_refs')):
        sample_values = dict(values)
        if 'revision' in sample_values:
            sample_values.setdefault('run_revision', sample_values['revision'])
            sample_values.pop('revision', None)
        return release_samples(store, run_id, values=sample_values)
    if values.get('case') and not any(values.get(key) for key in ('sample', 'sample_id', 'sample_key')) \
            and values.get('status') in ('APPROVED', 'HELD', 'RELEASED', 'ISSUED'):
        case_values = dict(values)
        if 'revision' in case_values:
            case_values.setdefault('run_revision', case_values['revision'])
            case_values.pop('revision', None)
        return release_case(store, run_id, values['case'], case_values)
    if _is_sample_review(values):
        sample_values = dict(values)
        # 通用 PATCH /runs/<id> 的 revision 按既有约定是批次版本；
        # 专用样品端点中的 revision 才是样品工作台版本。
        if 'revision' in sample_values:
            sample_values.setdefault('run_revision', sample_values['revision'])
            sample_values.pop('revision', None)
        return release_sample(store, run_id, values=sample_values)
    revision, status, note = (values.get(key) for key in ('revision','status','note'))
    if type(revision) is not int or revision < 0 or status not in ('APPROVED','HELD') or not isinstance(note,str) or not (3<=len(note.strip())<=200):
        raise DomainIssue('REVIEW_FIELDS')
    current = store.load(run_id)
    if revision != current['revision']:
        raise DomainIssue('STALE_REVIEW')
    if current['status'] == 'APPROVED':
        raise DomainIssue('RUN_FINALIZED')
    if status == 'APPROVED' and not current['report']['balance']['releasable']:
        raise DomainIssue('UNRESOLVED_ISSUES')
    return store.revise(run_id,revision,status,note.strip(),now())
