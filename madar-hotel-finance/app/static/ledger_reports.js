'use strict';
/* Read-only reports over sealed journal lines; no opening balances are assumed. */
window.LedgerReports = (() => {
  let dialog, model = null, opener = null, request = 0, busy = false, chart = null;
  const q = selector => dialog.querySelector(selector);
  const arr = value => Array.isArray(value) ? value : [];
  const dateToday = () => new Intl.DateTimeFormat('en-CA', {timeZone:'Africa/Cairo', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
  const defaults = () => {const to = dateToday();return {from_date:to.slice(0,4)+'-01-01',to_date:to,include_zero:false,supplier_id:null};};
  const sourceLabels = {supplier_invoice:'فاتورة مورد',supplier_return:'مرتجع مورد',supplier_payment:'سداد مورد',supplier_cheque_clearance:'صرف شيك',supplier_settlement:'تسوية مورد',supplier_settlement_reversal:'عكس تسوية',supplier_refund:'استرداد من مورد',supplier_refund_reversal:'عكس استرداد',supplier_payment_reversal:'عكس سداد',supplier_cheque_bounce:'شيك مرتد',withholding_remittance:'توريد خصم ضريبي',withholding_remittance_reversal:'عكس توريد خصم ضريبي',treasury_receipt:'سند قبض',treasury_payment:'سند صرف',treasury_transfer:'تحويل خزائن',treasury_reversal:'عكس مستند خزينة',custody_issue:'صرف عهدة',custody_settlement:'تسوية عهدة',custody_return:'رد عهدة',custody_reversal:'عكس مستند عهدة',opening_balance:'رصيد افتتاحي',bank_receipt:'متحصل بنك',bank_payment:'مدفوع بنك',bank_fee:'مصاريف بنك',bank_transfer:'تحويل بنكي',bank_reversal:'عكس حركة بنك',opening_cheque_clearance:'صرف شيك افتتاحي'};
  const side = amount => amount > 0 ? 'مدين' : amount < 0 ? 'دائن' : 'متوازن';
  const signed = amount => `${money(Math.abs(amount||0))} ${amount?side(amount):''}`;
  const button = (text, action, attrs='', primary=false) => `<button type="button" class="${primary?'primary':'secondary'}" data-ledger-action="${action}" ${attrs}>${esc(text)}</button>`;
  const number = cents => `<td class="lr-number">${money(cents||0)}</td>`;
  const notice = (text, kind='') => `<div class="lr-note ${kind}">${esc(text)}</div>`;

  function ensure() {
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='ledger-reports-window';dialog.dir='rtl';dialog.setAttribute('aria-labelledby','ledger-reports-title');
    dialog.innerHTML='<header class="lr-header"><span id="ledger-reports-art"></span><div><h2 id="ledger-reports-title"></h2><p id="ledger-reports-subtitle"></p></div><button type="button" class="lr-close" data-ledger-action="close" aria-label="إغلاق التقارير">×</button></header><div class="lr-scroll"><p id="ledger-reports-error" class="error" role="alert"></p><div id="ledger-reports-body"></div></div><footer id="ledger-reports-footer" class="lr-footer"></footer>';
    document.body.appendChild(dialog);
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('click',event=>{const b=event.target.closest('[data-ledger-action]');if(b&&!b.disabled)dispatch(b).catch(error);});
    dialog.addEventListener('input',event=>{
      if(event.target.matches('[data-ledger-search]')){model.search=event.target.value;renderTrialTable();return;}
      if(event.target.matches('[data-ledger-date]'))filtersChanged();
    });
    dialog.addEventListener('change',event=>{if(event.target.matches('[data-ledger-zero]'))filtersChanged();});
  }
  function open() {ensure();if(!dialog.open){opener=document.activeElement;dialog.showModal();}}
  function close() {++request;busy=false;dialog.close();model=null;if(opener?.isConnected)opener.focus();}
  function error(err) {ensure();q('#ledger-reports-error').textContent=err.message||String(err);if(!dialog.open)toast(err.message||String(err));}
  function title(text, subtitle='', art='chart') {q('#ledger-reports-title').textContent=text;q('#ledger-reports-subtitle').textContent=subtitle;q('#ledger-reports-art').innerHTML=illustration(art);}
  function display(body, footer) {q('#ledger-reports-body').innerHTML=body;q('#ledger-reports-footer').innerHTML=footer;q('#ledger-reports-error').textContent='';q('.lr-scroll').scrollTop=0;}
  function loading() {open();busy=true;title('جاري تحميل التقرير','من القيود المرحّلة المحفوظة');display('<div class="lr-empty" role="status">جاري قراءة التقرير…</div>',button('إغلاق','close'));}
  function params(filters, ledger=false) {const values={from_date:filters.from_date,to_date:filters.to_date};if(!ledger)values.include_zero=filters.include_zero?'1':'0';else if(filters.supplier_id)values.supplier_id=String(filters.supplier_id);return new URLSearchParams(values).toString();}
  function validate(filters) {if(!/^\d{4}-\d{2}-\d{2}$/.test(filters.from_date||'')||!/^\d{4}-\d{2}-\d{2}$/.test(filters.to_date||'')||filters.from_date>filters.to_date)throw Error('حدد بداية ونهاية صحيحتين؛ البداية لا تتجاوز النهاية.');return filters;}
  function filtersChanged() {const note=q('[data-ledger-pending]');if(note)note.hidden=false;const print=q('[data-ledger-action="print"]');if(print)print.disabled=true;}
  function currentFilters() {return validate({...model.filters,from_date:q('[data-ledger-date="from"]').value,to_date:q('[data-ledger-date="to"]').value,include_zero:!!q('[data-ledger-zero]')?.checked});}
  function controls(isLedger=false) {
    const f=model.filters;
    return `<div class="lr-controls"><label>من تاريخ<input type="date" data-ledger-date="from" value="${esc(f.from_date)}"></label><label>إلى تاريخ<input type="date" data-ledger-date="to" value="${esc(f.to_date)}"></label>${isLedger?button(model.report.supplier?model.report.supplier.name:'كل الموردين','supplier'):`<label class="lr-checkbox"><input type="checkbox" data-ledger-zero ${f.include_zero?'checked':''}>إظهار الحسابات دون حركة</label>`}${button('تحديث التقرير','reload','',true)}</div><p class="lr-pending" data-ledger-pending hidden>تغيرت محددات العرض. اضغط تحديث التقرير لعرض وطباعة النتيجة الجديدة.</p>`;
  }
  const sums = rows => rows.reduce((total,row)=>{for(const key of ['opening_debit_cents','opening_credit_cents','debit_cents','credit_cents','closing_debit_cents','closing_credit_cents'])total[key]=(total[key]||0)+(row[key]||0);return total;},{});
  function exceptions(report) {
    const errors=arr(report.exceptions);if(!errors.length)return '';
    return `<details class="lr-exceptions" open><summary>بنود تحتاج مراجعة • ${errors.length}</summary>${errors.map(item=>`<p>${esc(item.message||item.type)} ${item.journal_code?`<bdi>${esc(item.journal_code)}</bdi>`:''}${item.journal_id?button('تفاصيل القيد','journal',`data-id="${Number(item.journal_id)}"`):''}</p>`).join('')}</details>`;
  }
  function kpi(text, value, detail='') {return `<div class="lr-kpi"><small>${esc(text)}</small><strong>${money(value||0)}</strong>${detail?`<span>${esc(detail)}</span>`:''}</div>`;}
  async function trialBalance(filters=null) {
    const f=validate({...defaults(),...(filters||{})}),seq=++request;loading();
    try{const report=await api('trial-balance?'+params(f));if(seq!==request||!dialog.open)return;model={type:'trial',report,filters:{...f,from_date:report.from_date,to_date:report.to_date},search:'',groups:false};renderTrial();}catch(err){if(seq===request)error(err);}finally{if(seq===request)busy=false;}
  }
  function trialRows() {const text=(model.search||'').trim().toLocaleLowerCase();return arr(model.groups?model.report.groups:model.report.rows).filter(row=>!text||`${row.code} ${row.name}`.toLocaleLowerCase().includes(text));}
  function trialTable(rows, print=false) {
    const total=sums(rows),columns=['opening_debit_cents','opening_credit_cents','debit_cents','credit_cents','closing_debit_cents','closing_credit_cents'];
    return `<table class="lr-table lr-trial-table"><thead><tr><th rowspan="2">الحساب</th><th colspan="2">رصيد قبل الفترة</th><th colspan="2">حركة الفترة</th><th colspan="2">رصيد نهاية الفترة</th>${print?'':'<th rowspan="2">الحركة</th>'}</tr><tr>${Array.from({length:3},()=>'<th>مدين</th><th>دائن</th>').join('')}</tr></thead><tbody>${rows.map(row=>`<tr><td><bdi>${esc(row.code)}</bdi><span>${esc(row.name)}</span></td>${columns.map(key=>number(row[key])).join('')}${print?'':`<td>${model.groups?'<small>تجميعي</small>':button('كشف الحساب','account',`data-id="${Number(row.account_id)}"`)}</td>`}</tr>`).join('')||`<tr><td colspan="${print?7:8}" class="lr-empty">لا توجد حسابات ضمن محددات العرض.</td></tr>`}</tbody>${model.groups?'':`<tfoot><tr><th>${model.search?'إجمالي الحسابات المعروضة':'الإجمالي'}</th>${columns.map(key=>number(total[key])).join('')}${print?'':'<td></td>'}</tr></tfoot>`}</table>`;
  }
  function renderTrialTable() {q('#lr-table-wrap').innerHTML=trialTable(trialRows());q('[data-ledger-count]').textContent=`${trialRows().length} من ${arr(model.groups?model.report.groups:model.report.rows).length} حساب`;}
  function renderTrial() {
    const report=model.report,t=report.totals||{};
    title('ميزان المراجعة',`${report.from_date} — ${report.to_date} • ${report.currency}`);
    display(`${controls()}<div class="lr-status ${report.balanced?'balanced':'unbalanced'}"><b>${report.balanced?'الميزان متوازن':'الميزان يحتاج مراجعة'}</b><span>يعرض القيود المرحّلة فقط؛ المسودات لا تدخل الأرصدة.</span></div><div class="lr-kpis">${kpi('إجمالي مدين الفترة',t.debit_cents)}${kpi('إجمالي دائن الفترة',t.credit_cents)}${kpi('أرصدة مدينة في النهاية',t.closing_debit_cents)}${kpi('أرصدة دائنة في النهاية',t.closing_credit_cents)}</div>${exceptions(report)}${notice(report.opening_status?.message||'رصيد قبل الفترة يجمع القيود السابقة لتاريخ البداية.')}<section class="lr-section"><div class="lr-table-tools"><input type="search" data-ledger-search placeholder="بحث باسم الحساب أو الكود…" aria-label="بحث في الميزان" value="${esc(model.search)}"><span data-ledger-count></span>${button(model.groups?'عرض الحسابات التفصيلية':'عرض مجاميع الشجرة','groups')}</div>${model.groups?notice('المجاميع تشمل أرصدة الحسابات التابعة وقد تتداخل مستوياتها؛ لا تجمع صفوف هذا العرض معًا.','warning'):''}<div class="lr-table-scroll" id="lr-table-wrap"></div></section>`,button('طباعة التقرير','print')+button('اختيار حساب','choose-account')+'<span class="lr-spacer"></span>'+button('إغلاق','close'));
    renderTrialTable();
  }
  async function ledger(accountId=null, filters=null, back=null) {
    if(accountId===null)return chooseAccount();
    if(!Number.isInteger(Number(accountId))||Number(accountId)<1)throw Error('اختر حسابًا صحيحًا.');
    const f=validate({...defaults(),...(filters||{})}),seq=++request;loading();
    try{const report=await api('account-ledger?account_id='+Number(accountId)+'&'+params(f,true));if(seq!==request||!dialog.open)return;model={type:'ledger',report,accountId:Number(accountId),filters:{...f,from_date:report.from_date,to_date:report.to_date},back};renderLedger();}catch(err){if(seq===request)error(err);}finally{if(seq===request)busy=false;}
  }
  function ledgerTable(report, print=false) {
    return `<table class="lr-table"><thead><tr><th>التاريخ / القيد</th><th>المستند / المرجع</th><th>المورد / القسم</th><th>مدين</th><th>دائن</th><th>الرصيد الجاري</th>${print?'':'<th></th>'}</tr></thead><tbody><tr class="lr-opening"><td colspan="3">رصيد قبل الفترة</td><td>—</td><td>—</td><td class="lr-number">${signed(report.opening_balance_cents)}</td>${print?'':'<td></td>'}</tr>${arr(report.rows).map(row=>`<tr><td><bdi>${esc(row.entry_date||row.date)}</bdi><br><bdi>${esc(row.journal_code)}</bdi></td><td>${esc(sourceLabels[row.source_type]||row.source_type)}<br><bdi>${esc(row.document_code||'—')}</bdi>${row.reference?`<small>${esc(row.reference)}</small>`:''}</td><td>${esc(row.supplier_name||'—')}${row.department_name?`<small>${esc(row.department_name)}${row.cost_code?' • '+esc(row.cost_code):''}</small>`:''}</td>${number(row.debit_cents)}${number(row.credit_cents)}<td class="lr-number">${signed(row.running_balance_cents)}</td>${print?'':`<td>${button('القيد','journal',`data-id="${Number(row.journal_id)}"`)}</td>`}</tr>`).join('')||`<tr><td class="lr-empty" colspan="${print?6:7}">لا توجد حركة مرحّلة خلال الفترة المختارة.</td></tr>`}</tbody><tfoot><tr><th colspan="3">إجمالي الحركة والرصيد النهائي</th>${number(report.debit_cents)}${number(report.credit_cents)}<td class="lr-number">${signed(report.closing_balance_cents)}</td>${print?'':'<td></td>'}</tr></tfoot></table>`;
  }
  function renderLedger() {
    const report=model.report;
    title('كشف حركة الحساب',`${report.account.code} • ${report.account.name}`);
    display(`${controls(true)}<div class="lr-kpis">${kpi('رصيد قبل الفترة',Math.abs(report.opening_balance_cents),side(report.opening_balance_cents))}${kpi('مدين الفترة',report.debit_cents)}${kpi('دائن الفترة',report.credit_cents)}${kpi('الرصيد النهائي',Math.abs(report.closing_balance_cents),side(report.closing_balance_cents))}</div>${exceptions(report)}${notice(report.supplier?'العرض مخصص للمورد المختار فقط. الرصيد يجمع حركة هذا المورد على الحساب.':'الرصيد الجاري = المدين ناقص الدائن، مرتبًا بتاريخ القيد ورقمه ثم طرفه.')}<section class="lr-section"><div class="lr-table-scroll">${ledgerTable(report)}</div></section>`,button('طباعة كشف الحساب','print')+button('تغيير الحساب','choose-account')+button('ميزان المراجعة','trial')+(model.back?button('رجوع','back'):'')+'<span class="lr-spacer"></span>'+button('إغلاق','close'));
  }
  async function chooseAccount() {
    const previous=model,f=previous&&['trial','ledger'].includes(previous.type)?currentFilters():defaults();
    const seq=++request;
    try{
      chart=await api('accounting');if(seq!==request)return;
      picker('اختر حسابًا لعرض حركته',arr(chart.accounts).filter(a=>a.posting).map(a=>({id:a.id,label:a.code+' • '+a.name,detail:a.active?'حساب تفصيلي':'حساب موقوف • يمكن مراجعة حركته السابقة'})),row=>ledger(row.id,f,previous));
    }catch(err){if(seq===request)error(err);}
  }
  function chooseSupplier() {
    const previous=model,f=currentFilters();
    picker('تصفية كشف الحساب حسب المورد',[{id:0,label:'كل الموردين',detail:'جميع أطراف الحساب'},...arr(state.suppliers).map(s=>({id:s.id,label:s.name,detail:s.code}))],row=>ledger(previous.accountId,{...f,supplier_id:row.id||null},previous.back));
  }
  async function journal(id) {
    const previous=model,seq=++request;busy=true;
    try{const report=await api('journals/'+id);if(seq!==request||!dialog.open)return;model={type:'journal',report,back:previous};renderJournal();}catch(err){if(seq===request)error(err);}finally{if(seq===request)busy=false;}
  }
  function journalTable(report) {return `<table class="lr-table"><thead><tr><th>الحساب</th><th>المورد</th><th>القسم / مركز التكلفة</th><th>مدين</th><th>دائن</th></tr></thead><tbody>${arr(report.lines).map(row=>`<tr><td><bdi>${esc(row.account_code)}</bdi><span>${esc(row.account_name)}</span></td><td>${esc(row.supplier_name||'—')}</td><td>${esc(row.department_name||'—')}${row.cost_code?`<small>${esc(row.cost_code)}</small>`:''}</td>${number(row.debit_cents)}${number(row.credit_cents)}</tr>`).join('')}</tbody><tfoot><tr><th colspan="3">الإجمالي</th>${number(report.debit_cents)}${number(report.credit_cents)}</tr></tfoot></table>`;}
  function renderJournal() {const report=model.report;title('تفاصيل القيد المحاسبي',`${report.code} • ${report.entry_date}`,'edit');display(`${notice('قيد مرحّل محفوظ من '+(sourceLabels[report.source_type]||report.source_type)+'. العرض للقراءة فقط.')}<section class="lr-section"><div class="lr-table-scroll">${journalTable(report)}</div></section>`,button('طباعة القيد','print')+button('رجوع للتقرير','back')+'<span class="lr-spacer"></span>'+button('إغلاق','close'));}
  function back() {++request;busy=false;if(!model.back)return;model=model.back;if(model.type==='trial')renderTrial();else if(model.type==='ledger')renderLedger();else renderJournal();}
  function print() {
    const report=model.report;
    let body,heading,context;
    if(model.type==='trial'){heading=model.groups?'مجاميع شجرة الحسابات':'ميزان المراجعة';context=`من ${report.from_date} إلى ${report.to_date} • ${report.currency}${model.search?' • البحث: '+model.search:''}`;body=`${model.groups?notice('المجاميع متداخلة المستويات؛ لا تجمع صفوفها معًا.'):''}<p>الحالة: ${report.balanced?'متوازن':'يحتاج مراجعة'} • إجمالي حركة المدين ${money(report.totals.debit_cents)} • الدائن ${money(report.totals.credit_cents)}</p>${trialTable(trialRows(),true)}`;}
    else if(model.type==='ledger'){heading='كشف حركة الحساب';context=`${report.account.code} • ${report.account.name} • من ${report.from_date} إلى ${report.to_date} • ${report.currency}${report.supplier?' • المورد: '+report.supplier.name:''}`;body=ledgerTable(report,true);}
    else {heading='القيد المحاسبي '+report.code;context=`تاريخ القيد ${report.entry_date} • ${sourceLabels[report.source_type]||report.source_type}`;body=journalTable(report);}
    const alerts=arr(report.exceptions).map(item=>`<p>${esc(item.message||item.type)} ${esc(item.journal_code||'')}</p>`).join('');
    $('#print-document').innerHTML=`<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>${esc(heading)}</h2><p>${esc(context)}</p>${body}${alerts?'<h3>بنود تحتاج مراجعة</h3>'+alerts:''}<p>من القيود المرحّلة المسجلة فقط. تاريخ الطباعة ${dateToday()}.</p><div class="signatures"><span>أعدّه: ............</span><span>راجعه: ............</span><span>اعتمده: ............</span></div>`;
    window.print();
  }
  async function dispatch(b) {
    const action=b.dataset.ledgerAction;
    if(action==='close'){close();return;}
    if(busy)return;
    if(action==='reload'){const f=currentFilters();return model.type==='trial'?trialBalance(f):ledger(model.accountId,f,model.back);}
    if(action==='account')return ledger(Number(b.dataset.id),model.filters,model);
    if(action==='journal')return journal(Number(b.dataset.id));
    if(action==='choose-account')return chooseAccount();
    if(action==='supplier')return chooseSupplier();
    if(action==='trial')return trialBalance(model.filters);
    if(action==='groups'){model.groups=!model.groups;renderTrial();return;}
    if(action==='back'){back();return;}
    if(action==='print')print();
  }
  document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b)return;if(b.hasAttribute('data-ledger-trial'))trialBalance().catch(error);else if(b.hasAttribute('data-ledger-account'))ledger(b.dataset.ledgerAccount?Number(b.dataset.ledgerAccount):null).catch(error);});
  return {trialBalance,ledger};
})();
