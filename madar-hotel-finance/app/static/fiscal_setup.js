'use strict';
/* Tax setup and monthly locks are explicit review steps; they never invent opening balances. */
window.FiscalSetup = (() => {
  let dialog, model = null, dirty = false, busy = false, opener = null;
  const q = selector => dialog.querySelector(selector);
  const rows = value => Array.isArray(value) ? value : [];
  const dateToday = () => new Intl.DateTimeFormat('en-CA', {timeZone:'Africa/Cairo', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
  const labels = {unconfigured:'لم يحدد بعد', registered:'مسجل ضريبيًا', not_registered:'غير مسجل ضريبيًا', vat:'قيمة مضافة', zero_rated:'خاضع بسعر صفر', exempt:'معفى', out_of_scope:'خارج نطاق الضريبة', open:'مفتوحة', closed:'مقفلة', close:'قفل الفترة', reopen:'إعادة فتح الفترة'};
  const label = value => labels[value] || value || '—';
  const monthNames = ['يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'];
  const monthLabel = value => /^\d{4}-\d{2}$/.test(value || '') ? monthNames[Number(value.slice(5))-1]+' '+value.slice(0,4) : value;
  function button(text, action, css='secondary', attributes='', art='') { return `<button type="button" class="${css}" data-fiscal-action="${action}" ${attributes}>${art?illustration(art):''}<span>${esc(text)}</span></button>`; }
  function field(name, title, value='', type='text', required=false, disabled=false) { return `<label>${esc(title)}${required?' *':''}<input data-fiscal-field="${name}" type="${type}" value="${esc(value??'')}" ${type==='number'?'step="0.01" min="0" max="100" inputmode="decimal"':''} maxlength="${name==='tax_number'?80:200}" ${required?'required':''} ${disabled?'disabled':''}></label>`; }
  function memo(name,title,value='') { return `<label class="full">${esc(title)}<textarea data-fiscal-field="${name}" maxlength="2000" rows="3">${esc(value??'')}</textarea></label>`; }
  function note(text, kind='') { return `<div class="ap-note ${kind}">${esc(text)}</div>`; }
  function kpi(title, value) { return `<div class="ap-kpi"><small>${esc(title)}</small><strong>${money(Number(value)||0)}</strong></div>`; }
  function empty(text) { return `<div class="fiscal-empty">${illustration('edit')}<p>${esc(text)}</p></div>`; }
  function header(title, subtitle='', art='settings') { ensure(); q('#fiscal-title').textContent=title; q('#fiscal-subtitle').textContent=subtitle; q('#fiscal-art').innerHTML=illustration(art); }
  function ensure() {
    if (dialog) return;
    dialog=document.createElement('dialog'); dialog.id='fiscal-setup-window'; dialog.dir='rtl'; dialog.setAttribute('aria-labelledby','fiscal-title');
    dialog.innerHTML='<header class="fiscal-header"><span id="fiscal-art"></span><div><h2 id="fiscal-title"></h2><p id="fiscal-subtitle"></p></div><button type="button" class="close" data-fiscal-action="close" aria-label="إغلاق النافذة">×</button></header><div class="fiscal-scroll"><p id="fiscal-error" class="error" role="alert"></p><div id="fiscal-body"></div><footer id="fiscal-footer" class="fiscal-footer"></footer></div>';
    document.body.appendChild(dialog);
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('click',event=>{const b=event.target.closest('[data-fiscal-action]');if(!b||busy||b.disabled)return;dispatch(b.dataset.fiscalAction,b).catch(showError);});
    dialog.addEventListener('input',event=>input(event.target));
    dialog.addEventListener('change',event=>change(event.target));
    window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  }
  function lock() { dialog.querySelectorAll('button,input,textarea').forEach(el=>{if(!el.disabled){el.disabled=true;el.dataset.fiscalBusy='1';}}); }
  function display(html, footer) { q('#fiscal-body').innerHTML=html;q('#fiscal-footer').innerHTML=footer;q('#fiscal-error').textContent='';if(!dialog.open){opener=document.activeElement;dialog.showModal();}q('.fiscal-scroll').scrollTop=0;if(busy)lock(); }
  function close() { if(busy)return;if(dirty&&!confirm('توجد تعديلات لم تحفظ. إغلاق دون حفظ؟'))return;dirty=false;dialog.close();model=null;if(opener?.isConnected)opener.focus(); }
  function discard() { return !dirty||confirm('توجد تعديلات لم تحفظ. متابعة دون حفظ؟'); }
  function showError(error) { ensure();q('#fiscal-error').textContent=error.message||String(error);if(dialog.open)q('#fiscal-error').scrollIntoView({block:'nearest'});else toast(error.message||String(error)); }
  async function run(fn) { if(busy)return;busy=true;q('#fiscal-error').textContent='';lock();try{return await fn();}catch(error){showError(error);}finally{busy=false;dialog.querySelectorAll('[data-fiscal-busy]').forEach(el=>{el.disabled=false;delete el.dataset.fiscalBusy;});} }
  async function updated() { try{await refresh();}catch(error){toast('تم الحفظ؛ تعذر تحديث الرئيسية: '+error.message);}document.dispatchEvent(new CustomEvent('fiscal-config-updated')); }
  async function taxes() {
    if(!discard())return;
    try{const config=await api('tax-config');model={type:'tax-settings',config,settings:{...config.settings}};dirty=false;renderTaxes();}catch(error){showError(error);}
  }
  function codeRate(code) { return code.rate??(Number(code.rate_basis||0)/100).toFixed(2); }
  function codeRecovery(code) { return code.recoverable_percent??(Number(code.recoverable_basis||0)/100).toFixed(2); }
  function renderTaxes() {
    const settings=model.settings,codes=rows(model.config.codes||model.config.tax_codes);
    header('إعدادات الضرائب','مصر • EGP • أسعار بنود الموردين دون ضريبة','receipt');
    const registration=Object.keys(labels).filter(k=>['unconfigured','registered','not_registered'].includes(k)).map(status=>button(label(status),'registration','choice '+(settings.registration_status===status?'selected':''),`data-status="${status}"`)).join('');
    const body=`<section class="ap-section fiscal-first"><h3>موقف التسجيل الضريبي للفندق</h3><div class="ap-choices">${registration}</div><div class="ap-grid fiscal-gap">${field('tax_number','رقم التسجيل الضريبي',settings.tax_number,'text',settings.registration_status==='registered',settings.registration_status!=='registered')}${memo('notes','ملاحظات الإعداد',settings.notes)}</div>${note(settings.registration_status==='unconfigured'?'لم يتم افتراض تسجيل أو نسب ضريبية. يمكن حفظ الفواتير الحالية دون كود ضريبي؛ اختيار كود ضريبة يحتاج ضبط الإعداد أولًا.':'الأسعار في دورة الموردين دون الضريبة. الجزء القابل للاسترداد يذهب لحساب الضريبة، وغير القابل للاسترداد يضاف إلى تكلفة المصروف.')}</section><section class="ap-section"><div class="ap-inline-head"><h3>أكواد المعاملة الضريبية</h3>${button('كود ضريبي جديد','new-tax-code','primary','','add')}</div>${codes.length?`<div class="table-scroll"><table class="ap-ledger-table fiscal-tax-code-table"><thead><tr><th>الكود / الاسم</th><th>المعاملة</th><th>النسبة</th><th>قابل للاسترداد</th><th>الحالة</th><th></th></tr></thead><tbody>${codes.map(code=>`<tr><td><bdi>${esc(code.code)}</bdi><br>${esc(code.name)}</td><td>${esc(label(code.kind))}</td><td>${esc(codeRate(code))}%</td><td>${esc(codeRecovery(code))}%</td><td><span class="ap-pill ${code.active&&code.confirmed?'posted':''}">${!code.active?'موقوف':code.confirmed?'مراجع ومؤكد':'يحتاج مراجعة'}</span></td><td>${button('مراجعة / تعديل','edit-tax-code','secondary',`data-id="${code.id}"`)}</td></tr>`).join('')}</tbody></table></div>`:empty('لم تضف أكواد ضريبية. أدخل النسب المناسبة من المستندات المعتمدة عندكم؛ لا توجد نسب مفترضة.')}${note('الكود المؤكد والنشط فقط متاح للاختيار في الفاتورة. عند وجود جزء قابل للاسترداد، راجع وأكد ربط حساب ضريبة المدخلات في شجرة الحسابات قبل الاعتماد. تعديل الإعداد لا يغيّر فواتير معتمدة أو حسابات الضريبة المحفوظة معها.')}</section>`;
    display(body,button('حفظ إعداد التسجيل','save-tax-settings','primary','','save')+button('حركة ضرائب الموردين','tax-report','secondary','','chart')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  function editTaxCode(id=null) {
    if(!discard())return;
    const config=model.config,old=rows(config.codes||config.tax_codes).find(code=>code.id===Number(id));
    model={type:'tax-code',config,data:old?{...old,rate:String(codeRate(old)),recoverable_percent:String(codeRecovery(old))}:{name:'',kind:'vat',rate:'',recoverable_percent:'',active:true,confirmed:false,notes:''}};
    dirty=false;renderTaxCode();
  }
  function renderTaxCode() {
    const d=model.data,isVat=d.kind==='vat';
    header(d.id?'مراجعة كود ضريبي':'كود ضريبي جديد',d.code||'الكود يتولد تلقائيًا • النسب تدخل صراحةً','receipt');
    const kinds=['vat','zero_rated','exempt','out_of_scope'].map(kind=>button(label(kind),'tax-kind','choice '+(d.kind===kind?'selected':''),`data-kind="${kind}"`)).join('');
    display(`<section class="ap-section fiscal-first"><div class="ap-grid">${field('name','اسم المعاملة الضريبية',d.name,'text',true)}<label>كود المعاملة<input value="${esc(d.code||'يتولد عند الحفظ')}" readonly></label></div><h3 class="fiscal-gap">نوع المعاملة</h3><div class="ap-choices">${kinds}</div><div class="ap-grid fiscal-gap">${field('rate','نسبة الضريبة %',isVat?d.rate:'0','number',isVat,!isVat)}${field('recoverable_percent','الجزء القابل للاسترداد من الضريبة %',isVat?d.recoverable_percent:'0','number',isVat,!isVat)}</div>${note(isVat?'أدخل النسبة والجزء القابل للاسترداد بحسب موقف الفندق ومستند المعاملة. عند عدم التسجيل لا يستخدم جزء قابل للاسترداد.':'نسبة الضريبة والجزء القابل للاسترداد صفر تلقائيًا لهذا النوع؛ يظل نوع المعاملة محفوظًا مستقلًا.') }<div class="fiscal-flags"><label><input type="checkbox" data-fiscal-flag="active" ${d.active?'checked':''}>الكود نشط</label><label><input type="checkbox" data-fiscal-flag="confirmed" ${d.confirmed?'checked':''}>تمت مراجعة هذا الكود والنسب المسجلة</label></div><div class="ap-grid fiscal-gap">${memo('notes','ملاحظات المعاملة',d.notes)}</div></section>`,button('حفظ الكود الضريبي','save-tax-code','primary','','save')+button('رجوع للإعدادات','back-tax-settings')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  function percent(value) { const text=String(value).trim();if(!/^\d+(\.\d{1,2})?$/.test(text)||!Number.isFinite(Number(text))||Number(text)<0||Number(text)>100)throw Error('النسبة من صفر إلى ١٠٠ وبحد أقصى منزلتين عشريتين');return text; }
  async function saveTaxCode() {
    const d=model.data;if(!d.name.trim())throw Error('أدخل اسم المعاملة الضريبية');
    const data={name:d.name,kind:d.kind,rate:d.kind==='vat'?percent(d.rate):'0',recoverable_percent:d.kind==='vat'?percent(d.recoverable_percent):'0',active:!!d.active,confirmed:!!d.confirmed,notes:d.notes||''};if(d.id)data.revision=d.revision;
    await api('tax-codes'+(d.id?'/'+d.id:''),d.id?'PUT':'POST',data);dirty=false;await updated();await taxes();toast('تم حفظ الكود الضريبي');
  }
  async function taxReport(asOf=dateToday()) {
    if(!discard())return;
    try{const report=await api('tax-report?as_of='+encodeURIComponent(asOf));model={type:'tax-report',report,asOf};dirty=false;header('حركة ضرائب الموردين','من '+(report.from_date||asOf.slice(0,4)+'-01-01')+' إلى '+(report.to_date||asOf),'chart');renderTaxReport();}catch(error){showError(error);}
  }
  function taxReportTable(report) {
    const items=rows(report.rows),totals=report.totals||{};
    return `<table class="ap-ledger-table fiscal-tax-report"><thead><tr><th>التاريخ / المستند</th><th>المورد</th><th>المعاملة</th><th>الصافي</th><th>الضريبة</th><th>قابل للاسترداد</th><th>غير قابل للاسترداد</th><th>الإجمالي</th></tr></thead><tbody>${items.map(row=>`<tr><td>${esc(row.date||row.business_date||'')}<br><bdi>${esc(row.code||row.document_code||'')}</bdi></td><td>${esc(row.supplier_name||'—')}</td><td>${esc(row.tax_name||row.tax_code_name||row.tax_code||'—')}</td>${['net_cents','tax_cents','recoverable_cents','nonrecoverable_cents','gross_cents'].map(key=>`<td class="ap-number">${money(row[key]||0)}</td>`).join('')}</tr>`).join('')}<tr class="ap-table-total"><td colspan="3">الإجمالي</td>${['net_cents','tax_cents','recoverable_cents','nonrecoverable_cents','gross_cents'].map(key=>`<td class="ap-number">${money(totals[key]||0)}</td>`).join('')}</tr></tbody></table>`;
  }
  function renderTaxReport() {
    const report=model.report,totals=report.totals||{};
    display(`<div class="ap-report-controls"><label>التقرير حتى تاريخ<input type="date" data-fiscal-report-date value="${esc(model.asOf)}"></label>${button('تحديث التقرير','reload-tax-report')}</div><div class="ap-kpis">${kpi('صافي البنود',totals.net_cents)}${kpi('ضريبة المدخلات',totals.tax_cents)}${kpi('قابل للاسترداد',totals.recoverable_cents)}${kpi('غير قابل للاسترداد',totals.nonrecoverable_cents)}</div>${note('يعرض التقرير ضرائب فواتير الموردين ومرتجعاتها من بياناتها المحفوظة. لا يمثل إرسالًا إلكترونيًا أو إقرارًا ضريبيًا مقدمًا للجهة المختصة.')}<section class="ap-section"><div class="table-scroll">${rows(report.rows).length?taxReportTable(report):empty('لا توجد مستندات ضريبية مرحّلة حتى هذا التاريخ.')}</div></section>`,button('طباعة التقرير','print-tax-report','secondary','','print')+button('إعدادات الضرائب','back-tax-settings')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  async function periods(year=Number(dateToday().slice(0,4))) {
    if(!discard())return;
    if(!Number.isInteger(Number(year))||Number(year)<1900||Number(year)>9998)throw Error('السنة غير صحيحة');
    try{const snapshot=await api('accounting-periods?year='+Number(year));model={type:'periods',snapshot,year:Number(year)};dirty=false;renderPeriods();}catch(error){showError(error);}
  }
  function renderPeriods() {
    const periodRows=rows(model.snapshot.periods);
    header('الفترات المحاسبية','قفل شهري للحركات • السنة المالية يناير إلى ديسمبر','settings');
    display(`<div class="fiscal-year-controls">${button('السنة السابقة','previous-year')}<label>السنة<input type="number" min="1900" max="9998" step="1" data-fiscal-year value="${model.year}"></label>${button('عرض السنة','load-year')}${button('السنة التالية','next-year')}</div>${note('قفل الشهر يمنع إنشاء أو تعديل مستند مؤرخ داخله، ويمنع الترحيل والتصحيح إليه. الفترات الأقدم تُحجب عن التعديل أيضًا حتى لا تتغير أرصدة شهر مقفول بعدها. التقارير التاريخية تظل متاحة. هذا قفل حركة شهري، وليس قيد إقفال الأرباح والخسائر السنوي.')}<div class="fiscal-period-grid">${periodRows.map(period=>`<button type="button" class="fiscal-period-card ${period.status==='closed'||period.effective_locked?'closed':'open'}" data-fiscal-action="period-preview" data-month="${esc(period.month)}"><span class="fiscal-period-art">${illustration(period.status==='closed'?'save':'edit')}</span><span><b>${esc(monthLabel(period.month))}</b><small><bdi>${esc(period.start_date)}</bdi> — <bdi>${esc(period.end_date)}</bdi></small><em class="ap-pill ${period.status==='closed'||period.effective_locked?'fiscal-closed':'posted'}">${period.status==='open'&&period.blocked_by_later_closed?'الحركة محجوبة بفترة لاحقة':label(period.status)}</em>${period.status==='open'&&period.blocked_by_later_closed?`<small>حتى إعادة فتح ${esc(monthLabel(period.blocked_by_later_closed))}</small>`:''}</span><i aria-hidden="true">←</i></button>`).join('')}</div>`,button('طباعة الفترات','print-periods','secondary','','print')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  async function periodPreview(month) {
    if(!discard())return;
    const preview=await api('period-preview?month='+encodeURIComponent(month));model={type:'period-preview',preview,year:Number(month.slice(0,4))};dirty=false;renderPeriodPreview();
  }
  function periodChecks(preview) {
    const totals=preview.journal_totals||{},recon=preview.reconciliation||{},wh=preview.withholding_reconciliation,blockers=rows(preview.blockers),drafts=[...rows(preview.pending_drafts),...rows(preview.earlier_pending_drafts).map(row=>({...row,earlier:true}))];
    return `<div class="ap-kpis"><div class="ap-kpi"><small>القيود في الفترة</small><strong>${Number(totals.count)||0}</strong></div>${kpi('إجمالي المدين',totals.debit_cents)}${kpi('إجمالي الدائن',totals.credit_cents)}<div class="ap-kpi"><small>مطابقة الموردين</small><strong class="${recon.matched?'fiscal-ok':'fiscal-fail'}">${recon.matched?'مطابق':'تحتاج مراجعة'}</strong></div></div>${wh?note('مطابقة الخصم الضريبي والتوريدات مع القيود: '+(wh.matched?'مطابق':'تحتاج مراجعة'),wh.matched?'':'warning'):''}<div class="fiscal-check-result ${preview.can_close?'ready':'blocked'}">${illustration(preview.can_close?'save':'cancel')}<div><b>${preview.period.status==='closed'?'الفترة مقفلة':preview.can_close?'الفترة جاهزة للقفل':'توجد بنود تمنع قفل الفترة'}</b><p>${preview.period.status==='closed'?'إعادة الفتح تُسجل بسبب ومرجع مع الاحتفاظ بتاريخ القفل.':preview.can_close?'راجع المستندات والأرقام ثم سجّل مرجع وسبب القفل.':'عالج البنود الظاهرة ثم أعد معاينة الفترة.'}</p></div></div>${blockers.map(blocker=>note(blocker.message||blocker.code,'warning')).join('')}${rows(preview.warnings).map(warning=>note(warning,'warning')).join('')}${drafts.length?`<section class="ap-section"><h3>مسودات داخل الفترة أو أقدم منها تحتاج حسمًا • ${drafts.length}</h3><div class="table-scroll"><table class="ap-ledger-table"><tr><th>المستند</th><th>النوع</th><th>التاريخ</th></tr>${drafts.map(draft=>`<tr><td>${esc(draft.code)}${draft.earlier?'<br><small>أقدم من الفترة المختارة</small>':''}</td><td>${esc(({invoice:'فاتورة',return:'مرتجع',payment:'سداد',settlement:'تسوية',refund:'استرداد',withholding_remittance:'توريد خصم ضريبي'})[draft.type]||draft.label||draft.type)}</td><td>${esc(draft.date||draft.business_date||'')}</td></tr>`).join('')}</table></div></section>`:''}`;
  }
  function eventHistory(preview) {
    const events=rows(preview.events||preview.period?.events),last=preview.period?.last_event;
    const items=events.length?events:last?[last]:[];
    return items.length?`<section class="ap-section"><h3>سجل الفترة</h3><div class="table-scroll"><table class="ap-ledger-table"><tr><th>الكود / الإجراء</th><th>المرجع</th><th>السبب</th><th>التوقيت</th></tr>${items.map(event=>`<tr><td><bdi>${esc(event.code||'')}</bdi><br>${esc(label(event.action))}</td><td>${esc(event.reference)}</td><td>${esc(event.reason)}</td><td><bdi>${esc(event.created_at)}</bdi></td></tr>`).join('')}</table></div></section>`:'';
  }
  function renderPeriodPreview() {
    const preview=model.preview,period=preview.period;
    header('مراجعة الفترة • '+monthLabel(period.month),`${period.start_date} — ${period.end_date} • ${label(period.status)}`,'chart');
    display((period.status==='open'&&period.blocked_by_later_closed?note('تعديل الحركات داخل هذه الفترة محجوب بسبب قفل '+monthLabel(period.blocked_by_later_closed)+'. افتح الفترة اللاحقة أولًا إذا احتجت تعديل هذه الحركات.','warning'):'')+periodChecks(preview)+eventHistory(preview),(period.status==='open'?button('قفل الفترة','close-period','primary',preview.can_close?'':'disabled','save'):button('إعادة فتح الفترة','reopen-period','primary danger','','refresh'))+button('طباعة المراجعة','print-period-preview','secondary','','print')+button('رجوع للفترات','back-periods')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  function periodEvent(action) {
    const preview=model.preview;model={type:'period-event',action,preview,year:model.year,data:{reference:'',reason:'',request_key:crypto.randomUUID()}};dirty=false;
    header(label(action)+' • '+monthLabel(preview.period.month),action==='close'?'قفل الحركات داخل الشهر':'إعادة فتح الشهر مع سجل مراجعة','settings');
    display(note(action==='close'?'القفل لا ينشئ قيد إقفال سنوي. يمنع تسجيل حركات جديدة داخل هذه الفترة بعد اعتماده.':'يجب تسجيل سبب ومرجع إعادة الفتح. تظل أحداث القفل السابقة محفوظة.','warning')+`<section class="ap-section"><div class="ap-grid">${field('reference','مرجع قرار '+(action==='close'?'القفل':'إعادة الفتح'),'','text',true)}${memo('reason','سبب '+(action==='close'?'القفل':'إعادة الفتح'))}</div></section>`,button(action==='close'?'تأكيد قفل الفترة':'تأكيد إعادة الفتح','save-period-event','primary','','save')+button('رجوع للمراجعة','back-period-preview')+'<span class="fiscal-spacer"></span>'+button('إغلاق','close'));
  }
  function printHeader(title,subtitle='') { return `<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>${esc(title)}</h2><p>${esc(subtitle)}</p>`; }
  const signatures=()=>'<div class="ap-print-signatures"><span>أعده: ................</span><span>راجعه: ................</span><span>اعتمده: ................</span></div>';
  function printHTML(html) { document.querySelector('#print-document').innerHTML=html;window.print(); }
  function input(el) { if(!model||busy)return;const key=el.dataset.fiscalField;if(!key)return;const target=model.type==='tax-settings'?model.settings:model.data;if(!target)return;target[key]=el.value;dirty=true; }
  function change(el) { if(!model||busy||!el.dataset.fiscalFlag||model.type!=='tax-code')return;model.data[el.dataset.fiscalFlag]=el.checked;dirty=true; }
  async function dispatch(action,b) {
    if(action==='close')return close();
    if(action==='registration'){model.settings.registration_status=b.dataset.status;dirty=true;renderTaxes();return;}
    if(action==='save-tax-settings')return run(async()=>{const settings=model.settings;if(settings.registration_status==='registered'&&!settings.tax_number?.trim())throw Error('أدخل رقم التسجيل الضريبي');await api('tax-settings','PUT',{revision:settings.revision,registration_status:settings.registration_status,tax_number:settings.registration_status==='registered'?settings.tax_number:'',notes:settings.notes||''});dirty=false;await updated();await taxes();toast('تم حفظ إعداد التسجيل الضريبي');});
    if(action==='new-tax-code')return editTaxCode();
    if(action==='edit-tax-code')return editTaxCode(Number(b.dataset.id));
    if(action==='tax-kind'){model.data.kind=b.dataset.kind;if(model.data.kind!=='vat'){model.data.rate='0';model.data.recoverable_percent='0';}else if(model.data.rate==='0'){model.data.rate='';model.data.recoverable_percent='';}dirty=true;renderTaxCode();return;}
    if(action==='save-tax-code')return run(saveTaxCode);
    if(action==='back-tax-settings')return taxes();
    if(action==='tax-report')return taxReport();
    if(action==='reload-tax-report'){const date=q('[data-fiscal-report-date]').value;if(!date)throw Error('حدد تاريخ التقرير');return taxReport(date);}
    if(action==='print-tax-report')return printHTML(printHeader('حركة ضرائب الموردين','من '+(model.report.from_date||model.asOf.slice(0,4)+'-01-01')+' إلى '+(model.report.to_date||model.asOf)+' • EGP')+taxReportTable(model.report)+signatures());
    if(action==='previous-year')return periods(model.year-1);
    if(action==='next-year')return periods(model.year+1);
    if(action==='load-year')return periods(Number(q('[data-fiscal-year]').value));
    if(action==='period-preview')return run(()=>periodPreview(b.dataset.month));
    if(action==='back-periods')return periods(model.year||Number(model.preview.period.month.slice(0,4)));
    if(action==='close-period')return periodEvent('close');
    if(action==='reopen-period')return periodEvent('reopen');
    if(action==='back-period-preview'){if(!discard())return;dirty=false;return periodPreview(model.preview.period.month);}
    if(action==='save-period-event')return run(async()=>{const data=model.data,month=model.preview.period.month,eventAction=model.action;if(!data.reference.trim()||!data.reason.trim())throw Error('أدخل مرجع الإجراء وسببه');await api('accounting-periods/'+month+'/'+eventAction,'POST',{request_key:data.request_key,revision:model.preview.period.revision,reference:data.reference,reason:data.reason});dirty=false;await periodPreview(month);await updated();toast(eventAction==='close'?'تم قفل الفترة':'تمت إعادة فتح الفترة');});
    if(action==='print-period-preview'){const p=model.preview;return printHTML(printHeader('مراجعة الفترة المحاسبية',monthLabel(p.period.month)+' • '+label(p.period.status))+periodChecks(p)+eventHistory(p)+signatures());}
    if(action==='print-periods')return printHTML(printHeader('الفترات المحاسبية',String(model.year))+'<table><tr><th>الشهر</th><th>من</th><th>إلى</th><th>الحالة</th></tr>'+rows(model.snapshot.periods).map(period=>`<tr><td>${esc(monthLabel(period.month))}</td><td>${esc(period.start_date)}</td><td>${esc(period.end_date)}</td><td>${esc(period.status==='open'&&period.blocked_by_later_closed?'التعديل محجوب بقفل '+period.blocked_by_later_closed:label(period.status))}</td></tr>`).join('')+'</table>'+signatures());
  }
  return {taxes,periods,taxReport};
})();
