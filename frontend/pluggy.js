/* Pluggy UI. All financial records are read from this app's authenticated API. */
let pgData = null;
const pgEsc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));
const pgMoney = value => new Intl.NumberFormat('pt-BR', {style:'currency',currency:'BRL'}).format(Number(value)||0);
const pgMonth = () => document.getElementById('pg-month').value || new Date().toISOString().slice(0,7);
const pgStatus = message => { document.getElementById('pg-status').textContent = message; };

async function pgLoad(){
  if(!token)return;
  const input=document.getElementById('pg-month');
  if(!input.value){const d=new Date();input.value=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;}
  const manualDate=document.getElementById('pg-manual-date');
  if(!manualDate.value||manualDate.value.slice(0,7)!==input.value){
    const today=new Date();const localToday=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
    manualDate.value=localToday.slice(0,7)===input.value?localToday:input.value+'-01';
  }
  pgStatus('Carregando bancos e movimentações...');
  try{
    const [status,data]=await Promise.all([api('GET','/pluggy/status'),api('GET','/pluggy/dashboard?month='+encodeURIComponent(pgMonth()))]);
    pgData=data;
    document.getElementById('pg-import-box').style.display=currentUser.role==='admin'?'block':'none';
    pgStatus(status.configured ? `Integração Pluggy configurada${status.sandbox?' · sandbox disponível':''}. A atualização bancária ocorre conforme a conexão.` : 'Configure as credenciais Pluggy no servidor para conectar um banco.');
    pgRender(data);
  }catch(e){pgStatus('Erro ao carregar: '+e.message);}
}

