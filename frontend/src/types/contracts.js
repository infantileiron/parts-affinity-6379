/** @typedef {{id:number,code:string,equipment:string,parts:string[],note:string,withdrawal_id:number|null}} Job */
/** @typedef {{id:number,name:string,state:string,quarantine:string,result:object,last_withdrawal_id:number|null}} MiningRun */
/** @typedef {{id:number,run_id:number,antecedent:string[],consequent:string[],support:number,confidence:number,lift:number,count:number,status:string}} AssociationRule */
/** @typedef {{id:number,name:string,rule_id:number,parts:string[],note:string,status:string,invalidated_by:number|null,invalid_reason:string}} Kit */
/** @typedef {{id:number,code:string,job_id:number|null,reason:string,state:string,impact:object,reconciliation:object[]|null,error:string,processed_at:string|null,created:string}} Withdrawal */
/** @typedef {{id:number,withdrawal_id:number,target_type:string,target_id:number,state:string,detail:object|null,attempts:number,error:string}} Correction */
