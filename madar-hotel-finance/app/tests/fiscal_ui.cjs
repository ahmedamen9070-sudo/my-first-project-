/**
 * Configurable input VAT and monthly posting locks, against a DISPOSABLE database.
 * Start server.py with MADAR_DB pointing to a fresh temporary file and port8105.
 * AM_TEST_URL can select another isolated server. No production data is appropriate.
 */
'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.AM_TEST_URL || 'http://127.0.0.1:8105';
const W = '#fiscal-setup-window';
const A = name => `${W} [data-fiscal-action="${name}"]`;
const F = name => `${W} [data-fiscal-field="${name}"]`;
const AP = '#supplier-ap-window';
const money = cents => new Intl.NumberFormat('ar-EG', {minimumFractionDigits:2, maximumFractionDigits:2}).format(cents / 100);
const stamp = Date.now().toString(36);
let sequence = 0;
const key = prefix => prefix + '-' + stamp + '-' + (++sequence);

async function api(page, route, method='GET', body, expected=200) {
  const result = await page.evaluate(async ({route, method, body}) => {
    const boot = await (await fetch('/api/bootstrap')).json();
    const response = await fetch('/api/' + route, {method,
      headers:{'Content-Type':'application/json', 'X-Madar-CSRF':boot.csrf},
      body:body === undefined ? undefined : JSON.stringify(body)});
    return {status:response.status, body:await response.json()};
  }, {route, method, body});
  assert.equal(result.status, expected, `${method} ${route}: ${JSON.stringify(result.body)}`);
  return result.body;
}
async function clickResponse(page, selector, route, method='POST') {
  const pending = page.waitForResponse(r => new URL(r.url()).pathname === '/api/' + route && r.request().method() === method);
  await page.locator(selector).click();
  const response = await pending;
  const body = await response.json();
  assert.ok(response.ok(), `${method} ${route}: ${response.status()} ${JSON.stringify(body)}`);
  return body;
}
async function connected(page) {await page.waitForSelector('.connection-status[data-state="connected"]');}
async function closeFiscal(page) {await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#fiscal-setup-window').open);}
async function printSaved(page, selector, words) {
  const before = await page.evaluate(()=>window.__fiscalPrints||0);
  await page.locator(selector).click();
  await page.waitForFunction(n=>(window.__fiscalPrints||0)>n,before);
  const printed = await page.locator('#print-document').textContent();
  for (const word of words) assert.ok(printed.includes(word), `Printed document missing ${word}`);
  assert.match(printed,/اعتمده|اعتماد|توقيع|المراجعة|راجعه/,'Printed document has review/signatures');
}
async function invoiceAPI(page, supplier, products, taxCodeId, date, post=true) {
  const data={request_key:key('invoice'),supplier_id:supplier.id,department_id:1,
    reference:key('REF'),business_date:date,due_date:'2026-09-30',kind:'service',notes:'اختبار مالي معزول',
    tax_rate:'0',tax_code_id:taxCodeId,
    lines:products.map(p=>({product_id:p.id,quantity:p.quantity||'1',price:p.price||'1'}))};
  const invoice=await api(page,'invoices','POST',data);
  return post ? api(page,'invoices/'+invoice.id+'/post','POST',{revision:invoice.revision}) : invoice;
}
async function paymentAPI(page, supplier, cash, amount, date, options={}) {
  const data={request_key:key('payment'),supplier_id:supplier.id,payment_date:date,amount,
    method:'cash',funding_account_id:cash.id,allocation_mode:'manual',allow_advance:true,
    details:{received_by:'مستلم بيانات الاختبار'},...options};
  const payment=await api(page,'supplier-payments','POST',data);
  return api(page,'supplier-payments/'+payment.id+'/post','POST',{revision:payment.revision});
}
async function periodPreview(page, month) {
  await page.evaluate(year=>FiscalSetup.periods(year),Number(month.slice(0,4)));
  await page.locator(`${A('period-preview')}[data-month="${month}"]`).click();
  await page.waitForFunction(()=>document.querySelector('#fiscal-title')?.textContent.includes('الفترة')||document.querySelector('#fiscal-body')?.textContent.includes('الإقفال'));
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.__fiscalPrints=0;window.print=()=>{window.__fiscalPrints++}});
  try {
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});await connected(page);
    const boot=await api(page,'bootstrap');const chart=await api(page,'accounting');
    const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6'));
    const cash=chart.accounts.find(a=>a.code==='111002'), bank=chart.accounts.find(a=>a.code==='112001');
    assert.ok(expense&&cash&&bank,'Required fixture accounts exist');
    for(const role of ['payable_control','supplier_advances','issued_cheques','recoverable_input_vat']) {
      const mapping=chart.mappings.find(m=>m.role===role);assert.ok(mapping,role);
      await api(page,'account-mappings/'+mapping.id,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    }
    const supplier=await api(page,'suppliers','POST',{name:'اختبار الضرائب والفترات '+stamp,terms_days:30});
    const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:1,name:'خدمة ضريبية '+stamp,unit:'زيارة',price:'100',expense_account_id:expense.id});
    const zeroProduct=await api(page,'products','POST',{supplier_id:supplier.id,department_id:1,name:'بند صفري للاختبار '+stamp,unit:'وحدة',price:'0',expense_account_id:expense.id});
    await page.reload();await page.waitForSelector('#loading[hidden]',{state:'attached'});

    // Tax registration and percentage are explicit TEST inputs, never an application default.
    await page.evaluate(()=>FiscalSetup.taxes());
    await page.locator(`${A('registration')}[data-status="registered"]`).click();
    await page.locator(F('tax_number')).fill('123456789');
    await clickResponse(page,A('save-tax-settings'),'tax-settings','PUT');
    await page.locator(A('new-tax-code')).click();
    await page.locator(F('name')).fill('VAT TEST14 '+stamp);
    await page.locator(`${A('tax-kind')}[data-kind="vat"]`).click();
    await page.locator(F('rate')).fill('14');
    await page.locator(F('recoverable_percent')).fill('100');
    await page.locator(`${W} [data-fiscal-flag="confirmed"]`).check();
    const code=await clickResponse(page,A('save-tax-code'),'tax-codes');
    assert.ok(code.id,'Saved code identity');
    await closeFiscal(page);

    // The supplier advance and cheque are fixtures for valid closed-period guard cases.
    const advance=await paymentAPI(page,supplier,cash,'100','2026-09-01');
    const cheque=await paymentAPI(page,supplier,bank,'20','2026-09-02',{
      method:'cheque',funding_account_id:bank.id,
      details:{bank_name:'بنك الاختبار',cheque_number:key('CHQ'),cheque_book:'TEST',beneficiary:supplier.name,cheque_date:'2026-09-02',due_date:'2026-09-30'}});

    // Real invoice UI proves NET price, computed tax, saved totals, posting and print.
    await page.locator('[data-page="supplier"]:visible').first().click();
    await page.locator('#supplier [data-new-invoice]').click();
    await page.locator('#vendor').click();await page.locator(`[data-pick="${supplier.id}"]`).click();
    await page.locator('#date').fill('2026-09-10');await page.locator('#due-date').fill('2026-09-30');
    await page.locator('#reference').fill(key('UI-VAT'));
    await page.locator('#add-line').click();await page.locator(`[data-pick="${product.id}"]`).click();
    await page.locator('[data-qty]').fill('2');await page.locator('[data-qty]').dispatchEvent('change');
    await page.locator('#invoice-tax-choice').click();await page.locator(`[data-pick="${code.id}"]`).click();
    assert.equal(await page.locator('#subtotal').textContent(),money(20000));
    assert.equal(await page.locator('#grand').textContent(),money(22800));
    assert.ok((await page.locator('#invoice-tax-summary').textContent()).length>0,'Tax summary visible');
    const draft=await clickResponse(page,'#save','invoices');assert.equal(draft.tax_cents,2800);assert.equal(draft.total_cents,22800);
    const invoice=await clickResponse(page,'#post-invoice',`invoices/${draft.id}/post`);
    assert.equal(invoice.total_cents,22800);assert.ok(await page.locator('#invoice-tax-choice').isDisabled(),'Posted tax choice locked');
    const journal=await api(page,'journals/'+invoice.journal_id);
    assert.equal(journal.debit_cents,22800);assert.equal(journal.credit_cents,22800);
    assert.equal(journal.lines.find(l=>l.account_id===expense.id).debit_cents,20000);
    const vatAccount=chart.mappings.find(m=>m.role==='recoverable_input_vat').account_id;
    assert.equal(journal.lines.find(l=>l.account_id===vatAccount).debit_cents,2800);
    const latest=(await api(page,'bootstrap')).products.find(p=>p.id===product.id);
    assert.equal(latest.last_purchase_price_cents,10000,'Last purchase remains NET, tax never inflates price baseline');
    await printSaved(page,'#print',[invoice.code,money(22800),money(2800)]);

    // Changing today's code cannot retroactively rewrite an approved invoice/return.
    const currentCode=(await api(page,'tax-config')).codes.find(c=>c.id===code.id);
    await api(page,'tax-codes/'+code.id,'PUT',{revision:currentCode.revision,rate:'18'});
    assert.equal((await api(page,'invoices/'+invoice.id)).tax_cents,2800,'Approved invoice retains original14% snapshot');

    // Return original quantity/price and original VAT treatment through real AP UI.
    await page.locator('#return-invoice').click();
    await page.locator(`${AP} [data-ap-field="business_date"]`).fill('2026-09-20');
    await page.locator(`${AP} [data-ap-field="reference"]`).fill(key('VAT-RETURN'));
    await page.locator(`${AP} [data-ap-return-line="${invoice.lines[0].id}"]`).fill('0.5');
    const ret=await clickResponse(page,`${AP} [data-ap-action="save-return"]`,'supplier-returns');
    assert.equal(ret.total_cents,5700,'Return includes original 50EGP NET and7EGP VAT');
    const postedReturn=await clickResponse(page,`${AP} [data-ap-action="post-return"]`,`supplier-returns/${ret.id}/post`);
    assert.equal(postedReturn.total_cents,5700);
    const returnJournal=await api(page,'journals/'+postedReturn.journal_id);
    assert.equal(returnJournal.credit_cents,5700);
    assert.equal(returnJournal.lines.find(l=>l.account_id===vatAccount).credit_cents,700);
    await printSaved(page,`${AP} [data-ap-action="print-return"]`,[postedReturn.code,money(5700)]);
    await page.locator(`${AP} [data-ap-action="close"]`).first().click();await page.locator('#entry-close').click();
    assert.equal((await api(page,`suppliers/${supplier.id}/open-invoices?as_of=2026-09-30`)).find(i=>i.id===invoice.id).outstanding_cents,17100);

    // Partial recovery capitalizes only its non-recoverable part into the original expense.
    const partialCode=await api(page,'tax-codes','POST',{name:'VAT partial TEST '+stamp,kind:'vat',rate:'14',recoverable_percent:'50',active:true,confirmed:true});
    const partial=await invoiceAPI(page,supplier,[{id:product.id,price:'100'}],partialCode.id,'2026-09-04');
    const partialJournal=await api(page,'journals/'+partial.journal_id);
    assert.equal(partial.total_cents,11400);
    assert.equal(partialJournal.lines.find(l=>l.account_id===expense.id).debit_cents,10700,'NET100 +nonrecoverableVAT7 charged to expense');
    assert.equal(partialJournal.lines.find(l=>l.account_id===vatAccount).debit_cents,700,'OnlyhalfVAT is recoverable');

    // A fully zero-valued item return must also respect period locks despite no journal.
    const zeroInvoice=await invoiceAPI(page,supplier,[{id:product.id,price:'1'},{id:zeroProduct.id,price:'0'}],code.id,'2026-09-03');
    const zeroLine=zeroInvoice.lines.find(l=>l.product_id===zeroProduct.id);assert.ok(zeroLine);
    const pending=await invoiceAPI(page,supplier,[{id:product.id,price:'1'}],code.id,'2026-09-21',false);

    // A draft is a visible blocking reason; cancelling it allows a reviewed monthly close.
    await periodPreview(page,'2026-09');
    assert.match(await page.locator('#fiscal-body').textContent(),/مسود/,'Pending draft is identified');
    const blockedClose=page.locator(A('close-period'));
    assert.ok(!await blockedClose.count()||!await blockedClose.isVisible()||await blockedClose.isDisabled(),'Close action blocked until drafts resolved');
    await closeFiscal(page);
    await api(page,'invoices/'+pending.id,'PUT',{revision:pending.revision,action:'cancel'});
    await periodPreview(page,'2026-09');
    await page.locator(A('close-period')).click();
    await page.locator(F('reference')).fill('CLOSE-TEST-'+stamp);
    await page.locator(F('reason')).fill('مراجعة وإقفال شهر الاختبار بعد مطابقة الأرصدة');
    await clickResponse(page,A('save-period-event'),'accounting-periods/2026-09/close');
    await printSaved(page,A('print-period-preview'),['سبتمبر','نيو بانوراما']);
    await closeFiscal(page);

    const beforeCount=(await api(page,'dashboard')).journal_count;
    const closedInvoice={request_key:key('closed-invoice'),supplier_id:supplier.id,department_id:1,reference:key('closed-ref'),business_date:'2026-09-22',due_date:'2026-09-30',tax_rate:'0',tax_code_id:code.id,lines:[{product_id:product.id,quantity:'1',price:'1'}]};
    for(const [route,payload]of [
      ['invoices',closedInvoice],
      ['supplier-payments',{request_key:key('closed-pay'),supplier_id:supplier.id,payment_date:'2026-09-22',amount:'1',method:'cash',funding_account_id:cash.id,allocation_mode:'invoice',target_invoice_id:invoice.id,details:{received_by:'اختبار'}}],
      ['supplier-returns',{request_key:key('closed-zero'),invoice_id:zeroInvoice.id,business_date:'2026-09-22',reference:key('closed-return'),lines:[{invoice_line_id:zeroLine.id,quantity:'1'}]}],
      ['supplier-settlements',{request_key:key('closed-settle'),supplier_id:supplier.id,source_type:'advance',source_id:advance.id,business_date:'2026-09-22',reference:key('closed-settle-ref'),allocations:[{invoice_id:invoice.id,amount:'1'}]}],
      ['supplier-refunds',{request_key:key('closed-refund'),supplier_id:supplier.id,source_type:'advance',source_id:advance.id,business_date:'2026-09-22',reference:key('closed-refund-ref'),reason:'اختبار منع فترة مغلقة',amount:'1',method:'cash',funding_account_id:cash.id,details:{received_by:'أمين خزينة الاختبار'}}],
      [`supplier-payments/${advance.id}/reverse`,{request_key:key('closed-reverse'),revision:advance.revision,event_revision:0,business_date:'2026-09-22',reference:key('closed-reverse-ref'),reason:'اختبار منع العكس'}],
      [`supplier-payments/${cheque.id}/cheque-event`,{request_key:key('closed-stop'),revision:cheque.revision,event_revision:0,action:'stop',business_date:'2026-09-22',reference:key('closed-stop-ref'),reason:'اختبار إجراء بلا قيد'}],
    ]) {
      const failure=await api(page,route,'POST',payload,400);assert.match(failure.error,/مغلق|مقفل|إقفال|الفترة/,'Closed-period reason is explicit');
    }
    assert.equal((await api(page,'dashboard')).journal_count,beforeCount,'Blocked writes leave ledger unchanged');
    await api(page,'invoices/'+invoice.id);await api(page,'supplier-aging?as_of=2026-09-30');await api(page,'supplier-reconciliation?as_of=2026-09-30');
    const replay=await api(page,`invoices/${invoice.id}/post`,'POST',{revision:invoice.revision});assert.equal(replay.id,invoice.id,'Identical approved replay is safe in a closed month');
    assert.equal((await api(page,'dashboard')).journal_count,beforeCount);

    // Reopening is deliberate, reasoned, audited, and enables fresh dated transactions.
    await periodPreview(page,'2026-09');await page.locator(A('reopen-period')).click();
    await page.locator(F('reference')).fill('REOPEN-TEST-'+stamp);
    await page.locator(F('reason')).fill('إعادة فتح شهر الاختبار لتسجيل مستند راجعه المحاسب');
    const reopened=await clickResponse(page,A('save-period-event'),'accounting-periods/2026-09/reopen');
    assert.ok(reopened.event||reopened.period,'Reopen result includes saved audit/period');
    await closeFiscal(page);
    await paymentAPI(page,supplier,cash,'10','2026-09-25',{allocation_mode:'invoice',target_invoice_id:invoice.id,allow_advance:false});
    assert.equal((await api(page,`suppliers/${supplier.id}/open-invoices?as_of=2026-09-30`)).find(i=>i.id===invoice.id).outstanding_cents,16100);

    const taxReport=await api(page,'tax-report?from_date=2026-09-01&to_date=2026-09-30');
    assert.equal(taxReport.totals.net_cents,25100);
    assert.equal(taxReport.totals.tax_cents,3518);
    assert.equal(taxReport.totals.recoverable_cents,2818);
    assert.equal(taxReport.totals.nonrecoverable_cents,700);
    assert.equal(taxReport.totals.gross_cents,28618);
    await page.evaluate(()=>FiscalSetup.taxReport('2026-09-30'));
    await page.waitForSelector(W+'[open]');
    await printSaved(page,A('print-tax-report'),[invoice.code,postedReturn.code,partial.code,money(3518)]);
    await closeFiscal(page);
    await page.setViewportSize({width:392,height:844});await page.evaluate(()=>FiscalSetup.taxes());
    await page.locator(A('new-tax-code')).click();await page.locator(F('name')).fill('مسودة لم تحفظ');
    page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(W).evaluate(e=>e.open),'Declining discard preserves fiscal draft');
    page.once('dialog',d=>d.accept());await page.keyboard.press('Escape');
    await page.evaluate(()=>FiscalSetup.periods(2026));await page.waitForSelector(W+'[open]');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'No page overflow on phone');
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'No fiscal dialog overflow on phone');
    assert.equal(await page.locator('select').count(),0,'No dropdown selects');assert.equal(await page.locator('aside:visible').count(),0,'No sidebar');
    assert.deepEqual(errors,[],'No JavaScript page errors');
    console.log('PASS fiscal UI: explicit VAT configuration, NET/gross invoice and original-tax return journals, saved printing, pending-draft close block, monthly locks across financial and no-journal AP actions, safe replay, reasoned reopen, reports, Escape and phone layout.');
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