function pgRender(d){
  const bankAccounts=d.accounts.filter(a=>a.type==='BANK');
  const balance=bankAccounts.reduce((sum,a)=>sum+(Number(a.balance)||0),0);
  const planned=d.forecasts.reduce((sum,f)=>sum+f.expected,0);
  const paid=d.forecasts.reduce((sum,f)=>sum+f.paid,0);
  const bankOut=d.transactions.filter(t=>t.account_type==='BANK'&&t.direction==='DEBIT'&&t.status==='POSTED').reduce((sum,t)=>sum+Math.abs(t.amount),0);
  const cardOut=d.transactions.filter(t=>t.account_type==='CREDIT'&&t.direction==='DEBIT'&&t.status==='POSTED').reduce((sum,t)=>sum+Math.abs(t.amount),0);
  document.getElementById('pg-summary').innerHTML=`
    <div class="pg-card"><h3>Saldo das contas conectadas</h3><strong>${pgMoney(balance)}</strong><br><small>Último saldo sincronizado</small></div>
    <div class="pg-card"><h3>Gastos fixos previstos</h3><strong>${pgMoney(planned)}</strong><br><small>Valores do planejamento atual</small></div>
    <div class="pg-card"><h3>Já associados aos fixos</h3><strong>${pgMoney(paid)}</strong><br><small>Pix e saídas vinculados</small></div>
    <div class="pg-card"><h3>Ainda a pagar</h3><strong>${pgMoney(Math.max(0,planned-paid))}</strong><br><small>Dos gastos fixos cadastrados</small></div>
    <div class="pg-card"><h3>Saídas da conta</h3><strong>${pgMoney(bankOut)}</strong><br><small>Inclui Pix, transferências e pagamento de fatura</small></div>
    <div class="pg-card"><h3>Compras no cartão</h3><strong>${pgMoney(cardOut)}</strong><br><small>Mostradas separadamente para evitar soma duplicada</small></div>`;
  document.getElementById('pg-connections').innerHTML=d.items.length ? d.items.map(i=>`<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(i.display_name||i.connector_name||'Instituição conectada')}</b><small>${i.source==='manual'?'Extrato enviado manualmente':`Status: ${pgEsc(i.status)} · Última importação: ${pgEsc(i.last_sync||'aguardando')}${i.source==='dashboard'?' · Via Meu Pluggy':''}`}</small></div>${i.source==='dashboard'||i.source==='manual'?'':`<button class="btn btn-ghost" onclick="pgReconnect('${pgEsc(i.id)}')">Reconectar / autorizar</button>`}</div>`).join('') : '<div class="pg-empty">Nenhuma instituição conectada.</div>';
  document.getElementById('pg-accounts').innerHTML=d.accounts.length ? d.accounts.map(a=>{
    const item=d.items.find(i=>i.id===a.item_id);
    return `<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(a.name)}</b><small>${a.type==='CREDIT'?'Cartão':'Conta bancária'} · ${pgEsc(item?.display_name||item?.connector_name||'Banco conectado')}</small></div><div class="pg-row-side"><b>${a.balance==null?'Saldo indisponível':pgMoney(a.balance)}</b>${item?.source==='manual'?'':`<button class="btn btn-ghost" onclick="pgSync('${pgEsc(a.item_id)}')">Sincronizar dados</button>`}</div></div>`;
  }).join('') : '<div class="pg-empty">Nenhuma conta conectada ainda.</div>';
  document.getElementById('pg-forecasts').innerHTML=d.forecasts.length ? d.forecasts.map(f=>{
    const state=f.remaining<=0.01?'Pago':f.paid>0?'Parcial':'A pagar';
    return `<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(f.name)}</b><small>${state} · Previsto ${pgMoney(f.expected)} · Pago ${pgMoney(f.paid)}</small></div><div class="pg-row-side"><strong>${pgMoney(f.remaining)} restante</strong></div></div>`;
  }).join('') : '<div class="pg-empty">Cadastre gastos fixos na aba Fixos para acompanhar pagamentos.</div>';
  const categoryTotals=new Map();
  for(const t of d.transactions){
    if(t.direction!=='DEBIT'||t.status!=='POSTED')continue;
    const key=(t.account_type==='CREDIT'?'Cartão · ':'Conta · ')+(d.categories.find(c=>c.nome===t.user_category)?.label||'Sem categoria');
    categoryTotals.set(key,(categoryTotals.get(key)||0)+Math.abs(t.amount));
  }
  document.getElementById('pg-category-summary').innerHTML=categoryTotals.size ? [...categoryTotals.entries()].sort((a,b)=>b[1]-a[1]).map(([name,total])=>`<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(name)}</b></div><strong>${pgMoney(total)}</strong></div>`).join('') : '<div class="pg-empty">Nenhuma saída efetivada neste mês.</div>';
  document.getElementById('pg-rule-gasto').innerHTML=d.gastos.map(g=>`<option value="${g.id}">${pgEsc(g.nome)}</option>`).join('');
  document.getElementById('pg-rules').innerHTML=d.rules.length ? d.rules.map(r=>`<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(r.gasto_nome)}</b><small>Reconhecer Pix para ${pgEsc(r.recipient_name)}${r.recipient_document?' · documento cadastrado':''}</small></div><button class="btn btn-ghost" onclick="pgDeleteRule(${r.gasto_id})">Remover regra</button></div>`).join('') : '<div class="pg-empty">Sem regras. Cadastre uma para marcar um Pix automaticamente.</div>';
  document.getElementById('pg-transactions').innerHTML=d.transactions.length ? d.transactions.map(t=>{
    const debit=t.direction==='DEBIT';
    const canLink=t.account_type==='BANK' && debit && t.status==='POSTED';
    const options=d.forecasts.map(f=>`<option value="${f.gasto_id}">${pgEsc(f.name)}</option>`).join('');
    const action=t.matched_gasto_id ? `<button class="btn btn-ghost" onclick="pgUnlink('${pgEsc(t.id)}')">Desvincular</button>` : canLink && options ? `<select id="pg-link-${pgEsc(t.id)}"><option value="">Vincular a fixo...</option>${options}</select><button class="btn btn-ghost" onclick="pgLink('${pgEsc(t.id)}')">Vincular</button>` : '';
    const remove=d.items.find(i=>i.id===t.item_id)?.source==='manual' ? `<button class="btn btn-ghost" onclick="pgDeleteManualEntry('${pgEsc(t.id)}')">Remover</button>` : '';
    const categorySelect=`<select onchange="pgSetCategory('${pgEsc(t.id)}',this.value)"><option value="">Sem categoria</option>${d.categories.map(c=>`<option value="${pgEsc(c.nome)}" ${t.user_category===c.nome?'selected':''}>${pgEsc(c.label)}</option>`).join('')}</select>`;
    const sign=debit?'-':'+';
    return `<div class="pg-row"><div class="pg-row-main"><b>${pgEsc(t.description)}</b><small>${pgEsc(t.transaction_date)} · ${pgEsc(t.account_name)} · ${pgEsc(t.recipient_name||t.payment_method||'')} · ${t.status==='POSTED'?'Efetivado':'Pendente'}${t.matched_gasto_id?' · Associado a gasto fixo':''}</small></div><div class="pg-row-side"><strong>${sign}${pgMoney(Math.abs(t.amount))}</strong>${categorySelect}${action}${remove}</div></div>`;
  }).join('') : '<div class="pg-empty">Nenhuma movimentação importada neste mês.</div>';
}

async function pgConnect(){return pgOpenWidget(null);}
async function pgReconnect(itemId){return pgOpenWidget(itemId);}
async function pgOpenWidget(itemId){
  pgStatus('Preparando conexão segura...');
  try{
    if(typeof PluggyConnect==='undefined')throw new Error('Widget Pluggy indisponível. Confira sua conexão com a internet.');
    const endpoint=itemId?`/pluggy/items/${encodeURIComponent(itemId)}/connect-token`:'/pluggy/connect-token';
    const {accessToken}=await api('POST',endpoint);
    const status=await api('GET','/pluggy/status');
    const widget=new PluggyConnect({connectToken:accessToken,updateItem:itemId||undefined,includeSandbox:status.sandbox,language:'pt',
      onSuccess:async ({item})=>{
        if(!item?.id){pgStatus('Conexão concluída. Aguarde a sincronização automática.');return;}
        pgStatus('Banco conectado. Importando contas e transações...');
        try{await api('POST',`/pluggy/items/${encodeURIComponent(item.id)}/sync`);await pgLoad();}
        catch(e){pgStatus('Banco conectado. A importação será tentada novamente pelo webhook: '+e.message);}
      },
      onError:error=>pgStatus('A conexão não foi concluída: '+(error?.message||'tente novamente.'))});
    widget.init();
  }catch(e){pgStatus(e.message);}
}

