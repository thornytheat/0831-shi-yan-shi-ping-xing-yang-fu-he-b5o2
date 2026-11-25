from app.business import DomainIssue
from app.intake.service import inspect
from app.modules.calculate import compute
from app.modules.grouping import case_groups
from app.modules.policy import policy
from app.modules.prepare import prepare
from app.modules.reconcile import balance
from app.modules.sample_index import build_sample_index


def evaluate(rows, stage):
    case = {'case':rows[0]['case'],'records':[r['record'] for r in rows], 'status':'READY'}
    try:
        case['prep'], payload = prepare(rows)
        if stage >= 3:
            case['measure'] = compute(payload)
        if stage >= 4:
            case.update(policy(case['measure']))
    except DomainIssue as error:
        case['status'] = error.code
    return case


def run(data, stage=5):
    intake = inspect(data)
    output = {'intake': {k:v for k,v in intake.items() if k!='rows'}}
    if stage == 1:
        return output
    cases = [evaluate(rows, stage) for _, rows in case_groups(intake['rows'])]
    output['cases'] = cases
    sample_index = build_sample_index(intake['rows'])
    output['samples'] = sample_index
    output['sample_index'] = sample_index
    samples_by_case = {}
    for sample in sample_index:
        samples_by_case.setdefault(sample['case'], []).append(sample)
    for case in cases:
        case['samples'] = samples_by_case.get(case['case'], [])
    if stage >= 5:
        output['balance'] = balance(intake,cases)
    return output
