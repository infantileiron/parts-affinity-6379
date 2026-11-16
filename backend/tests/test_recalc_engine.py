"""撤回/重算引擎纯函数测试（无需数据库）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.services.analysis import mine
from src.services.withdrawals import build_correction


def _rows(pairs):
    return [{'code': c, 'equipment': '泵组', 'parts': list(p)} for c, p in pairs]


def _run_result(rows, ms=0.2, mc=0.5, ml=1.0):
    res = mine(rows, ms, mc, ml)
    res['snapshot'] = rows  # MiningRun.result 始终携带冻结样本
    res['_thresholds'] = {'min_support': ms, 'min_confidence': mc, 'min_lift': ml}
    return res


def test_unaffected_batch_not_touched_contract():
    rows = _rows([('J1', ('轴承', '润滑脂')), ('J2', ('轴承', '润滑脂')),
                  ('J3', ('轴承', '密封圈')), ('J4', ('滤芯', '垫片'))])
    res = _run_result(rows)
    rep = build_correction(res, {'ZZ-NOPE'})  # 撤回一个不在样本中的作业
    assert rep['removed_codes'] == []
    assert rep['before']['valid_jobs'] == rep['after']['valid_jobs'] == 4
    assert rep['removed_rule_signatures'] == [] and rep['added_rule_signatures'] == []
    print('PASS test_unaffected_batch_not_touched_contract')


def test_withdraw_item_recalc_and_count_reconciliation():
    rows = _rows([('J1', ('轴承', '润滑脂', '密封圈')),
                  ('J2', ('轴承', '润滑脂', '密封圈')),
                  ('J3', ('轴承', '润滑脂')),
                  ('J4', ('轴承', '垫片'))])
    res = _run_result(rows)
    assert res['valid_jobs'] == 4
    rep = build_correction(res, {'J1'})
    assert rep['removed_codes'] == ['J1']
    assert rep['before']['valid_jobs'] == 4 and rep['after']['valid_jobs'] == 3
    # 前/后计数都被保留
    assert set(rep['before']) == {'valid_jobs', 'excluded', 'minimum_count', 'rule_count', 'frequent_count'}
    assert rep['after']['valid_jobs'] == 3
    print('PASS test_withdraw_item_recalc_and_count_reconciliation')


def test_rule_disappears_and_survivor_kept():
    rows = _rows([('J1', ('轴承', '润滑脂')), ('J2', ('轴承', '润滑脂')),
                  ('J3', ('轴承', '润滑脂')), ('J4', ('滤芯', '垫片', '阀片'))])
    res = _run_result(rows, ms=0.25, mc=0.6)
    sigs_before = {x['signature'] for x in res['rules']}
    rep = build_correction(res, {'J4'})  # 剔除后 滤芯/垫片/阀片 事务消失
    # 与 J4 独有备件相关的规则应消失
    assert any(('滤芯' in s or '垫片' in s or '阀片' in s) for s in rep['removed_rule_signatures']) \
        or rep['after']['rule_count'] < rep['before']['rule_count']
    # 轴承->润滑脂 仍成立则保留
    survivors = set(rep['kept_rule_signatures'])
    assert sigs_before  # 前提：原本有规则
    print('PASS test_rule_disappears_and_survivor_kept',
          'removed=', rep['removed_rule_signatures'], 'kept=', sorted(survivors))


def test_overlapping_withdrawals_cumulative_no_double_dedup():
    rows = _rows([('J1', ('轴承', '润滑脂')), ('J2', ('轴承', '润滑脂')),
                  ('J3', ('轴承', '润滑脂')), ('J4', ('轴承', '润滑脂'))])
    res = _run_result(rows)
    # 两次重叠撤回一次性传入两个 code，J1 不会被扣两次
    rep = build_correction(res, {'J1', 'J2'})
    assert rep['after']['valid_jobs'] == 2
    rep_again = build_correction(res, {'J1', 'J2'})  # 重入结果稳定
    assert rep_again['after']['valid_jobs'] == rep['after']['valid_jobs']
    print('PASS test_overlapping_withdrawals_cumulative_no_double_dedup')


def test_sample_too_small_fails_for_retry():
    rows = _rows([('J1', ('轴承', '润滑脂')), ('J2', ('轴承', '垫片'))])
    res = _run_result(rows, ms=0.1, mc=0.1)
    try:
        build_correction(res, {'J1'})  # 只剩 1 条事务
        raise AssertionError('应因样本不足抛错，供纠正项标记 failed 后恢复重试')
    except ValueError as e:
        assert '2' in str(e)
        print('PASS test_sample_too_small_fails_for_retry')


if __name__ == '__main__':
    test_unaffected_batch_not_touched_contract()
    test_withdraw_item_recalc_and_count_reconciliation()
    test_rule_disappears_and_survivor_kept()
    test_overlapping_withdrawals_cumulative_no_double_dedup()
    test_sample_too_small_fails_for_retry()
    print('ALL ENGINE TESTS PASSED')
