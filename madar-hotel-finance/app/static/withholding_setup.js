'use strict';
/* Operator-reviewed withholding codes and dated reports; rates are never assumed. */
window.WithholdingSetup = (() => {
  let dialog, model = null, dirty = false, busy = false, opener = null, request = 0;
  const q = selector => dialog.querySelector(selector);
  const list = value => Array.isArray(value) ? value : [];
  const currentDate = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Africa/Cairo',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const rate = code => String(code.rate ?? (Number(code.rate_basis||0)/100).toFixed(2));
  const button = (text, action, css='secondary', attrs='', art='') => `<button type="button" class="${css}" data-withholding-action="${action}" ${attrs}>${art?illustration(art):''}<span>${esc(text)}</span></button>`;
  const note = (text, kind='') => `<div class="wh-note ${kind}">${esc(text)}</div>`;
  const empty = text => `<div class="wh-empty">${illustration('receipt')}<p>${esc(text)}</p></div>`;
  const number = value => `<td class="wh-number">${money(value||0)}</td>`;
  const field = (name,title,value='',type='text') => `<label>${esc(title)}<input data-withholding-field="${name}" type="${type}" value="${esc(value??'')}" ${type==='number'?'min="0.01" max="100" step="0.01" inputmode="decimal"':'maxlength="200"'}></label>`;
  const signatures = () => '<div class="ap-print-signatures"><span>أعده: ................</span><span>راجعه: ................</span><span>اعتمده: ................</span></div>';
  function ensure() {
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='withholding-setup-window';dialog.dir='rtl';dialog.setAttribute('aria-labelledby','withholding-title');
    dialog.innerHTML='<header class="wh-header"><span id="withholding-art"></span><div><h2 id="withholding-title"></h2><p id="withholding-subtitle"></p></div><button type="button" class="wh-close" data-withholding-action="close" aria-label="إغلاق الخصم تحت حساب الضريبة">×</button></header><div class="wh-scroll"><p class="error" id="withholding-error" role="alert"></p><div id="withholding-body"></div></div><footer class="wh-footer" id="withholding-footer"></footer>';
    document.body.appendChild(dialog);
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('click',event=>{const b=event.target.closest('[data-withholding-action]');if(!b||busy||b.disabled)return;dispatch(b.dataset.withholdingAction,b).catch(showError);});
    dialog.addEventListener('input',event=>{
      const target=event.target;if(busy||!model)return;
      if(target.dataset.withholdingField&&model.type==='code'){model.data[target.dataset.withholdingField]=target.value;dirty=true;}
      if(target.matches('[data-withholding-date]'))pendingFilters();
    });
    dialog.addEventListener('change',event=>{if(busy||model?.type!=='code')return;const name=event.target.dataset.withholdingFlag;if(name){model.data[name]=event.target.checked;dirty=true;}});
    window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  }
  function show() {ensure();if(!dialog.open){opener=document.activeElement;dialog.showModal();}}
  function header(title,subtitle='',art='receipt') {q('#withholding-title').textContent=title;q('#withholding-subtitle').textContent=subtitle;q('#withholding-art').innerHTML=illustration(art);}
  function display(body,footer) {q('#withholding-body').innerHTML=body;q('#withholding-footer').innerHTML=footer;q('#withholding-error').textContent='';q('.wh-scroll').scrollTop=0;if(busy)lock();}
  function loading(title='جاري قراءة البيانات') {show();header(title,'الخصم تحت حساب الضريبة للموردين');display('<div class="wh-empty" role="status">جاري التحميل…</div>',button('إغلاق','close'));}
  function discard() {if(busy)return false;return !dirty||confirm('توجد تعديلات لم تحفظ. متابعة دون حفظ؟');}
  function close() {if(busy||!discard())return;++request;dirty=false;dialog.close();model=null;if(opener?.isConnected)opener.focus();}
  function showError(error) {ensure();q('#withholding-error').textContent=error.message||String(error);if(dialog.open)q('#withholding-error').scrollIntoView({block:'nearest'});else toast(error.message||String(error));}
  function lock() {dialog.querySelectorAll('button,input,textarea').forEach(el=>{if(!el.disabled){el.disabled=true;el.dataset.whBusy='1';}});}
  async function run(task) {if(busy)return;busy=true;lock();q('#withholding-error').textContent='';try{return await task();}catch(error){showError(error);}finally{busy=false;dialog.querySelectorAll('[data-wh-busy]').forEach(el=>{el.disabled=false;delete el.dataset.whBusy;});}}
  async function codes() {
    if(!discard())return;dirty=false;const seq=++request;loading('إعداد الخصم تحت حساب الضريبة');
    try{const [config,chart]=await Promise.all([api('withholding-config'),api('accounting')]);if(seq!==request||!dialog.open)return;model={type:'codes',config,chart};renderCodes();}catch(error){if(seq===request)showError(error);}
  }
  function mappingSummary() {
    const mapping=list(model.chart?.mappings).find(item=>item.module==='الضرائب'&&item.role==='withholding_payable');
    if(!mapping)return note('راجع ربط حساب الخصم والتحصيل المستحق للغير من إعدادات الفندق قبل اعتماد سداد يتضمن خصمًا.','warning');
    return `<section class="wh-mapping"><span>${illustration('bank')}</span><div><b>حساب الخصم والتحصيل المستحق للغير</b><p><bdi>${esc(mapping.account_code)}</bdi> • ${esc(mapping.account_name)}</p><small>${mapping.status==='confirmed'?'الربط مراجع ومؤكد':'الربط مقترح؛ يلزم مراجعته وتأكيده من شجرة الحسابات قبل الاعتماد'}</small></div>${button('فتح ربط الحسابات','mappings')}</section>`;
  }
  function renderCodes() {
    const codes=list(model.config.codes);header('الخصم تحت حساب الضريبة','أكواد بنسب يراجعها المشغل • يطبق عند سداد الفواتير');
    display(`${mappingSummary()}${note('يحسب الخصم على الجزء المسدد من صافي الفواتير دون القيمة المضافة. اختيار كود للسداد يحدد نسبته تلقائيًا؛ لا يطبق على الدفعات المقدمة في هذه المرحلة.')}<section class="wh-section"><div class="wh-section-title"><h3>أكواد الخصم</h3>${button('كود خصم جديد','new-code','primary','','add')}</div>${codes.length?`<div class="wh-table-scroll"><table class="wh-table"><thead><tr><th>الكود / المعاملة</th><th>النسبة</th><th>أساس الاحتساب</th><th>الحالة</th><th></th></tr></thead><tbody>${codes.map(code=>`<tr><td><bdi>${esc(code.code)}</bdi><span>${esc(code.name)}</span></td><td class="wh-number">${esc(rate(code))}%</td><td>صافي الفواتير المسدد دون القيمة المضافة</td><td><span class="wh-pill ${code.active&&code.confirmed?'confirmed':''}">${!code.active?'موقوف':code.confirmed?'مراجع ومؤكد':'يحتاج مراجعة'}</span></td><td>${button('مراجعة / تعديل','edit-code','secondary',`data-id="${code.id}"`)}</td></tr>`).join('')}</tbody></table></div>`:empty('لم تضف أكواد خصم بعد. أدخل الاسم والنسبة من السياسة والمستندات المعتمدة لديكم.')}${note('يستخدم السداد كودًا واحدًا. عند اختلاف المعاملة أو نسبة الخصم، سجل مستند سداد مستقلًا. تعديل الكود لا يغير المستندات المعتمدة أو شهاداتها.')}</section>`,button('تقرير الخصم','report','secondary','','chart')+'<button type="button" class="secondary" data-withholding-balances>موقف التوريد والمستحق</button>'+'<span class="wh-spacer"></span>'+button('إغلاق','close'));
  }
  function editCode(id=null) {
    if(!discard())return;const config=model.config,chart=model.chart,old=list(config.codes).find(item=>item.id===Number(id));
    model={type:'code',config,chart,data:old?{...old,rate:rate(old)}:{name:'',rate:'',active:true,confirmed:false,notes:''}};dirty=false;renderCode();
  }
  function renderCode() {
    const d=model.data;header(d.id?'مراجعة كود الخصم':'كود خصم جديد',d.code||'الكود يتولد عند الحفظ • أدخل النسبة صراحةً');
    display(`<section class="wh-section"><div class="wh-grid">${field('name','اسم المعاملة *',d.name)}${field('rate','نسبة الخصم % *',d.rate,'number')}<label>كود المعاملة<input value="${esc(d.code||'يتولد تلقائيًا')}" readonly></label><label>أساس الاحتساب<input value="صافي الفواتير المسدد دون القيمة المضافة" readonly></label></div><div class="wh-flags"><label><input type="checkbox" data-withholding-flag="active" ${d.active?'checked':''}>الكود نشط</label><label><input type="checkbox" data-withholding-flag="confirmed" ${d.confirmed?'checked':''}>تمت مراجعة الاسم والنسبة</label></div><label class="wh-memo">ملاحظات المعاملة<textarea data-withholding-field="notes" maxlength="2000" rows="3">${esc(d.notes||'')}</textarea></label></section>${note('التسجيل في ضريبة القيمة المضافة لا يحدد موقف الخصم تلقائيًا. راجع المعاملة ونسبة الخصم المناسبة قبل تأكيد الكود.')}`,button('حفظ كود الخصم','save-code','primary','','save')+button('رجوع للأكواد','back-codes')+'<span class="wh-spacer"></span>'+button('إغلاق','close'));
  }
  function percentage(value) {const text=String(value??'').trim();if(!/^\d+(\.\d{1,2})?$/.test(text)||!Number.isFinite(Number(text))||Number(text)<=0||Number(text)>100)throw Error('أدخل نسبة أكبر من صفر وحتى ١٠٠، وبحد أقصى منزلتين عشريتين');return text;}
  async function saveCode() {
    const d=model.data;if(!d.name.trim())throw Error('أدخل اسم معاملة الخصم');const data={name:d.name,rate:percentage(d.rate),active:!!d.active,confirmed:!!d.confirmed,notes:d.notes||''};if(d.id)data.revision=d.revision;
    const saved=await api('withholding-codes'+(d.id?'/'+d.id:''),d.id?'PUT':'POST',data);model.data={...saved,rate:rate(saved)};dirty=false;document.dispatchEvent(new CustomEvent('withholding-config-updated'));
    try{await refresh();}catch(error){toast('تم حفظ الكود؛ تعذر تحديث الرئيسية: '+error.message);}
    // A fresh read follows the successful write, without asking to discard saved data.
    const [config,chart]=await Promise.all([api('withholding-config'),api('accounting')]);model={type:'codes',config,chart};renderCodes();toast('تم حفظ كود الخصم');
  }
  function validateDates(from,to) {if(!/^\d{4}-\d{2}-\d{2}$/.test(from||'')||!/^\d{4}-\d{2}-\d{2}$/.test(to||'')||from>to)throw Error('حدد بداية ونهاية صحيحتين للتقرير؛ البداية لا تتجاوز النهاية');return {from_date:from,to_date:to};}
  function pendingFilters() {const pending=q('[data-withholding-pending]');if(pending)pending.hidden=false;const print=q('[data-withholding-action="print-report"]');if(print)print.disabled=true;}
  function reportFilters() {const dates=validateDates(q('[data-withholding-date="from"]').value,q('[data-withholding-date="to"]').value);return {...dates,supplier_id:model.filters.supplier_id};}
  async function report(from=null,to=null,supplierId=null) {
    if(!discard())return;const end=to||currentDate(),f={...validateDates(from||end.slice(0,4)+'-01-01',end),supplier_id:supplierId?Number(supplierId):null};dirty=false;const seq=++request;loading('تقرير الخصم تحت حساب الضريبة');
    const params=new URLSearchParams({from_date:f.from_date,to_date:f.to_date});if(f.supplier_id)params.set('supplier_id',String(f.supplier_id));
    try{const result=await api('withholding-report?'+params);if(seq!==request||!dialog.open)return;model={type:'report',report:result,filters:{...f,from_date:result.from_date||f.from_date,to_date:result.to_date||f.to_date}};renderReport();}catch(error){if(seq===request)showError(error);}
  }
  function supplierName() {return model.report.supplier?.name||list(state.suppliers).find(item=>item.id===model.filters.supplier_id)?.name||'كل الموردين';}
  function reportControls() {
    const f=model.filters;return `<div class="wh-controls"><label>من تاريخ<input type="date" data-withholding-date="from" value="${esc(f.from_date)}"></label><label>إلى تاريخ<input type="date" data-withholding-date="to" value="${esc(f.to_date)}"></label>${button(supplierName(),'supplier')}${button('تحديث التقرير','reload-report','primary')}</div><div class="wh-periods" role="group" aria-label="فترة التقرير">${button('الشهر المختار','month')}${[1,2,3,4].map(quarter=>button('الربع '+['الأول','الثاني','الثالث','الرابع'][quarter-1],'quarter','secondary',`data-quarter="${quarter}"`)).join('')}${button('السنة المختارة','year')}</div><p class="wh-pending" data-withholding-pending hidden>تغيرت محددات العرض. حدث التقرير قبل الطباعة.</p>`;
  }
  function reportTable(report,print=false) {
    const rows=list(report.rows);return `<table class="wh-table wh-report-table"><thead><tr><th>التاريخ / المستند</th><th>المورد / الرقم الضريبي</th><th>المعاملة / النسبة</th><th>إجمالي التسوية</th><th>أساس الخصم</th><th>الخصم</th><th>الصافي للمورد</th>${print?'':'<th>القيد</th>'}</tr></thead><tbody>${rows.map(row=>`<tr class="${Number(row.withholding_cents)<0?'wh-reversal':''}"><td><bdi>${esc(row.business_date||row.date||row.payment_date||'')}</bdi><span>${esc(row.type==='reversal'||row.source_type==='supplier_payment_reversal'?'عكس خصم':'خصم مورد')}</span><bdi>${esc(row.code||row.payment_code||row.document_code||'')}</bdi>${row.type==='reversal'&&row.original_code?`<small>الأصل: <bdi>${esc(row.original_code)}</bdi></small>`:row.certificate_code?`<small><bdi>${esc(row.certificate_code)}</bdi></small>`:''}</td><td>${esc(row.supplier_name||'—')}<small>${esc(row.supplier_tax_number||'—')}</small></td><td>${esc(row.code_name||row.withholding_name||row.name||'—')}<small>${esc(row.rate??(Number(row.rate_basis||0)/100).toFixed(2))}%${row.account_code?' • '+esc(row.account_code):''}</small></td>${number(row.gross_cents??row.amount_cents??0)}${number(row.base_cents??row.withholding_base_cents??0)}${number(row.withholding_cents??0)}${number(row.net_cash_cents??0)}${print?'':`<td>${row.journal_id?button('فتح القيد','journal','secondary',`data-id="${Number(row.journal_id)}"`):'—'}</td>`}</tr>`).join('')||`<tr><td colspan="${print?7:8}">${empty('لا توجد حركات خصم مرحّلة ضمن الفترة المختارة.')}</td></tr>`}</tbody><tfoot><tr><th colspan="3">الإجمالي</th>${number(report.totals?.gross_cents??report.totals?.amount_cents??0)}${number(report.totals?.base_cents??report.totals?.withholding_base_cents??0)}${number(report.totals?.withholding_cents??0)}${number(report.totals?.net_cash_cents??0)}${print?'':'<td></td>'}</tr></tfoot></table>`;
  }
  function kpi(label,value,description='') {return `<div class="wh-kpi"><small>${esc(label)}</small><strong>${money(value||0)}</strong>${description?`<span>${esc(description)}</span>`:''}</div>`;}
  function renderReport() {
    const report=model.report,t=report.totals||{};header('تقرير الخصم تحت حساب الضريبة',`${model.filters.from_date} — ${model.filters.to_date} • ${report.currency||'EGP'}`,'chart');
    display(`${reportControls()}<div class="wh-kpis">${kpi('إجمالي التسوية',t.gross_cents??t.amount_cents)}${kpi('أساس الخصم دون القيمة المضافة',t.base_cents??t.withholding_base_cents)}${kpi('صافي الخصم',t.withholding_cents)}${kpi('الصافي للمورد',t.net_cash_cents)}</div>${note('يعرض التقرير الخصم وقت السداد، والتصحيح بقيم سالبة في تاريخ العكس، من بيانات المستندات المحفوظة. الشيك يثبت الخصم عند الإصدار.')}<section class="wh-section"><div class="wh-table-scroll">${reportTable(report)}</div></section>`,button('طباعة تقرير الخصم','print-report','secondary','','print')+button('إعداد أكواد الخصم','back-codes')+'<button type="button" class="secondary" data-withholding-remittances>مستندات توريد الخصم</button>'+'<span class="wh-spacer"></span>'+button('إغلاق','close'));
  }
  function periodRange(action,quarter=null) {
    const f=reportFilters(),[year,month]=f.to_date.split('-').map(Number);let first,last;
    if(action==='year'){first=1;last=12;}else if(action==='quarter'){if(!Number.isInteger(quarter)||quarter<1||quarter>4)throw Error('اختر ربعًا صحيحًا');first=(quarter-1)*3+1;last=first+2;}else{first=month;last=month;}
    const start=`${String(year).padStart(4,'0')}-${String(first).padStart(2,'0')}-01`,end=`${String(year).padStart(4,'0')}-${String(last).padStart(2,'0')}-${String(new Date(Date.UTC(year,last,0)).getUTCDate()).padStart(2,'0')}`;return report(start,end,f.supplier_id);
  }
  function chooseSupplier() {const f=reportFilters();picker('تصفية تقرير الخصم حسب المورد',[{id:0,label:'كل الموردين',detail:'جميع مستندات الخصم'},...list(state.suppliers).map(item=>({id:item.id,label:item.name,detail:item.code}))],row=>report(f.from_date,f.to_date,row.id||null));}
  async function journal(id) {
    const previous=model,seq=++request;loading('تفاصيل القيد');try{const result=await api('journals/'+id);if(seq!==request||!dialog.open)return;model={type:'journal',data:result,back:previous};renderJournal();}catch(error){if(seq===request)showError(error);}
  }
  function journalTable(j) {return `<table class="wh-table"><thead><tr><th>الحساب</th><th>المورد</th><th>القسم</th><th>مدين</th><th>دائن</th></tr></thead><tbody>${list(j.lines).map(row=>`<tr><td><bdi>${esc(row.account_code)}</bdi><span>${esc(row.account_name)}</span></td><td>${esc(row.supplier_name||'—')}</td><td>${esc(row.department_name||'—')}</td>${number(row.debit_cents)}${number(row.credit_cents)}</tr>`).join('')}</tbody><tfoot><tr><th colspan="3">الإجمالي</th>${number(j.debit_cents)}${number(j.credit_cents)}</tr></tfoot></table>`;}
  function renderJournal() {const j=model.data;header('تفاصيل القيد',`${j.code} • ${j.entry_date}`,'edit');display(`<section class="wh-section"><div class="wh-table-scroll">${journalTable(j)}</div></section>`,button('طباعة القيد','print-journal','secondary','','print')+button('رجوع للتقرير','back-report')+'<span class="wh-spacer"></span>'+button('إغلاق','close'));}
  const printHeader = (title,subtitle='') => `<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>${esc(title)}</h2><p>${esc(subtitle)}</p>`;
  function print(html) {document.querySelector('#print-document').innerHTML=html;window.print();}
  async function dispatch(action,b) {
    if(action==='close')return close();
    if(action==='new-code')return editCode();
    if(action==='edit-code')return editCode(Number(b.dataset.id));
    if(action==='save-code')return run(saveCode);
    if(action==='back-codes')return codes();
    if(action==='report')return report();
    if(action==='reload-report'){const f=reportFilters();return report(f.from_date,f.to_date,f.supplier_id);}
    if(action==='month'||action==='quarter'||action==='year')return periodRange(action,Number(b.dataset.quarter));
    if(action==='supplier')return chooseSupplier();
    if(action==='journal')return journal(Number(b.dataset.id));
    if(action==='back-report'){model=model.back;renderReport();return;}
    if(action==='mappings'){if(!discard())return;close();page('accounting');if(typeof accountingTab==='function')accountingTab('mappings');return;}
    if(action==='print-report'){const f=model.filters;return print(printHeader('تقرير الخصم تحت حساب الضريبة',f.from_date+' — '+f.to_date+' • '+(model.report.currency||'EGP')+' • '+supplierName())+reportTable(model.report,true)+signatures());}
    if(action==='print-journal'){const j=model.data;return print(printHeader('قيد الخصم تحت حساب الضريبة',j.code+' • '+j.entry_date)+journalTable(j)+signatures());}
  }
  document.addEventListener('click',event=>{const target=event.target.closest('button');if(!target)return;if(target.hasAttribute('data-withholding-setup'))codes().catch(showError);if(target.hasAttribute('data-withholding-report'))report().catch(showError);});
  return {codes,report};
})();
