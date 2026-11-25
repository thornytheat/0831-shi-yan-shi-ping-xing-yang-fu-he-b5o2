"""样品级签发规则。

这里不接触 HTTP/SQLite，便于用任意字典数据复核：
原测必须自身成对判断；复测只能在同一个 round/retest_id 内成对，
绝不把一侧复测与另一侧原测或另一次复测拼接。
"""
from __future__ import annotations

import re
import unicodedata
from hashlib import sha256
import json

from app.business import DomainIssue
from app.intake.numeric import MISSING

SAMPLE_STATUSES = {'PENDING', 'APPROVED', 'HELD'}

CONCENTRATION_KEYS = ('concentration', 'result', 'measured', 'reading', 'value')
LOD_KEYS = ('lod', 'detection_limit', 'detect_limit')
LOQ_KEYS = ('loq', 'quantification_limit', 'quantitation_limit')
LIMIT_KEYS = ('limit', 'rpd_limit_bp', 'rpd_limit', 'control_limit')
ABSOLUTE_LIMIT_KEYS = ('absolute_limit', 'abs_limit', 'absolute_tolerance')
RECORD_KEYS = ('record', 'record_id', 'record_key', 'source_record', 'id', 'key')
SIDE_KEYS = ('side', 'kind', 'position', 'arm', 'report_side', 'report_type', 'sample_side')

PRIMARY_SIDE_NAMES = {'primary', 'p', 'a', 'left', 'first', 'side_a', '1'}
DUPLICATE_SIDE_NAMES = {'duplicate', 'd', 'b', 'right', 'second', 'side_b', '2'}

MEASUREMENT_FIELDS = ('concentration', 'lod', 'loq', 'limit', 'absolute_limit')


def as_int(value):
    """接受整数、全角数字和完整千分位；无效值返回 None。"""
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    text = unicodedata.normalize('NFKC', str(value)).strip()
    if not text or text.upper() in MISSING:
        return None
    if not re.fullmatch(r'(?:[0-9]{1,12}|[0-9]{1,3}(?:,[0-9]{3}){1,3})', text):
        return None
    return int(text.replace(',', ''))


def first_value(data, keys, default=None):
    for key in keys:
        if isinstance(data, dict) and key in data and data[key] not in (None, ''):
            return data[key]
    return default


def canonical_side(value):
    if value is None:
        return None
    text = str(value).strip().lower().replace('-', '_').replace(' ', '_')
    if text.startswith('retest_'):
        text = text[7:]
    if text in PRIMARY_SIDE_NAMES:
        return 'primary'
    if text in DUPLICATE_SIDE_NAMES:
        return 'duplicate'
    return text or None


def canonical_measurement(raw, default_side=None):
    if raw is None:
        return None
    if isinstance(raw, (str, int, float)) and not isinstance(raw, dict):
        number = as_int(raw)
        result = {'concentration': number if number is not None else None,
                  'lod': None, 'loq': None, 'limit': None,
                  'absolute_limit': None, 'record': str(raw), 'side': default_side}
        if number is not None:
            result['record'] = None
        return result
    if not isinstance(raw, dict):
        return None

    concentration = first_value(raw, CONCENTRATION_KEYS)
    lod = first_value(raw, LOD_KEYS)
    loq = first_value(raw, LOQ_KEYS)
    limit = first_value(raw, LIMIT_KEYS)
    absolute_limit = first_value(raw, ABSOLUTE_LIMIT_KEYS)
    record = first_value(raw, RECORD_KEYS)
    side = canonical_side(first_value(raw, SIDE_KEYS, default_side))
    return {
        'record': None if record is None else str(record),
        'side': side,
        'concentration': as_int(concentration),
        'lod': as_int(lod),
        'loq': as_int(loq),
        'limit': as_int(limit),
        'absolute_limit': as_int(absolute_limit),
        'note': raw.get('note'),
    }


def _pair_from_labeled(values):
    primary_keys = ('primary', 'primary_report', 'side_a', 'a', 'left', 'first')
    duplicate_keys = ('duplicate', 'duplicate_report', 'side_b', 'b', 'right', 'second')
    primary = first_value(values, primary_keys)
    duplicate = first_value(values, duplicate_keys)
    if primary is None and duplicate is None:
        return None
    return {
        'primary': canonical_measurement(primary, 'primary'),
        'duplicate': canonical_measurement(duplicate, 'duplicate'),
    }


