'use strict';
/* Supplier documents are separate, saved windows; approval is an explicit step. */
window.SupplierAP = (() => {
  let dialog, model = null, changed = false, working = false, opener = null, accounts = [], withholdingCodes = [], withholdingMapping = null;
  let paymentPreview = null, returnInvoice = null, returnPreviewTimer=null, returnPreviewSeq=0;
  const methods = [
    {id:'cash',label:'نقدي',art:'wallet'}, {id:'cheque',label:'شيك',art:'edit'},
    {id:'bank_transfer',label:'تحويل بنكي',art:'bank'}, {id:'bank_deposit',label:'إيداع بنكي',art:'bank'},
    {id:'instapay',label:'إنستاباي',art:'payroll'}, {id:'bank_card',label:'بطاقة بنكية',art:'receipt'}
  ];
  const methodFields = {
    cash:[['received_by','اسم مستلم النقدية','text',true],['receipt_reference','مرجع إقرار الاستلام (اختياري)','text',false]],
    cheque:[['bank_name','اسم البنك','text',true],['cheque_book','دفتر الشيكات','text',true],['cheque_number','رقم الشيك','text',true],['beneficiary','اسم المستفيد','text',true],['cheque_date','تاريخ الشيك','date',true],['due_date','تاريخ الاستحقاق','date',true]],
    bank_transfer:[['bank_name','البنك المحوّل إليه','text',true],['beneficiary','المستفيد','text',true],['beneficiary_account','حساب المستفيد / IBAN','text',true],['transaction_reference','مرجع التحويل','text',true],['value_date','تاريخ القيمة','date',true]],
    bank_deposit:[['bank_name','البنك المودع إليه','text',true],['beneficiary_account','حساب المورد المودع إليه','text',true],['deposit_reference','رقم إيصال الإيداع','text',true],['value_date','تاريخ القيمة','date',true]],
    instapay:[['recipient','حساب / عنوان / هاتف المستفيد','text',true],['transaction_reference','الرقم المرجعي للعملية','text',true],['value_date','تاريخ العملية','date',true]],
    bank_card:[['last_four','آخر أربعة أرقام فقط','text',true],['transaction_reference','مرجع عملية البطاقة','text',true],['value_date','تاريخ العملية','date',true]]
  };
  const ap = s => dialog.querySelector(s);
  const todayLocal = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Africa/Cairo',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const supplier = id => state.suppliers.find(s => s.id === Number(id));
  const paymentLabel = id => methods.find(m => m.id === id)?.label || id;
  const statusLabel = status => ({draft:'مسودة • غير مرحّلة',posted:'معتمد ومرحّل',cancelled:'ملغى',void:'ملغى',reversed:'معكوس بسند تصحيح'}[status] || status);
  const effectiveStatus = p => p.effective_status || p.status;
  const chequeStatus = p => p.effective_cheque_status || p.cheque_status;
  const chequeLabel = p => ({issued:'شيك صادر لم يصرف',cleared:'شيك تم صرفه',stopped:'شيك موقوف',cancelled:'شيك ملغى',bounced:'شيك مرتد',reversed:'شيك معكوس'})[chequeStatus(p)]||'—';
  const arr = value => Array.isArray(value) ? value : [];
  // Monetary payloads stay decimal strings; all displayed totals come from server cents.
  function decimalCents(value){const s=String(value);if(!/^\d+(\.\d{1,2})?$/.test(s))return NaN;const [whole,fraction='']=s.split('.');const n=Number(whole)*100+Number(fraction.padEnd(2,'0'));return Number.isSafeInteger(n)?n:NaN}
  const fromCents = n => (Number(n || 0)/100).toFixed(2);
  function btn(label, action, css='secondary', art=''){return `<button type="button" class="${css}" data-ap-action="${action}">${art?illustration(art):''}<span>${label}</span></button>`}
  function empty(text,art='edit'){return `<div class="ap-empty">${illustration(art)}${esc(text)}</div>`}
  function dateField(name,label,value,disabled=false){return `<label>${esc(label)}<input type="date" data-ap-field="${name}" value="${esc(value)}" ${disabled?'disabled':''}></label>`}
  function textField(name,label,value,type='text',required=false){return `<label>${esc(label)}${required?' *':''}<input type="${type}" data-ap-field="${name}" value="${esc(value||'')}" ${type==='number'?'min="0.01" step="0.01" inputmode="decimal"':''} maxlength="${name==='details.last_four'?4:200}" ${name==='details.last_four'?'inputmode="numeric" pattern="[0-9]{4}"':''} ${required?'required':''}></label>`}
  function kpi(label,value,note=''){return `<div class="ap-kpi"><small>${esc(label)}</small><strong>${money(Number(value)||0)}</strong>${note?`<em>${esc(note)}</em>`:''}</div>`}
  function detailsGrid(rows){return `<dl class="ap-doc-details">${rows.map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value==null||value===''?'—':value)}</dd></div>`).join('')}</dl>`}
  function ensureDialog(){
    if(dialog)return;
    dialog=document.createElement('dialog');dialog.id='supplier-ap-window';dialog.dir='rtl';
    dialog.innerHTML='<header class="ap-header"><span id="ap-art"></span><div><h2 id="ap-title"></h2><p id="ap-subtitle"></p></div><button type="button" class="close" data-ap-action="close" aria-label="إغلاق النافذة">×</button></header><div class="ap-scroll"><p class="error ap-error" id="ap-error" role="alert"></p><div class="ap-body" id="ap-body"></div><footer class="ap-footer" id="ap-footer"></footer></div>';
    document.body.appendChild(dialog);
    dialog.addEventListener('cancel',e=>{e.preventDefault();close()});
    dialog.addEventListener('click',e=>{const b=e.target.closest('[data-ap-action]');if(!b||working||b.disabled)return;dispatch(b.dataset.apAction,b).catch(error=>errorMessage(error))});
    dialog.addEventListener('input',e=>onInput(e.target));
    dialog.addEventListener('change',e=>onChange(e.target));
    window.addEventListener('beforeunload',e=>{if(changed){e.preventDefault();e.returnValue=''}});
  }
  function title(label,subtitle,art='edit'){
    ensureDialog();ap('#ap-title').textContent=label;ap('#ap-subtitle').textContent=subtitle;ap('#ap-art').innerHTML=illustration(art);
  }
  function lockControls(){dialog.querySelectorAll('button,input,textarea').forEach(control=>{if(!control.disabled){control.disabled=true;control.dataset.apBusyDisabled='1'}})}
  function display(body,footer){ap('#ap-body').innerHTML=body;ap('#ap-footer').innerHTML=footer;ap('#ap-error').textContent='';if(!dialog.open){opener=document.activeElement;dialog.showModal()}dialog.querySelector('.ap-scroll').scrollTop=0;if(working)lockControls()}
  function close(){if(working)return;if(changed&&!confirm('توجد تعديلات لم تحفظ. إغلاق النافذة دون حفظ؟'))return;changed=false;model=null;dialog.close();if(opener?.isConnected)opener.focus()}
  function errorMessage(error){ensureDialog();ap('#ap-error').textContent=error.message||String(error);ap('#ap-error').scrollIntoView({block:'nearest'})}
  async function run(task){if(working)return;working=true;ap('#ap-error').textContent='';lockControls();try{return await task()}catch(error){errorMessage(error);return null}finally{working=false;dialog.querySelectorAll('[data-ap-busy-disabled]').forEach(control=>{control.disabled=false;delete control.dataset.apBusyDisabled})}}
  async function updated(){try{await refresh()}catch(error){toast('المستند محفوظ؛ تعذر تحديث الرئيسية: '+error.message)}}
  function chooseSupplier(next){picker('اختر المورد',state.suppliers.map(s=>({id:s.id,label:s.name,detail:s.code})),r=>next(r.id))}
  async function readAccounts(){const [chart,config]=await Promise.all([api('accounting'),api('withholding-config')]);accounts=chart.accounts;withholdingMapping=arr(chart.mappings).find(m=>m.role==='withholding_payable');withholdingCodes=arr(config.codes)}
  function fundingAccounts(method){return accounts.filter(a=>a.active&&a.posting&&a.normal_side==='debit'&&(method==='bank_deposit'?a.code.startsWith('111')||a.code.startsWith('112'):a.code.startsWith(method==='cash'?'111':'112')))}
  function paymentDetailsRows(p){const d=p.details||{};const rows=[];if(p.method==='bank_card')rows.push(['نوع البطاقة',d.card_type]);for(const [key,label]of(methodFields[p.method]||[]))rows.push([label,d[key]]);return rows}
  function sourceName(id){const a=accounts.find(a=>a.id===Number(id));return a?`${a.code} • ${a.name}`:'غير محدد'}
  const netCash = p => p.net_cash_cents ?? p.amount_cents ?? 0;
  function withholdingStale(p){
    if(!p.withholding_code_id||!p.withholding)return false;
    const code=withholdingCodes.find(c=>c.id===Number(p.withholding_code_id)),w=p.withholding;
    return !code||!code.active||!code.confirmed||Number(code.rate_basis)!==Number(w.rate_basis)||code.name!==w.name||(w.code_revision!=null&&code.revision!==w.code_revision)||(withholdingMapping&&(withholdingMapping.account_id!==w.account_id||withholdingMapping.status!=='confirmed'));
  }
  function withholdingSummaryHTML(p,caption=''){
    const w=p.withholding;if(!w)return '';
    return `<div class="ap-withholding-summary" id="ap-withholding-summary">${caption?`<p class="ap-hint">${esc(caption)}</p>`:''}<div class="ap-kpis">${kpi('إجمالي التسوية',w.gross_cents??p.amount_cents??decimalCents(model.data.amount))}${kpi('وعاء الخصم دون القيمة المضافة',p.withholding_base_cents??w.base_cents)}${kpi('الخصم تحت حساب الضريبة',p.withholding_cents??w.withholding_cents,(w.rate||'0')+'%')}${kpi('صافي المبلغ للمورد',netCash(p),p.currency||state.currency||'EGP')}</div><p class="ap-hint">${esc(w.code||'')} • ${esc(w.name||'')} • النسبة ${esc(w.rate||'0')}%${w.account_code?' • حساب الالتزام '+esc(w.account_code)+' '+esc(w.account_name||''):''}</p></div>`;
  }
  function withholdingChoiceHTML(p){
    const selected=withholdingCodes.find(c=>c.id===Number(p.withholding_code_id)),saved=p.withholding;
    const name=p.withholding_code_id?(saved?.name||selected?.name||'كود محفوظ'):'بدون خصم تحت حساب الضريبة';
    const rate=p.withholding_code_id?(saved?.rate||selected?.rate):null;
    return `<section class="ap-section ap-withholding-section"><div class="ap-inline-head"><h3>الخصم تحت حساب الضريبة</h3><button type="button" class="secondary ap-withholding-choice" data-ap-action="withholding-code"><span>${esc(name)}</span>${rate?`<bdi>${esc(rate)}%</bdi>`:''}<span aria-hidden="true">⌕</span></button></div>${p.withholding_code_id?'<p class="ap-hint">المبلغ المدخل هو إجمالي تسوية الفواتير. يخصم النظام الضريبة من هذا الإجمالي ويعرض صافي النقد أو الشيك أو التحويل. الوعاء هو الجزء المسدد من صافي الفاتورة دون القيمة المضافة. تسجل الدفعات المقدمة في سند منفصل دون هذا الكود.</p>':'<p class="ap-hint">اختيار الكود صريح؛ لا توجد نسبة مفترضة. عند اختياره يحسب الوعاء والنسبة وصافي المبلغ تلقائيًا من توزيع الفواتير.</p>'}${withholdingStale(p)?'<div class="ap-note warning">تغير إعداد الخصم بعد حفظ المسودة. بياناتها المحفوظة ما زالت ظاهرة؛ حدّث التوزيع وراجع المعاينة ثم أعد حفظها قبل الاعتماد.</div>':''}</section>`;
  }
  async function chooseWithholding(){
    const p=model.data;if(p.status!=='draft')return;
    const config=await api('withholding-config');if(!dialog?.open||model?.type!=='payment'||model.data!==p)return;withholdingCodes=arr(config.codes);
    picker('اختر كود الخصم تحت حساب الضريبة',[{id:0,label:'بدون خصم تحت حساب الضريبة',detail:'السداد دون استقطاع ضريبة'},...withholdingCodes.filter(c=>c.active&&c.confirmed).map(c=>({id:c.id,label:c.code+' • '+c.name,detail:c.rate+'% • من الجزء المسدد دون القيمة المضافة'}))],row=>{
      if(!dialog?.open||model?.type!=='payment'||model.data!==p||p.status!=='draft')return;
      p.withholding_code_id=row.id||null;p.withholding=null;if(row.id)p.allow_advance=false;
      changed=true;paymentPreview=null;renderPayment();
    });
  }
  async function payment(supplierId=null,invoiceId=null){
    if(!supplierId)return chooseSupplier(id=>payment(id,invoiceId));
    if(!supplier(supplierId))return toast('المورد غير موجود');
    try{
      await readAccounts();
      const posted=await api('suppliers/'+Number(supplierId)+'/open-invoices?as_of='+todayLocal());
      let selected=null;
      if(invoiceId){selected=await api('invoices/'+Number(invoiceId));if(selected.supplier_id!==Number(supplierId)||!(selected.status==='posted'||selected.posted||selected.posted_at||selected.journal_id||selected.journal_entry_id))throw Error('السداد يرتبط بفاتورة معتمدة للمورد نفسه')}
      model={type:'payment',data:{request_key:crypto.randomUUID(),supplier_id:Number(supplierId),payment_date:todayLocal(),amount:'',currency:state.currency||'EGP',method:'cash',funding_account_id:null,allocation_mode:invoiceId?'invoice':'manual',target_invoice_id:invoiceId?Number(invoiceId):null,allocations:[],details:{received_by:supplier(supplierId).name},notes:'',allow_advance:false,withholding_code_id:null,status:'draft'},invoices:posted,selectedInvoice:selected};
      paymentPreview=null;changed=false;renderPayment();
    }catch(error){toast(error.message)}
  }
  function renderPayment(){
    const p=model.data,s=supplier(p.supplier_id),saved=!!p.id,posted=p.status!=='draft';
    title(posted?'مستند سداد مورد':'سداد مورد',`${s?.name||p.supplier_name||''} • ${p.code||'حفظ مسودة ثم اعتماد وترحيل'}`,p.method==='cheque'?'edit':p.method==='cash'?'wallet':'bank');
    if(posted){renderPaymentDetail();return}
    const own=fundingAccounts(p.method);
    const invoiceRows=model.invoices;
    const rows=invoiceRows.length?`<div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>الفاتورة / مرجع المورد</th><th>التاريخ / الاستحقاق</th><th>المتبقي</th>${p.allocation_mode==='manual'?'<th>المبلغ المخصص</th>':''}</tr></thead><tbody>${invoiceRows.map(i=>{const allocation=p.allocations.find(a=>a.invoice_id===i.id);const outstanding=i.outstanding_cents??i.balance_cents??i.total_cents;return `<tr><td>${esc(i.code)}<br><small>${esc(i.reference)}</small></td><td>${esc(i.business_date)}<br><small>${esc(i.due_date||'استحقاق غير مضبوط')}</small></td><td class="ap-number">${money(outstanding)}</td>${p.allocation_mode==='manual'?`<td><input type="number" data-ap-allocation="${i.id}" value="${esc(allocation?.amount||'')}" min="0" step="0.01" aria-label="المخصص للفاتورة ${esc(i.code)}" placeholder="0.00"></td>`:''}</tr>`}).join('')}</tbody></table></div>`:empty('لا توجد فواتير معتمدة. يمكن حفظ السداد العام كدفعة مقدمة بعد تحديد ذلك صراحة.','receipt');
    const specific=model.selectedInvoice||invoiceRows.find(i=>i.id===p.target_invoice_id);
    let html=`<div class="ap-meta">${saved?`<bdi>${esc(p.code)}</bdi>`:''}<span class="ap-pill">${statusLabel(p.status)}</span><strong>${esc(s?.name||p.supplier_name)}</strong></div><div class="ap-grid">${dateField('payment_date','تاريخ السداد *',p.payment_date)}${textField('amount',(p.withholding_code_id?'إجمالي التسوية':'المبلغ')+' ('+(p.currency||'EGP')+')',p.amount,'number',true)}<label>نوع السداد<input value="${p.allocation_mode==='invoice'?'سداد فاتورة محددة':'سداد عام من حساب المورد'}" readonly></label></div>`;
    html+=withholdingChoiceHTML(p);
    html+=`<section class="ap-section"><h3>طريقة السداد</h3><div class="ap-choices">${methods.map(m=>`<button type="button" class="choice ${m.id===p.method?'selected':''}" data-ap-action="method" data-method="${m.id}" aria-pressed="${m.id===p.method}">${illustration(m.art)}<span>${m.label}</span></button>`).join('')}</div></section>`;
    html+=`<section class="ap-section"><h3>${p.method==='cash'?'الخزينة الصادر منها السداد':p.method==='cheque'?'الحساب البنكي مصدر دفتر الشيكات':p.method==='bank_deposit'?'الخزينة أو البنك الصادر منه الإيداع':'الحساب البنكي الصادر منه السداد'}</h3><div class="ap-account-list">${own.length?own.map(a=>`<button type="button" class="choice ap-bank-choice ${p.funding_account_id===a.id?'selected':''}" data-ap-action="funding" data-account-id="${a.id}"><span>${esc(a.name)}</span><small dir="ltr">${esc(a.code)}</small></button>`).join(''):empty('لا توجد حسابات مصدر نشطة مناسبة. راجع شجرة الحسابات.','bank')}</div>${p.method==='cheque'?'<div class="ap-note">إصدار الشيك يُثبت التزام شيكات صادرة. البنك يتأثر عند تسجيل صرف الشيك في خطوة مستقلة.</div>':''}</section>`;
    html+=`<section class="ap-section"><h3>تفاصيل ${paymentLabel(p.method)}</h3>${p.method==='bank_card'?`<div class="ap-choices">${['Visa','MasterCard','Meeza','other'].map(brand=>`<button class="choice ${p.details.card_type===brand?'selected':''}" type="button" data-ap-action="card-type" data-card-type="${brand}">${brand==='other'?'أخرى':brand}</button>`).join('')}</div><p class="ap-hint">نحفظ آخر أربعة أرقام ومرجع العملية فقط.</p>`:''}<div class="ap-grid">${(methodFields[p.method]||[]).map(([key,label,type,required])=>textField('details.'+key,label,p.details[key],type,required)).join('')}</div></section>`;
    html+=`<section class="ap-section"><div class="ap-inline-head"><h3>توزيع السداد</h3>${btn('تحديث التوزيع والأرصدة','preview')}</div>${p.allocation_mode==='invoice'?`<div class="ap-note">سداد جزئي أو كامل للفاتورة <b>${esc(specific?.code||p.target_invoice_id)}</b> • مرجع المورد: ${esc(specific?.reference||'—')}. المبلغ يُخصص لهذه الفاتورة فقط.</div>`:`<div class="ap-choices"><button type="button" class="choice ${p.allocation_mode==='manual'?'selected':''}" data-ap-action="allocation-mode" data-mode="manual">توزيع يدوي على الفواتير</button><button type="button" class="choice ${p.allocation_mode==='fifo'?'selected':''}" data-ap-action="allocation-mode" data-mode="fifo">الأقدم استحقاقًا أولًا</button></div><p class="ap-hint">السداد العام يسوّي فواتير الحساب حسب التوزيع المختار. المسودات لا تدخل في الرصيد.</p>${rows}`}${p.allocation_mode!=='invoice'&&!p.withholding_code_id?`<label class="ap-advance-toggle"><input type="checkbox" data-ap-advance ${p.allow_advance?'checked':''}> السماح بتسجيل الزيادة عن المخصص كدفعة مقدمة مستقلة</label>`:''}<div id="ap-preview">${paymentPreview?previewHTML(paymentPreview):(p.withholding?withholdingSummaryHTML(p,'بيانات الخصم المحفوظة في المسودة؛ تحديث التوزيع يعرض المعاينة الحالية.'):'')+'<div class="ap-note">حدّث التوزيع لعرض المبالغ المخصصة والدفعة المقدمة وأثر السداد على حساب المصدر.</div>'}</div></section>`;
    html+=`<section class="ap-section"><div class="ap-grid"><label class="full">ملاحظات (اختياري)<textarea data-ap-field="notes" maxlength="2000">${esc(p.notes||'')}</textarea></label></div></section>`;
    display(html,btn('حفظ المسودة','save-payment','primary','save')+(saved?btn('اعتماد وترحيل','post-payment','primary','receipt')+btn('طباعة','print-payment','secondary','print'):'')+'<span class="ap-spacer"></span>'+btn('إغلاق','close','secondary','close'));
  }
  function paymentPayload(){
    const p=model.data;
    if(!p.payment_date||!Number.isSafeInteger(decimalCents(p.amount))||decimalCents(p.amount)<=0)throw Error('أدخل تاريخ السداد ومبلغًا موجبًا حتى منزلتين عشريتين');
    if(!p.funding_account_id)throw Error('اختر الخزينة أو الحساب البنكي مصدر السداد');
    for(const allocation of p.allocations){if(String(allocation.amount).trim()!==''&&!Number.isSafeInteger(decimalCents(allocation.amount)))throw Error('راجع مبالغ توزيع السداد؛ استخدم قيمة موجبة حتى منزلتين أو اترك الحقل فارغًا')}
    const result={request_key:p.request_key,supplier_id:p.supplier_id,payment_date:p.payment_date,amount:String(p.amount),currency:p.currency||state.currency||'EGP',method:p.method,funding_account_id:p.funding_account_id,allocation_mode:p.allocation_mode,allocations:p.allocations.filter(a=>Number(a.amount)>0).map(a=>({invoice_id:a.invoice_id,amount:String(a.amount)})),details:{...p.details},notes:p.notes||'',allow_advance:!!p.allow_advance};
    if(p.withholding_code_id)result.withholding_code_id=Number(p.withholding_code_id);
    if(p.target_invoice_id)result.target_invoice_id=p.target_invoice_id;
    if(p.id)result.revision=p.revision;
    return result;
  }
  function previewHTML(p){
    const rows=arr(p.allocations);
    return `${p.withholding?withholdingSummaryHTML(p):''}<div class="ap-kpis">${kpi('إجمالي المخصص للفواتير',p.allocated_cents)}${kpi('دفعة مقدمة',p.advance_cents)}${kpi('صافي المستحق قبل السداد',p.net_payable_cents)}${kpi('رصيد مرتجعات غير مستخدم',p.unapplied_return_credit_cents)}</div>${rows.length?`<div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>الفاتورة</th><th>المخصص</th><th>المتبقي قبل السداد</th></tr></thead><tbody>${rows.map(a=>`<tr><td>${esc(a.code||a.invoice_code||a.invoice_id)}</td><td class="ap-number">${money(a.amount_cents)}</td><td class="ap-number">${money(a.outstanding_cents??0)}</td></tr>`).join('')}</tbody></table></div>`:''}<div class="ap-note">رصيد المصدر قبل السداد: <b>${money(p.funding_balance_before_cents||0)}</b> • بعد السداد: <b>${money(p.funding_balance_after_cents||0)}</b> ${esc(p.currency||state.currency||'EGP')}</div>${arr(p.warnings).map(w=>`<div class="ap-note warning">${esc(typeof w==='string'?w:w.message||JSON.stringify(w))}</div>`).join('')}`;
  }
  async function previewPayment(){await readAccounts();const preview=await api('payment-preview','POST',paymentPayload());paymentPreview=preview;if(Array.isArray(preview.open_invoices))model.invoices=preview.open_invoices;renderPayment();return preview}
  async function savePayment(){await previewPayment();const p=model.data;const saved=await api('supplier-payments'+(p.id?'/'+p.id:''),p.id?'PUT':'POST',paymentPayload());model.data=normalizePayment(saved);changed=false;renderPayment();await updated();toast('تم حفظ '+saved.code+' كمسودة دون ترحيل')}
  function normalizePayment(p){const allocationRows=p.status==='draft'?arr(p.requested_allocations):arr(p.allocations);return {...p,amount:p.amount!=null?String(p.amount):fromCents(p.amount_cents),details:typeof p.details==='string'?JSON.parse(p.details):p.details||{},allocations:allocationRows.map(a=>({...a,amount:a.amount!=null?String(a.amount):fromCents(a.amount_cents)})),currency:p.currency||state.currency||'EGP'}}
  async function openPayment(id,onlyIfCurrent=false){
    if(changed&&!confirm('توجد تعديلات لم تحفظ. متابعة دون حفظ؟'))return;
    try{await readAccounts();const p=normalizePayment(await api('supplier-payments/'+Number(id)));const invoices=await api('suppliers/'+p.supplier_id+'/open-invoices?as_of='+encodeURIComponent(p.payment_date));if(onlyIfCurrent&&(!dialog?.open||model?.type!=='payment'||model.data.id!==Number(id)||changed))return;model={type:'payment',data:p,invoices};changed=false;paymentPreview=null;renderPayment();if(p.status==='draft'&&!withholdingStale(p)){try{paymentPreview=await api('payment-preview','POST',paymentPayload());renderPayment()}catch(error){errorMessage(error)}}}catch(error){toast(error.message)}
  }
  function renderPaymentDetail(){
    const p=model.data,s=supplier(p.supplier_id),alloc=arr(p.allocations);
    const cheque=p.method==='cheque',effective=effectiveStatus(p),cs=chequeStatus(p);
    title('مستند سداد مورد',`${s?.name||p.supplier_name||''} • ${p.code}`,cheque?'edit':p.method==='cash'?'wallet':'bank');
    const rows=[['تاريخ السداد',p.payment_date],['المورد',s?.name||p.supplier_name],['الطريقة',paymentLabel(p.method)],['المصدر',sourceName(p.funding_account_id)],['القيد',p.journal_code||p.journal_entry_id||p.journal_id],['نوع التوزيع',({invoice:'فاتورة محددة',fifo:'الأقدم استحقاقًا',manual:'توزيع يدوي'})[p.allocation_mode]],...paymentDetailsRows(p)];
    let html=`<div class="ap-meta"><bdi>${esc(p.code)}</bdi><span class="ap-pill ${esc(effective)}">${statusLabel(effective)}</span>${cheque?`<span class="ap-pill ${cs==='cleared'?'posted':''}">${chequeLabel(p)}</span>`:''}</div>${p.withholding?withholdingSummaryHTML(p):''}<div class="ap-kpis">${kpi(p.withholding?'إجمالي التسوية':'قيمة السداد',p.amount_cents,p.currency)}${kpi('المخصص للفواتير',p.allocated_cents??alloc.reduce((sum,a)=>sum+Number(a.amount_cents||0),0))}${kpi('دفعة مقدمة',p.advance_cents)}<div class="ap-kpi"><small>الفواتير المرتبطة</small><strong>${alloc.length}</strong><em>فواتير معتمدة</em></div></div><section class="ap-section"><h3>بيانات السداد المحفوظة</h3>${detailsGrid(rows)}</section><section class="ap-section"><h3>الفواتير المرتبطة</h3>${alloc.length?`<div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>رقم الفاتورة</th><th>مرجع المورد</th><th>${p.withholding?'إجمالي التسوية':'قيمة السداد'}</th></tr></thead><tbody>${alloc.map(a=>`<tr><td>${esc(a.code||a.invoice_code||a.invoice_id)}</td><td>${esc(a.reference||a.invoice_reference||'—')}</td><td class="ap-number">${money(a.amount_cents)}</td></tr>`).join('')}</tbody></table></div>`:empty('دفعة مقدمة دون تخصيص لفاتورة.')}</section>${p.notes?`<div class="ap-note">${esc(p.notes)}</div>`:''}`;
    if(cheque&&p.cheque_status==='cleared')html+=`<section class="ap-section"><h3>صرف الشيك من البنك</h3>${detailsGrid([['تاريخ الصرف',p.clearance_date||p.clearance?.clearance_date],['المرجع',p.clearance_reference||p.clearance?.reference],['البنك',sourceName(p.clearance_bank_account_id||p.clearance?.bank_account_id||p.funding_account_id)]])}</section>`;
    if(p.reversal)html+=`<section class="ap-section"><h3>مستند التصحيح المرتبط</h3>${detailsGrid([['مستند التصحيح',p.reversal.code],['التاريخ',p.reversal.business_date],['المرجع',p.reversal.reference],['السبب',p.reversal.reason],['قيد التصحيح',p.reversal.journal_id]])}</section>`;
    display(html,btn('طباعة المستند','print-payment','secondary','print')+(p.withholding?btn('طباعة شهادة الخصم','print-withholding','secondary','receipt'):'')+(cheque&&effective==='posted'&&cs==='issued'?btn('تسجيل صرف الشيك','clear-cheque','primary','bank'):'')+(effective==='posted'&&!cheque?btn('عكس السداد','reverse-payment','secondary danger','cancel'):'')+(cheque&&p.status==='posted'?btn('حالات الشيك وسجله','cheque-events','secondary','edit'):'')+'<span class="ap-spacer"></span>'+btn('سداد جديد','new-payment','secondary','add')+btn('إغلاق','close','secondary','close'));
  }
  function printHeader(titleText,code,status){return `<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>${esc(titleText)}</h2><p class="ap-print-state">${esc(code||'')} • ${esc(status||'')}</p>`}
  function signatures(cash=false){return `<div class="ap-print-signatures"><span>أعده: ................</span><span>راجعه: ................</span><span>اعتمده: ................</span><span>${cash?'مستلم النقدية':'المستلم'}: ................</span></div>`}
  function printHTML(html){document.querySelector('#print-document').innerHTML=html;window.print()}
  function printPayment(){
    const p=model.data;if(!p.id||changed)throw Error('احفظ تعديلات المستند أولًا لطباعة البيانات المحفوظة');
    const s=supplier(p.supplier_id),allocationRows=p.withholding?arr(p.withholding.lines).map(a=>({...a,code:a.invoice_code,reference:a.invoice_reference,amount_cents:a.allocated_cents})):p.status==='draft'&&p.allocation_mode!=='manual'?arr(paymentPreview?.allocations):arr(p.allocations),cash=p.method==='cash';
    const alloc=allocationRows.map(a=>{const invoice=arr(model.invoices).find(i=>i.id===a.invoice_id||i.invoice_id===a.invoice_id);return {...a,code:a.code||a.invoice_code||invoice?.code,reference:a.reference||a.invoice_reference||invoice?.reference}});
    const titleText=cash?'إذن صرف نقدية وإقرار استلام مورد':({cheque:'إذن إصدار شيك',bank_transfer:'إذن تحويل بنكي',bank_deposit:'إذن إيداع بنكي',instapay:'إذن سداد إنستاباي',bank_card:'إذن سداد ببطاقة بنكية'})[p.method];
    let html=printHeader(titleText,p.code,statusLabel(effectiveStatus(p)));
    const advance=p.status==='draft'?paymentPreview?.advance_cents||0:p.advance_cents;
    html+=`<div class="ap-print-details">${[['المورد',s?.name||p.supplier_name],['كود المورد',s?.code||p.supplier_code],['تاريخ السداد',p.payment_date],['العملة',p.currency],['المصدر',sourceName(p.funding_account_id)],...paymentDetailsRows(p)].map(([key,value])=>`<p><b>${esc(key)}:</b> ${esc(value||'—')}</p>`).join('')}</div><h2>${p.withholding?(p.method==='cheque'?'قيمة الشيك الصافية':'صافي المبلغ للمورد'):'القيمة'}: ${money(netCash(p))} ${esc(p.currency)}</h2>${p.withholding?`<p>إجمالي تسوية الفواتير: ${money(p.amount_cents)} • وعاء الخصم دون القيمة المضافة: ${money(p.withholding_base_cents)} • الخصم تحت حساب الضريبة: ${money(p.withholding_cents)} (${esc(p.withholding.rate)}%) • شهادة ${esc(p.withholding.certificate_code||'WHT-'+p.code)}</p>`:''}${p.status==='draft'?'<p>توزيع المسودة للمراجعة، ويثبت نهائيًا عند الاعتماد.</p>':''}<table><thead><tr><th>رقم الفاتورة</th><th>مرجع فاتورة المورد</th><th>${p.withholding?'إجمالي تسوية الفاتورة':'المسدد'}</th></tr></thead><tbody>${alloc.map(a=>`<tr><td>${esc(a.code||a.invoice_code||a.invoice_id)}</td><td>${esc(a.reference||a.invoice_reference||'—')}</td><td>${money(a.amount_cents)}</td></tr>`).join('')}${advance?`<tr><td colspan="2">دفعة مقدمة غير مخصصة</td><td>${money(advance)}</td></tr>`:''}</tbody></table>`;
    if(cash)html+=`<p class="ap-print-note">${effectiveStatus(p)==='posted'?'إقرار استلام':effectiveStatus(p)==='reversed'?'نسخة المستند الأصلي — السداد معكوس بسند تصحيح':'مسودة إقرار استلام — لم يعتمد السداد'}: أقر أنا / ${esc(p.details.received_by||'................')} باستلام مبلغ ${money(netCash(p))} ${esc(p.currency)} من نيو بانوراما رمسيس عن المستند والفواتير المبينة أعلاه، لحساب المورد / ${esc(s?.name||p.supplier_name)}.</p><p>توقيع المستلم: ............................ &nbsp; رقم إثبات الشخصية: ............................ &nbsp; تاريخ الاستلام: ............................</p>`;
    if(p.method==='cheque')html+=`<p class="ap-print-note">حالة الشيك: ${chequeLabel(p)}.</p>`;
    if(p.reversal)html+=`<p class="ap-print-note">السداد معكوس بتاريخ ${esc(p.reversal.business_date)} • السبب: ${esc(p.reversal.reason)} • قيد التصحيح: ${esc(p.reversal.journal_id)}</p>`;
    html+=`<p>${esc(p.notes||'')}</p>${signatures(cash)}`;printHTML(html);
  }
  function printWithholding(){
    const p=model.data,w=p.withholding;
    if(!p.id||p.status!=='posted'||changed||!w)throw Error('شهادة الخصم تطبع من سند السداد المعتمد المحفوظ فقط');
    const code=w.certificate_code||'WHT-'+p.code;
    let html=printHeader('شهادة خصم تحت حساب الضريبة — نسخة داخلية',code,effectiveStatus(p)==='reversed'?'ملغاة بعكس السداد بتاريخ '+p.reversal?.business_date:'مرتبطة بسداد معتمد');
    html+=`<div class="ap-print-details">${[['المورد',w.supplier_name],['كود المورد',w.supplier_code],['الرقم الضريبي للمورد',w.supplier_tax_number],['سند السداد',p.code],['تاريخ السداد',p.payment_date],['طريقة السداد',paymentLabel(p.method)],['كود الخصم',w.code],['المعاملة',w.name],['نسبة الخصم',w.rate+'%'],['حساب الضريبة',w.account_code+' • '+w.account_name]].map(([key,value])=>`<p><b>${esc(key)}:</b> ${esc(value||'—')}</p>`).join('')}</div><h3>الفواتير المخصصة ووعاء الخصم دون القيمة المضافة</h3><table><thead><tr><th>الفاتورة</th><th>مرجع المورد</th><th>إجمالي التسوية</th><th>وعاء الخصم</th><th>قيمة الخصم</th></tr></thead><tbody>${arr(w.lines).map(line=>`<tr><td>${esc(line.invoice_code||line.invoice_id)}</td><td>${esc(line.invoice_reference||'—')}</td><td>${money(line.allocated_cents)}</td><td>${money(line.base_cents)}</td><td>${money(line.withholding_cents)}</td></tr>`).join('')}<tr><th colspan="2">الإجمالي</th><th>${money(w.gross_cents??p.amount_cents)}</th><th>${money(w.base_cents??p.withholding_base_cents)}</th><th>${money(w.withholding_cents??p.withholding_cents)}</th></tr></tbody></table><p>صافي المبلغ للمورد: <b>${money(netCash(p))} ${esc(p.currency)}</b></p>`;
    if(p.method==='cheque')html+=`<p>رقم الشيك: ${esc(p.details.cheque_number)} • ${esc(p.details.bank_name)} • الحالة: ${esc(chequeLabel(p))}.</p>`;
    if(p.reversal)html+=`<p class="ap-print-note">عُكس سند السداد بالكامل بمستند ${esc(p.reversal.code||'')} بتاريخ ${esc(p.reversal.business_date)}. السبب: ${esc(p.reversal.reason)} • قيد العكس: ${esc(p.reversal.journal_id)}.</p>`;
    html+=signatures();printHTML(html);
  }
  async function payments(supplierId=null){
    if(!supplierId)return chooseSupplier(payments);
    try{const rows=await api('supplier-payments?supplier_id='+Number(supplierId));model={type:'payment-list',supplierId:Number(supplierId),rows:arr(rows)};changed=false;title('سداد المورد',supplier(supplierId)?.name||'','wallet');display(rows.length?`<div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>المستند</th><th>التاريخ</th><th>الطريقة</th><th>القيمة</th><th>الحالة</th><th></th></tr></thead><tbody>${rows.map(p=>`<tr><td>${esc(p.code)}</td><td>${esc(p.payment_date)}</td><td>${esc(paymentLabel(p.method))}</td><td class="ap-number">${money(p.amount_cents)}${p.withholding?`<small>صافي للمورد ${money(netCash(p))}<br>خصم ضريبة ${money(p.withholding_cents)}</small>`:''}</td><td><span class="ap-pill ${esc(effectiveStatus(p))}">${statusLabel(effectiveStatus(p))}</span>${p.method==='cheque'?`<br><small>${chequeLabel(p)}</small>`:''}</td><td>${btn('فتح','open-payment')}<span hidden data-ap-row-id="${p.id}"></span></td></tr>`).join('')}</tbody></table></div>`:empty('لا توجد مستندات سداد لهذا المورد.','wallet'),btn('سداد جديد','new-payment','primary','add')+'<span class="ap-spacer"></span>'+btn('إغلاق','close'));}catch(error){toast(error.message)}
  }
  async function returns(invoiceId){
    try{
      const invoice=await api('invoices/'+Number(invoiceId));
      if(invoice.is_opening)throw Error('الرصيد الافتتاحي مستند سابق دون أصناف؛ لا ينشأ عنه مرتجع مخزني');
      if(invoice.status!=='posted'&&!invoice.posted_at&&!invoice.posted&&!invoice.journal_id&&!invoice.journal_entry_id)throw Error('المرتجع يرتبط بفاتورة معتمدة ومرحّلة');
      returnInvoice=invoice;model={type:'return',data:{request_key:crypto.randomUUID(),invoice_id:invoice.id,supplier_id:invoice.supplier_id,business_date:todayLocal(),reference:'',notes:'',status:'draft',lines:[]}};changed=false;renderReturn();
    }catch(error){toast(error.message)}
  }
  async function openReturn(id){
    try{const r=await api('supplier-returns/'+Number(id));returnInvoice=await api('invoices/'+r.invoice_id);model={type:'return',data:r};changed=false;renderReturn()}catch(error){toast(error.message)}
  }
  function returnLineCents(quantity,price){const s=String(quantity);if(!/^\d+(\.\d{1,3})?$/.test(s))return 0;const [whole,part='']=s.split('.');const n=BigInt(whole)*1000n+BigInt(part.padEnd(3,'0'));return Number((n*BigInt(price)+500n)/1000n)}
  function scheduleTaxReturnPreview(){
    clearTimeout(returnPreviewTimer);const sequence=++returnPreviewSeq;
    if(model?.type!=='return'||model.data.status!=='draft'||!returnInvoice?.tax_code_id)return;
    const data=model.data,lines=data.lines.filter(l=>Number(l.quantity)>0).map(l=>({invoice_line_id:l.invoice_line_id,quantity:String(l.quantity)}));
    if(!lines.length)return;
    if(ap('#ap-return-total'))ap('#ap-return-total').textContent='جاري حساب الإجمالي شامل الضريبة…';
    returnPreviewTimer=setTimeout(async()=>{try{
      const report=await api('supplier-return-preview','POST',{invoice_id:returnInvoice.id,business_date:data.business_date,reference:data.reference,notes:data.notes||'',lines});
      if(sequence!==returnPreviewSeq||model?.data!==data||!dialog?.open)return;
      for(const line of report.lines){const el=ap('[data-return-line-total="'+line.invoice_line_id+'"]');if(el)el.textContent=money(line.total_cents)}
      ap('#ap-return-total').textContent=money(report.total_cents);
      ap('#ap-return-tax-note').textContent='صافي المرتجع: '+money(report.net_cents)+' • الضريبة: '+money(report.tax_cents)+' • الإجمالي يشمل الضريبة الأصلية للفاتورة.';
    }catch(error){if(sequence===returnPreviewSeq&&model?.data===data){ap('#ap-return-total').textContent='راجع الكميات والتاريخ';ap('#ap-return-tax-note').textContent=error.message}}},150);
  }
  function renderReturn(){
    const r=model.data,invoice=returnInvoice,posted=r.status!=='draft',s=supplier(r.supplier_id),lines=arr(r.lines);
    title(r.status==='posted'?'مرتجع مورد معتمد':r.status==='cancelled'?'مرتجع مورد ملغى':'مرتجع فاتورة مورد',`${s?.name||invoice.supplier_name||''} • الفاتورة الأصلية ${invoice.code}`,'receipt');
    const available=arr(invoice.lines).filter(l=>Number(l.returnable_quantity??l.quantity)>0||lines.some(x=>x.invoice_line_id===l.id));
    const rendered=(posted?lines:available).map(l=>{const lineId=posted?l.invoice_line_id:l.id;const selected=lines.find(x=>x.invoice_line_id===lineId);const qty=posted?l.quantity:selected?.quantity||'';const original=arr(invoice.lines).find(x=>x.id===lineId)||l;const value=posted?l.total_cents:returnLineCents(qty,original.price_cents);return `<tr><td>${esc(original.name)}<br><small>${esc(original.unit)}</small></td><td class="ap-number">${esc(original.quantity)}</td><td class="ap-number">${esc(original.returnable_quantity??original.quantity)}</td><td>${posted?esc(qty):`<input class="ap-qty" type="number" min="0" max="${esc(original.returnable_quantity??original.quantity)}" step="0.001" value="${esc(qty)}" data-ap-return-line="${lineId}" aria-label="كمية مرتجع ${esc(original.name)}" placeholder="0">`}</td><td class="ap-number">${money(original.price_cents)}</td><td class="ap-number" data-return-line-total="${lineId}">${money(value)}</td></tr>`}).join('');
    const total=posted?r.total_cents:lines.reduce((sum,l)=>sum+returnLineCents(l.quantity,invoice.lines.find(x=>x.id===l.invoice_line_id)?.price_cents||0),0);
    const head=`<div class="ap-meta">${r.id?`<bdi>${esc(r.code)}</bdi>`:''}<span class="ap-pill ${esc(r.status)}">${statusLabel(r.status)}</span><strong>${esc(s?.name||invoice.supplier_name)}</strong></div>`;
    const info=posted?`<section class="ap-section">${detailsGrid([['تاريخ المرتجع',r.business_date],['مرجع المرتجع',r.reference],['الفاتورة الأصلية',invoice.code],['مرجع فاتورة المورد',invoice.reference],['قيد المرتجع',r.journal_code||r.journal_id||r.journal_entry_id]])}</section>`:`<div class="ap-grid">${dateField('business_date','تاريخ المرتجع *',r.business_date)}${textField('reference','رقم / مرجع المرتجع',r.reference,'text',true)}<label>الفاتورة الأصلية<input readonly value="${esc(invoice.code+' • '+invoice.reference)}"></label></div>`;
    const table=`<section class="ap-section"><h3>بنود المرتجع</h3><div class="ap-note">السعر والضريبة وحساب المصروف ومركز التكلفة تؤخذ من الفاتورة الأصلية تلقائيًا. المرتجع لا يغيّر آخر سعر شراء.</div><div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>البند / الوحدة</th><th>الكمية الأصلية</th><th>المتاح للرد حاليًا</th><th>كمية المرتجع</th><th>سعر دون ضريبة</th><th>القيمة شاملة الضريبة</th></tr></thead><tbody>${rendered||'<tr><td colspan="6" class="ap-empty">لا توجد كمية متاحة للرد.</td></tr>'}<tr class="ap-table-total"><td colspan="5">إجمالي المرتجع (${esc(state.currency||'EGP')})</td><td id="ap-return-total" class="ap-number">${money(total)}</td></tr></tbody></table></div></section>`;
    const notes=posted?(r.notes?`<div class="ap-note">${esc(r.notes)}</div>`:''):`<section class="ap-section"><div class="ap-grid"><label class="full">ملاحظات (اختياري)<textarea data-ap-field="notes" maxlength="2000">${esc(r.notes||'')}</textarea></label></div></section>`;
    display(head+info+table+'<div class="ap-note" id="ap-return-tax-note">السعر دون ضريبة؛ إجمالي المرتجع يتضمن الضريبة الأصلية إن وجدت.</div>'+notes,(posted?'':btn('حفظ مسودة المرتجع','save-return','primary','save'))+(r.id&&!posted?btn('اعتماد وترحيل','post-return','primary','receipt'):'')+(r.id?btn('طباعة المرتجع','print-return','secondary','print'):'')+'<span class="ap-spacer"></span>'+btn('إغلاق','close','secondary','close'));
    scheduleTaxReturnPreview();
  }
  function returnPayload(){const r=model.data;for(const line of r.lines){if(String(line.quantity).trim()!==''&&!/^\d+(\.\d{1,3})?$/.test(String(line.quantity)))throw Error('راجع كميات المرتجع؛ استخدم كمية موجبة حتى ثلاث منازل عشرية')}return {request_key:r.request_key,invoice_id:r.invoice_id,reference:r.reference,business_date:r.business_date,notes:r.notes||'',revision:r.revision,lines:r.lines.filter(l=>Number(l.quantity)>0).map(l=>({invoice_line_id:l.invoice_line_id,quantity:String(l.quantity)}))}}
  async function saveReturn(){const r=model.data;if(!r.reference?.trim()||!r.business_date)throw Error('أدخل تاريخ ومرجع المرتجع');if(!r.lines.some(l=>Number(l.quantity)>0))throw Error('أدخل كمية مرتجعة لبند واحد على الأقل');const saved=await api('supplier-returns'+(r.id?'/'+r.id:''),r.id?'PUT':'POST',returnPayload());model.data=saved;changed=false;renderReturn();await updated();toast('تم حفظ '+saved.code+' كمسودة')}
  function printReturn(){const r=model.data;if(!r.id||changed)throw Error('احفظ تعديلات المرتجع أولًا لطباعة البيانات المحفوظة');const i=returnInvoice,s=supplier(r.supplier_id);printHTML(printHeader('مرتجع فاتورة مورد',r.code,statusLabel(r.status))+`<p>المورد: ${esc(s?.name||i.supplier_name)} • كود المورد: ${esc(s?.code||i.supplier_code)}</p><p>تاريخ المرتجع: ${esc(r.business_date)} • مرجع المرتجع: ${esc(r.reference)}</p><p>الفاتورة الأصلية: ${esc(i.code)} • مرجع فاتورة المورد: ${esc(i.reference)}</p><table><thead><tr><th>البند</th><th>الوحدة</th><th>الكمية المرتجعة</th><th>سعر دون ضريبة</th><th>القيمة شاملة الضريبة</th></tr></thead><tbody>${r.lines.map(l=>`<tr><td>${esc(l.name)}</td><td>${esc(l.unit)}</td><td>${esc(l.quantity)}</td><td>${money(l.price_cents)}</td><td>${money(l.total_cents)}</td></tr>`).join('')}</tbody></table><p>صافي المرتجع: ${money(r.subtotal_cents??r.net_cents??r.total_cents)} • الضريبة: ${money(r.tax_cents||0)}</p><h2>إجمالي المرتجع: ${money(r.total_cents)} ${esc(state.currency||'EGP')}</h2><p>${esc(r.notes)}</p>${signatures()}`)}
  async function returnList(supplierId=null){if(!supplierId)return chooseSupplier(returnList);try{const rows=await api('supplier-returns?supplier_id='+Number(supplierId));model={type:'return-list',supplierId:Number(supplierId),rows};changed=false;title('مرتجعات المورد',supplier(supplierId)?.name||'','receipt');display(rows.length?`<div class="table-scroll"><table class="ap-ledger-table"><thead><tr><th>المرتجع / المرجع</th><th>التاريخ</th><th>الفاتورة</th><th>القيمة</th><th>الحالة</th><th></th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.code)}<br><small>${esc(r.reference)}</small></td><td>${esc(r.business_date)}</td><td>${esc(r.invoice_code||r.invoice_id)}</td><td class="ap-number">${money(r.total_cents)}</td><td><span class="ap-pill ${esc(r.status)}">${statusLabel(r.status)}</span></td><td>${btn('فتح','open-return')}<span hidden data-ap-row-id="${r.id}"></span></td></tr>`).join('')}</tbody></table></div>`:empty('لا توجد مرتجعات مسجلة لهذا المورد.'),btn('مرتجع جديد','choose-return-invoice','primary','add')+'<span class="ap-spacer"></span>'+btn('إغلاق','close'))}catch(error){toast(error.message)}}
  async function chooseReturnInvoice(){const id=model.supplierId||model.data?.supplier_id;const list=(await api('invoices?supplier_id='+id)).filter(i=>!i.is_opening&&(i.status==='posted'||i.posted_at||i.journal_id||i.journal_entry_id));if(!list.length)throw Error('لا توجد فاتورة معتمدة لهذا المورد');picker('الفاتورة الأصلية للمرتجع',list.map(i=>({id:i.id,label:i.code+' • '+i.reference,detail:money(i.total_cents)+' '+(state.currency||'EGP')})),r=>returns(r.id))}
  async function aging(asOf=todayLocal()){
    try{const report=await api('supplier-aging?as_of='+encodeURIComponent(asOf));model={type:'aging',report,asOf};changed=false;title('أعمار مديونيات الموردين','حسب تاريخ الاستحقاق • الفواتير والمرتجعات والسداد المرحّل حتى تاريخ التقرير','chart');renderAging()}catch(error){toast(error.message)}
  }
  function agingTable(report,print=false){
    const keys=['current_cents','days_1_30_cents','days_31_60_cents','days_61_90_cents','days_over_90_cents','total_cents','credit_cents','advance_cents','net_payable_cents'];
    const rows=arr(report.suppliers),totals=report.totals||{};
    return `<table class="ap-ledger-table ap-aging-table"><thead><tr><th>المورد</th><th>غير مستحق</th><th>١–٣٠ يومًا</th><th>٣١–٦٠ يومًا</th><th>٦١–٩٠ يومًا</th><th>أكثر من ٩٠</th><th>إجمالي الفواتير</th><th>رصيد مرتجعات</th><th>دفعات مقدمة</th><th>صافي المستحق</th></tr></thead><tbody>${rows.map(s=>`<tr><td>${print?esc(s.name):`<button type="button" class="ap-name-button" data-ap-action="supplier-statement" data-supplier-id="${s.supplier_id}">${esc(s.name)}</button>`}<br><small>${esc(s.code)}</small></td>${keys.map(k=>`<td class="ap-number">${money(s[k]||0)}</td>`).join('')}</tr>`).join('')}<tr class="ap-table-total"><td>الإجمالي</td>${keys.map(k=>`<td class="ap-number">${money(totals[k]??rows.reduce((sum,r)=>sum+Number(r[k]||0),0))}</td>`).join('')}</tr></tbody></table>`;
  }
  function renderAging(){const r=model.report,t=r.totals||{};display(`<div class="ap-report-controls"><label>التقرير حتى تاريخ<input type="date" data-ap-report-date value="${esc(model.asOf)}"></label>${btn('تحديث التقرير','reload-aging')}</div><div class="ap-kpis">${kpi('إجمالي الفواتير المفتوحة',t.total_cents)}${kpi('رصيد مرتجعات غير مستخدم',t.credit_cents)}${kpi('دفعات مقدمة',t.advance_cents)}${kpi('صافي المستحق',t.net_payable_cents)}</div><div class="ap-note">العمر يُحسب من تاريخ الاستحقاق، مع استبعاد الحركات اللاحقة لتاريخ التقرير. الدفعات المقدمة ورصيد المرتجعات يظهران منفصلين حتى يظل الرصيد قابلًا للمراجعة.</div><section class="ap-section"><div class="table-scroll">${r.suppliers?.length?agingTable(r):empty('لا توجد مديونيات موردين حتى هذا التاريخ.','chart')}</div></section>`,btn('طباعة التقرير','print-aging','secondary','print')+'<span class="ap-spacer"></span>'+btn('إغلاق','close'))}
  function statementTable(report,print=false){const labels={invoice:'فاتورة مورد',supplier_invoice:'فاتورة مورد',return:'مرتجع مورد',supplier_return:'مرتجع مورد',payment:'سداد مورد',supplier_payment:'سداد مورد',cheque_clearance:'صرف شيك',supplier_settlement:'تسوية رصيد مورد',supplier_settlement_reversal:'عكس تسوية',supplier_refund:'استرداد من المورد',supplier_payment_reversal:'عكس سداد',supplier_cheque_bounce:'ارتداد شيك',supplier_cheque_clearance:'صرف شيك'};const rows=arr(report.rows);return `<table class="ap-ledger-table"><thead><tr><th>التاريخ</th><th>المستند / النوع</th><th>البيان</th><th>مدين</th><th>دائن</th><th>الرصيد الدائن</th><th>دفعة مقدمة مستقلة</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.code)}<br><small>${esc(labels[r.type]||r.type)}</small></td><td>${esc(r.description)}</td><td class="ap-number">${money(r.debit_cents)}</td><td class="ap-number">${money(r.credit_cents)}</td><td class="ap-number">${money(r.balance_cents)}</td><td class="ap-number">${r.advance_cents?money(r.advance_cents):'—'}</td></tr>`).join('')}<tr class="ap-table-total"><td colspan="3">إجمالي الحركة / الرصيد</td><td class="ap-number">${money(report.totals?.debit_cents||0)}</td><td class="ap-number">${money(report.totals?.credit_cents||0)}</td><td class="ap-number">${money(report.totals?.balance_cents||0)}</td><td class="ap-number">${money(report.totals?.advance_cents||0)}</td></tr></tbody></table>`}
  async function statement(supplierId=null,asOf=todayLocal()){
    if(!supplierId)return chooseSupplier(id=>statement(id,asOf));
    try{const report=await api('suppliers/'+Number(supplierId)+'/statement?as_of='+encodeURIComponent(asOf));model={type:'statement',supplierId:Number(supplierId),report,asOf};changed=false;title('كشف حساب المورد',report.supplier?.name||supplier(supplierId)?.name||'','edit');renderStatement()}catch(error){toast(error.message)}
  }
  function renderStatement(){const r=model.report,t=r.totals||{};display(`<div class="ap-report-controls"><label>كشف الحساب حتى<input type="date" data-ap-report-date value="${esc(model.asOf)}"></label>${btn('تحديث الكشف','reload-statement')}</div><div class="ap-kpis">${kpi('إجمالي المدين',t.debit_cents)}${kpi('إجمالي الدائن',t.credit_cents)}${kpi('الرصيد الدائن للمورد',t.balance_cents)}${kpi('دفعات مقدمة منفصلة',t.advance_cents)}</div><div class="ap-note">الرصيد الموجب دائن لصالح المورد، والرصيد السالب مدين لصالح الفندق. الحركات المرحّلة فقط تظهر هنا.<br>صافي المستحق بعد خصم الدفعات المقدمة: <b>${money(t.net_payable_cents??t.balance_cents??0)} ${esc(r.currency||state.currency||'EGP')}</b>.</div><section class="ap-section"><div class="table-scroll">${r.rows?.length?statementTable(r):empty('لا توجد حركات محاسبية مرحّلة للمورد حتى هذا التاريخ.')}</div></section>`,btn('طباعة الكشف','print-statement','secondary','print')+btn('مستندات السداد','payment-list','secondary','wallet')+btn('المرتجعات','return-list','secondary','receipt')+'<span class="ap-spacer"></span>'+btn('سداد جديد','new-payment','primary','add')+btn('إغلاق','close'))}
  function clearCheque(){const p=model.data;model.clearance={clearance_date:todayLocal(),bank_account_id:p.funding_account_id,reference:''};title('تسجيل صرف شيك من البنك',`${p.code} • الشيك ${p.details.cheque_number}`,'bank');display(`<div class="ap-note">هذه الخطوة تخفض حساب الشيكات الصادرة وتثبت الخصم من البنك. سداد المورد مسجل وقت إصدار الشيك.</div><div class="ap-kpis">${kpi('قيمة الشيك',netCash(p),p.currency)}${p.withholding?kpi('خصم تحت حساب الضريبة',p.withholding_cents,p.currency):''}</div><div class="ap-grid two">${dateField('clearance.clearance_date','تاريخ الصرف من البنك *',model.clearance.clearance_date)}${textField('clearance.reference','مرجع كشف البنك / الصرف','','text',true)}</div><section class="ap-section"><h3>البنك مصدر الشيك • تلقائي</h3>${detailsGrid([['حساب البنك',sourceName(p.funding_account_id)],['البنك المسحوب عليه',p.details.bank_name],['رقم الشيك',p.details.cheque_number]])}</section>`,btn('إثبات صرف الشيك','post-clearance','primary','save')+'<span class="ap-spacer"></span>'+btn('عودة للمستند','back-payment')+btn('إغلاق','close'))}
  function onInput(input){
    if(!model||working)return;
    const field=input.dataset.apField;
    if(field&&model.data&&model.data.status!=='posted'){
      if(field.startsWith('details.'))model.data.details[field.slice(8)]=input.value;
      else model.data[field]=input.value;
      changed=true;paymentPreview=null;
      if(model.type==='payment'&&ap('#ap-preview'))ap('#ap-preview').innerHTML='<div class="ap-note">تم تعديل بيانات السداد. حدّث التوزيع لمراجعة الأرصدة.</div>';
    }else if(field?.startsWith('clearance.')&&model.clearance){model.clearance[field.slice(10)]=input.value;changed=true}
    if(input.hasAttribute('data-ap-allocation')&&model.type==='payment'){
      const id=Number(input.dataset.apAllocation);const old=model.data.allocations.find(a=>a.invoice_id===id);if(old)old.amount=input.value;else model.data.allocations.push({invoice_id:id,amount:input.value});changed=true;paymentPreview=null;ap('#ap-preview').innerHTML='<div class="ap-note">حدّث التوزيع بعد تعديل المبالغ المخصصة.</div>';
    }
    if(input.hasAttribute('data-ap-return-line')&&model.type==='return'&&model.data.status!=='posted'){
      const id=Number(input.dataset.apReturnLine),old=model.data.lines.find(l=>l.invoice_line_id===id);if(old)old.quantity=input.value;else model.data.lines.push({invoice_line_id:id,quantity:input.value});changed=true;
      const row=returnInvoice.lines.find(l=>l.id===id);ap('[data-return-line-total="'+id+'"]').textContent=money(returnLineCents(input.value,row.price_cents));ap('#ap-return-total').textContent=money(model.data.lines.reduce((sum,l)=>sum+returnLineCents(l.quantity,returnInvoice.lines.find(x=>x.id===l.invoice_line_id)?.price_cents||0),0));scheduleTaxReturnPreview();
    }
  }
  function onChange(input){if(model?.type==='payment'&&input.hasAttribute('data-ap-advance')){model.data.allow_advance=input.checked;changed=true;paymentPreview=null;ap('#ap-preview').innerHTML='<div class="ap-note">حدّث التوزيع بعد تغيير سياسة الدفعة المقدمة.</div>'}}
  async function dispatch(action,button){
    if(action==='close')return close();
    if(action==='method'){
      const p=model.data;if(p.method===button.dataset.method)return;p.method=button.dataset.method;p.funding_account_id=null;p.details={};if(p.method==='cash')p.details.received_by=supplier(p.supplier_id)?.name||'';if(p.method==='cheque'){p.details.cheque_date=p.payment_date;p.details.due_date=p.payment_date;p.details.beneficiary=supplier(p.supplier_id)?.name||''}else if(p.method!=='cash')p.details.value_date=p.payment_date;changed=true;paymentPreview=null;renderPayment();return;
    }
    if(action==='funding'){model.data.funding_account_id=Number(button.dataset.accountId);changed=true;paymentPreview=null;renderPayment();return}
    if(action==='card-type'){model.data.details.card_type=button.dataset.cardType;changed=true;renderPayment();return}
    if(action==='allocation-mode'){model.data.allocation_mode=button.dataset.mode;model.data.allocations=[];changed=true;paymentPreview=null;renderPayment();return}
    if(action==='withholding-code')return chooseWithholding();
    if(action==='print-withholding')return printWithholding();
    if(action==='preview')return run(previewPayment);
    if(action==='save-payment')return run(savePayment);
    if(action==='post-payment'){
      if(changed)throw Error('احفظ تعديلات السداد أولًا ثم راجع المسودة واعتمدها');
      return run(async()=>{const p=await api('supplier-payments/'+model.data.id+'/post','POST',{revision:model.data.revision});model.data=normalizePayment(p);changed=false;renderPayment();await updated();toast('تم اعتماد السداد وترحيل القيد')});
    }
    if(action==='print-payment')return printPayment();
    if(action==='open-payment')return openPayment(Number(button.parentElement.querySelector('[data-ap-row-id]').dataset.apRowId));
    if(action==='new-payment'){const id=model.supplierId||model.data?.supplier_id;return payment(id)}
    if(action==='payment-list')return payments(model.supplierId||model.data?.supplier_id);
    if(action==='return-list')return returnList(model.supplierId||model.data?.supplier_id);
    if(action==='save-return')return run(saveReturn);
    if(action==='post-return'){
      if(changed)throw Error('احفظ تعديلات المرتجع أولًا قبل الاعتماد');
      return run(async()=>{const r=await api('supplier-returns/'+model.data.id+'/post','POST',{revision:model.data.revision});model.data=r;returnInvoice=await api('invoices/'+r.invoice_id);changed=false;renderReturn();await updated();toast('تم اعتماد المرتجع وترحيل القيد')});
    }
    if(action==='print-return')return printReturn();
    if(action==='open-return')return openReturn(Number(button.parentElement.querySelector('[data-ap-row-id]').dataset.apRowId));
    if(action==='choose-return-invoice')return chooseReturnInvoice();
    if(action==='supplier-statement')return statement(Number(button.dataset.supplierId),model.asOf);
    if(action==='reload-aging'){const date=ap('[data-ap-report-date]').value;if(!date)throw Error('حدد تاريخ التقرير');return aging(date)}
    if(action==='reload-statement'){const date=ap('[data-ap-report-date]').value;if(!date)throw Error('حدد تاريخ الكشف');return statement(model.supplierId,date)}
    if(action==='print-aging')return printHTML(printHeader('أعمار مديونيات الموردين','',`حتى ${model.asOf} • ${state.currency||'EGP'}`)+agingTable(model.report,true)+signatures());
    if(action==='print-statement')return printHTML(printHeader('كشف حساب مورد',model.report.supplier?.code,`حتى ${model.asOf} • ${state.currency||'EGP'}`)+`<h3>${esc(model.report.supplier?.name||supplier(model.supplierId)?.name)}</h3>`+statementTable(model.report,true)+`<p>دفعات مقدمة منفصلة: ${money(model.report.totals?.advance_cents||0)}</p>`+signatures());
    if(action==='reverse-payment')return SupplierSettlement.reversePayment(model.data.id);
    if(action==='cheque-events')return SupplierSettlement.chequeEvents(model.data.id);
    if(action==='clear-cheque')return clearCheque();
    if(action==='clearance-bank'){model.clearance.bank_account_id=Number(button.dataset.accountId);changed=true;dialog.querySelectorAll('[data-ap-action="clearance-bank"]').forEach(b=>b.classList.toggle('selected',b===button));return}
    if(action==='back-payment'){if(changed&&!confirm('الرجوع دون حفظ بيانات صرف الشيك؟'))return;changed=false;delete model.clearance;return renderPayment()}
    if(action==='post-clearance')return run(async()=>{const p=await api('supplier-payments/'+model.data.id+'/clear','POST',{revision:model.data.revision,event_revision:model.data.adjustment_revision||0,...model.clearance});model.data=normalizePayment(p);delete model.clearance;changed=false;renderPayment();await updated();toast('تم تسجيل صرف الشيك من البنك')});
  }
  async function refreshCurrent(){if(!dialog?.open||changed||working)return;if(model?.type==='payment'&&model.data?.id)return openPayment(model.data.id,true);if(model?.type==='payment-list')return payments(model.supplierId)}
  return {payment,payments,returns,returnList,aging,statement,openPayment,openReturn,refreshCurrent};
})();
