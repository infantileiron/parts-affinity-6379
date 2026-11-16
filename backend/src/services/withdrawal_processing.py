"""撤回受理、影响追踪与纠正执行（含并发控制与崩溃恢复）。"""
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import text
from src.models.entities import (Job, MiningRun, AssociationRule, Kit,
                                 Withdrawal, Correction)
from src.services.business import dump
from src.services.withdrawals import build_correction

# 命名咨询锁：同一时刻只允许一个撤回纠正流程写历史批次，避免重叠撤回交叉扣减
LOCK_NAMESPACE = 'withdrawal_correction'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _thresholds(run):
    return {'min_support': run.min_support,
            'min_confidence': run.min_confidence,
            'min_lift': run.min_lift}


def _impact_preview(s, job):
    """受理前计算影响面：哪些冻结批次包含该作业、哪些备件包间接受影响。"""
    runs, kits = [], []
    for run in s.query(MiningRun).order_by(MiningRun.id).all():
        snapshot = (run.result or {}).get('snapshot', [])
        codes = {row.get('code') for row in snapshot}
        if job.code in codes:
            rule_ids = [r.id for r in s.query(AssociationRule).filter_by(run_id=run.id).all()]
            run_kits = s.query(Kit).filter(Kit.rule_id.in_(rule_ids)).all() if rule_ids else []
            runs.append({'run_id': run.id, 'run_name': run.name,
                         'quarantine': run.quarantine,
                         'kit_ids': [k.id for k in run_kits]})
            kits.extend(k.id for k in run_kits)
    return {'affected_run_ids': [r['run_id'] for r in runs],
            'affected_kit_ids': sorted(set(kits)),
            'runs': runs,
            'excluded_from_new_mining': True}


def _accept_locked(s, user, code, reason):
    """受理撤回：幂等。同一作业号存在未完成撤回则拒绝重复受理；已完成则返回原单不重复扣减。

    调用方必须持有 withdrawal_lock 并在同一连接事务内调用，保证 GET_LOCK 不被 commit 提前释放。
    """
    job = s.query(Job).filter_by(code=code).first()
    if not job:
        raise HTTPException(404, '作业号不存在，无法撤回：' + code)

    existing = s.query(Withdrawal).filter_by(code=code).order_by(Withdrawal.id).all()
    open_one = next((w for w in existing if w.state in ('accepted', 'processing', 'failed')), None)
    if open_one:
        raise HTTPException(409, f'作业 {code} 已有进行中的撤回单 #{open_one.id}，请勿重复受理')
    done_one = next((w for w in existing if w.state == 'completed'), None)
    if done_one:
        # 重试同一撤回直接返回历史单据，不产生新的扣减
        return dump(done_one), False

    impact = _impact_preview(s, job)
    w = Withdrawal(code=code, job_id=job.id, reason=reason or '',
                   state='accepted', impact=impact, created=_now())
    s.add(w)
    s.flush()  # 同事务内取 w.id，不提交，连接与咨询锁保持不变

    # 作业立即标记撤回，新挖掘从源头排除
    job.withdrawal_id = w.id
    # 受影响历史批次立即隔离，污染结果在纠正完成前无法导出/签核
    affected_runs = s.query(MiningRun).filter(MiningRun.id.in_(impact['affected_run_ids'])).all() \
        if impact['affected_run_ids'] else []
    for run in affected_runs:
        if run.quarantine == 'cleared':
            run.quarantine = 'quarantined'
        run.last_withdrawal_id = w.id
    # 为每个受影响批次建一条 pending 纠正项；备件包失效随所属批次规则一并处理
    for rid in impact['affected_run_ids']:
        s.add(Correction(withdrawal_id=w.id, target_type='run', target_id=rid,
                         state='pending', detail=None, attempts=0))
    return dump(w), True


def _get_lock(s):
    """MySQL 命名咨询锁（立即返回）；调用方需在释放前保持同一连接（不 commit 到归还连接）。

    SQLite 不支持 GET_LOCK，返回 True 并依赖测试环境注入的进程内锁串行化。
    """
    try:
        row = s.execute(text("SELECT GET_LOCK(:n, 0)"), {'n': LOCK_NAMESPACE}).first()
        return row is not None and row[0] == 1
    except Exception:
        return True


def _release_lock(s):
    try:
        s.execute(text("SELECT RELEASE_LOCK(:n)"), {'n': LOCK_NAMESPACE})
    except Exception:
        pass


class withdrawal_lock:
    """上下文管理器：在同一连接事务内持有命名咨询锁，全程不归还连接，避免 commit 提前释放锁。"""
    def __init__(self, s):
        self.s = s; self.acquired = False
    def __enter__(self):
        if not _get_lock(self.s):
            raise HTTPException(409, '另一个撤回纠正正在处理，请稍后重试')
        self.acquired = True
        self.s.begin() if not self.s.in_transaction() else None
        return self.s
    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.s.commit()
            else:
                self.s.rollback()
        finally:
            if self.acquired:
                _release_lock(self.s)
        return False