def normalize_pair(pair):
    if pair is None:
        return {'primary': None, 'duplicate': None}
    if isinstance(pair, dict):
        nested = first_value(pair, ('measurements', 'reports', 'sides', 'pair'))
        if nested is not None and not any(k in pair for k in ('primary', 'duplicate', 'left', 'right')):
            pair = nested
        else:
            labeled = _pair_from_labeled(pair)
            if labeled is not None:
                return labeled
    if isinstance(pair, (list, tuple)):
        items = [canonical_measurement(item) for item in pair]
        items = [item for item in items if item is not None]
        labeled = {item.get('side'): item for item in items if item.get('side') in ('primary', 'duplicate')}
        if len(labeled) == 2:
            return labeled
        return {
            'primary': items[0] if items else None,
            'duplicate': items[1] if len(items) > 1 else None,
        }
    return {'primary': None, 'duplicate': None}


def evaluate_pair(pair=None, control=None, **limits):
    """按仓库既有整数交叉积规则判断一对测量值。"""
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and limits.get('_two_measurements'):
        normalized = {'primary': canonical_measurement(pair[0], 'primary'),
                      'duplicate': canonical_measurement(pair[1], 'duplicate')}
    else:
        normalized = normalize_pair(pair)
    primary, duplicate = normalized.get('primary'), normalized.get('duplicate')
    sides = [item for item in (primary, duplicate) if item]
    record_ids = [item['record'] for item in sides if item.get('record')]

    base = {
        'pass': False,
        'status': 'HELD',
        'code': 'SAMPLE_PAIR_MISSING',
        'reason': 'SAMPLE_PAIR_MISSING',
        'mode': None,
        'value': None,
        'record_ids': record_ids,
        'measurements': normalized,
    }
    if primary is None or duplicate is None:
        base.update(code='RETEST_PAIR_INCOMPLETE', reason='RETEST_SIDE_MISSING',
                    status='HELD', missing_side='primary' if primary is None else 'duplicate')
        return base
    if any(item.get('concentration') is None for item in (primary, duplicate)):
        base.update(code='RETEST_PAIR_INCOMPLETE', reason='MEASUREMENT_VALUE_MISSING')
        return base

    lods = {item.get('lod') for item in (primary, duplicate)}
    loqs = {item.get('loq') for item in (primary, duplicate)}
    if None in lods or None in loqs:
        base.update(code='METHOD_MISMATCH', reason='METHOD_LIMIT_MISSING')
        return base
    if len(lods) != 1 or len(loqs) != 1:
        base.update(code='METHOD_MISMATCH', reason='METHOD_MISMATCH',
                    lods=sorted(x for x in lods if x is not None),
                    loqs=sorted(x for x in loqs if x is not None))
        return base
    lod, loq = lods.pop(), loqs.pop()
    if not (0 < lod < loq):
        base.update(code='INVALID_SAMPLE', reason='INVALID_SAMPLE')
        return base

    control = control if isinstance(control, dict) else {}
    rpd_limit = as_int(first_value(limits, ('rpd_limit_bp', 'limit')))
    if rpd_limit is None:
        rpd_limit = as_int(first_value(control, LIMIT_KEYS))
    if rpd_limit is None:
        rpd_limit = 1000
    absolute_limit = as_int(first_value(limits, ABSOLUTE_LIMIT_KEYS))
    if absolute_limit is None:
        absolute_limit = as_int(first_value(control, ABSOLUTE_LIMIT_KEYS + ('loq',)))
    if absolute_limit is None:
        # 没有质控对照时不擅自放宽，返回缺少依据而不是凑出结论。
        base.update(code='LIMIT_BASIS_MISSING', reason='LIMIT_BASIS_MISSING', lod=lod, loq=loq)
        return base

    left, right = sorted((primary['concentration'], duplicate['concentration']))
    gap = right - left
    if right < lod:
        mode, value, passed = 'BOTH_ND', None, True
    elif left < lod:
        mode, value, passed = 'MIXED_LOD', None, False
    elif left >= 5 * loq:
        mode = 'RPD'
        numerator, denominator = gap * 20000, left + right
        value = numerator // denominator if denominator else 0
        passed = numerator <= rpd_limit * denominator
    else:
        mode, value, passed = 'ABSOLUTE', gap, gap <= absolute_limit

    result = {
        'pass': passed,
        'status': 'READY' if passed else 'QUALITY_LIMIT',
        'code': None if passed else 'QUALITY_LIMIT',
        'reason': None if passed else ('MIXED_LOD' if mode == 'MIXED_LOD' else 'QUALITY_LIMIT'),
        'mode': mode,
        'value': value,
        'expected': 0,
        'left': left,
        'right': right,
        'gap': gap,
        'lod': lod,
        'loq': loq,
        'rpd_limit_bp': rpd_limit,
        'absolute_limit': absolute_limit,
        'record_ids': record_ids,
        'measurements': normalized,
    }
    return result


