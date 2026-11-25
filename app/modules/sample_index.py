from app.release.domain import canonical_measurement


def measurement_view(row):
    view = canonical_measurement({
        'record': row.get('record'),
        'side': row.get('kind'),
        'concentration': row.get('concentration'),
        'lod': row.get('lod'),
        'loq': row.get('loq'),
        'limit': row.get('limit'),
        'note': row.get('note'),
    }, row.get('kind'))
    # 保留业务字段字典里的原始键，方便证据导出。
    view['sample'] = str(row['sample']) if row.get('sample') is not None else None
    return view


def build_sample_index(rows):
    groups = {}
    controls = {}
    for row in rows:
        case = str(row.get('case'))
        if row.get('kind') == 'control':
            controls[case] = measurement_view(row)
            continue

        sample = str(row.get('sample'))
        key = f'{case}:{sample}'
        item = groups.setdefault(key, {
            'key': key,
            'case': case,
            'case_id': case,
            'sample': sample,
            'sample_id': sample,
            'measurements': [],
            'control': controls.get(case),
            'record_ids': [],
        })
        item['record_ids'].append(row['record'])
        item['measurements'].append(measurement_view(row))

    for key, item in groups.items():
        item['control'] = controls.get(item['case'])

    def sort_key(item):
        try:
            sample_number = int(item['sample'])
        except (TypeError, ValueError):
            sample_number = 10**18
        return item['case'], sample_number, item['sample']

    return [groups[key] for key in sorted(groups, key=lambda k: sort_key(groups[k]))]
