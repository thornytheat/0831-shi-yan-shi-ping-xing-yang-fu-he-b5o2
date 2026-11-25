from app.business import decision


def policy(result):
    return decision(result,'QUALITY_LIMIT' if result['value'] else None)
