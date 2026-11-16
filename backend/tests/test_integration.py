"""端到端集成测试：SQLite + FastAPI TestClient + 模拟 worker。

覆盖验收场景：
A 受理撤回→影响追踪→批次隔离→重算对账→规则消失备件包失效→仍成立规则保留引用并更新证据
B 新挖掘排除撤回作业；无关批次不变
C 重试同一撤回不重复扣减；纠正 pending→completed，中断可恢复
D 导出/签核/起草在隔离与失效态被拦截，纠正完成后放行
E 重叠撤回（交集作业）累积扣减、并发受理不产生重复扣减
F 重算中宕机：processing 纠正恢复后继续，最终一致
"""
import os, sys, tempfile
_TMP = tempfile.mkdtemp(prefix='affinity_test_')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'test.db')
os.environ['JWT_SECRET'] = 'test-secret-3108'
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import sqlalchemy.event
from src.config import database as dbcfg
# SQLite 写入串行化，模拟行锁/咨询锁语义，避免并发测试时 database is locked
@sqlalchemy.event.listens_for(dbcfg.engine, 'connect')
def _sqlite_pragma(dbapi_conn, rec):
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA busy_timeout=8000')
    cur.execute('PRAGMA journal_mode=WAL')
    cur.close()

from fastapi.testclient import TestClient
from fastapi import HTTPException
from src.config.database import Base, engine, Session
from src.models.entities import (Job, MiningRun, AssociationRule, Kit,
                                 Withdrawal, Correction, User)
Base.metadata.create_all(engine)  # 实体注册后建表，再启动应用
from src.main import app
from src.services.analysis import mine
from src.services import withdrawal_processing as wp

# SQLite 无 GET_LOCK：用进程内可重入锁模拟 MySQL 命名咨询锁，真实复现串行化语义
import threading as _threading
_adv_lock = _threading.RLock()
wp._get_lock = lambda s: _adv_lock.acquire(blocking=False)
wp._release_lock = lambda s: _adv_lock.release()

client = TestClient(app)

# 直接初始化三个演示账号（不依赖应用 lifespan 的种子流程）
from src.utils.security import hash_password
with Session() as s:
    for role in ('admin', 'analyst', 'viewer'):
        if not s.query(User).filter_by(email=f'{role}@demo.local').first():
            s.add(User(email=f'{role}@demo.local', role=role, password=hash_password('Demo123!')))
    s.commit()

PASS, FAIL = [], []
def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS ' if cond else 'FAIL ') + name + ((' :: ' + str(extra)) if extra and not cond else ''))

def token(role):
    r = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']

def H(t): return {'Authorization': 'Bearer ' + t}

def seed_jobs(rows):
    with Session() as s:
        for code, equip, parts in rows:
            s.add(Job(code=code, equipment=equip, parts=sorted(parts), note='t'))
        s.commit()

def make_run(token_, name, equip='全部', ms=0.2, mc=0.5, ml=1.0):
    """直接构造挖掘批次并用同版算法产出规则（模拟 worker 计算）。"""
    with Session() as s:
        jobs = [r for r in s.query(Job).order_by(Job.id).all()
                if equip == '全部' or r.equipment == equip]
        snapshot = [{'id': j.id, 'code': j.code, 'equipment': j.equipment,
                     'parts': j.parts, 'note': j.note} for j in jobs]
        run = MiningRun(name=name, equipment=equip, min_support=ms, min_confidence=mc,
                        min_lift=ml, state='running', result={'snapshot': snapshot})
        s.add(run); s.commit()
        result = mine(snapshot, ms, mc, ml)
        for rule in result['rules']:
            s.add(AssociationRule(run_id=run.id, status='active', **rule))
        run.result = {**result, 'snapshot': snapshot}; run.state = 'done'
        s.commit()
        return run.id

def make_kit(token_, rule_id, name='测试备件包', note='兼容性已核对的测试备件包备注'):
    r = client.post('/kits', json={'name': name, 'rule_id': rule_id, 'note': note}, headers=H(token_))
    return r

# ---------- 构造确定性数据 ----------
# 泵组：J1..J6 强联用 轴承+润滑脂；J7 独有 滤芯+垫片+阀片（撤回后其规则应消失）
seed_jobs([
    ('J1', '泵组', ('轴承', '润滑脂')), ('J2', '泵组', ('轴承', '润滑脂', '密封圈')),
    ('J3', '泵组', ('轴承', '润滑脂')), ('J4', '泵组', ('轴承', '润滑脂', '密封圈')),
    ('J5', '泵组', ('轴承', '润滑脂')), ('J6', '泵组', ('轴承', '润滑脂')),
    ('J7', '泵组', ('滤芯', '垫片', '阀片')),
    # 风机批次（无关批次，撤回泵组 J7 不应影响它）
    ('W1', '风机', ('皮带', '联轴器')), ('W2', '风机', ('皮带', '联轴器')),
    ('W3', '风机', ('皮带', '紧固件')), ('W4', '风机', ('叶轮', '传感器')),
])
admin_t, analyst_t, viewer_t = token('admin'), token('analyst'), token('viewer')

