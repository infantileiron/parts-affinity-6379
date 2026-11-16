ROLES=('admin','analyst','viewer')
STATES=('queued','running','done','failed')
EQUIPMENT=('泵组','压缩机','风机')
PARTS=('轴承', '密封圈', '润滑脂', '滤芯', '垫片', '联轴器', '皮带', '紧固件', '叶轮', '阀片', '传感器', '清洗剂')
# 撤回单状态：受理后纠正从 pending 开始，处理中 processing，完成 completed，失败 failed（可恢复重试）
WITHDRAWAL_STATES=('accepted','processing','completed','failed')
# 撤回纠正项（逐历史批次/备件包）状态
CORRECTION_STATES=('pending','processing','completed','skipped','failed')
# 规则在撤回重算后的状态：active 仍成立；invalid 规则消失，来源备件包随之失效
RULE_STATES=('active','invalid')
# 备件包状态：draft 起草；approved 已签核；invalidated 来源规则消失后失效
KIT_STATUSES=('draft','approved','invalidated')
# 挖掘批次隔离状态：cleared 未受影响/已纠正干净；quarantined 撤回受理后隔离；cleaned 重算完成且仍有结果
RUN_QUARANTINE=('cleared','quarantined','cleaned')
