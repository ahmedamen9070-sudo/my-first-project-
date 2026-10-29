'use strict';
/* Physical counts are evidence documents. They never post a cash adjustment. */
window.CashCounts = (() => {
  let dialog, model = null, dirty = false, busy = false, request = 0, opener = null;
  const rows = value => Array.isArray(value) ? value : [];
  const q = selector => dialog.querySelector(selector);
  const stateLabel = value => ({draft:'مسودة محضر',confirmed:'محضر معتمد',cancelled:'مسودة ملغاة',voided:'محضر ملغى بمستند مستقل'})[value] || value;
  const status = document => document.effective_status || document.status || 'draft';
  const amount = value => `<bdi class="cc-money">${money(Number(value || 0))}</bdi>`;
  const button = (label, action, attrs = '', primary = false) => `<button type="button" class="${primary ? 'primary' : 'secondary'}" data-cash-count-action="${action}" ${attrs}>${esc(label)}</button>`;
  const note = (text, tone = '') => `<p class="cc-note ${tone}">${esc(text)}</p>`;
  const field = (name, label, value = '', type = 'text', required = true, wide = false) => `<label class="${wide ? 'cc-wide' : ''}">${esc(label)}${required ? ' *' : ''}<input type="${type}" data-cash-count-field="${name}" value="${esc(value)}" ${required ? 'required' : ''} maxlength="${name === 'reference' ? 100 : 150}"></label>`;
  const defaultDenominations = [20000,10000,5000,2000,1000,500,100,50,25];
  const nonPostingNote = 'محضر الجرد يوثق العدّ والمقارنة فقط، ولا ينشئ قيدًا أو تسوية نقدية. اعتماد فروق الجرد ماليًا يحتاج مراجعة أرصدة البداية الفعلية المؤجلة.';
  function ensure() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.id = 'cash-count-window';
    dialog.dir = 'rtl';
    dialog.setAttribute('aria-labelledby', 'cash-count-title');
    dialog.innerHTML = '<header class="cc-header"><span class="cc-heading-art"></span><div><small>PHYSICAL CASH COUNT</small><h2 id="cash-count-title"></h2><p id="cash-count-subtitle"></p></div><button type="button" class="cc-close" data-cash-count-action="close" aria-label="إغلاق الجرد">×</button></header><div class="cc-scroll"><p id="cash-count-error" class="error" role="alert"></p><div id="cash-count-body"></div></div><footer class="cc-footer" id="cash-count-footer"></footer>';
    document.body.appendChild(dialog);
    q('.cc-heading-art').innerHTML = illustration('wallet');
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener('input', input);
    dialog.addEventListener('click', event => {
      const control = event.target.closest('[data-cash-count-action]');
      if (control && !control.disabled) dispatch(control).catch(error);
    });
  }
  function discard() { return !busy && (!dirty || confirm('توجد تعديلات لم تحفظ في محضر الجرد. هل تريد تركها؟')); }
  function close() {
    if (!discard()) return;
    ++request; model = null; dirty = false;
    if (dialog?.open) dialog.close();
    if (opener?.isConnected) opener.focus();
  }
  function error(err) {
    ensure();
    q('#cash-count-error').textContent = err.message || String(err);
    if (!dialog.open) toast(err.message || String(err));
  }
  function show() {
    ensure();
    if (!dialog.open) { opener = document.activeElement; dialog.showModal(); }
  }
  function title(label, subtitle = '') {
    q('#cash-count-title').textContent = label;
    q('#cash-count-subtitle').textContent = subtitle;
    q('#cash-count-error').textContent = '';
  }
  function display(body, footer) {
    q('#cash-count-body').innerHTML = body;
    q('#cash-count-footer').innerHTML = footer || button('إغلاق', 'close');
    setBusy();
  }
  function setBusy() {
    if (!dialog) return;
    dialog.querySelectorAll('button,input,textarea').forEach(element => {
      if (element.dataset.cashCountLocked !== 'true') element.disabled = busy;
    });
    pending();
  }
  async function load(loader) {
    if (!discard()) return;
    const seq = ++request;
    dirty = false; model = null; show();
    title('الجرد الفعلي للخزينة', 'قراءة المحاضر والبيانات المحفوظة…');
    display('<div class="cc-loading" role="status">جاري تجهيز الجرد…</div>', button('إغلاق', 'close'));
    try {
      const next = await loader();
      if (seq !== request || !dialog.open) return;
      model = next; render();
    } catch (err) {
      if (seq === request && dialog.open) {
        display('<div class="cc-empty">تعذر تحميل المحضر. راجع الرسالة ثم أعد المحاولة.</div>', button('قائمة الجرد', 'list') + button('إغلاق', 'close'));
        error(err);
      }
    }
  }
  async function open() {
    return load(async () => ({view:'list', documents:rows(await api('cash-counts')), search:''}));
  }
  async function newDocument(preset = {}) {
    return load(async () => {
      const config = await api('cash-count-config');
      const denominations = rows(config.denominations_cents).length ? config.denominations_cents : defaultDenominations;
      return {view:'form', config, preview:null, data:{request_key:crypto.randomUUID(), status:'draft', currency:'EGP', business_date:today(), reference:'', cash_account_id:null, counted_by:'', reviewed_by:'', notes:'', lines:denominations.map(value => ({denomination_cents:Number(value), quantity:0})), ...preset}};
    });
  }
  async function openDocument(id) {
    return load(async () => {
      const [config, document] = await Promise.all([api('cash-count-config'), api('cash-counts/' + Number(id))]);
      return {view:'form', config, preview:null, data:normalize(document)};
    });
  }
  function normalize(document) { return {...document, lines:rows(document.lines).map(line => ({...line, denomination_cents:Number(line.denomination_cents), quantity:Number(line.quantity)}))}; }
  function editable() { return model?.view === 'form' && model.data.status === 'draft'; }
  function changed() {
    dirty = true;
    if (model) model.preview = null;
    const preview = q('#cash-count-preview');
    if (preview) preview.innerHTML = note('حدّث معاينة المقارنة بعد تعديل البيانات، ثم احفظ المسودة قبل الاعتماد أو الطباعة.');
    computed(); pending();
  }
  function input(event) {
    const element = event.target, name = element.dataset.cashCountField;
    if (!model) return;
    if (name === 'search' && model.view === 'list') { model.search = element.value; renderListRows(); return; }
    if (name && (editable() || model.view === 'void')) {
      if (name === 'custom_denomination') return;
      model.data[name] = element.value; changed();
    }
    if (element.dataset.cashCountQuantity !== undefined && editable()) {
      const line = model.data.lines.find(row => row.denomination_cents === Number(element.dataset.cashCountQuantity));
      if (line) { line.quantity = element.value; changed(); }
    }
  }
  function digits(value) { return String(value ?? '').trim().replace(/[٠-٩]/g, digit => String(digit.charCodeAt(0) - 1632)).replace(/[۰-۹]/g, digit => String(digit.charCodeAt(0) - 1776)).replace(/٫/g, '.'); }
  function quantity(value) {
    const text = digits(value);
    if (!text) return 0;
    if (!/^[0-9]+$/.test(text) || !Number.isSafeInteger(Number(text)) || Number(text) > 1000000) throw Error('عدد القطع عدد صحيح من صفر إلى مليون، دون كسور.');
    return Number(text);
  }
  function denomination(value) {
    const text = digits(value);
    if (!/^(0|[1-9][0-9]{0,9})(\.[0-9]{1,2})?$/.test(text)) throw Error('أدخل قيمة فئة موجبة بالجنيه، بحد أقصى منزلتين عشريتين.');
    const [whole, decimal = ''] = text.split('.');
    const cents = Number(whole) * 100 + Number(decimal.padEnd(2, '0'));
    if (!Number.isSafeInteger(cents) || cents <= 0 || cents > 10000000000000) throw Error('قيمة الفئة خارج النطاق المسموح.');
    return cents;
  }
  function total() {
    return rows(model?.data?.lines).reduce((sum, line) => sum + Number(line.denomination_cents) * quantity(line.quantity), 0);
  }
  function computed() {
    if (model?.view !== 'form') return;
    try {
      const value = total();
      const actual = q('[data-cash-count-actual]');
      if (actual) actual.innerHTML = amount(value);
      for (const line of model.data.lines) {
        const cell = q(`[data-cash-count-line-total="${line.denomination_cents}"]`);
        if (cell) cell.innerHTML = amount(line.denomination_cents * quantity(line.quantity));
      }
    } catch (err) {
      const actual = q('[data-cash-count-actual]');
      if (actual) actual.textContent = 'راجع عدد القطع';
    }
  }
  function pending() {
    if (model?.view !== 'form') return;
    for (const action of ['confirm', 'print']) {
      const control = q(`[data-cash-count-action="${action}"]`);
      if (control) control.disabled = busy || dirty || (action === 'confirm' && (model.data.book_changed || rows(model.data.live_exceptions).length > 0));
    }
    const pendingNote = q('[data-cash-count-pending]');
    if (pendingNote) pendingNote.hidden = !dirty;
  }
  function render() {
    if (model.view === 'list') renderList();
    else if (model.view === 'form') renderForm();
    else if (model.view === 'void') renderVoid();
  }
  function renderList() {
    title('محاضر الجرد الفعلي', 'عدّ النقد حسب الفئات • مقارنة موثقة دون قيد تسوية تلقائي');
    display(`<div class="cc-list-intro"><div>${illustration('wallet')}<h3>نقدية فعلية ومحضر واضح</h3><p>كل محضر يحتفظ بالعدّ والرصيد الدفتري وفرق المقارنة وقت حفظه واعتماده.</p></div>${button('محضر جرد جديد', 'new', '', true)}</div>${note(nonPostingNote)}<section class="cc-section"><div class="cc-section-head"><h3>المحاضر المحفوظة</h3><label class="cc-search"><span>بحث بالكود أو الخزينة أو المرجع</span><input type="search" data-cash-count-field="search" value="${esc(model.search)}" placeholder="اكتب للبحث…"></label><span data-cash-count-list-count></span></div><div id="cash-count-list-rows"></div></section>`, button('محضر جديد', 'new', '', true) + button('تحديث القائمة', 'refresh') + '<span class="cc-spacer"></span>' + button('إغلاق', 'close'));
    renderListRows();
  }
  function renderListRows() {
    if (model?.view !== 'list') return;
    const term = model.search.trim().toLocaleLowerCase();
    const documents = model.documents.filter(document => !term || [document.code, document.reference, document.cash_account_code, document.cash_account_name, document.counted_by, document.reviewed_by, stateLabel(status(document))].some(value => String(value || '').toLocaleLowerCase().includes(term)));
    q('[data-cash-count-list-count]').textContent = `${documents.length} من ${model.documents.length} محضر`;
    q('#cash-count-list-rows').innerHTML = `<div class="cc-table-wrap"><table class="cc-table"><thead><tr><th>المحضر / التاريخ</th><th>الخزينة / المرجع</th><th>النقد المعدود</th><th>الرصيد المحفوظ</th><th>فرق المقارنة</th><th>الحالة</th><th></th></tr></thead><tbody>${documents.map(document => `<tr data-cash-count-row="${Number(document.id)}"><td><bdi>${esc(document.code)}</bdi><small>${esc(document.business_date)}</small></td><td>${esc(document.cash_account_name)}<small><bdi>${esc(document.cash_account_code)}</bdi> • ${esc(document.reference)}</small></td><td>${amount(document.actual_cents)}</td><td>${amount(document.book_cents)}</td><td class="${Number(document.variance_cents) === 0 ? '' : 'cc-difference'}">${amount(document.variance_cents)}</td><td><span class="cc-status ${esc(status(document))}">${esc(stateLabel(status(document)))}</span>${document.book_changed ? '<small>قيود لاحقة تحتاج مراجعة</small>' : ''}</td><td>${button('فتح المحضر', 'open', `data-id="${Number(document.id)}"`)}</td></tr>`).join('') || '<tr><td colspan="7"><div class="cc-empty">لا توجد محاضر ضمن البحث الحالي.</div></td></tr>'}</tbody></table></div>`;
  }
  function selectedAccount() { return rows(model.config?.cash_accounts).find(account => account.id === Number(model.data.cash_account_id)); }
  function accountChoice() {
    const document = model.data, account = selectedAccount();
    const name = account ? account.code + ' • ' + account.name : document.cash_account_name ? (document.cash_account_code || '') + ' • ' + document.cash_account_name : 'اختيار الخزينة';
    return `<div class="cc-choice"><span>الخزينة الفعلية *</span>${button(name, 'cash-account')}</div>`;
  }
  function denominationTable(lines, printing = false, locked = false) {
    return `<table class="cc-table cc-denominations"><thead><tr><th>الفئة بالجنيه</th><th>عدد القطع</th><th>الإجمالي</th>${printing || locked ? '' : '<th></th>'}</tr></thead><tbody>${rows(lines).map(line => `<tr><td>${amount(line.denomination_cents)}<small>EGP</small></td><td>${printing ? esc(line.quantity) : `<input class="cc-quantity" type="number" min="0" max="1000000" step="1" inputmode="numeric" aria-label="عدد فئة ${esc((line.denomination_cents / 100).toFixed(2))}" data-cash-count-quantity="${Number(line.denomination_cents)}" value="${esc(line.quantity)}" ${locked ? 'disabled data-cash-count-locked="true"' : ''}>`}</td><td data-cash-count-line-total="${Number(line.denomination_cents)}">${amount(line.line_total_cents ?? line.denomination_cents * Number(line.quantity || 0))}</td>${printing || locked ? '' : `<td>${button('حذف الفئة', 'remove-denomination', `data-denomination-cents="${Number(line.denomination_cents)}"`)}</td>`}</tr>`).join('')}</tbody></table>`;
  }
  function comparison(plan, saved = false) {
    if (!plan) return note('أدخل العدّ وبيانات المسؤولين، ثم عاين الرصيد الدفتري وفرق المقارنة.');
    const actual = Number(plan.actual_cents || 0), book = Number(plan.book_cents || 0), variance = Number(plan.variance_cents || 0);
    return `<div class="cc-comparison"><article><small>النقد المعدود</small><strong>${amount(actual)}</strong></article><article><small>${saved ? 'الرصيد الدفتري المحفوظ' : 'الرصيد الدفتري في المعاينة'}</small><strong data-cash-count-book>${amount(book)}</strong></article><article class="${variance === 0 ? 'cc-balanced' : 'cc-difference'}"><small>فرق مقارنة للمراجعة</small><strong data-cash-count-variance>${amount(variance)}</strong><span>${variance === 0 ? 'العدّ يساوي الرصيد المسجل' : 'يحتاج مراجعة مستندات وأرصدة البداية'}</span></article></div>${saved && plan.captured_at ? note('لقطة الرصيد المحفوظة: ' + plan.captured_at) : ''}${rows(plan.exceptions).length ? '<div class="cc-attention"><b>قيود تحتاج مراجعة قبل اعتماد المحضر</b>' + rows(plan.exceptions).map(item => note(item.message || item.type || String(item), 'warning')).join('') + '</div>' : ''}${rows(plan.warnings).length ? '<details class="cc-method"><summary>أساس المقارنة وبيانات الفئات</summary>' + rows(plan.warnings).map(item => note(typeof item === 'string' ? item : item.message)).join('') + '</details>' : ''}`;
  }
  function liveComparison(document) {
    if (!document.id) return '';
    const issues = rows(document.live_exceptions).length ? '<section class="cc-attention"><b>استثناءات في القيود المتاحة حاليًا</b>' + rows(document.live_exceptions).map(item => note((item.message || item.type) + (item.journal_code ? ' • ' + item.journal_code : ''), 'warning')).join('') + '</section>' : '';
    if (!document.book_changed) return issues;
    return `<section class="cc-live" data-cash-count-book-changed><b>تغيرت قيود الخزينة بعد لقطة هذا المحضر</b><p>الأرقام الأصلية أعلاه محفوظة. هذه قراءة محدثة للقيود المؤرخة حتى يوم الجرد للمراجعة، وقد تتغير حتى مع تعادل صافي الحركة.</p><div><span>الرصيد الدفتري المحدث ${amount(document.live_book_cents)}</span><span>المقارنة المحدثة ${amount(document.live_variance_cents)}</span></div>${document.status === 'draft' ? '<p>أعد معاينة المسودة وحفظها قبل اعتماد المحضر.</p>' : ''}</section>` + issues;
  }
  function renderForm() {
    const document = model.data, locked = document.status !== 'draft';
    title('محضر الجرد الفعلي للخزينة', (document.code || 'رقم المحضر عند أول حفظ') + ' • ' + stateLabel(status(document)));
    const comparisonData = model.preview || (document.id ? document : null);
    display(`<div class="cc-form-grid"><section class="cc-section"><div class="cc-section-head"><h3>بيانات الجرد</h3><span class="cc-status ${esc(status(document))}">${esc(stateLabel(status(document)))}</span></div><div class="cc-grid">${field('business_date', 'يوم الجرد', document.business_date, 'date')}${field('reference', 'مرجع المحضر', document.reference)}${accountChoice()}<div class="cc-fixed"><small>العملة</small><strong>الجنيه المصري • EGP</strong></div>${field('counted_by', 'مسؤول العدّ', document.counted_by)}${field('reviewed_by', 'مسؤول المراجعة', document.reviewed_by)}<label class="cc-wide">ملاحظات اختيارية<textarea rows="2" maxlength="2000" data-cash-count-field="notes">${esc(document.notes || '')}</textarea></label></div></section><section class="cc-total-card"><small>النقد الموجود حسب الفئات</small><strong data-cash-count-actual>${amount(document.actual_cents ?? 0)}</strong><span>EGP</span><p>الإجمالي يحسب من قيمة الفئة × عدد القطع.</p>${note('المحضر دليل عدّ ومقارنة؛ تأكيده لا يرحّل قيدًا ولا يغير رصيد الخزينة.')}</section></div><section class="cc-section"><div class="cc-section-head"><div><h3>عدّ الأوراق والعملات</h3><p>أدخل عدد القطع لكل قيمة نقدية، وأضف قيمة فئة أخرى عند الحاجة.</p></div>${locked ? '' : `<div class="cc-custom">${field('custom_denomination', 'إضافة قيمة فئة', '', 'text', false)}${button('إضافة الفئة', 'add-denomination')}</div>`}</div><div class="cc-table-wrap">${denominationTable(document.lines, false, locked)}</div></section><p class="cc-pending" data-cash-count-pending hidden>توجد تعديلات لم تحفظ؛ احفظ المسودة قبل اعتمادها أو طباعتها.</p><section class="cc-section"><div class="cc-section-head"><h3>${document.id && !model.preview ? 'المقارنة المحفوظة' : 'معاينة المقارنة'}</h3><span>حتى يوم الجرد، حسب القيود المتاحة وقت قراءة اللقطة</span></div><div id="cash-count-preview">${comparison(comparisonData, !!document.id && !model.preview)}</div></section>${liveComparison(document)}${document.confirmed_at ? note('اعتماد المحضر: ' + document.confirmed_at + ' • اعتماد عدّ ومقارنة دون قيد مالي.') : ''}${document.void ? voidDetails(document.void) : ''}${note(nonPostingNote, 'warning')}`, (locked ? '' : button('معاينة المقارنة', 'preview') + button('حفظ المسودة', 'save', '', true) + (document.id ? button('اعتماد محضر الجرد', 'confirm', '', true) + button('إلغاء المسودة', 'cancel') : '')) + (document.id ? button('طباعة المحضر', 'print') + button('تحديث حالة المحضر', 'refresh') : '') + (document.status === 'confirmed' && !document.void ? button('إلغاء المحضر المعتمد', 'void') : '') + '<span class="cc-spacer"></span>' + button('قائمة الجرد', 'list') + button('إغلاق', 'close'));
    if (locked) dialog.querySelectorAll('#cash-count-body input,#cash-count-body textarea,[data-cash-count-action="cash-account"]').forEach(element => { element.disabled = true; element.dataset.cashCountLocked = 'true'; });
    computed(); pending();
  }
  function voidDetails(value) { return `<section class="cc-section cc-void"><h3>إلغاء محضر معتمد</h3><p><bdi>${esc(value.code || '')}</bdi> • ${esc(value.business_date)}</p><p>المرجع: ${esc(value.reference)}</p><p>السبب: ${esc(value.reason)}</p>${note('الأصل وبيانات العدّ محفوظة. إلغاء المحضر لا يعكس حركة خزينة أو ينشئ قيدًا.')}</section>`; }
  function validateDate(value) {
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value || '') || value.startsWith('0000')) throw Error('حدد يوم جرد صحيحًا.');
    const date = new Date(value + 'T00:00:00Z');
    if (Number.isNaN(date.getTime()) || date.toISOString().slice(0,10) !== value) throw Error('حدد يوم جرد صحيحًا.');
  }
  function payload() {
    const document = model.data;
    validateDate(document.business_date);
    if (!document.cash_account_id) throw Error('اختر الخزينة الفعلية المطلوب جردها.');
    for (const name of ['reference', 'counted_by', 'reviewed_by']) if (!String(document[name] || '').trim()) throw Error('أكمل مرجع المحضر ومسؤول العدّ ومسؤول المراجعة.');
    if (!document.lines.length || document.lines.length > 20) throw Error('محضر الجرد يحتاج من فئة إلى ٢٠ فئة مختلفة.');
    const lines = document.lines.map(line => ({denomination_cents:Number(line.denomination_cents), quantity:quantity(line.quantity)}));
    if (lines.reduce((sum, line) => sum + line.denomination_cents * line.quantity, 0) > 10000000000000) throw Error('إجمالي النقد يتجاوز النطاق المسموح.');
    return {request_key:document.request_key, revision:document.revision, currency:'EGP', business_date:document.business_date, reference:String(document.reference).trim(), cash_account_id:Number(document.cash_account_id), counted_by:String(document.counted_by).trim(), reviewed_by:String(document.reviewed_by).trim(), notes:document.notes || '', lines};
  }
  async function preview() {
    const plan = await api('cash-count-preview', 'POST', payload());
    model.preview = plan;
    if (model.data.id) Object.assign(model.data, {
      live_book_cents:plan.book_cents,
      live_variance_cents:plan.variance_cents,
      live_evidence:plan.evidence,
      live_exceptions:plan.exceptions,
      book_changed:plan.evidence_fingerprint !== model.data.evidence_fingerprint,
    });
    renderForm();
    return plan;
  }
  async function save() {
    const plan = await preview(), data = payload();
    const document = await api('cash-counts' + (model.data.id ? '/' + model.data.id : ''), model.data.id ? 'PUT' : 'POST', data);
    model = {...model, data:normalize(document), preview:null}; dirty = false; renderForm();
    toast('تم حفظ مسودة محضر الجرد دون قيد مالي');
    return plan;
  }
  async function confirmDocument() {
    if (dirty) throw Error('احفظ تعديلات المسودة قبل اعتماد المحضر.');
    const document = await api('cash-counts/' + model.data.id + '/confirm', 'POST', {revision:model.data.revision});
    model.data = normalize(document); model.preview = null; dirty = false; renderForm();
    toast('تم اعتماد محضر الجرد؛ لم ينشأ قيد تسوية');
  }
  async function cancel() {
    if (!confirm('إلغاء مسودة محضر الجرد المحفوظة؟')) return;
    const document = await api('cash-counts/' + model.data.id, 'PUT', {revision:model.data.revision, action:'cancel'});
    model.data = normalize(document); model.preview = null; dirty = false; renderForm();
  }
  function selectCash() {
    const choices = rows(model.config.cash_accounts).map(account => ({id:account.id, label:account.code + ' • ' + account.name, detail:'خزينة نقدية بالجنيه المصري'}));
    picker('اختيار خزينة الجرد', choices, account => { model.data.cash_account_id = account.id; changed(); renderForm(); });
  }
  function addDenomination() {
    const value = denomination(q('[data-cash-count-field="custom_denomination"]').value);
    if (model.data.lines.some(line => line.denomination_cents === value)) throw Error('الفئة موجودة بالفعل؛ عدّل عدد القطع في صفها.');
    if (model.data.lines.length >= 20) throw Error('الحد الأقصى ٢٠ فئة مختلفة للمحضر.');
    model.data.lines.push({denomination_cents:value, quantity:0});
    model.data.lines.sort((a,b) => b.denomination_cents - a.denomination_cents);
    changed(); renderForm();
  }
  function startVoid() {
    if (!discard()) return;
    model = {view:'void', original:model, data:{request_key:crypto.randomUUID(), business_date:today(), reference:'', reason:''}};
    dirty = false; renderVoid();
  }
  function renderVoid() {
    const document = model.original.data, value = model.data;
    title('إلغاء محضر جرد معتمد', document.code);
    display(note('إلغاء إداري موثق للمحضر؛ تبقى بيانات العدّ والمقارنة الأصلية محفوظة ولا تنشأ حركة خزينة أو تسوية مالية.', 'warning') + `<section class="cc-section"><div class="cc-grid">${field('business_date', 'تاريخ إلغاء المحضر', value.business_date, 'date')}${field('reference', 'مرجع الإلغاء', value.reference)}<label class="cc-wide">سبب الإلغاء *<textarea rows="3" maxlength="2000" data-cash-count-field="reason">${esc(value.reason)}</textarea></label></div></section>`, button('تأكيد إلغاء المحضر', 'confirm-void', '', true) + button('رجوع للمحضر', 'back') + '<span class="cc-spacer"></span>' + button('إغلاق', 'close'));
  }
  async function confirmVoid() {
    const document = model.original.data, value = model.data;
    validateDate(value.business_date);
    if (!value.reference.trim() || !value.reason.trim()) throw Error('أدخل مرجع الإلغاء وسببه.');
    const saved = await api('cash-counts/' + document.id + '/void', 'POST', {...value, revision:document.revision, event_revision:document.adjustment_revision || 0});
    model = {...model.original, data:normalize(saved), preview:null}; dirty = false; renderForm();
    toast('تم حفظ إلغاء المحضر مع بقاء بيانات الأصل');
  }
  const signatures = () => '<div class="cc-signatures"><span>مسؤول العدّ: ................</span><span>مسؤول المراجعة: ................</span><span>أمين الخزينة: ................</span><span>التوقيعات: ................</span></div>';
  function printDocument() {
    if (dirty || !model.data.id) throw Error('احفظ المحضر أولًا قبل الطباعة.');
    const document = model.data;
    $('#print-document').innerHTML = `<h1>نيو بانوراما رمسيس</h1><p dir="ltr">A&amp;M — Accounting Hotel for Audit &amp;Control</p><h2>محضر الجرد الفعلي للخزينة</h2><p><bdi>${esc(document.code)}</bdi> • ${esc(stateLabel(status(document)))}</p><dl class="cc-print-details"><dt>يوم الجرد</dt><dd>${esc(document.business_date)}</dd><dt>الخزينة</dt><dd>${esc(document.cash_account_code)} • ${esc(document.cash_account_name)}</dd><dt>المرجع</dt><dd>${esc(document.reference)}</dd><dt>مسؤول العدّ</dt><dd>${esc(document.counted_by)}</dd><dt>مسؤول المراجعة</dt><dd>${esc(document.reviewed_by)}</dd><dt>وقت اللقطة المحفوظة</dt><dd>${esc(document.captured_at)}</dd>${document.confirmed_at ? `<dt>اعتماد المحضر</dt><dd>${esc(document.confirmed_at)}</dd>` : ''}</dl>${denominationTable(document.lines, true, true)}${comparison(document, true)}${document.book_changed ? note('تغيرت قيود بعد اللقطة المحفوظة. الأرقام المطبوعة للمقارنة الأصلية وقت حفظ المحضر، وتفاصيل القراءة المحدثة متاحة في شاشته.', 'warning') : ''}${document.void ? voidDetails(document.void) : ''}${document.notes ? note('الملاحظات: ' + document.notes) : ''}${note(nonPostingNote)}${signatures()}`;
    $('#print-document').querySelectorAll('details').forEach(section => { section.open = true; });
    window.print();
  }
  async function mutate(operation) {
    if (busy) return;
    busy = true; setBusy();
    try { return await operation(); }
    finally { busy = false; setBusy(); }
  }
  async function dispatch(control) {
    const action = control.dataset.cashCountAction;
    if (action === 'close') return close();
    if (busy) return;
    if (action === 'list') return open();
    if (action === 'new') return newDocument();
    if (action === 'open') return openDocument(Number(control.dataset.id));
    if (action === 'refresh') return model?.view === 'form' ? openDocument(model.data.id) : open();
    if (action === 'cash-account') return selectCash();
    if (action === 'add-denomination') return addDenomination();
    if (action === 'remove-denomination') { model.data.lines = model.data.lines.filter(line => line.denomination_cents !== Number(control.dataset.denominationCents)); changed(); return renderForm(); }
    if (action === 'preview') return mutate(preview);
    if (action === 'save') return mutate(save);
    if (action === 'confirm') return mutate(confirmDocument);
    if (action === 'cancel') return mutate(cancel);
    if (action === 'print') return printDocument();
    if (action === 'void') return startVoid();
    if (action === 'confirm-void') return mutate(confirmVoid);
    if (action === 'back') { if (!discard()) return; model = model.original; dirty = false; return renderForm(); }
  }
  document.addEventListener('click', event => { if (event.target.closest('[data-cash-counts]')) open().catch(error); });
  return {open, newDocument, openDocument};
})();