# 兼容可能的函数命名。
evaluate_measurements = evaluate_pair
check_pair = evaluate_pair


def normalize_rounds(retests):
    """把多种复测载荷归一成 [{'round_id': ..., 'measurements': [...]}]。"""
    if not retests:
        return []
    if isinstance(retests, dict):
        if 'rounds' in retests:
            return normalize_rounds(retests['rounds'])
        # {round_id: {primary:..., duplicate:...}} 或单个 round。
        if any(key in retests for key in ('primary', 'duplicate', 'measurements', 'reports')):
            retests = [retests]
        else:
            rounds = []
            for round_id, body in retests.items():
                if isinstance(body, dict):
                    body = {**body, 'round_id': body.get('round_id', round_id)}
                rounds.append(body)
            retests = rounds
    if not isinstance(retests, (list, tuple)):
        return []

    grouped = {}
    order = []
    for item in retests:
        if not isinstance(item, dict):
            continue
        round_id = str(first_value(item, ('round_id', 'round', 'retest_id', 'retest_round',
                                          'retest_round_id', 'retest_no', 'round_no', 'cycle',
                                          'attempt', 'session', 'batch_id', 'pair_id'), 'default'))
        if round_id not in grouped:
            grouped[round_id] = {'round_id': round_id, 'measurements': []}
            order.append(round_id)
        if any(key in item for key in ('primary', 'duplicate')):
            pair = normalize_pair(item)
            for side in ('primary', 'duplicate'):
                if pair.get(side):
                    grouped[round_id]['measurements'].append(pair[side])
        elif any(key in item for key in ('measurements', 'reports', 'sides')):
            nested = first_value(item, ('measurements', 'reports', 'sides'))
            for measurement in nested or []:
                grouped[round_id]['measurements'].append(canonical_measurement(measurement))
        else:
            grouped[round_id]['measurements'].append(canonical_measurement(item))
        for key in ('created', 'created_at', 'sequence', 'seq', 'updated'):
            if key in item:
                grouped[round_id][key] = item[key]
    return [grouped[round_id] for round_id in order]


def apply_corrections(rounds, corrections):
    """把追加式更正记录应用到归一后的轮次，返回新的轮次结构。"""
    if not corrections:
        return rounds
    if isinstance(corrections, dict):
        corrections = [corrections]
    for correction in corrections:
        if not isinstance(correction, dict):
            continue
        target = first_value(correction, ('target', 'record', 'record_id', 'report_id',
                                         'measurement_id', 'corrects'))
        patch = first_value(correction, ('changes', 'patch', 'after', 'fields'))
        if not isinstance(patch, dict):
            patch = {key: correction[key] for key in
                     ('concentration', 'result', 'value', 'lod', 'loq', 'limit', 'absolute_limit')
                     if key in correction}
        for round_body in rounds:
            for measurement in round_body.get('measurements', []):
                matches = target is None or target in (
                    measurement.get('record'), measurement.get('report_id'), str(measurement.get('record'))
                )
                if matches and patch:
                    measurement.update(canonical_measurement({**measurement, **patch}))
    return rounds


def evaluate_round(round_body, control=None, **limits):
    round_id = round_body.get('round_id') if isinstance(round_body, dict) else None
    measurements = round_body.get('measurements', []) if isinstance(round_body, dict) else round_body
    result = evaluate_pair(measurements, control, **limits)
    result['round_id'] = round_id
    result['complete'] = result.get('code') != 'RETEST_PAIR_INCOMPLETE'
    result['method_consistent'] = result.get('code') != 'METHOD_MISMATCH'
    return result


def invalidation_evidence(value):
    if not value:
        return None
    if isinstance(value, str):
        text = value.strip()
        return {'evidence': text} if text else None
    if isinstance(value, dict):
        evidence = first_value(value, ('evidence', 'evidence_id', 'code', 'credential',
                                       'certificate', 'document', 'void_evidence',
                                       'void_certificate', 'invalidation_evidence',
                                       'invalidation_id', 'voidance_evidence',
                                       'original_void_evidence'))
        if evidence is None and any(value.get(key) for key in ('voided', 'invalidated', 'is_void')):
            evidence = first_value(value, ('id', 'record', 'record_id'))
        if not evidence or not str(evidence).strip():
            return None
        result = dict(value)
        result['evidence'] = str(evidence).strip()
        return result
    return None


