from flask import jsonify

LABELS = {
    'FILE_TOO_LARGE':'文件超过 256 KiB', 'FILE_EMPTY':'文件没有内容', 'FILE_BLANK':'文件只有空白',
    'HEADER_ONLY':'文件只有标题行', 'HEADER_MISMATCH':'列名与字段字典不一致', 'ENCODING_INVALID':'请使用 UTF-8 CSV',
    'CSV_MALFORMED':'CSV 引号或行格式异常', 'ROW_LIMIT':'文件超过 2000 行', 'FILE_REQUIRED':'请选择 CSV 文件',
    'RUN_NOT_FOUND':'批次不存在', 'REVIEW_FIELDS':'请填写 3 至 200 字复核意见', 'STALE_REVIEW':'批次或样品结论已更新，请刷新',
    'RUN_FINALIZED':'批次或样品已签发', 'UNRESOLVED_ISSUES':'存在待处理差异', 'SAMPLE_UNKNOWN':'样例不存在', 'STAGE_UNKNOWN':'处理阶段不存在',
    'SAMPLE_IDENTIFIER_REQUIRED':'请指定业务组和样品编号', 'SAMPLE_IDENTIFIER_AMBIGUOUS':'样品编号不唯一，请同时提供业务组',
    'MEASUREMENT_NOT_FOUND':'复测测量记录不存在', 'RETEST_FIELDS':'复测报告字段不完整',
    'CORRECTION_FIELDS':'请提供复测更正字段', 'CORRECTION_TARGET_REQUIRED':'请指定要更正的复测记录',
    'ORIGINAL_VOID_EVIDENCE_REQUIRED':'缺少原测作废凭据', 'RETEST_PAIR_INCOMPLETE':'同次复测缺少一侧',
    'RETEST_SIDE_MISSING':'同次复测缺少一侧', 'RETEST_METHOD_MISMATCH':'复测两侧检出限或定量限不一致',
}

NOT_FOUND = {'RUN_NOT_FOUND', 'SAMPLE_UNKNOWN', 'STAGE_UNKNOWN', 'MEASUREMENT_NOT_FOUND', 'SAMPLE_NOT_FOUND'}
CONFLICT = {
    'STALE_REVIEW', 'RUN_FINALIZED', 'UNRESOLVED_ISSUES', 'SAMPLE_IDENTIFIER_AMBIGUOUS',
    'ORIGINAL_VOID_EVIDENCE_REQUIRED', 'RETEST_PAIR_INCOMPLETE', 'RETEST_SIDE_MISSING',
    'RETEST_METHOD_MISMATCH', 'QUALITY_LIMIT', 'METHOD_MISMATCH', 'SAMPLE_PAIR_MISSING',
}


def response(error):
    code = error.code
    if code in NOT_FOUND:
        status = 404
    elif code in CONFLICT:
        status = 409
    elif code == 'FILE_TOO_LARGE':
        status = 413
    else:
        status = 422
    response = jsonify(code=code, message=LABELS.get(code, code),
                       aliases=getattr(error, 'aliases', ()),
                       reason=code)
    response.status_code = status
    return response
