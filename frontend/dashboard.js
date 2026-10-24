/* Financial dashboard backed by the authenticated, locally stored Pluggy data. */
let dashData=null;
let dashShown=200;
const dashEl=id=>document.getElementById(id);
const dashEsc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dashMoney=value=>new Intl.NumberFormat('pt-BR',{style:'currency',currency:'BRL'}).format(Number(value)||0);
const dashName=item=>item?.display_name||item?.connector_name||'Banco';
const dashEmpty=message=>`<div class="pg-empty">${dashEsc(message)}</div>`;

function dashDefaultDates(){
  const now=new Date(),y=now.getFullYear(),m=String(now.getMonth()+1).padStart(2,'0');
  dashEl('dash-from').value=`${y}-${m}-01`;
  dashEl('dash-to').value=`${y}-${m}-${String(new Date(y,now.getMonth()+1,0).getDate()).padStart(2,'0')}`;
}

async function dashLoad(){
  if(!token)return;
  if(!dashEl('dash-from').value||!dashEl('dash-to').value)dashDefaultDates();
  const from=dashEl('dash-from').value,to=dashEl('dash-to').value;
  if(to<from){dashEl('dash-status-text').textContent='A data final deve ser igual ou posterior à inicial.';return;}
  dashEl('dash-status-text').textContent='Carregando dados dos bancos...';
  try{
    dashData=await api('GET',`/pluggy/analytics?from_date=${encodeURIComponent(from)}&to_date=${encodeURIComponent(to)}`);
    const bank=dashEl('dash-bank').value,category=dashEl('dash-category').value;
    dashEl('dash-bank').innerHTML='<option value="">Todos os bancos</option>'+dashData.items.map(i=>`<option value="${dashEsc(i.id)}">${dashEsc(dashName(i))}</option>`).join('');
    dashEl('dash-bank').value=bank;
    dashEl('dash-category').innerHTML='<option value="">Todas</option>'+dashData.categories.map(c=>`<option value="${dashEsc(c.nome)}">${dashEsc(c.label)}</option>`).join('')+'<option value="__none__">Sem categoria</option>';
    dashEl('dash-category').value=category;
    dashBankChanged();
    dashEl('dash-status-text').textContent=`${dashData.transactions.length.toLocaleString('pt-BR')} movimentações carregadas no período.${dashData.truncated?' Limite de 20.000 atingido: reduza o período para ver todos os dados.':''}`;
  }catch(e){dashEl('dash-status-text').textContent='Erro ao carregar Dashboard: '+e.message;}
}

function dashBankChanged(){
  if(!dashData)return;
  const bank=dashEl('dash-bank').value,old=dashEl('dash-account').value;
  const accounts=dashData.accounts.filter(a=>!bank||a.item_id===bank);
  dashEl('dash-account').innerHTML='<option value="">Todas as contas</option>'+accounts.map(a=>`<option value="${dashEsc(a.id)}">${dashEsc(a.name)} · ${a.type==='CREDIT'?'Cartão':'Conta'}</option>`).join('');
  dashEl('dash-account').value=old;
  dashRender();
}

function dashMethod(t){
  const method=String(t.payment_method||'').toUpperCase();
  const description=String(t.description||'').toUpperCase();
  if(t.account_type==='CREDIT')return 'CARD';
  if(method.includes('PIX')||description.includes('PIX'))return 'PIX';
  if(method.includes('TRANSFER')||description.includes('TRANSFER'))return 'TRANSFER';
  return 'OTHER';
}

function dashFiltered(){
  if(!dashData)return [];
  const bank=dashEl('dash-bank').value,account=dashEl('dash-account').value,type=dashEl('dash-type').value;
  const direction=dashEl('dash-direction').value,status=dashEl('dash-status').value;
  const category=dashEl('dash-category').value,method=dashEl('dash-method').value;
  const search=dashEl('dash-search').value.trim().toLocaleLowerCase('pt-BR');
  return dashData.transactions.filter(t=>(!bank||t.item_id===bank)&&(!account||t.account_id===account)&&
    (!type||t.account_type===type)&&(!direction||t.direction===direction)&&(!status||t.status===status)&&
    (!category||(category==='__none__'?!t.user_category:t.user_category===category))&&
    (!method||dashMethod(t)===method)&&
    (!search||`${t.description} ${t.recipient_name||''}`.toLocaleLowerCase('pt-BR').includes(search)));
}