def _apply_run_correction(s, w, correction):
    """对单个历史批次执行剔除重算并落库。可安全重入：每次都从原始冻结快照重算。"""
    run = s.get(MiningRun, correction.target_id)
    if not run:
        correction.state = 'skipped'
        correction.detail = {'reason': '批次已不存在'}
        return
    snapshot = (run.result or {}).get('snapshot', [])

    # 累积该批次上所有已受理/处理中/完成的撤回作业（支持重叠撤回，一次性全部剔除）
    withdrawn_codes = {x.code for x in s.query(Withdrawal).filter(
        Withdrawal.state.in_(('accepted', 'processing', 'completed'))).all()
        if any(r.get('code') == x.code for r in snapshot)}

    thresholds = _thresholds(run)
    original = dict(run.result)
    original['_thresholds'] = thresholds
    report = build_correction(original, withdrawn_codes)
    new_result = report.pop('new_result')

    # 更新批次结果：保留原始 snapshot，新产物覆盖计算字段并记录撤回对账
    run.result = {**new_result, 'snapshot': snapshot,
                  'withdrawn_codes': sorted(withdrawn_codes),
                  'recompute_history': (run.result.get('recompute_history', []) if run.result else [])
                  + [{'withdrawal_id': w.id, 'code': w.code, 'at': _now(),
                      'before': report['before'], 'after': report['after']}]}
    run.quarantine = 'cleaned' if report['cleaned'] else 'quarantined'
    run.last_withdrawal_id = w.id
    run.state = 'done'
    run.error = ''

    # 规则按 signature 对齐：更新仍成立规则的证据；消失的规则标 invalid 并失效其备件包
    old_rules = {r.signature: r for r in s.query(AssociationRule).filter_by(run_id=run.id).all()}
    new_by_sig = {r['signature']: r for r in new_result['rules']}
    for sig, rule in old_rules.items():
        if sig in new_by_sig:
            nr = new_by_sig[sig]
            rule.status = 'active'
            rule.count, rule.support = nr['count'], nr['support']
            rule.confidence, rule.lift = nr['confidence'], nr['lift']
        else:
            rule.status = 'invalid'
            for kit in s.query(Kit).filter_by(rule_id=rule.id).all():
                if kit.status != 'invalidated':
                    kit.status = 'invalidated'
                    kit.invalidated_by = w.id
                    kit.invalid_reason = f'来源规则 {sig} 在撤回作业 {w.code} 后不再成立'
    # 新出现的规则入库（重算可能产生新 signature），状态 active
    for sig, nr in new_by_sig.items():
        if sig not in old_rules:
            s.add(AssociationRule(run_id=run.id, status='active', **nr))

    correction.detail = report
    correction.state = 'completed'
    correction.error = ''


def _process_locked(s, withdrawal_id):
    """锁内执行一个撤回单的全部纠正项。调用方已持有 withdrawal_lock。

    - 逐项处理，单项失败只标记该项 failed，不影响其他批次，中断后可重跑；
    - 每个纠正项独立提交以保证崩溃恢复粒度；全部 completed/skipped 后撤回单 completed。
    """
    w = s.get(Withdrawal, withdrawal_id)
    if not w:
        raise HTTPException(404, '撤回单不存在')
    if w.state == 'completed':
        return w, False  # 幂等：完成态重试不重复扣减

    w.state = 'processing'
    w.error = ''
    s.commit()
    corrections = s.query(Correction).filter_by(withdrawal_id=w.id).order_by(Correction.id).all()
    any_failure = False
    for c in corrections:
        if c.state == 'completed':
            continue  # 崩溃恢复：已完成项不重复处理
        c.state = 'processing'
        c.attempts += 1
        s.commit()
        try:
            _apply_run_correction(s, w, c)
        except Exception as e:  # 样本不足等：标记 failed，保留现场，等待恢复重试
            s.rollback()
            c = s.get(Correction, c.id)
            w = s.get(Withdrawal, w.id)
            c.state = 'failed'
            c.error = str(e)
            any_failure = True
        s.commit()

    w = s.get(Withdrawal, w.id)
    corrections = s.query(Correction).filter_by(withdrawal_id=w.id).all()
    if any(c.state in ('pending', 'processing', 'failed') for c in corrections):
        w.state = 'failed'
        w.error = '部分批次纠正未完成，可恢复重试'
        any_failure = True
    else:
        w.state = 'completed'
        w.processed_at = _now()
        w.reconciliation = [c.detail for c in corrections if c.detail]
    s.commit()
    return w, not any_failure


def process_withdrawal(s, withdrawal_id):
    """带咨询锁的纠正入口（供重试接口调用）。"""
    with withdrawal_lock(s):
        return _process_locked(s, withdrawal_id)


def accept_and_process(s, user, code, reason):
    """受理并在同一把咨询锁内完成纠正，保证受理→隔离→重算原子且不交叉。"""
    with withdrawal_lock(s):
        w, created = _accept_locked(s, user, code, reason)
        if created:
            s.commit()  # 落库受理单/隔离/纠正项，连接与命名锁仍保持
            ww, _ = _process_locked(s, w['id'])
            return dump(ww), True
        return w, False


def recover(s):
    """启动/手动恢复：把中断的 processing 纠正退回 pending，并依次重试未完成撤回单。"""
    s.query(Correction).filter(Correction.state == 'processing').update(
        {Correction.state: 'pending'}, synchronize_session=False)
    s.query(Withdrawal).filter(Withdrawal.state == 'processing').update(
        {Withdrawal.state: 'accepted'}, synchronize_session=False)
    s.commit()
    results = []
    for w in s.query(Withdrawal).filter(Withdrawal.state.in_(('accepted', 'failed'))).all():
        try:
            with withdrawal_lock(s):
                ww, _ = _process_locked(s, w.id)
            results.append({'withdrawal_id': w.id, 'state': ww.state})
        except HTTPException as e:
            results.append({'withdrawal_id': w.id, 'state': w.state, 'deferred': e.detail})
    return results
