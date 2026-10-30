'use strict';
/* One reviewed opening position. Each source keeps its own journal and original document. */
window.OpeningBalances = (() => {
  let dialog, lineDialog, model = null, editor = null, dirty = false, lineDirty = false, busy = false, serial = 0, opener;
  const rows = value => Array.isArray(value) ? value : [];
  const q = selector => dialog.querySelector(selector);
  const lq = selector => lineDialog.querySelector(selector);
  const labels = {general:'رصيد حساب عام',supplier_invoice:'فواتير موردين مستحقة',supplier_credit:'أرصدة دائنة لصالح المنشأة لدى مورد',supplier_advance:'دفعات مقدمة للموردين',custody:'عهد تشغيل لدى موظفين',withholding:'خصم ضريبي سابق مستحق',issued_cheque:'شيكات صادرة قبل الافتتاح'};
  const hints = {general:'أدخل الرصيد الفعلي للحساب، وحدد جانبه من ميزان المراجعة.',supplier_invoice:'الرصيد المتبقي من فاتورة سابقة؛ يظهر في حساب المورد وأعمار الديون، دون تسجيل مشتريات جديدة.',supplier_credit:'رصيد مدين للمورد مثل إشعار دائن أو مرتجع سابق، قابل للتسوية مع مستحقاته.',supplier_advance:'مقدم سابق للمورد، دون إنشاء حركة نقدية جديدة.',custody:'عهدة تشغيل ما زالت لدى صاحبها في يوم الافتتاح؛ لا تنشئ صرفًا نقديًا جديدًا.',withholding:'التزام خصم ضريبي سابق يرحّل للتوريد، دون إنشاء سداد أو شهادة خصم جديدة.',issued_cheque:'صافي شيك سابق مستحق للمستفيد؛ لا يسدد فاتورة ولا يحتسب خصمًا ضريبيًا جديدًا.'};
  const fixedSide = {supplier_invoice:'credit',supplier_credit:'debit',supplier_advance:'debit',custody:'debit',withholding:'credit',issued_cheque:'credit'};
  const statusLabel = status => ({draft:'مسودة افتتاح',posted:'افتتاح معتمد ومرحّل',cancelled:'مسودة ملغاة'})[status] || status;
  const amount = cents => `<bdi class="ob-money">${money(Number(cents || 0))}</bdi>`;
  const button = (label, action, attrs = '', primary = false) => `<button type="button" class="${primary ? 'primary' : 'secondary'}" data-opening-action="${action}" ${attrs}>${esc(label)}</button>`;
  const note = (message, tone = '') => `<p class="ob-note ${tone}">${esc(message)}</p>`;
  const uuid = () => crypto.randomUUID();
  const codeOf = account => account?.code || account?.account_code || '';
  const nameOf = account => account?.name || account?.account_name || '';
  const accountText = account => account ? codeOf(account) + ' • ' + nameOf(account) : 'غير مضبوط في الربط المحاسبي';
  function ensure() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.id = 'opening-window'; dialog.dir = 'rtl'; dialog.setAttribute('aria-labelledby', 'opening-title');
    dialog.innerHTML = '<header class="ob-header"><span class="ob-heading-art"></span><div><small>OPENING FINANCIAL POSITION</small><h2 id="opening-title"></h2><p id="opening-subtitle"></p></div><button type="button" class="ob-close" data-opening-action="close" aria-label="إغلاق الأرصدة الافتتاحية">×</button></header><div class="ob-scroll"><p id="opening-error" class="error" role="alert"></p><div id="opening-body"></div></div><footer class="ob-footer" id="opening-footer"></footer>';
    document.body.appendChild(dialog);
    q('.ob-heading-art').innerHTML = illustration('settings');
    lineDialog = document.createElement('dialog');
    lineDialog.id = 'opening-line-window'; lineDialog.dir = 'rtl'; lineDialog.setAttribute('aria-labelledby','opening-line-title');
    document.body.appendChild(lineDialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    lineDialog.addEventListener('cancel', event => { event.preventDefault(); closeLine(); });
    for (const window of [dialog,lineDialog]) window.addEventListener('click', event => {
      const control = event.target.closest('[data-opening-action],[data-opening-kind],[data-opening-side]');
      if (!control || control.disabled) return;
      if (control.dataset.openingKind) { chooseKind(control.dataset.openingKind); return; }
      if (control.dataset.openingSide) { editor.data.side = control.dataset.openingSide; lineDirty = true; renderLine(); return; }
      dispatch(control).catch(error);
    });
    dialog.addEventListener('input', event => {
      const name = event.target.dataset.openingField;
      if (!name || !model) return;
      if (name === 'search' && model.view === 'list') { model.search = event.target.value; renderListRows(); return; }
      if (editable()) { model.data[name] = event.target.value; changed(); }
    });
    lineDialog.addEventListener('input', event => {
      const name = event.target.dataset.openingLineField;
      if (!name || !editor || busy) return;
      if (name.startsWith('extra.')) editor.data.extra[name.slice(6)] = event.target.value;
      else editor.data[name] = event.target.value;
      lineDirty = true;
      lq('#opening-line-error').textContent = '';
    });
  }
  function error(err) {
    const message = err.message || String(err);
    if (lineDialog?.open) lq('#opening-line-error').textContent = message;
    else if (dialog?.open) q('#opening-error').textContent = message;
    else toast(message);
  }
  function show() { ensure(); if (!dialog.open) { opener = document.activeElement; dialog.showModal(); } }
  function discard() { return !busy && (!dirty || confirm('توجد تعديلات لم تحفظ في الأرصدة الافتتاحية. هل تريد تركها؟')); }
  function close() {
    if (lineDialog?.open && !closeLine()) return;
    if (!discard()) return;
    ++serial; model = null; dirty = false;
    if (dialog?.open) dialog.close();
    if (opener?.isConnected) opener.focus();
  }
  function closeLine() {
    if (busy || (lineDirty && !confirm('توجد تعديلات لم تضف إلى الكشف. هل تريد تركها؟'))) return false;
    editor = null; lineDirty = false;
    if (lineDialog.open) lineDialog.close();
    return true;
  }
  function title(text, subtitle = '') { q('#opening-title').textContent = text; q('#opening-subtitle').textContent = subtitle; q('#opening-error').textContent = ''; }
  function display(body, footer) { q('#opening-body').innerHTML = body; q('#opening-footer').innerHTML = footer || button('إغلاق','close'); setBusy(); }
  async function load(loader) {
    ensure();
    if (lineDialog.open && !closeLine()) return;
    if (!discard()) return;
    const request = ++serial; model = null; dirty = false; show();
    title('الأرصدة الافتتاحية للنظام', 'تجهيز موقف البداية المحاسبي…');
    display('<div class="ob-empty" role="status">جاري تحميل الأرصدة والإعدادات…</div>', button('إغلاق','close'));
    try { const next = await loader(); if (request !== serial || !dialog.open) return; model = next; render(); }
    catch (err) { if (request === serial && dialog.open) { display('<div class="ob-empty">تعذر تحميل البيانات. يمكنك إعادة المحاولة دون تغيير الأرصدة المحفوظة.</div>',button('إعادة المحاولة','list') + button('إغلاق','close')); error(err); } }
  }
  async function open() {
    return load(async () => {
      const [config, documents] = await Promise.all([api('opening-config'),api('opening-balances')]);
      return {view:'list',config,documents:rows(documents.batches || documents),search:''};
    });
  }
  async function newBatch() {
    return load(async () => {
      const [config,documents] = await Promise.all([api('opening-config'),api('opening-balances')]);
      if (config.posted_batch) return {view:'form',config,data:normalize(await api('opening-balances/' + config.posted_batch.id)),preview:null};
      const draft = rows(documents.batches || documents).find(document => document.status === 'draft');
      if (draft) return {view:'form',config,data:normalize(await api('opening-balances/' + draft.id)),preview:null};
      return {view:'form',config,preview:null,data:{request_key:uuid(),status:'draft',business_date:'',reference:'',notes:'',lines:[]}};
    });
  }
  async function openBatch(id) {
    return load(async () => {
      const [config,document] = await Promise.all([api('opening-config'),api('opening-balances/' + Number(id))]);
      return {view:'form',config,preview:null,data:normalize(document)};
    });
  }
  function normalize(document) { return {...document,lines:rows(document.lines).map(line => ({...line,extra:{...(line.extra || {})},amount_cents:Number(line.amount_cents)}))}; }
  function editable() { return model?.view === 'form' && model.data.status === 'draft' && !model.config.posted_batch; }
  function changed() { dirty = true; model.preview = null; const preview = q('#opening-preview'); if (preview) preview.innerHTML = note('تغيرت البيانات. حدّث المعاينة ثم احفظ المسودة قبل الاعتماد أو الطباعة.'); computed(); pending(); }
  function setBusy() {
    for (const window of [dialog,lineDialog]) if (window) window.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = busy || control.dataset.openingLocked === 'true'; });
    if (dialog) dialog.setAttribute('aria-busy',String(busy));
    pending();
  }
  function pending() {
    if (model?.view !== 'form') return;
    const plan = model.preview || model.data.preview;
    const post = q('[data-opening-action="post"]');
    if (post) post.disabled = busy || dirty || !model.data.id || !editable() || !plan?.can_approve;
    const print = q('[data-opening-action="print"]');
    if (print) print.disabled = busy || dirty || !model.data.id;
    const pending = q('[data-opening-pending]'); if (pending) pending.hidden = !dirty;
  }
  function total(lines = model?.data?.lines) {
    return rows(lines).reduce((result,line) => { const cents = Number(line.amount_cents || 0); result[line.side === 'credit' ? 'credit_cents' : 'debit_cents'] += cents; result.difference_cents = result.debit_cents - result.credit_cents; return result; },{debit_cents:0,credit_cents:0,difference_cents:0});
  }
  function computed() { if (model?.view !== 'form') return; const value = total(); for (const key of ['debit_cents','credit_cents','difference_cents']) { const node = q('[data-opening-total="'+key+'"]'); if (node) node.innerHTML = amount(value[key]); } }
  function render() { if (model.view === 'list') renderList(); else if (model.view === 'journal') renderJournal(); else renderForm(); }
  function renderList() {
    title('الأرصدة الافتتاحية للنظام', 'موقف بداية واحد • الجنيه المصري • السنة المالية من يناير إلى ديسمبر');
    const posted = model.config.posted_batch;
    const draft = model.documents.find(document => document.status === 'draft');
    const start = posted ? button('فتح الافتتاح المعتمد','open',`data-id="${Number(posted.id)}"`,true) : draft ? button('استكمال مسودة الافتتاح','open',`data-id="${Number(draft.id)}"`,true) : button('كشف افتتاح جديد','new','',true);
    display(`<section class="ob-intro"><div><small>نيو بانوراما رمسيس</small><h3>ابدأ بأرصدة فعلية موثقة</h3><p>أدخل ميزان البداية وكل الأرصدة التفصيلية من الإعدادات. تحفظ المسودة حتى يكتمل التعادل، ثم ينشئ الاعتماد القيود الافتتاحية ومصادرها تلقائيًا.</p></div>${start}</section>${posted ? note('تم اعتماد افتتاح النظام بتاريخ '+posted.business_date+'؛ الأرصدة الأصلية محفوظة للعرض والطباعة، ولن ينشأ افتتاح ابتدائي آخر.','success') : note('أدخل جميع الأصول والالتزامات وحقوق الملكية الفعلية. فرق الكشف يظل ظاهرًا إلى أن يكتمل؛ لا ينشأ رأس مال أو رصيد موازن تلقائي.','warning')}<section class="ob-section"><div class="ob-section-head"><h3>كشوف الافتتاح المحفوظة</h3><label class="ob-search">بحث بالكود أو المرجع<input type="search" data-opening-field="search" value="${esc(model.search)}" placeholder="اكتب للبحث…"></label></div><div id="opening-list-rows"></div></section>`,button('تحديث القائمة','list') + '<span class="ob-spacer"></span>' + button('إغلاق','close'));
    renderListRows();
  }
  function renderListRows() {
    const term = (model.search || '').trim().toLocaleLowerCase();
    const documents = model.documents.filter(document => !term || [document.code,document.reference,document.business_date,statusLabel(document.status)].some(value => String(value || '').toLocaleLowerCase().includes(term)));
    q('#opening-list-rows').innerHTML = `<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>الكشف / التاريخ</th><th>المرجع</th><th>مدين</th><th>دائن</th><th>فرق الكشف</th><th>الحالة</th><th></th></tr></thead><tbody>${documents.map(document => `<tr><td><bdi>${esc(document.code)}</bdi><small>${esc(document.business_date)}</small></td><td>${esc(document.reference)}</td><td>${amount(document.debit_cents ?? document.totals?.debit_cents)}</td><td>${amount(document.credit_cents ?? document.totals?.credit_cents)}</td><td>${amount(document.totals?.difference_cents ?? Number(document.debit_cents || 0)-Number(document.credit_cents || 0))}</td><td><span class="ob-status ${esc(document.status)}">${esc(statusLabel(document.status))}</span></td><td>${button('فتح الكشف','open',`data-id="${Number(document.id)}"`)}</td></tr>`).join('') || '<tr><td colspan="7"><div class="ob-empty">لا توجد كشوف ضمن البحث الحالي.</div></td></tr>'}</tbody></table></div>`;
  }
  function field(name,label,value = '',type = 'text',required = false,extra = '') { return `<label>${esc(label)}${required ? ' *' : ''}<input type="${type}" data-opening-field="${name}" value="${esc(value || '')}" ${required ? 'required' : ''} ${extra}></label>`; }
  function totalsMarkup(value) { return `<div class="ob-totals"><article><small>إجمالي مدين</small><strong data-opening-total="debit_cents">${amount(value.debit_cents)}</strong><span>EGP</span></article><article><small>إجمالي دائن</small><strong data-opening-total="credit_cents">${amount(value.credit_cents)}</strong><span>EGP</span></article><article class="${value.difference_cents ? 'ob-difference' : 'ob-balanced'}"><small>فرق الكشف — يجب أن يساوي صفرًا</small><strong data-opening-total="difference_cents">${amount(value.difference_cents)}</strong><span>تسمح المسودة بالفرق لحين استكمال الأرصدة</span></article></div>`; }
  function renderForm() {
    const d = model.data, locked = !editable(), value = total(), bound = model.config.earliest_financial_date || model.config.baseline_date;
    title('كشف الأرصدة الافتتاحية', (d.code || 'رقم الكشف عند الحفظ') + ' • ' + statusLabel(d.status));
    const dateHelp = bound ? 'آخر تاريخ مسموح للافتتاح: '+bound+'، وفق أقدم قيد مالي مسجل.' : 'اختر تاريخ بداية الأرصدة الفعلية وفق ميزان المراجعة المعتمد.';
    display(`${d.status === 'posted' ? note('هذه أرصدة بداية النظام الأصلية بعد الاعتماد. القيود والمصادر التفصيلية ثابتة؛ الحركات التالية تظهر في تقارير كل وحدة.','success') : model.config.posted_batch ? note('يوجد افتتاح معتمد للنظام؛ لا يمكن اعتماد هذه المسودة أو تعديلها. افتح الكشف المعتمد من القائمة.','warning') : ''}<section class="ob-section"><div class="ob-section-head"><h3>بيانات موقف البداية</h3><span class="ob-status ${esc(d.status)}">${esc(statusLabel(d.status))}</span></div><div class="ob-grid">${field('business_date','تاريخ الأرصدة الافتتاحية',d.business_date,'date',true,bound ? `max="${esc(bound)}"` : '')}${field('reference','مرجع ميزان البداية',d.reference,'text',true,'maxlength="100"')}<div class="ob-fixed"><small>العملة الأساسية</small><strong>الجنيه المصري • EGP</strong></div><label class="ob-wide">ملاحظات المراجعة<textarea data-opening-field="notes" rows="2" maxlength="2000">${esc(d.notes || '')}</textarea></label></div>${note(dateHelp)}</section>${totalsMarkup(value)}<section class="ob-section"><div class="ob-section-head"><div><h3>الأرصدة الفعلية وتفاصيلها</h3><p>كل سطر يمثل حسابًا أو مصدرًا تفصيليًا مستقلًا، وله قيد افتتاحي عند الاعتماد.</p></div>${locked ? '' : button('إضافة رصيد','line-new','',true)}</div><div class="ob-table-wrap">${linesTable(d.lines,false,locked)}</div></section><p class="ob-pending" data-opening-pending hidden>توجد تعديلات لم تحفظ. احفظها قبل الاعتماد أو الطباعة.</p>${locked ? '' : mappingsMarkup()}<section class="ob-section"><div class="ob-section-head"><h3>${d.status === 'posted' ? 'القيود الافتتاحية المنشأة' : 'مراجعة قابلية الاعتماد'}</h3>${locked ? '' : button('تحديث المعاينة','preview')}</div><div id="opening-preview">${previewMarkup(model.preview || d.preview,d)}</div>${d.status === 'posted' ? journalsMarkup(d.journals) : ''}</section>${note('الحساب الوسيط للافتتاح يجمع الطرف المقابل للقيود التفصيلية ويكون صافي رصيده صفرًا عند اعتماد الكشف المتعادل. لا يمثل رأس مال تلقائيًا.')}<p class="ob-brand">A&amp;M — Accounting Hotel for Audit &amp;Control</p>`,(locked ? '' : button('معاينة الافتتاح','preview') + button('حفظ المسودة','save','',true) + (d.id ? button('اعتماد وترحيل الافتتاح','post','',true) + button('إلغاء المسودة','cancel') : '')) + (d.id ? button('طباعة الكشف','print') : '') + button('تحديث الإعدادات','refresh-config') + '<span class="ob-spacer"></span>' + button('قائمة الكشوف','list') + button('إغلاق','close'));
    if (locked) q('#opening-body').querySelectorAll('input,textarea').forEach(control => { control.disabled = true; control.dataset.openingLocked = 'true'; });
    computed(); pending();
  }
  function lineDescription(line) {
    const party = line.supplier_name || line.custodian_name || '';
    const extra = line.extra || {};
    return [party, line.kind === 'issued_cheque' ? [extra.bank_name,extra.cheque_number,extra.beneficiary].filter(Boolean).join(' • ') : '',line.reference].filter(Boolean).join(' • ');
  }
  function linesTable(lines,printing = false,locked = true) {
    return `<table class="ob-table ob-lines"><thead><tr><th># / نوع الرصيد</th><th>الحساب / الطرف / المستند</th><th>المصدر / الاستحقاق</th><th>مدين</th><th>دائن</th>${printing ? '' : '<th>المستند</th>'}</tr></thead><tbody>${rows(lines).map((line,index) => `<tr data-opening-row="${index}"><td><small>${index+1}</small>${esc(labels[line.kind] || line.kind)}</td><td><bdi>${esc(line.account_code || codeOf(accountFor(line)))}</bdi> ${esc(line.account_name || nameOf(accountFor(line)))}<small>${esc(lineDescription(line))}</small>${line.remaining_net_cents != null || line.extra?.remaining_net_cents != null ? `<small>صافي الوعاء المتبقي: ${amount(line.remaining_net_cents ?? line.extra.remaining_net_cents)}</small>` : ''}</td><td><bdi>${esc(line.source_date || '—')}</bdi>${line.due_date ? `<small>الاستحقاق: <bdi>${esc(line.due_date)}</bdi></small>` : ''}</td><td>${line.side === 'debit' ? amount(line.amount_cents) : '—'}</td><td>${line.side === 'credit' ? amount(line.amount_cents) : '—'}</td>${printing ? '' : `<td><div class="ob-row-actions">${locked ? (line.journal_id ? button('القيد','journal',`data-id="${Number(line.journal_id)}"`) : '') + sourceButton(line,index) : button('تعديل','line-edit',`data-index="${index}"`) + button('حذف','line-remove',`data-index="${index}"`)}</div></td>`}</tr>`).join('') || `<tr><td colspan="${printing ? 5 : 6}"><div class="ob-empty">أضف الأرصدة الفعلية من ميزان البداية والمستندات الأصلية.</div></td></tr>`}</tbody></table>`;
  }
  function accountFor(line) { return line.kind === 'general' ? rows(model?.config?.accounts).find(account => Number(account.id) === Number(line.account_id)) : model?.config?.kind_accounts?.[line.kind]; }
  function sourceButton(line,index) { return line.document_id && ['supplier_invoice','supplier_credit','custody','issued_cheque'].includes(line.kind) ? button('المصدر','open-source',`data-index="${index}"`) : ''; }
  function relevantMappings() {
    const roles = {supplier_invoice:'payable_control',supplier_credit:'payable_control',supplier_advance:'supplier_advances',custody:'operating_custodies',withholding:'withholding_payable',issued_cheque:'issued_cheques'};
    const used = new Set(rows(model.data.lines).map(line => roles[line.kind]).filter(Boolean));
    const clearing = model.config.clearing_mapping;
    const entries = rows(model.config.mappings).filter(mapping => used.has(mapping.role) || mapping.role === 'opening_clearing');
    if (clearing && !entries.some(mapping => Number(mapping.id) === Number(clearing.id))) entries.unshift(clearing);
    return entries;
  }
  function mappingsMarkup() {
    return `<section class="ob-section"><div class="ob-section-head"><div><h3>مراجعة الربط المحاسبي</h3><p>راجع كود الحساب واسمه ثم أكد الربط المقترح صراحة. يتطلب الاعتماد ربطًا مؤكدًا لكل مصدر مستخدم.</p></div></div><div class="ob-mappings">${relevantMappings().map(mapping => `<article data-opening-mapping="${Number(mapping.id)}"><div><strong>${esc(mapping.label || (mapping.role === 'opening_clearing' ? 'الحساب الوسيط للافتتاح' : mapping.role) || 'الحساب الوسيط للافتتاح')}</strong><span><bdi>${esc(mapping.account_code)}</bdi> • ${esc(mapping.account_name)}</span><small>${esc(mapping.module || 'الإعدادات')} • ${mapping.status === 'confirmed' ? 'ربط مؤكد' : 'ربط مقترح يحتاج مراجعة'}</small></div>${mapping.status === 'confirmed' ? '<span class="ob-status posted">تمت المراجعة</span>' : button('راجعت وأؤكد هذا الربط','confirm-mapping',`data-id="${Number(mapping.id)}"`)}</article>`).join('') || note('لم يتوفر الربط المحاسبي. حدّث الإعدادات أو راجع شاشة ربط الحسابات.','warning')}</div></section>`;
  }
  function previewMarkup(plan,d) {
    if (d.status === 'posted') return note('تم الترحيل بتاريخ '+(d.posted_at || d.business_date)+' • صافي الحساب الوسيط '+money(d.clearing_balance_cents || 0)+' EGP.','success');
    if (d.preview_error && !plan) return note(d.preview_error,'warning');
    if (!plan) return note('أكمل تاريخ الافتتاح والمرجع والأرصدة، ثم عاين الكشف. حفظ المسودة لا ينشئ قيودًا مالية.');
    return note(plan.can_approve ? 'الكشف متعادل والربط المطلوب مؤكد. احفظ المسودة ثم راجعها قبل اعتمادها.' : 'المسودة محفوظة للمراجعة؛ أكمل المتطلبات التالية قبل اعتماد الافتتاح.',plan.can_approve ? 'success' : 'warning') + rows(plan.blockers).map(item => note(item.message || String(item),'warning')).join('') + rows(plan.warnings).map(item => note(item.message || String(item))).join('') + `<div class="ob-clearing"><span>الحساب الوسيط</span><strong><bdi>${esc(plan.clearing_account_code || '')}</bdi> ${esc(plan.clearing_account_name || '')}</strong><span>صافي الرصيد ${amount(plan.clearing_balance_cents ?? -Number(plan.totals?.difference_cents || 0))} EGP</span></div>`;
  }
  function digits(value) { return String(value ?? '').trim().replace(/[٠-٩]/g,digit => String(digit.charCodeAt(0)-1632)).replace(/[۰-۹]/g,digit => String(digit.charCodeAt(0)-1776)).replace(/٫/g,'.'); }
  function cents(value,zero = false) {
    const text = digits(value);
    if (!/^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$/.test(text)) throw Error('أدخل المبلغ دون فواصل آلاف وبحد أقصى منزلتين عشريتين.');
    const [whole,decimal=''] = text.split('.'), parsed = Number(whole)*100 + Number(decimal.padEnd(2,'0'));
    if (!Number.isSafeInteger(parsed) || parsed > 10000000000000 || parsed < (zero ? 0 : 1)) throw Error('راجع المبلغ؛ يجب أن يكون موجبًا وضمن النطاق المسموح.');
    return parsed;
  }
  function decimal(cents) { return Math.floor(Number(cents || 0)/100)+'.'+String(Number(cents || 0)%100).padStart(2,'0'); }
  function date(value,label) { if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '') || value.startsWith('0000')) throw Error('حدد '+label+' صحيحًا.'); const d = new Date(value+'T00:00:00Z'); if (Number.isNaN(d.getTime()) || d.toISOString().slice(0,10) !== value) throw Error('حدد '+label+' صحيحًا.'); }
  function payload() {
    const d = model.data;
    date(d.business_date,'تاريخ الافتتاح');
    const bound = model.config.earliest_financial_date || model.config.baseline_date;
    if (bound && d.business_date > bound) throw Error('تاريخ الافتتاح لا يلي أقدم قيد مالي مسجل: '+bound);
    if (!String(d.reference || '').trim()) throw Error('أدخل مرجع ميزان البداية.');
    if (!d.lines.length) throw Error('أضف رصيدًا فعليًا واحدًا على الأقل.');
    const sums = total();
    if (![sums.debit_cents,sums.credit_cents].every(Number.isSafeInteger)) throw Error('إجمالي الكشف خارج النطاق المسموح.');
    return {request_key:d.request_key || (d.request_key = uuid()),revision:d.revision,business_date:d.business_date,reference:d.reference.trim(),notes:d.notes || '',lines:d.lines.map(line => {
      const value = {kind:line.kind,account_id:Number(line.account_id),side:line.side,amount_cents:line.amount_cents,reference:line.reference || '',source_date:line.source_date || null,due_date:line.due_date || null,extra:{...(line.extra || {})}};
      for (const key of ['supplier_id','custodian_id','department_id']) if (line[key]) value[key] = Number(line[key]);
      if (line.remaining_net_cents != null) value.remaining_net_cents = line.remaining_net_cents;
      return value;
    })};
  }
  function blankLine(kind = 'general') { return {kind,account_id:null,side:fixedSide[kind] || 'debit',amount:'',supplier_id:null,custodian_id:null,department_id:null,reference:'',source_date:'',due_date:'',remaining_net_amount:'',extra:{}}; }
  function openLine(index = null) {
    if (!editable()) return;
    const saved = index === null ? null : model.data.lines[index];
    editor = {index,data:saved ? {...saved,extra:{...(saved.extra || {})},amount:decimal(saved.amount_cents),remaining_net_amount:saved.remaining_net_cents != null || saved.extra?.remaining_net_cents != null ? decimal(saved.remaining_net_cents ?? saved.extra.remaining_net_cents) : ''} : blankLine()};
    lineDirty = false; renderLine(); if (!lineDialog.open) lineDialog.showModal();
  }
  function chooseKind(kind) {
    if (!editor || busy || editor.data.kind === kind) return;
    if (lineDirty && !confirm('تغيير نوع الرصيد سيعيد حقول هذا السطر. هل تريد المتابعة؟')) return;
    editor.data = blankLine(kind); lineDirty = true; renderLine();
  }
  function lineField(name,label,value='',type='text',required=false,extra='') { return `<label>${esc(label)}${required ? ' *' : ''}<input type="${type}" data-opening-line-field="${name}" value="${esc(value ?? '')}" ${required ? 'required' : ''} ${extra}></label>`; }
  function choice(name,label,items,optional=false) {
    const selected = rows(items).find(item => Number(item.id) === Number(name.startsWith('extra.') ? editor.data.extra[name.slice(6)] : editor.data[name]));
    return `<div class="ob-choice"><span>${esc(label)}${optional ? ' — اختياري' : ' *'}</span>${button(selected ? accountText(selected) : 'اختر '+label,'pick',`data-opening-pick="${name}"`)}${optional && selected ? button('مسح الاختيار','clear-pick',`data-opening-pick="${name}"`) : ''}</div>`;
  }
  function renderLine() {
    const d = editor.data, kind = d.kind, config = model.config, general = kind === 'general', supplier = ['supplier_invoice','supplier_credit','supplier_advance','withholding','issued_cheque'].includes(kind), account = accountFor(d), cheque = kind === 'issued_cheque';
    const kinds = rows(config.kinds).length ? config.kinds : Object.keys(labels).map(id => ({id,label:labels[id]}));
    const sourceRequired = ['supplier_invoice','withholding','issued_cheque'].includes(kind);
    lineDialog.innerHTML = `<header class="ob-header"><div><small>OPENING SOURCE</small><h2 id="opening-line-title">${editor.index === null ? 'إضافة رصيد افتتاحي' : 'تعديل رصيد افتتاحي'}</h2><p>حدد نوع الرصيد ثم أدخل بياناته الفعلية.</p></div><button type="button" class="ob-close" data-opening-action="line-close" aria-label="إغلاق سطر الرصيد">×</button></header><div class="ob-scroll"><p id="opening-line-error" class="error" role="alert"></p><div class="ob-kinds" role="group" aria-label="نوع الرصيد">${kinds.map(item => `<button type="button" data-opening-kind="${esc(item.id)}" aria-pressed="${item.id === kind}" class="${item.id === kind ? 'is-active' : ''}">${esc(labels[item.id] || item.label)}</button>`).join('')}</div>${note(hints[kind] || '')}<div class="ob-grid ob-line-grid">${general ? choice('account_id','الحساب العام',config.general_accounts) : `<div class="ob-fixed ob-wide"><small>الحساب المحدد تلقائيًا من الربط</small><strong><bdi>${esc(accountText(account))}</bdi></strong></div>`}${general ? `<div class="ob-choice"><span>جانب الرصيد *</span><div class="ob-segments" role="group" aria-label="جانب الرصيد"><button type="button" data-opening-side="debit" aria-pressed="${d.side === 'debit'}">مدين</button><button type="button" data-opening-side="credit" aria-pressed="${d.side === 'credit'}">دائن</button></div></div>` : `<div class="ob-fixed"><small>جانب الرصيد</small><strong>${fixedSide[kind] === 'debit' ? 'مدين' : 'دائن'} — يحدد تلقائيًا</strong></div>`}${lineField('amount',cheque ? 'صافي مبلغ الشيك (EGP)' : 'مبلغ الرصيد (EGP)',d.amount,'text',true,'inputmode="decimal" autocomplete="off" maxlength="16"')}${supplier ? choice('supplier_id','المورد',config.suppliers,cheque) : ''}${kind === 'custody' ? choice('custodian_id','صاحب العهدة',rows(config.custodians).filter(person => person.active !== false && person.active !== 0 && person.active !== '0')) : ''}${general ? choice('department_id','القسم / مركز التكلفة',typeof state !== 'undefined' ? state.departments : [],true) : ''}${lineField('reference',general ? 'مرجع الرصيد — اختياري' : cheque ? 'مرجع مستند الشيك' : 'مرجع المستند الأصلي',d.reference,'text',!general,'maxlength="100"')}${!general ? lineField('source_date',cheque ? 'تاريخ إصدار الشيك الأصلي' : kind === 'withholding' ? 'تاريخ الخصم الضريبي السابق' : kind === 'supplier_invoice' ? 'تاريخ الفاتورة الأصلي' : 'تاريخ المستند الأصلي',d.source_date,'date',sourceRequired,model.data.business_date ? `max="${esc(model.data.business_date)}"` : '') : ''}${['supplier_invoice','issued_cheque'].includes(kind) ? lineField('due_date','تاريخ الاستحقاق الأصلي',d.due_date,'date',true) : ''}${kind === 'supplier_invoice' ? lineField('remaining_net_amount','صافي الوعاء المتبقي للخصم عند السداد — اختياري',d.remaining_net_amount,'text',false,'inputmode="decimal" maxlength="16"') : ''}${cheque ? choice('extra.bank_account_id','الحساب البنكي المسحوب عليه',config.bank_accounts) + lineField('extra.cheque_number','رقم الشيك',d.extra.cheque_number,'text',true,'maxlength="100"') + lineField('extra.beneficiary','اسم المستفيد',d.extra.beneficiary,'text',true,'maxlength="200"') + lineField('extra.bank_name','اسم البنك',d.extra.bank_name,'text',true,'maxlength="150"') : ''}</div>${kind === 'supplier_invoice' ? note('الصافي المتبقي لا يستنتج من إجمالي الفاتورة أو الضريبة. أدخله من مستنداتك إذا كان سيطبق خصم ضريبي على سدادها لاحقًا.') : ''}${!general && !sourceRequired ? note('تاريخ المصدر اختياري لهذا النوع؛ إدخال تاريخ المستند الأصلي يحافظ على وضوح المراجعة.') : ''}${supplier && !rows(config.suppliers).length ? note('لا يوجد موردون للاختيار. أضف المورد من ملف الموردين، ثم حدّث الإعدادات مع الاحتفاظ بالمسودة.','warning') : ''}${kind === 'custody' && !rows(config.custodians).length ? note('أضف صاحب العهدة من الخزائن والعهد، ثم حدّث الإعدادات.','warning') : ''}</div><footer class="ob-footer">${button('إضافة إلى الكشف','line-save','',true)}<span class="ob-spacer"></span>${button('إغلاق','line-close')}</footer>`;
    setBusy();
  }
  function pick(name) {
    if (!editor || busy) return;
    const config = model.config;
    const collection = name === 'account_id' ? config.general_accounts : name === 'supplier_id' ? config.suppliers : name === 'custodian_id' ? rows(config.custodians).filter(person => person.active !== false && person.active !== 0 && person.active !== '0') : name === 'extra.bank_account_id' ? config.bank_accounts : typeof state !== 'undefined' ? state.departments : [];
    const token = editor;
    picker('اختيار '+({account_id:'حساب عام',supplier_id:'المورد',custodian_id:'صاحب العهدة',department_id:'القسم / مركز التكلفة','extra.bank_account_id':'الحساب البنكي'})[name],rows(collection).map(item => ({id:item.id,label:accountText(item),detail:item.bank_name || item.cost_code || ''})),selected => {
      if (!editor || editor !== token || !lineDialog.open) return;
      const id = selected.id;
      if (name.startsWith('extra.')) { editor.data.extra[name.slice(6)] = Number(id); if (name === 'extra.bank_account_id') { const account = rows(collection).find(item => Number(item.id) === Number(id)); if (account?.bank_name) editor.data.extra.bank_name = account.bank_name; } }
      else editor.data[name] = Number(id);
      lineDirty = true; renderLine();
    });
  }
  function saveLine() {
    const d = editor.data, general = d.kind === 'general', cheque = d.kind === 'issued_cheque', account = accountFor(d);
    const value = {kind:d.kind,account_id:Number(account?.id || d.account_id),account_code:codeOf(account),account_name:nameOf(account),side:fixedSide[d.kind] || d.side,amount_cents:cents(d.amount),reference:String(d.reference || '').trim(),source_date:d.source_date || null,due_date:d.due_date || null,extra:{...(d.extra || {})}};
    if (!value.account_id) throw Error(general ? 'اختر حسابًا عامًا نشطًا قابلًا للقيد.' : 'لم يضبط حساب هذا النوع في الربط المحاسبي.');
    if (!general && !value.reference) throw Error('أدخل مرجع المستند الأصلي لهذا الرصيد.');
    if (['supplier_invoice','supplier_credit','supplier_advance','withholding'].includes(d.kind) && !d.supplier_id) throw Error('اختر المورد لهذا الرصيد.');
    if (d.kind === 'custody' && !d.custodian_id) throw Error('اختر صاحب العهدة.');
    if (['supplier_invoice','withholding','issued_cheque'].includes(d.kind) && !d.source_date) throw Error(d.kind === 'supplier_invoice' ? 'أدخل تاريخ الفاتورة الأصلي الفعلي لحساب أعمار المديونية.' : 'أدخل تاريخ المصدر الأصلي الفعلي.');
    if (d.source_date) { date(d.source_date,'تاريخ المصدر'); if (model.data.business_date && d.source_date > model.data.business_date) throw Error('تاريخ المصدر الأصلي لا يلي تاريخ الأرصدة الافتتاحية.'); }
    if (['supplier_invoice','issued_cheque'].includes(d.kind)) { date(d.due_date,'تاريخ الاستحقاق'); if (cheque && d.due_date < d.source_date) throw Error('استحقاق الشيك لا يسبق تاريخ إصداره.'); }
    for (const key of ['supplier_id','custodian_id','department_id']) if (d[key]) value[key] = Number(d[key]);
    const supplier = rows(model.config.suppliers).find(row => Number(row.id) === Number(d.supplier_id)), custodian = rows(model.config.custodians).find(row => Number(row.id) === Number(d.custodian_id));
    if (supplier) { value.supplier_name = supplier.name; value.supplier_code = supplier.code; }
    if (custodian) { value.custodian_name = custodian.name; value.custodian_code = custodian.code; }
    delete value.extra.remaining_net_cents; delete value.extra.remaining_net_amount;
    if (d.kind === 'supplier_invoice' && String(d.remaining_net_amount || '').trim()) { value.remaining_net_cents = cents(d.remaining_net_amount,true); if (value.remaining_net_cents > value.amount_cents) throw Error('صافي الوعاء المتبقي لا يتجاوز إجمالي رصيد الفاتورة.'); }
    if (cheque) {
      value.extra.bank_account_id = Number(d.extra.bank_account_id);
      if (!value.extra.bank_account_id || !String(d.extra.cheque_number || '').trim() || !String(d.extra.beneficiary || '').trim() || !String(d.extra.bank_name || '').trim()) throw Error('أكمل الحساب البنكي ورقم الشيك واسم المستفيد والبنك.');
      for (const key of ['cheque_number','beneficiary','bank_name']) value.extra[key] = String(value.extra[key]).trim();
    }
    if (editor.index === null) model.data.lines.push(value); else model.data.lines[editor.index] = value;
    lineDirty = false; closeLine(); changed(); renderForm();
  }
  async function mutate(task) {
    if (busy) return; busy = true; setBusy();
    try { await task(); } finally { busy = false; setBusy(); }
  }
  function previewPayload() { return {...payload(),...(model.data.id ? {batch_id:model.data.id} : {})}; }
  async function preview() { const plan = await api('opening-preview','POST',previewPayload()); model.preview = plan; renderForm(); return plan; }
  async function save() {
    const data = payload(), saved = await api('opening-balances'+(model.data.id ? '/'+model.data.id : ''),model.data.id ? 'PUT' : 'POST',data);
    model.data = normalize(saved); model.preview = null; dirty = false; renderForm(); toast('تم حفظ مسودة الأرصدة الافتتاحية دون ترحيل مالي');
  }
  async function post() {
    if (dirty || !model.data.id) throw Error('احفظ المسودة قبل اعتمادها.');
    const plan = await api('opening-preview','POST',previewPayload()); model.preview = plan; renderForm();
    if (!plan.can_approve) throw Error('الكشف غير جاهز للاعتماد. راجع التعادل ومتطلبات الربط الظاهرة.');
    if (!confirm('اعتماد كشف '+model.data.code+' بتاريخ '+model.data.business_date+'؟ سيُنشأ افتتاح النظام بقيود متعادلة ومصادر تفصيلية ثابتة، ولن يمكن تعديل هذا الكشف أو إنشاء افتتاح ابتدائي آخر.')) return;
    const saved = await api('opening-balances/'+model.data.id+'/post','POST',{revision:model.data.revision});
    model.data = normalize(saved); model.preview = null; dirty = false;
    model.config.posted_batch = {id:saved.id,code:saved.code,business_date:saved.business_date};
    renderForm(); toast('تم اعتماد الأرصدة وإنشاء القيود الافتتاحية والمصادر التفصيلية');
  }
  async function cancel() {
    if (!model.data.id || dirty) throw Error('احفظ تعديلات المسودة أو أعد فتح نسختها المحفوظة قبل الإلغاء.');
    if (!confirm('إلغاء مسودة الأرصدة الافتتاحية '+model.data.code+'؟')) return;
    model.data = normalize(await api('opening-balances/'+model.data.id,'PUT',{action:'cancel',revision:model.data.revision})); model.preview = null; dirty = false; renderForm();
  }
  async function refreshConfig() { const config = await api('opening-config'); model.config = config; model.preview = null; renderForm(); toast('تم تحديث الإعدادات مع الاحتفاظ ببيانات الكشف'); }
  async function confirmMapping(id) {
    const mapping = relevantMappings().find(item => Number(item.id) === Number(id));
    if (!mapping || mapping.status === 'confirmed') return;
    if (!confirm('تأكيد مراجعة ربط '+(mapping.label || 'الحساب الوسيط للافتتاح')+' بالحساب '+mapping.account_code+' — '+mapping.account_name+'؟ لا يرحّل هذا الإجراء أي أرصدة.')) return;
    await api('account-mappings/'+Number(id),'PUT',{revision:mapping.revision,status:'confirmed'});
    await refreshConfig();
  }
  function journalTable(journal) { return `<div class="ob-table-wrap"><table class="ob-table"><thead><tr><th>الحساب</th><th>البيان</th><th>مدين</th><th>دائن</th></tr></thead><tbody>${rows(journal.lines).map(line => `<tr><td><bdi>${esc(line.account_code || line.code || '')}</bdi> ${esc(line.account_name || line.name || '')}</td><td>${esc(line.description || line.memo || '')}</td><td>${amount(line.debit_cents)}</td><td>${amount(line.credit_cents)}</td></tr>`).join('')}</tbody></table></div>`; }
  function journalsMarkup(journals) { return `<div class="ob-journals">${rows(journals).map(journal => button((journal.code || 'قيد '+journal.id)+' • '+(journal.entry_date || journal.business_date || model.data.business_date),'journal',`data-id="${Number(journal.id)}"`)).join('')}</div>`; }
  function openJournal(id) {
    const journal = rows(model.data.journals).find(row => Number(row.id) === Number(id));
    if (!journal) throw Error('القيد غير متاح في بيانات الكشف. أعد فتح الكشف لقراءته.');
    model = {view:'journal',journal,back:model}; render();
  }
  function renderJournal() { title('القيد الافتتاحي',model.journal.code || ''); display(`<section class="ob-section"><h3><bdi>${esc(model.journal.code)}</bdi></h3>${note('تاريخ القيد: '+(model.journal.entry_date || model.back.data.business_date))}${journalTable(model.journal)}</section>`,button('العودة إلى الكشف','back')+button('طباعة القيد','print-journal')+'<span class="ob-spacer"></span>'+button('إغلاق','close')); }
  async function source(index) {
    const line = model.data.lines[index]; if (!line?.document_id) return;
    if (!discard()) return;
    if (['supplier_invoice','supplier_credit'].includes(line.kind) && typeof openInvoice === 'function') { close(); return openInvoice(Number(line.document_id)); }
    if (line.kind === 'custody' && window.Treasury?.openCustody) { close(); return Treasury.openCustody(Number(line.document_id)); }
    if (line.kind === 'issued_cheque' && window.Banking?.openCheque) { close(); return Banking.openCheque(Number(line.document_id)); }
    if (line.journal_id) return openJournal(line.journal_id);
  }
  const signatures = () => '<div class="ob-signatures"><span>توقيع المُعد: ................</span><span>توقيع المراجع: ................</span><span>توقيع المعتمد: ................</span></div>';
  const printHead = (label,code) => `<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>${esc(label)}</h2><p><bdi>${esc(code || '')}</bdi></p>`;
  function printBatch() {
    if (dirty || !model.data.id) throw Error('احفظ الكشف قبل طباعة نسخة مطابقة للأصل المحفوظ.');
    const d = model.data;
    document.querySelector('#print-document').innerHTML = printHead('كشف الأرصدة الافتتاحية',d.code)+`<dl class="ob-print-details"><dt>تاريخ الافتتاح</dt><dd>${esc(d.business_date)}</dd><dt>مرجع ميزان البداية</dt><dd>${esc(d.reference)}</dd><dt>الحالة</dt><dd>${esc(statusLabel(d.status))}</dd><dt>العملة</dt><dd>EGP — الجنيه المصري</dd>${d.posted_at ? `<dt>وقت الاعتماد</dt><dd>${esc(d.posted_at)}</dd>` : ''}</dl>`+(d.status === 'posted' ? '' : note('مسودة للمراجعة — لم ترحّل أي أرصدة افتتاحية.','warning'))+totalsMarkup(total())+linesTable(d.lines,true,true)+(d.notes ? note('ملاحظات: '+d.notes) : '')+note('الحساب الوسيط للافتتاح: '+(d.clearing_account_code || model.config.clearing_mapping?.account_code || '')+' • '+(d.clearing_account_name || model.config.clearing_mapping?.account_name || '')+' • صافي '+money(d.clearing_balance_cents ?? -total().difference_cents)+' EGP')+rows(d.journals).map(journal => `<section class="ob-print-journal"><h3>القيد <bdi>${esc(journal.code)}</bdi></h3>${journalTable(journal)}</section>`).join('')+signatures();
    window.print();
  }
  async function dispatch(control) {
    const action = control.dataset.openingAction;
    if (action === 'close') return close(); if (action === 'line-close') return closeLine(); if (busy) return;
    if (action === 'list') return open(); if (action === 'new') return newBatch(); if (action === 'open') return openBatch(control.dataset.id);
    if (action === 'line-new') return openLine(); if (action === 'line-edit') return openLine(Number(control.dataset.index));
    if (action === 'line-remove') { if (!editable()) return; if (!confirm('حذف هذا الرصيد من مسودة الافتتاح؟')) return; model.data.lines.splice(Number(control.dataset.index),1); changed(); return renderForm(); }
    if (action === 'line-save') return saveLine(); if (action === 'pick') return pick(control.dataset.openingPick);
    if (action === 'clear-pick') { editor.data[control.dataset.openingPick] = null; lineDirty = true; return renderLine(); }
    if (action === 'preview') return mutate(preview); if (action === 'save') return mutate(save); if (action === 'post') return mutate(post); if (action === 'cancel') return mutate(cancel);
    if (action === 'confirm-mapping') return mutate(() => confirmMapping(control.dataset.id)); if (action === 'refresh-config') return mutate(refreshConfig);
    if (action === 'print') return printBatch(); if (action === 'journal') return openJournal(control.dataset.id); if (action === 'open-source') return source(Number(control.dataset.index));
    if (action === 'back') { model = model.back; return render(); }
    if (action === 'print-journal') { document.querySelector('#print-document').innerHTML = printHead('قيد افتتاحي',model.journal.code)+journalTable(model.journal)+signatures(); return window.print(); }
  }
  document.addEventListener('click',event => {
    const control = event.target.closest('[data-opening-balances]');
    if (control && !control.disabled) open().catch(error);
  });
  return {open,newBatch,openBatch};
})();
