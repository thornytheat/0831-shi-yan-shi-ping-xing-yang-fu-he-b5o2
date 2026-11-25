import json


def artifact(run):
    packet = {
        'schema': 'review-evidence/1',
        'manifest': run['report']['manifest'],
        'balance': run['report']['balance'],
        'cases': run['report']['cases'],
        'samples': run['report'].get('samples', []),
        'intake': run['report']['intake'],
        'review': {key:run[key] for key in ('status','revision','note','reviewed')},
    }
    if run.get('sample_board') is not None:
        packet['sample_reviews'] = run['sample_board']
    return (json.dumps(packet, ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