def resolve_release(original=None, retests=None, invalidation=None, control=None, **options):
    """返回不落库的样品签发结论。

    eligible=True 只表示按当前测量依据可签发；是否已签发要看持久化 status。
    """
    limits = {key: options[key] for key in
              ('rpd_limit_bp', 'absolute_limit', 'limit') if key in options}
    if isinstance(options.get('limits'), dict):
        limits.update(options['limits'])
    control = control if control is not None else options.get('control')
    invalidation = invalidation if invalidation is not None else first_value(
        options, ('void_evidence', 'original_void_evidence', 'voidance', 'invalidation_evidence'))
    inv = invalidation_evidence(invalidation)
    source = original if isinstance(original, dict) else {}
    if control is None:
        control = source.get('control')
    original_pair = source.get('measurements') or source.get('original') or original
    original_result = evaluate_pair(original_pair, control, **limits)

    normalized_rounds = normalize_rounds(retests)
    apply_corrections(normalized_rounds, options.get('corrections'))
    # 复测载荷未重复填写方法限时，沿用原测同组方法限；若两侧显式填写且不一致仍暂缓。
    fallback_lod = original_result.get('lod')
    fallback_loq = original_result.get('loq')
    if fallback_lod is None:
        fallback_lod = options.get('fallback_lod')
    if fallback_loq is None:
        fallback_loq = options.get('fallback_loq')
    for round_body in normalized_rounds:
        for measurement in round_body.get('measurements', []):
            if measurement.get('lod') is None and fallback_lod is not None:
                measurement['lod'] = fallback_lod
            if measurement.get('loq') is None and fallback_loq is not None:
                measurement['loq'] = fallback_loq

    rounds = [evaluate_round(round_body, control, **limits)
              for round_body in normalized_rounds]
    qualified = [item for item in rounds if item.get('pass')]
    adopted = None
    reason_codes = []

    if original_result.get('pass'):
        eligible = True
        status = 'READY'
        reason = 'ORIGINAL_PAIR_QUALIFIED'
        adopted = {'basis': 'ORIGINAL', 'round_id': None, **_adoption_fields(original_result)}
    elif qualified and inv:
        chosen = qualified[-1]
        eligible = True
        status = 'READY'
        reason = 'RETEST_PAIR_QUALIFIED_WITH_VOID_EVIDENCE'
        adopted = {'basis': 'RETEST', 'round_id': chosen.get('round_id'),
                   **_adoption_fields(chosen)}
    elif qualified and not inv:
        eligible = False
        status = 'HELD'
        reason = 'ORIGINAL_VOID_EVIDENCE_REQUIRED'
    else:
        eligible = False
        status = 'HELD'
        reason = _latest_failure_reason(original_result, rounds)

    if original_result.get('code'):
        reason_codes.append(original_result['code'])
    reason_codes.extend(item.get('code') for item in rounds if item.get('code'))

    return {
        'status': status,
        'conclusion': status,
        'eligible': eligible,
        'issuable': eligible,
        'releasable': eligible,
        'can_release': eligible,
        'reason': reason,
        'reason_codes': [code for code in reason_codes if code],
        'original': original_result,
        'retests': rounds,
        'invalidation': inv,
        'adopted': adopted,
        'adopted_records': adopted.get('record_ids', []) if adopted else [],
    }


evaluate_release = resolve_release
decide_release = resolve_release
evaluate_sample = resolve_release
decide_sample = resolve_release
resolve_sample = resolve_release
recompute_sample = resolve_release
recompute_pending = resolve_release
adjudicate_sample = resolve_release
sample_decision = resolve_release


def _adoption_fields(evaluation):
    return {
        'mode': evaluation.get('mode'),
        'value': evaluation.get('value'),
        'pass': evaluation.get('pass'),
        'code': evaluation.get('code'),
        'record_ids': evaluation.get('record_ids', []),
        'measurements': evaluation.get('measurements'),
        'evaluation': {key: evaluation.get(key) for key in
                       ('mode', 'value', 'pass', 'code', 'reason', 'left', 'right',
                        'gap', 'lod', 'loq', 'rpd_limit_bp', 'absolute_limit')},
    }


def _latest_failure_reason(original_result, rounds):
    if rounds:
        latest = rounds[-1]
        if latest.get('code') == 'METHOD_MISMATCH':
            return 'RETEST_METHOD_MISMATCH'
        if latest.get('code') == 'RETEST_PAIR_INCOMPLETE':
            return 'RETEST_PAIR_INCOMPLETE'
        if latest.get('code'):
            return latest['code']
    return original_result.get('code') or 'HELD'


def basis_digest(board_like):
    """测量依据指纹；更正改变浓度/限值/采用记录时指纹随之改变。"""
    payload = {
        'original': board_like.get('original'),
        'retests': board_like.get('retests'),
        'invalidation': board_like.get('invalidation'),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode()).hexdigest()
