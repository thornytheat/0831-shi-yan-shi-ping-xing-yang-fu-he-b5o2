EXPECTED = {'0001': (2, 0, 'READY'),
 '0002': (1, 0, 'READY'),
 '0003': (0, None, 'SAMPLE_PAIR_MISSING'),
 '0004': (1, 1, 'QUALITY_LIMIT')}
EVIDENCE = {'0001': {'pairs': {'1': {'mode': 'RPD', 'value': 952, 'pass': True},
                    '2': {'mode': 'ABSOLUTE', 'value': 3, 'pass': True}},
          'failed': []},
 '0004': {'pairs': {'1': {'mode': 'MIXED_LOD', 'value': None, 'pass': False}},
          'failed': [1]}}
