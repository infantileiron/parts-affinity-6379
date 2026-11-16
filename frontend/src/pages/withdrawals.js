import {state} from '../stores/session.js';
import {esc} from '../utils/html.js';
import {badge} from '../components/common/status.js';
import {field} from '../components/common/fields.js';
import {WITHDRAWAL_STATES,CORRECTION_STATES,QUARANTINE} from '../constants/enums.js';

const wstate=s=>WITHDRAWAL_STATES[s]||s;
const cstate=s=>CORRECTION_STATES[s]||s;

function impactBlock(w){
 const imp=w.impact||{};
 const runs=(imp.runs||[]).map(r=>`<tr><td>#${r.run_id} ${esc(r.run_name)}</td><td><span class="tag">${QUARANTINE[r.quarantine]||r.quarantine}</span></td><td>${(r.kit_ids||[]).map(k=>'#'+k).join('、')||'—'}</td></tr>`).join('');
 return `<div class="scroll"><table><thead><tr><th>受影响历史批次</th><th>隔离状态（受理时）</th><th>牵连备件包</th></tr></thead><tbody>${runs||'<tr><td>无冻结批次包含该作业</td><td>—</td><td>—</td></tr>'}</tbody></table></div>
 <p class="notice">新挖掘样本已排除该作业：${imp.excluded_from_new_mining?'是':'否'} · 受影响备件包 #${(imp.affected_kit_ids||[]).join('、#')||'无'}</p>`;
}

function reconciliationBlock(detail){
 if(!detail)return '<p class="notice">该纠正项暂无对账数据。</p>';
 const b=detail.before||{},a=detail.after||{};
 const row=(k,label)=>`<tr><td>${label}</td><td>${b[k]??'—'}</td><td>${a[k]??'—'}</td></tr>`;
 const upd=(detail.updated_rules||[]).map(u=>`<li>${esc(u.signature)}：count ${u.before.count}→${u.after.count}，lift ${Number(u.before.lift).toFixed(3)}→${Number(u.after.lift).toFixed(3)}</li>`).join('');
 return `<div class="scroll"><table><thead><tr><th>指标</th><th>撤回前</th><th>撤回后</th></tr></thead><tbody>
   ${row('valid_jobs','有效事务数')}${row('excluded','空事务排除')}${row('minimum_count','最小计数阈值')}
   ${row('frequent_count','频繁项集数')}${row('rule_count','关联规则数')}</tbody></table></div>
 <p class="notice">剔除作业：${(detail.removed_codes||[]).map(esc).join('、')||'无'}</p>
 <p>仍成立规则（保留原引用并更新证据）：${(detail.kept_rule_signatures||[]).map(esc).join('、')||'无'}</p>
 <p>消失规则（来源备件包随之失效）：${(detail.removed_rule_signatures||[]).map(esc).join('、')||'无'}</p>
 ${upd?`<p>证据更新明细：<ul>${upd}</ul></p>`:''}`;
}

export function render(){
 const w=state.selectedWithdrawal;
 return `<div class="split">
 <section class="panel">
  <h2>误采作业撤回与影响追踪 ${w?badge(wstate(w.state)):''}</h2>
  ${w?`<h3>撤回单 #${w.id} · 作业 ${esc(w.code)}</h3>
   <p>原因：${esc(w.reason||'（未填写）')} · 受理于 ${esc((w.created||'').replace('T',' ').slice(0,19))}${w.processed_at?` · 完成于 ${esc(w.processed_at.replace('T',' ').slice(0,19))}`:''}</p>
   ${w.error?`<p class="error">${esc(w.error)}</p>`:''}
   <h3>影响明细</h3>${impactBlock(w)}
   <h3>纠正进度与前后计数对账</h3>
   <div id="correctionList"><p class="notice">加载中…</p></div>
   <div class="metrics">
     <button id="retryBtn" ${w.state==='completed'?'disabled':''}>恢复/重试纠正</button>
     <button id="exportBtn" ${w.state!=='completed'?'disabled':''}>下载撤回对账 JSON</button>
     ${state.user.role==='admin'?`<button id="signoffBtn" class="primary" ${w.state!=='completed'?'disabled':''}>负责人签核</button>`:''}
   </div>`
   :'<p class="notice">在右侧按作业号受理误采撤回。受理后受影响历史批次立即隔离，纠正完成前禁止导出与签核污染结果。</p>'}
  <h3>撤回单列表</h3>
  ${state.withdrawals.map(x=>`<button class="study" data-wd="${x.id}">#${x.id} ${esc(x.code)} · ${wstate(x.state)}</button>`).join('')||'<p class="notice">暂无撤回单。</p>'}
 </section>
 <aside class="side">
  <h2>受理撤回</h2>
  <form id="withdrawal">
   ${field('误采作业号','code','text','required placeholder="如 YH-1115-018"')}
   <label>撤回原因<textarea name="reason" rows="4" maxlength="1000" placeholder="维修站确认误采的背景与影响范围"></textarea></label>
   <p>受理即：标记作业撤回（新挖掘排除）、隔离含该作业的历史批次、按各批次冻结样本剔除重算并保留前后计数。重试同一撤回不会重复扣减。</p>
   <button class="primary">受理并执行纠正</button>
  </form>
  <p><button id="refresh">刷新进度</button></p>
 </aside></div>`;
}

export async function loadCorrections(api){
 const host=document.querySelector('#correctionList');
 if(!host||!state.selectedWithdrawal)return;
 const w=state.selectedWithdrawal;
 if(w.state==='accepted'||w.state==='processing'||w.state==='failed'){
  const d=await api('/withdrawals/'+w.id);
  const items=d.corrections.map(c=>`<details><summary>批次 #${c.target_id} · ${cstate(c.state)} · 尝试 ${c.attempts} 次 ${c.error?esc(c.error):''}</summary>${reconciliationBlock(c.detail)}</details>`).join('');
  host.innerHTML=items||'<p class="notice">该撤回单没有需要纠正的历史批次。</p>';
 }else{
  host.innerHTML=(w.reconciliation||[]).length
    ? w.reconciliation.map((detail,i)=>`<details ${i===0?'open':''}><summary>纠正项 #${i+1} · ${cstate('completed')}</summary>${reconciliationBlock(detail)}</details>`).join('')
    : '<p class="notice">该撤回单没有需要纠正的历史批次。</p>';
 }
}
