from app.business import DomainIssue, prepared, single


def prepare(rows):
    control = single(rows,'control')
    pairs = {}
    for row in rows:
        kind = row['kind']
        if kind=='control':
            continue
        bundle = pairs.setdefault(row['sample'],{})
        if kind in bundle or not 0 < row['lod'] < row['loq']:
            raise DomainIssue('INVALID_SAMPLE')
        bundle[kind] = row
    if not pairs or any(len(pair)!=2 for pair in pairs.values()):
        raise DomainIssue('SAMPLE_PAIR_MISSING')
    if any(len({(r['lod'],r['loq']) for r in pair.values()})!=1 for pair in pairs.values()):
        raise DomainIssue('METHOD_MISMATCH')
    if control['limit']>20000:
        raise DomainIssue('INVALID_POLICY')
    return prepared(len(pairs),{'rpd_limit_bp':control['limit'],'absolute_limit':control['loq']},(pairs,control))
