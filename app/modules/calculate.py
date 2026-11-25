from app.business import measured


def compute(data):
    pairs,control = data
    evidence, failed = {}, []
    for sample,pair in sorted(pairs.items()):
        source = pair['primary']
        left,right = sorted((source['concentration'],pair['duplicate']['concentration']))
        gap = right-left
        if right < source['lod']:
            mode,value,passed = 'BOTH_ND',None,True
        elif left < source['lod']:
            mode,value,passed = 'MIXED_LOD',None,False
        elif left >= 5*source['loq']:
            numerator,denominator = gap*20000,left+right
            mode,value,passed = 'RPD',numerator//denominator,numerator<=control['limit']*denominator
        else:
            mode,value,passed = 'ABSOLUTE',gap,gap<=control['loq']
        evidence[str(sample)] = {'mode':mode,'value':value,'pass':passed}
        if not passed:
            failed.append(sample)
    return measured(len(failed),0,'组',{'pairs':evidence,'failed':failed})