function dashRender(reset=true){
  if(!dashData)return;
  if(reset)dashShown=200;
  const filtered=dashFiltered(),posted=filtered.filter(t=>t.status==='POSTED');
  const bank=dashEl('dash-bank').value,account=dashEl('dash-account').value,type=dashEl('dash-type').value;
  const visibleAccounts=dashData.accounts.filter(a=>(!bank||a.item_id===bank)&&(!account||a.id===account)&&(!type||a.type===type));
  const visibleBills=dashData.bills.filter(b=>(!bank||b.item_id===bank)&&(!account||b.account_id===account)&&type!=='BANK');
  const bankOut=posted.filter(t=>t.account_type==='BANK'&&t.direction==='DEBIT').reduce((sum,t)=>sum+Math.abs(t.amount),0);
  const cardOut=posted.filter(t=>t.account_type==='CREDIT'&&t.direction==='DEBIT').reduce((sum,t)=>sum+Math.abs(t.amount),0);
  const bankIn=posted.filter(t=>t.account_type==='BANK'&&t.direction==='CREDIT').reduce((sum,t)=>sum+Math.abs(t.amount),0);
  const balance=visibleAccounts.filter(a=>a.type==='BANK'&&a.balance!=null).reduce((sum,a)=>sum+a.balance,0);
  const billTotal=visibleBills.reduce((sum,b)=>sum+Math.max(0,b.total_amount),0);
  const billRemaining=visibleBills.reduce((sum,b)=>sum+Math.max(0,b.total_amount-Math.max(0,b.paid_amount)),0);
  const metrics=[['Saldo das contas',dashMoney(balance),'Saldo sincronizado; Neon manual sem saldo'],['Saídas da conta',dashMoney(bankOut),'Pix, transferências e faturas pagas'],['Compras no cartão',dashMoney(cardOut),'Separadas das saídas da conta'],['Entradas na conta',dashMoney(bankIn),'Inclui transferências recebidas'],['Faturas no período',dashMoney(billTotal),`${visibleBills.length} fatura(s) com vencimento`],['Faturas a pagar',dashMoney(billRemaining),'Conforme pagamentos informados pela Pluggy']];
  dashEl('dash-metrics').innerHTML=metrics.map(([name,value,note])=>`<div class="dash-metric"><small>${dashEsc(name)}</small><strong>${dashEsc(value)}</strong><small>${dashEsc(note)}</small></div>`).join('');

  const selectedItems=dashData.items.filter(i=>!bank||i.id===bank);
  const maxSpend=Math.max(1,...selectedItems.map(i=>posted.filter(t=>t.item_id===i.id&&t.direction==='DEBIT'&&t.account_type==='BANK').reduce((s,t)=>s+Math.abs(t.amount),0)));
  dashEl('dash-banks').innerHTML=selectedItems.length?selectedItems.map(i=>{
    const rows=posted.filter(t=>t.item_id===i.id),out=rows.filter(t=>t.account_type==='BANK'&&t.direction==='DEBIT').reduce((s,t)=>s+Math.abs(t.amount),0);
    const card=rows.filter(t=>t.account_type==='CREDIT'&&t.direction==='DEBIT').reduce((s,t)=>s+Math.abs(t.amount),0);
    const count=visibleAccounts.filter(a=>a.item_id===i.id).length;
    return `<div class="dash-row"><div style="flex:1"><b>${dashEsc(dashName(i))}</b><small>${count} conta(s) · Conta ${dashMoney(out)} · Cartão ${dashMoney(card)}</small><div class="dash-bar"><span style="width:${Math.round(out/maxSpend*100)}%"></span></div></div></div>`;
  }).join(''):dashEmpty('Nenhum banco neste filtro.');

  const categoryMap=new Map();
  for(const t of posted){if(t.direction!=='DEBIT')continue;const label=dashData.categories.find(c=>c.nome===t.user_category)?.label||'Sem categoria';const key=`${t.account_type==='CREDIT'?'Cartão':'Conta'} · ${label}`;categoryMap.set(key,(categoryMap.get(key)||0)+Math.abs(t.amount));}
  const categoryRows=[...categoryMap].sort((a,b)=>b[1]-a[1]);
  const maxCategory=Math.max(1,...categoryRows.map(r=>r[1]));
  dashEl('dash-categories').innerHTML=categoryRows.length?categoryRows.slice(0,12).map(([name,value])=>`<div class="dash-row"><div style="flex:1"><b>${dashEsc(name)}</b><div class="dash-bar"><span style="width:${Math.round(value/maxCategory*100)}%"></span></div></div><strong>${dashMoney(value)}</strong></div>`).join(''):dashEmpty('Sem saídas nos filtros escolhidos.');

  const itemById=new Map(dashData.items.map(i=>[i.id,i]));
  dashEl('dash-bills').innerHTML=visibleBills.length?visibleBills.map(b=>{
    const paid=Math.min(Math.max(0,b.paid_amount),Math.max(0,b.total_amount));
    const remaining=Math.max(0,b.total_amount-paid);
    return `<div class="dash-row"><div><b>${dashEsc(dashName(itemById.get(b.item_id)))}</b><small>${dashEsc(b.account_name)} · vence ${dashEsc(b.due_date.split('-').reverse().join('/'))}</small><small>${remaining<0.01?'Paga':`Pago ${dashMoney(paid)} · resta ${dashMoney(remaining)}`}</small></div><strong>${dashMoney(b.total_amount)}</strong></div>`;
  }).join(''):dashEmpty('A Pluggy ainda não trouxe faturas com vencimento neste período.');

  const months=new Map();
  for(const t of posted){const key=t.transaction_date.slice(0,7);if(!months.has(key))months.set(key,{bank:0,card:0,income:0});const row=months.get(key);if(t.account_type==='BANK'&&t.direction==='DEBIT')row.bank+=Math.abs(t.amount);if(t.account_type==='CREDIT'&&t.direction==='DEBIT')row.card+=Math.abs(t.amount);if(t.account_type==='BANK'&&t.direction==='CREDIT')row.income+=Math.abs(t.amount);}
  dashEl('dash-months').innerHTML=months.size?[...months].sort((a,b)=>a[0].localeCompare(b[0])).map(([month,row])=>`<div class="dash-row"><div><b>${dashEsc(month.split('-').reverse().join('/'))}</b><small>Conta ${dashMoney(row.bank)} · Cartão ${dashMoney(row.card)}</small></div><strong style="color:var(--green)">+${dashMoney(row.income)}</strong></div>`).join(''):dashEmpty('Sem movimentações efetivadas neste período.');

  const shown=filtered.slice(0,dashShown);
  dashEl('dash-count').textContent=`Exibindo ${shown.length.toLocaleString('pt-BR')} de ${filtered.length.toLocaleString('pt-BR')} movimentações filtradas. Compras no cartão e pagamento da fatura são exibidos separadamente.`;
  dashEl('dash-transactions').innerHTML=shown.length?shown.map(t=>`<div class="dash-txn"><div><b>${dashEsc(t.description)}</b><small>${dashEsc(t.transaction_date.split('-').reverse().join('/'))} · ${dashEsc(dashName(itemById.get(t.item_id)))} · ${dashEsc(t.account_name)} · ${dashEsc(t.recipient_name||t.payment_method||'')} · ${t.status==='POSTED'?'Efetivado':'Pendente'}</small></div><strong style="color:${t.direction==='DEBIT'?'var(--red)':'var(--green)'}">${t.direction==='DEBIT'?'-':'+'}${dashMoney(Math.abs(t.amount))}</strong></div>`).join(''):dashEmpty('Nenhuma movimentação corresponde aos filtros.');
  dashEl('dash-more').style.display=filtered.length>dashShown?'inline-flex':'none';
}

function dashMore(){dashShown+=200;dashRender(false);}
function dashReset(){
  dashDefaultDates();
  for(const id of ['dash-bank','dash-account','dash-type','dash-direction','dash-status','dash-category','dash-method','dash-search'])dashEl(id).value='';
  dashLoad();
}
