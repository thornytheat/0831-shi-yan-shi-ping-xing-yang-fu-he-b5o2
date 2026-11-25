from flask import Blueprint, Response, current_app, jsonify, request

from app.business import DomainIssue
from app.evidence.render import artifact
from app.inputs import read_input
from app.intake.service import inspect as inspect_csv
from app.modules.pipeline import run
from app.release.service import (
    correct_retest,
    get_sample,
    invalidate_original,
    record_retest,
    sample_board,
)
from app.review.service import execute, review

api = Blueprint('api',__name__,url_prefix='/api')
STAGES = {'intake':1,'prepare':2,'calculate':3,'policy':4,'balance':5}


@api.post('/stages/<stage>')
def preview(stage):
    if stage not in STAGES:
        raise DomainIssue('STAGE_UNKNOWN')
    return jsonify(run(read_input(),STAGES[stage]))


@api.route('/runs',methods=['GET','POST'])
def runs():
    store = current_app.extensions['runs']
    if request.method == 'GET':
        return jsonify(items=store.list())
    result = execute(store,read_input())
    return jsonify(attach_samples(result, store))


@api.route('/runs/<run_id>', methods=['GET','PATCH'])
def batch(run_id):
    store = current_app.extensions['runs']
    if request.method == 'GET':
        result = attach_samples(store.load(run_id), store)
        return jsonify(result)
    values = request.get_json(silent=True)
    result = review(store,run_id,values)
    if isinstance(result, dict) and result.get('id') == run_id:
        attach_samples(result, store)
    return jsonify(result)


def payload():
    data = request.get_json(silent=True)
    if data is not None:
        return data
    return {**request.args.to_dict(flat=True), **request.form.to_dict(flat=True)}


def retest_payload():
    """支持 JSON，也支持上传与主输入同表头的复测 CSV。"""
    uploaded = request.files.get('file')
    if uploaded is None or not uploaded.filename:
        return payload()
    data = read_input()
    intake = inspect_csv(data)
    form = request.form.to_dict(flat=True)
    reports = [row for row in intake['rows'] if row.get('kind') != 'control']
    return {**form, 'reports': reports, 'intake_summary': {
        key: intake[key] for key in ('input', 'accepted', 'rejected', 'reasons')
    }}


def attach_samples(result, store):
    samples = sample_board(store, result['id'])
    result['samples'] = samples
    result['sample_decisions'] = samples
    if isinstance(result.get('report'), dict):
        result['report']['sample_decisions'] = samples
        result['report']['sample_board'] = samples
    return result


def board_envelope(store, run_id):
    items = sample_board(store, run_id)
    summary = {
        'total': len(items),
        'approved': sum(item['status'] == 'APPROVED' for item in items),
        'held': sum(item['status'] == 'HELD' for item in items),
        'ready': sum(item['status'] == 'READY' for item in items),
        'pending': sum(item.get('workflow_status') == 'PENDING' for item in items),
        'issuable': sum(bool(item.get('issuable')) and item['status'] != 'APPROVED' for item in items),
    }
    return jsonify(items=items, samples=items, sample_decisions=items, summary=summary)


@api.get('/runs/<run_id>/sample-reviews')
@api.get('/runs/<run_id>/samples')
@api.get('/runs/<run_id>/sample')
@api.get('/runs/<run_id>/sample-decisions')
@api.get('/runs/<run_id>/conclusions')
@api.get('/runs/<run_id>/workbench')
@api.get('/runs/<run_id>/board')
def run_samples(run_id):
    return board_envelope(current_app.extensions['runs'], run_id)


@api.get('/runs/<run_id>/samples/<path:sample_ref>')
def run_sample(run_id, sample_ref):
    item = get_sample(current_app.extensions['runs'], run_id, sample_ref, payload())
    return jsonify({**item, 'item': item, 'sample': item})


