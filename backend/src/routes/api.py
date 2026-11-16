import os,json
from datetime import datetime,timezone
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from src.config.database import db
from src.models.entities import User,Job,MiningRun,AssociationRule,Kit,Audit,Withdrawal,Correction
from src.schemas.business import Login,JobInput,RunInput,KitInput,KitEdit,WithdrawalInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.jobs import save_job
from src.services import withdrawal_processing as wp
from src.controllers.business import ensure_run,ensure_rule,ensure_kit
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/jobs')
def jobs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Job).order_by(Job.id).all()]
@router.post('/jobs')
def create_job(data:JobInput,u=Depends(editor),s=Depends(db)):
 r=save_job(s,data);audit(s,u,'录入作业 '+r['code']);return r
@router.put('/jobs/{i}')
def edit_job(i:int,data:JobInput,u=Depends(editor),s=Depends(db)):
 r=save_job(s,data,i);audit(s,u,'修订作业 '+str(i));return r
@router.get('/runs')
def runs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(MiningRun).order_by(MiningRun.id.desc()).all()]
@router.post('/runs')
def run(data:RunInput,u=Depends(editor),s=Depends(db)):
 # 新挖掘样本排除全部已撤回作业，污染不进入新批次
 rows=[dump(r) for r in s.query(Job).order_by(Job.id).all()
       if (data.equipment=='全部' or r.equipment==data.equipment) and r.withdrawal_id is None]
 if not rows:raise HTTPException(409,'有效作业全部撤回，无法建立挖掘样本')
 r=MiningRun(**data.model_dump(),quarantine='cleared',result={'snapshot':rows});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('affinity:runs',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，请恢复后新建挖掘';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交挖掘 '+str(r.id));return dump(r)
@router.get('/runs/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_run(s,i))
@router.get('/rules')
def rules(run_id:int,u=Depends(current),s=Depends(db)):
 ensure_run(s,run_id);return [dump(r) for r in s.query(AssociationRule).filter_by(run_id=run_id).order_by(AssociationRule.lift.desc()).all()]
@router.get('/runs/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_run(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功挖掘可以导出')
 # 隔离中的污染批次禁止导出；cleaned 批次导出带撤回对账，保证不放出污染结果
 if r.quarantine=='quarantined':raise HTTPException(409,'该批次受误采作业影响已隔离，撤回纠正完成前禁止导出')
 payload=dump(r)
 if r.quarantine=='cleaned':payload['quarantine_note']='该批次已按撤回作业剔除重算，规则证据为纠正后口径'
 return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="parts-affinity-{i}.json"'})
@router.get('/kits')
def kits(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Kit).order_by(Kit.id.desc()).all()]
@router.post('/kits')
def kit(data:KitInput,u=Depends(editor),s=Depends(db)):
 rule=ensure_rule(s,data.rule_id)
 run=ensure_run(s,rule.run_id)
 if run.state!='done':raise HTTPException(409,'来源规则尚未完成')
 if run.quarantine=='quarantined':raise HTTPException(409,'来源批次处于隔离，纠正完成前不能起草备件包')
 if rule.status=='invalid':raise HTTPException(409,'来源规则已因撤回失效，不能据此起草备件包')
 r=Kit(**data.model_dump(),parts=sorted(set(rule.antecedent+rule.consequent)));s.add(r);s.commit();audit(s,u,'起草备件包 '+str(r.id));return dump(r)
@router.put('/kits/{i}')
def edit_kit(i:int,data:KitEdit,u=Depends(editor),s=Depends(db)):
 r=ensure_kit(s,i)
 if r.status=='invalidated':raise HTTPException(409,'备件包已随来源规则失效，不可修改')
 if r.status!='draft':raise HTTPException(409,'已签核备件包不可覆盖，需新建方案')
 r.name=data.name;r.note=data.note;s.commit();audit(s,u,'修改备件包 '+str(i));return dump(r)
@router.post('/kits/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure_kit(s,i)
 if r.status=='invalidated':raise HTTPException(409,'备件包已失效，不能签核')
 if r.status!='draft':raise HTTPException(409,'已签核方案不可重复签核')
 rule=ensure_rule(s,r.rule_id);run=ensure_run(s,rule.run_id)
 # 签核前再校验：隔离批次/失效规则绝不放出
 if run.quarantine=='quarantined' or rule.status=='invalid':raise HTTPException(409,'来源存在未纠正的污染，签核已拦截')
 r.status='approved';s.commit();audit(s,u,'签核备件包 '+str(i));return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

# ---- 误采作业撤回 ----
@router.get('/withdrawals')
def list_withdrawals(u=Depends(current),s=Depends(db)):
 return [dump(w) for w in s.query(Withdrawal).order_by(Withdrawal.id.desc()).all()]
@router.post('/withdrawals/recover')
def recover(u=Depends(admin),s=Depends(db)):
 result=wp.recover(s);audit(s,u,'恢复中断的撤回纠正');return {'recovered':result}
@router.get('/withdrawals/{i}')
def withdrawal_detail(i:int,u=Depends(current),s=Depends(db)):
 w=s.get(Withdrawal,i)
 if not w:raise HTTPException(404,'撤回单不存在')
 corrections=s.query(Correction).filter_by(withdrawal_id=i).order_by(Correction.id).all()
 return {'withdrawal':dump(w),'corrections':[dump(c) for c in corrections]}
@router.post('/withdrawals')
def create_withdrawal(data:WithdrawalInput,u=Depends(editor),s=Depends(db)):
 code=data.code.strip()
 try:
  w,created=wp.accept_and_process(s,u,code,data.reason)
 except HTTPException:
  raise
 if created:audit(s,u,f'受理撤回 {code} 撤回单#{w["id"]}')
 return w
@router.post('/withdrawals/{i}/retry')
def retry_withdrawal(i:int,u=Depends(editor),s=Depends(db)):
 w=s.get(Withdrawal,i)
 if not w:raise HTTPException(404,'撤回单不存在')
 if w.state=='completed':return dump(w)  # 幂等：重试已完成单不重复扣减
 ww,_=wp.process_withdrawal(s,i);audit(s,u,f'恢复撤回纠正 #{i}');return dump(ww)
@router.get('/withdrawals/{i}/export')
def withdrawal_export(i:int,u=Depends(current),s=Depends(db)):
 w=s.get(Withdrawal,i)
 if not w:raise HTTPException(404,'撤回单不存在')
 if w.state!='completed':raise HTTPException(409,'撤回纠正未完成，暂不能导出签核包')
 corrections=s.query(Correction).filter_by(withdrawal_id=i).order_by(Correction.id).all()
 payload={'withdrawal':dump(w),'corrections':[dump(c) for c in corrections],
          'exported_at':datetime.now(timezone.utc).isoformat()}
 return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',
  headers={'Content-Disposition':f'attachment; filename="withdrawal-{i}.json"'})
@router.post('/withdrawals/{i}/signoff')
def signoff(i:int,u=Depends(admin),s=Depends(db)):
 w=s.get(Withdrawal,i)
 if not w:raise HTTPException(404,'撤回单不存在')
 if w.state!='completed':raise HTTPException(409,'撤回纠正未完成，不能签核')
 audit(s,u,f'签核撤回单 #{i}（作业 {w.code}）')
 return dump(w)