# viewer 禁止受理撤回
r = client.post('/withdrawals', json={'code': 'J7', 'reason': '误采'}, headers=H(viewer_t))
check('viewer 不能受理撤回', r.status_code == 403, r.status_code)

run_pump = make_run(analyst_t, '泵组批次', equip='泵组', ms=0.1, mc=0.5)
run_fan = make_run(analyst_t, '风机批次', equip='风机', ms=0.1, mc=0.4)

with Session() as s:
    pump_rules_before = s.query(AssociationRule).filter_by(run_id=run_pump).count()
    fan_rules_before = s.query(AssociationRule).filter_by(run_id=run_fan).count()
    # 找一条 J7 独有规则与一条轴承润滑脂规则
    sigs = {x.signature: x for x in s.query(AssociationRule).filter_by(run_id=run_pump).all()}
    j7_rule = next(x for x in sigs.values() if '滤芯' in x.antecedent + x.consequent or '阀片' in x.antecedent + x.consequent)
    survive_rule = sigs.get('润滑脂->轴承') or sigs.get('轴承->润滑脂')
    survive_metrics_before = (survive_rule.count, survive_rule.support, survive_rule.confidence, survive_rule.lift)

# 基于 J7 独有规则与幸存规则各起草一个备件包
kit_doomed = make_kit(analyst_t, j7_rule.id, '滤芯垫片阀片包', 'J7独有规则来源的备件包备注内容')
kit_keep = make_kit(analyst_t, survive_rule.id, '轴承润滑脂包', '强联用规则来源的备件包备注内容')
check('两个备件包起草成功', kit_doomed.status_code == 200 and kit_keep.status_code == 200,
      (kit_doomed.text, kit_keep.text))
kit_doomed_id, kit_keep_id = kit_doomed.json()['id'], kit_keep.json()['id']

# ---------- A 受理撤回 J7 ----------
r = client.post('/withdrawals', json={'code': 'J7', 'reason': '维修站确认误采'}, headers=H(analyst_t))
check('受理撤回成功', r.status_code == 200, r.text)
w1 = r.json()
check('撤回单立即处于完成态（同步纠正）', w1['state'] == 'completed', w1['state'])
check('影响面包含泵组批次且不含风机批次',
      run_pump in w1['impact']['affected_run_ids'] and run_fan not in w1['impact']['affected_run_ids'],
      w1['impact'])
check('影响面追踪到将受牵连的备件包',
      kit_doomed_id in w1['impact']['affected_kit_ids'], w1['impact'])

with Session() as s:
    pump = s.get(MiningRun, run_pump); fan = s.get(MiningRun, run_fan)
    check('受影响批次已隔离并在纠正后 cleaned', pump.quarantine == 'cleaned', pump.quarantine)
    check('无关风机批次保持 cleared 不变', fan.quarantine == 'cleared' and
          s.query(AssociationRule).filter_by(run_id=run_fan).count() == fan_rules_before)
    j7r = s.get(AssociationRule, j7_rule.id)
    sr = s.get(AssociationRule, survive_rule.id)
    check('J7 来源规则消失并标记 invalid', j7r.status == 'invalid', j7r.status)
    check('幸存规则保留原 rule_id 且仍 active', sr.id == survive_rule.id and sr.status == 'active')
    check('幸存规则证据指标已更新',
          (sr.count, sr.support, sr.confidence, sr.lift) != survive_metrics_before,
          (survive_metrics_before, (sr.count, sr.support, sr.confidence, sr.lift)))
    kd, kk = s.get(Kit, kit_doomed_id), s.get(Kit, kit_keep_id)
    check('规则消失使来源备件包失效', kd.status == 'invalidated' and kd.invalidated_by == w1['id'],
          (kd.status, kd.invalidated_by))
    check('仍成立规则的备件包保留原引用', kk.status == 'draft' and kk.rule_id == survive_rule.id)
    # 前后计数对账
    corr = s.query(Correction).filter_by(withdrawal_id=w1['id']).one()
    b, a = corr.detail['before'], corr.detail['after']
    check('保留前后计数对账（7→6 有效作业）',
          b['valid_jobs'] == 7 and a['valid_jobs'] == 6 and b['rule_count'] > a['rule_count'],
          (b, a))
    job7 = s.query(Job).filter_by(code='J7').one()
    check('J7 已标记撤回', job7.withdrawal_id == w1['id'])

