from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Job(Base):
 __tablename__='jobs'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);equipment=Column(String(40));parts=Column(JSON);note=Column(Text,default='')
 # 撤回标记：空表示有效；记录撤回单 id 表示该误采作业已撤回，新挖掘样本必须排除
 withdrawal_id=Column(Integer,ForeignKey('withdrawals.id'),nullable=True)
class MiningRun(Base):
 __tablename__='mining_runs'
 id=Column(Integer,primary_key=True);name=Column(String(100));equipment=Column(String(40));min_support=Column(Float);min_confidence=Column(Float);min_lift=Column(Float);state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
 # 隔离状态：撤回受理后受影响批次立即 quarantined，重算完成转 cleaned，未受影响保持 cleared
 quarantine=Column(String(20),default='cleared',nullable=False)
 # 最近一次作用于该批次的撤回单 id（仅作展示追溯）
 last_withdrawal_id=Column(Integer,nullable=True)
class AssociationRule(Base):
 __tablename__='association_rules'
 id=Column(Integer,primary_key=True);run_id=Column(Integer,ForeignKey('mining_runs.id'));signature=Column(String(200));antecedent=Column(JSON);consequent=Column(JSON);support=Column(Float);confidence=Column(Float);lift=Column(Float);count=Column(Integer)
 # active 仍成立（重算后更新证据指标）；invalid 规则消失，保留行用于对账和备件包失效溯源
 status=Column(String(20),default='active',nullable=False)
 __table_args__=(UniqueConstraint('run_id','signature'),)
class Kit(Base):
 __tablename__='kits'
 id=Column(Integer,primary_key=True);name=Column(String(100));rule_id=Column(Integer,ForeignKey('association_rules.id'));parts=Column(JSON);note=Column(Text);status=Column(String(20),default='draft')
 # 失效证据：记录使其失效的撤回单 id 与原因，已签核方案同样保留记录不物理删除
 invalidated_by=Column(Integer,ForeignKey('withdrawals.id'),nullable=True);invalid_reason=Column(Text,default='')
class Withdrawal(Base):
 """误采作业撤回单：按作业号受理，驱动历史批次重算与备件包失效。"""
 __tablename__='withdrawals'
 id=Column(Integer,primary_key=True)
 code=Column(String(60),nullable=False)                 # 被撤回作业号
 job_id=Column(Integer,ForeignKey('jobs.id'),nullable=True)
 reason=Column(Text,default='')
 state=Column(String(20),default='accepted',nullable=False)
 # 影响明细快照（受理时冻结）：受影响批次 id、备件包 id、新挖掘是否排除
 impact=Column(JSON,nullable=True)
 # 前后计数对账汇总（每个受影响批次一条）
 reconciliation=Column(JSON,nullable=True)
 error=Column(Text,default='')
 processed_at=Column(String(40),nullable=True)
 created=Column(String(40),nullable=False)
 __table_args__=(UniqueConstraint('code','state'),)
class Correction(Base):
 """撤回纠正项：一个撤回单 × 一个受影响历史批次/备件包，一条可恢复的工作单元。"""
 __tablename__='corrections'
 id=Column(Integer,primary_key=True)
 withdrawal_id=Column(Integer,ForeignKey('withdrawals.id'),nullable=False)
 target_type=Column(String(20),nullable=False)        # run / kit
 target_id=Column(Integer,nullable=False)
 state=Column(String(20),default='pending',nullable=False)
 detail=Column(JSON,nullable=True)                     # 前后计数、规则增删改等对账数据
 attempts=Column(Integer,default=0,nullable=False)
 error=Column(Text,default='')
 __table_args__=(UniqueConstraint('withdrawal_id','target_type','target_id'),)
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