async function pgSync(itemId){
  pgStatus('Sincronizando dados já disponíveis na Pluggy...');
  try{await api('POST',`/pluggy/items/${encodeURIComponent(itemId)}/sync`);await pgLoad();}
  catch(e){pgStatus('Falha na sincronização: '+e.message);}
}
async function pgImportItem(){
  const item_id=document.getElementById('pg-import-id').value.trim();
  if(!item_id){pgStatus('Cole o Item ID do Dashboard.');return;}
  pgStatus('Importando conexão do Meu Pluggy...');
  try{await api('POST','/pluggy/import-item',{item_id});document.getElementById('pg-import-id').value='';await pgLoad();}
  catch(e){pgStatus('Falha ao importar item: '+e.message);}
}
async function pgImportStatement(){
  const input=document.getElementById('pg-manual-file');
  const file=input.files?.[0];
  if(!file){pgStatus('Selecione o extrato do Neon.');return;}
  if(!/\.(csv|ofx)$/i.test(file.name)||file.size>5*1024*1024){pgStatus('Escolha um CSV ou OFX de até 5 MB.');return;}
  const form=new FormData();form.append('month',pgMonth());form.append('file',file);
  pgStatus('Importando extrato do Neon...');
  try{
    const response=await fetch('/api/pluggy/manual-import',{method:'POST',headers:{Authorization:'Bearer '+token},body:form});
    const result=await response.json();
    if(!response.ok)throw new Error(result.detail||'Falha na importação');
    input.value='';
    await pgLoad();
    pgStatus(`Neon: ${result.imported} movimentações importadas, ${result.duplicates} já existentes no mês ${result.month}.`);
  }catch(e){pgStatus('Falha ao importar extrato: '+e.message);}
}
async function pgAddManualEntry(){
  const transaction_date=document.getElementById('pg-manual-date').value;
  const description=document.getElementById('pg-manual-desc').value.trim();
  const amount=Number(document.getElementById('pg-manual-amount').value);
  const direction=document.getElementById('pg-manual-direction').value;
  if(transaction_date.slice(0,7)!==pgMonth()){pgStatus('A data deve estar no mês selecionado.');return;}
  if(description.length<2||!Number.isFinite(amount)||amount<=0){pgStatus('Informe descrição e valor positivo.');return;}
  try{
    await api('POST','/pluggy/manual-entry',{transaction_date,description,amount,direction});
    document.getElementById('pg-manual-desc').value='';
    document.getElementById('pg-manual-amount').value='';
    await pgLoad();pgStatus('Lançamento do Neon registrado.');
  }catch(e){pgStatus('Falha ao registrar Neon: '+e.message);}
}
async function pgDeleteManualEntry(id){
  if(!window.confirm('Remover este lançamento manual do Neon?'))return;
  try{await api('DELETE',`/pluggy/manual-entry/${encodeURIComponent(id)}`);await pgLoad();pgStatus('Lançamento manual removido.');}
  catch(e){pgStatus('Falha ao remover: '+e.message);}
}
async function pgSaveRule(){
  const gasto_id=Number(document.getElementById('pg-rule-gasto').value);
  const recipient_name=document.getElementById('pg-rule-name').value.trim();
  const recipient_document=document.getElementById('pg-rule-doc').value.trim();
  if(!gasto_id||recipient_name.length<2){pgStatus('Escolha um gasto e informe o destinatário.');return;}
  try{await api('PUT','/pluggy/rules',{gasto_id,recipient_name,recipient_document});await pgLoad();}
  catch(e){pgStatus('Erro ao salvar regra: '+e.message);}
}
async function pgDeleteRule(id){
  try{await api('DELETE',`/pluggy/rules/${id}`);await pgLoad();}
  catch(e){pgStatus(e.message);}
}
async function pgLink(transactionId){
  const gasto_id=Number(document.getElementById('pg-link-'+transactionId)?.value);
  if(!gasto_id){pgStatus('Escolha o gasto fixo para vincular.');return;}
  try{await api('POST','/pluggy/matches',{gasto_id,transaction_id:transactionId});await pgLoad();}
  catch(e){pgStatus('Erro ao vincular: '+e.message);}
}
async function pgUnlink(transactionId){
  try{await api('DELETE',`/pluggy/matches/${encodeURIComponent(transactionId)}`);await pgLoad();}
  catch(e){pgStatus('Erro ao desvincular: '+e.message);}
}
async function pgSetCategory(transactionId,category){
  try{await api('PUT',`/pluggy/transactions/${encodeURIComponent(transactionId)}/category`,{category});await pgLoad();}
  catch(e){pgStatus('Erro ao categorizar: '+e.message);}
}