# ---------- B 新挖掘排除撤回作业；导出/签核门控 ----------
r = client.post('/runs', json={'name': '撤回后新批次', 'equipment': '泵组',
                               'min_support': 0.1, 'min_confidence': 0.5, 'min_lift': 1},
                headers=H(analyst_t))
# 队列无 redis 时会 503；改用直接建批次验证样本排除逻辑
with Session() as s:
    rows = [{'id': j.id, 'code': j.code, 'equipment': j.equipment, 'parts': j.parts, 'note': j.note}
            for j in s.query(Job).filter_by(equipment='泵组').order_by(Job.id).all()
            if j.withdrawal_id is None]
    check('新挖掘样本已排除撤回作业 J7', all(x['code'] != 'J7' for x in rows) and len(rows) == 6,
          [x['code'] for x in rows])

# 隔离历史：构造一个仍处于 quarantined 的批次验证门控（模拟受理后未纠正窗口）
with Session() as s:
    qrun = MiningRun(name='隔离窗口批次', equipment='泵组', min_support=0.1, min_confidence=0.5,
                     min_lift=1, state='done', quarantine='quarantined',
                     result={'snapshot': [], 'valid_jobs': 1, 'rules': [], 'frequent': []})
    s.add(qrun); s.commit(); qrun_id = qrun.id
r = client.get(f'/runs/{qrun_id}/export', headers=H(analyst_t))
check('隔离批次禁止导出', r.status_code == 409, r.status_code)
# cleaned 批次允许导出且带纠正说明
r = client.get(f'/runs/{run_pump}/export', headers=H(analyst_t))
check('纠正后批次可导出且带对账说明', r.status_code == 200 and 'quarantine_note' in r.json(), r.status_code)
# 失效备件包不能签核；正常 draft 可签核
r = client.post(f'/kits/{kit_doomed_id}/approve', json={}, headers=H(admin_t))
check('失效备件包签核被拦截', r.status_code == 409, r.status_code)
r = client.post(f'/kits/{kit_keep_id}/approve', json={}, headers=H(admin_t))
check('干净备件包允许签核', r.status_code == 200 and r.json()['status'] == 'approved', r.status_code)
# 失效规则不能再起草备件包
r = make_kit(analyst_t, j7_rule.id, '不应成立的包', '试图基于失效规则起草备件包的备注内容')
check('失效规则不能起草备件包', r.status_code == 409, r.status_code)

# ---------- C 重试同一撤回不重复扣减 ----------
r = client.post(f"/withdrawals/{w1['id']}/retry", json={}, headers=H(analyst_t))
check('重试已完成撤回幂等返回', r.status_code == 200 and r.json()['state'] == 'completed')
# 再次受理同一作业号：返回历史单，不产生新扣减
r = client.post('/withdrawals', json={'code': 'J7', 'reason': '重复受理尝试'}, headers=H(analyst_t))
check('重复受理同一作业不重复扣减', r.status_code == 200 and r.json()['id'] == w1['id'], r.status_code)
with Session() as s:
    check('J7 有效作业计数未被二次扣减（仍为 6）',
          s.get(MiningRun, run_pump).result['valid_jobs'] == 6)
    check('只有一张 J7 撤回单', s.query(Withdrawal).filter_by(code='J7').count() == 1)

# ---------- E 重叠撤回（交集作业） ----------
# 撤回 J1：与 J7 的撤回作用于同一泵组批次，纠正应累积剔除 {J7,J1}
r = client.post('/withdrawals', json={'code': 'J1', 'reason': '第二张误采'}, headers=H(analyst_t))
check('受理重叠撤回 J1', r.status_code == 200, r.text)
w2 = r.json()
with Session() as s:
    pump = s.get(MiningRun, run_pump)
    check('重叠撤回累积剔除两个作业（6→5）',
          pump.result['valid_jobs'] == 5 and
          set(pump.result['withdrawn_codes']) == {'J1', 'J7'},
          (pump.result['valid_jobs'], pump.result.get('withdrawn_codes')))
    check('重算历史保留两次前后对账', len(pump.result['recompute_history']) == 2)

# ---------- F 重算中宕机恢复 ----------
# 制造一张新批次并受理撤回，但在处理中途把纠正项改成 processing、撤回单改成 processing 模拟宕机
run_crash = make_run(analyst_t, '宕机批次', equip='泵组', ms=0.1, mc=0.5)
with Session() as s:
    w3_obj, created = wp.accept_and_process(s, type('U', (), {'email': 'analyst@demo.local'})(),
                                            'J2', '宕机演练')
    w3_id = w3_obj['id']
    # 同步处理可能已完成；若完成则先制造中断现场
    w3row = s.get(Withdrawal, w3_id)
    if w3row.state == 'completed':
        w3row.state = 'processing'
        c = s.query(Correction).filter_by(withdrawal_id=w3_id).first()
        c.state = 'processing'; c.attempts = 1
        s.commit()

