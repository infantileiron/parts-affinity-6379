"""误采作业撤回与历史批次重算引擎。

设计要点：
- 纯函数 build_correction 只依赖传入的批次结果与撤回作业，便于单测与崩溃恢复；
- 重算从各批次自己的冻结样本剔除撤回项后重新跑同一版 Apriori，无关批次不被调用；
- 保留前后计数对账（valid_jobs、excluded、频繁项集、规则的增/删/改）；
- 规则按 signature 匹配：仍成立则更新证据指标并保留原 rule_id 引用，消失则标记 invalid；
- 规则消失使来源备件包失效；仍成立规则的备件包保留原引用。
"""
from src.services.analysis import mine


def _rule_index(rules):
    return {r['signature']: r for r in rules}


def build_correction(run_result, withdrawn_codes):
    """对单个挖掘批次结果执行撤回重算，返回收敛后的对账数据。

    run_result: MiningRun.result（含 snapshot 与首次挖掘产物）。
    withdrawn_codes: 本撤回单涉及的作业号集合（多次重叠撤回会累积）。
    返回 dict：before/after 计数、规则增删改、新结果与隔离标记。不抛异常即成功。
    """
    withdrawn = set(withdrawn_codes)
    snapshot = run_result.get('snapshot', [])
    kept = [row for row in snapshot if row.get('code') not in withdrawn]
    removed = [row for row in snapshot if row.get('code') in withdrawn]

    before = {
        'valid_jobs': run_result.get('valid_jobs', 0),
        'excluded': run_result.get('excluded', 0),
        'minimum_count': run_result.get('minimum_count', 0),
        'rule_count': len(run_result.get('rules', [])),
        'frequent_count': len(run_result.get('frequent', [])),
    }

    # 样本剔除后可能不足 2 条有效事务：mine 会抛 ValueError，由调用方决定 failed 可恢复
    new_result = mine(kept,
                      run_result['_thresholds']['min_support'],
                      run_result['_thresholds']['min_confidence'],
                      run_result['_thresholds']['min_lift'])
    after = {
        'valid_jobs': new_result['valid_jobs'],
        'excluded': new_result['excluded'],
        'minimum_count': new_result['minimum_count'],
        'rule_count': len(new_result['rules']),
        'frequent_count': len(new_result['frequent']),
    }

    old_rules = _rule_index(run_result.get('rules', []))
    new_rules = _rule_index(new_result['rules'])
    kept_rules, updated_rules, removed_rules, added_rules = [], [], [], []
    for sig, old in old_rules.items():
        if sig in new_rules:
            new = new_rules[sig]
            kept_rules.append(sig)
            if (old['count'], old['support'], old['confidence'], old['lift']) != \
               (new['count'], new['support'], new['confidence'], new['lift']):
                updated_rules.append({'signature': sig,
                                      'before': {k: old[k] for k in ('count', 'support', 'confidence', 'lift')},
                                      'after': {k: new[k] for k in ('count', 'support', 'confidence', 'lift')}})
        else:
            removed_rules.append(sig)
    for sig in new_rules:
        if sig not in old_rules:
            added_rules.append(sig)

    return {
        'removed_codes': sorted(r['code'] for r in removed),
        'before': before,
        'after': after,
        'kept_rule_signatures': kept_rules,
        'updated_rules': updated_rules,
        'removed_rule_signatures': removed_rules,
        'added_rule_signatures': added_rules,
        'new_result': new_result,
        # 重算后仍有规则才算干净可放出；否则批次保持隔离，禁止导出/签核
        'cleaned': len(new_rules) > 0,
    }