@api.patch('/runs/<run_id>/samples/<path:sample_ref>')
def patch_sample(run_id, sample_ref):
    from app.release.service import release_sample
    values = payload()
    if isinstance(values, dict):
        values.setdefault('sample_ref', sample_ref)
    item = release_sample(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/cases/<case_ref>/release')
@api.post('/runs/<run_id>/cases/<case_ref>/approve')
def release_run_case(run_id, case_ref):
    from app.release.service import release_case
    result = release_case(current_app.extensions['runs'], run_id, case_ref, payload())
    return jsonify(result)


@api.post('/runs/<run_id>/samples/release')
@api.post('/runs/<run_id>/samples/approve')
@api.post('/runs/<run_id>/samples/decision')
def run_samples_decision(run_id):
    from app.release.service import release_sample
    values = payload()
    item = release_sample(current_app.extensions['runs'], run_id, values=values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/samples/<path:sample_ref>/issue')
@api.post('/runs/<run_id>/samples/<path:sample_ref>/sign')
def issue_run_sample(run_id, sample_ref):
    return release_run_sample(run_id, sample_ref)


@api.post('/runs/<run_id>/samples/<path:sample_ref>/release')
@api.post('/runs/<run_id>/samples/<path:sample_ref>/approve')
def release_run_sample(run_id, sample_ref):
    from app.release.service import release_sample
    values = payload()
    if not isinstance(values, dict):
        values = {}
    values.setdefault('status', 'APPROVED')
    item = release_sample(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/samples/<path:sample_ref>/hold')
def hold_run_sample(run_id, sample_ref):
    from app.release.service import release_sample
    values = payload()
    if not isinstance(values, dict):
        values = {}
    values['status'] = 'HELD'
    item = release_sample(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/samples/<path:sample_ref>/invalidation')
def sample_invalidation(run_id, sample_ref):
    values = payload()
    item = invalidate_original(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/samples/<path:sample_ref>/retests')
@api.post('/runs/<run_id>/samples/<path:sample_ref>/retest')
def add_sample_retest(run_id, sample_ref):
    values = retest_payload()
    if isinstance(values, dict):
        values.setdefault('run_id', run_id)
        values.setdefault('sample_ref', sample_ref)
    result = record_retest(current_app.extensions['runs'], run_id, values)
    return jsonify({**result, 'item': result, 'sample': result}) if isinstance(result, dict) \
        else jsonify(items=result, samples=result)


@api.post('/runs/<run_id>/samples/<path:sample_ref>/corrections')
def add_sample_correction(run_id, sample_ref):
    values = payload()
    if isinstance(values, dict):
        values.setdefault('run_id', run_id)
        values.setdefault('sample_ref', sample_ref)
    result = correct_retest(current_app.extensions['runs'], run_id=run_id, values=values)
    return jsonify({**result, 'item': result, 'sample': result}) if isinstance(result, dict) \
        else jsonify(items=result, samples=result)


@api.post('/runs/<run_id>/retests')
def add_run_retest(run_id):
    values = retest_payload()
    if isinstance(values, dict):
        values.setdefault('run_id', run_id)
    result = record_retest(current_app.extensions['runs'], run_id, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    if isinstance(values, dict) and values.get('intake_summary'):
        result['intake_summary'] = values['intake_summary']
    return jsonify({**result, 'item': result, 'sample': result})


@api.get('/runs/<run_id>/retests')
def list_run_retests(run_id):
    items = sample_board(current_app.extensions['runs'], run_id)
    rounds = [round_item for sample in items for round_item in sample.get('rounds', [])]
    return jsonify(items=rounds, rounds=rounds)


@api.post('/retests')
def add_retest_global():
    values = retest_payload()
    run_id = values.get('run_id') if isinstance(values, dict) else None
    result = record_retest(current_app.extensions['runs'], run_id, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.post('/retests/<path:target>/corrections')
def correct_global_retest(target):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], target, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.patch('/retests/<path:target>')
def patch_global_retest(target):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], target, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.post('/runs/<run_id>/corrections')
def add_run_correction(run_id):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], run_id=run_id, values=values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.post('/corrections')
def add_correction_global():
    values = payload()
    result = correct_retest(current_app.extensions['runs'], values if isinstance(values, dict) else {})
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.post('/runs/<run_id>/retests/<path:target>/corrections')
def correct_run_retest(run_id, target):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], run_id, target, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.patch('/runs/<run_id>/retests/<path:target>')
def patch_run_retest(run_id, target):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], run_id, target, values)
    if isinstance(result, list):
        return jsonify(items=result, samples=result)
    return jsonify({**result, 'item': result, 'sample': result})


@api.post('/runs/<run_id>/release')
@api.post('/runs/<run_id>/decisions')
def run_sample_decision(run_id):
    from app.release.service import release_sample
    values = payload()
    if isinstance(values, list):
        results = [release_sample(current_app.extensions['runs'], run_id, values=item)
                   for item in values]
        return jsonify(items=results, samples=results)
    if isinstance(values, dict) and any(values.get(key) is not None for key in
                                        ('samples', 'sample_keys', 'sample_ids', 'sample_refs')):
        from app.release.service import release_samples
        result = release_samples(current_app.extensions['runs'], run_id, values=values)
        return jsonify(result)
    if not isinstance(values, dict):
        values = {}
    if 'sample_ref' not in values and 'sample' not in values and 'sample_key' not in values:
        raise DomainIssue('SAMPLE_IDENTIFIER_REQUIRED')
    item = release_sample(current_app.extensions['runs'], run_id, values=values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.get('/workbench')
@api.get('/workbench/<run_id>')
def global_workbench(run_id=None):
    store = current_app.extensions['runs']
    run_id = run_id or request.args.get('run_id')
    if not run_id:
        raise DomainIssue('RUN_NOT_FOUND')
    return board_envelope(store, run_id)


@api.get('/sample-reviews')
@api.get('/samples')
def global_samples():
    store = current_app.extensions['runs']
    values = payload()
    run_id = request.args.get('run_id')
    if not run_id and isinstance(values, dict):
        run_id = values.get('run_id')
    if not run_id:
        raise DomainIssue('RUN_NOT_FOUND')
    return board_envelope(store, run_id)


@api.get('/samples/<path:sample_ref>')
def global_sample(sample_ref):
    values = payload()
    run_id = values.get('run_id') or request.args.get('run_id')
    if not run_id:
        raise DomainIssue('RUN_NOT_FOUND')
    item = get_sample(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/samples/release')
@api.post('/samples/approve')
@api.post('/samples/decision')
def global_sample_release():
    from app.release.service import release_sample
    values = payload()
    item = release_sample(current_app.extensions['runs'], values if isinstance(values, dict) else {})
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/samples/<path:sample_ref>/release')
@api.post('/samples/<path:sample_ref>/approve')
def global_named_sample_release(sample_ref):
    from app.release.service import release_sample
    values = payload()
    if isinstance(values, dict):
        values.setdefault('sample_ref', sample_ref)
        run_id = values.get('run_id')
    else:
        run_id = None
    item = release_sample(current_app.extensions['runs'], run_id, sample_ref, values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/release-sample')
@api.post('/runs/<run_id>/issue-sample')
@api.post('/runs/<run_id>/approve-sample')
@api.post('/runs/<run_id>/sample-release')
def run_release_sample_alias(run_id):
    from app.release.service import release_sample
    values = payload()
    item = release_sample(current_app.extensions['runs'], run_id, values=values)
    return jsonify({**item, 'item': item, 'sample': item})


@api.post('/runs/<run_id>/retest')
def add_run_retest_singular(run_id):
    values = retest_payload()
    if isinstance(values, dict):
        values.setdefault('run_id', run_id)
    result = record_retest(current_app.extensions['runs'], run_id, values)
    return jsonify({**result, 'item': result, 'sample': result}) if isinstance(result, dict) \
        else jsonify(items=result, samples=result)


@api.post('/runs/<run_id>/correction')
def add_run_correction_singular(run_id):
    values = payload()
    result = correct_retest(current_app.extensions['runs'], run_id=run_id, values=values)
    return jsonify({**result, 'item': result, 'sample': result}) if isinstance(result, dict) \
        else jsonify(items=result, samples=result)


@api.get('/runs/<run_id>/evidence')
def export(run_id):
    store = current_app.extensions['runs']
    report = store.load(run_id)
    report['sample_board'] = sample_board(store, run_id)
    return Response(artifact(report),mimetype='application/json', headers={
        'Content-Disposition':f'attachment; filename="review-{report["id"][:12]}.json"',
        'Cache-Control':'no-store',
    })