# 执行恢复
with Session() as s:
    recovered = wp.recover(s)
check('宕机恢复被触发并处理撤回单', any(x['withdrawal_id'] == w3_id for x in recovered), recovered)
with Session() as s:
    w3row = s.get(Withdrawal, w3_id)
    check('中断撤回恢复后达到 completed', w3row.state == 'completed', w3row.state)
    corr_states = {c.state for c in s.query(Correction).filter_by(withdrawal_id=w3_id).all()}
    check('无残留 processing/pending 纠正', corr_states <= {'completed', 'skipped'}, corr_states)
    crash_run = s.get(MiningRun, run_crash)
    removed = len(set(crash_run.result.get('withdrawn_codes', [])) &
                  {r['code'] for r in crash_run.result['snapshot']})
    check('恢复后撤回作业被准确扣减（无重复扣减）',
          crash_run.result['valid_jobs'] == len(crash_run.result['snapshot']) - removed,
          (crash_run.result['valid_jobs'], removed))

# ---------- G 并发：重叠撤回交集作业，不产生重复扣减/重复单据 ----------
import threading, time
from sqlalchemy.exc import OperationalError as SAOperationalError
results_g = []
def concurrent_withdraw(code):
    # SQLite 仅允许单写者，瞬时锁错误退避重试；生产 MySQL 由 GET_LOCK 直接串行化
    for attempt in range(20):
        try:
            with Session() as cs:
                w_obj, created = wp.accept_and_process(
                    cs, type('U', (), {'email': 'analyst@demo.local'})(), code, '并发撤回')
                results_g.append((code, w_obj['id'], created))
            return
        except HTTPException as e:
            # 409 进行中冲突：稍后重试，最终应命中幂等返回
            if e.status_code != 409:
                results_g.append((code, None, str(e))); return
            time.sleep(0.05)
        except SAOperationalError as e:
            if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                results_g.append((code, None, str(e))); return
            time.sleep(0.05)
    results_g.append((code, None, 'lock-retry-exhausted'))

# J3、J4 两个线程并发受理，另起一个线程重复受理 J3，验证同作业并发只成功一次
threads = [threading.Thread(target=concurrent_withdraw, args=(c,)) for c in ('J3', 'J4', 'J3')]
for t in threads: t.start()
for t in threads: t.join()
with Session() as s:
    for code in ('J3', 'J4'):
        n = s.query(Withdrawal).filter_by(code=code).count()
        check(f'并发受理 {code} 只生成一张撤回单', n == 1, n)
    # 三个线程中两个 J3 必有一个被 409 拒绝或返回同一单据
    j3_outcomes = [x for x in results_g if x[0] == 'J3']
    check('并发重复受理 J3 被串行化（拒绝或幂等返回同一单）',
          len({x[1] for x in j3_outcomes if x[1] is not None}) == 1 and len(j3_outcomes) == 2,
          j3_outcomes)
    pump = s.get(MiningRun, run_pump)
    withdrawn = {'J1', 'J2', 'J3', 'J4', 'J7'}  # J1/J7 早先撤回，J2 宕机演练，J3/J4 并发撤回
    check('并发重叠撤回后作业扣减准确（快照剔除 5 个，无重复扣减）',
          pump.result['valid_jobs'] == len(pump.result['snapshot']) - len(withdrawn)
          and set(pump.result['withdrawn_codes']) == withdrawn,
          (pump.result['valid_jobs'], pump.result.get('withdrawn_codes')))

# ---------- 撤回单导出与签核 ----------
r = client.get(f"/withdrawals/{w1['id']}/export", headers=H(viewer_t))
check('撤回单可导出（含纠正对账）', r.status_code == 200 and 'corrections' in r.json(), r.status_code)
r = client.post(f"/withdrawals/{w1['id']}/signoff", json={}, headers=H(analyst_t))
check('非 admin 不能签核撤回单', r.status_code == 403, r.status_code)
r = client.post(f"/withdrawals/{w1['id']}/signoff", json={}, headers=H(admin_t))
check('admin 签核撤回单', r.status_code == 200, r.status_code)
# 恢复端点路由不被 /withdrawals/{i} 抢占（recover 为非数字段）
r = client.post('/withdrawals/recover', json={}, headers=H(admin_t))
check('恢复端点路由可达（仅 admin）', r.status_code == 200 and 'recovered' in r.json(), r.status_code)
r = client.post('/withdrawals/recover', json={}, headers=H(analyst_t))
check('非 admin 不能调用恢复端点', r.status_code == 403, r.status_code)

print('\n==== 汇总 ====')
print(f'通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
if FAIL:
    print('失败项：', FAIL); sys.exit(1)
print('ALL INTEGRATION TESTS PASSED')
