/** Operator-entered WHT, NET payments and dated reports. DISPOSABLE MADAR_DB only. */
'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.AM_TEST_URL || 'http://127.0.0.1:8124';
const W = '#withholding-setup-window', AP = '#supplier-ap-window';
const A = action => `${W} [data-withholding-action="${action}"]`;
const F = field => `${W} [data-withholding-field="${field}"]`;
const PA = action => `${AP} [data-ap-action="${action}"]`;
const PF = field => `${AP} [data-ap-field="${field}"]`;
const stamp = Date.now().toString(36);
let serial = 0;
const key = prefix => `WHT-TEST-${stamp}-${prefix}-${++serial}`;
const money = cents => new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function api(page, route, method='GET', data, expected=200) {
  const result = await page.evaluate(async ({route,method,data}) => {
    const boot=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return {status:response.status,body:await response.json()};
  },{route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);
  return result.body;
}
async function clickResponse(page, selector, route, method='POST') {
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();
  const response=await pending, result=await response.json();
  assert.equal(response.status(),200,`${method} ${route}: ${JSON.stringify(result)}`);
  return result;
}
async function printed(page, selector, words) {
  const before=await page.evaluate(()=>window.__withholdingPrints);
  await page.locator(selector).click();
  await page.waitForFunction(n=>window.__withholdingPrints>n,before);
  const html=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(html.includes(word),'Print contains '+word);
  assert.match(html,/راجعه|اعتمده|توقيع/);
  await page.emulateMedia({media:'print'});
  assert.equal(await page.locator('dialog:visible').count(),0,'Dialogs hidden while printing');
  assert.ok(await page.locator('#print-document').isVisible());
  await page.emulateMedia({media:'screen'});
  return html;
}
async function close(page, selector=A('close')) {
  await page.locator(selector).last().click();
}
async function paymentForm(page, supplier, invoice, code, funding, cheque=false) {
  await page.evaluate(({supplier,invoice})=>SupplierAP.payment(supplier,invoice),{supplier:supplier.id,invoice:invoice.id});
  await page.waitForSelector(PF('amount'));
  if(cheque)await page.locator(`${PA('method')}[data-method="cheque"]`).click();
  await page.locator(PF('payment_date')).fill(cheque?'2026-04-06':'2026-04-05');
  await page.locator(PF('amount')).fill('57');
  await page.locator(`${PA('funding')}[data-account-id="${funding.id}"]`).click();
  if(cheque) {
    for(const [field,value] of Object.entries({bank_name:'بنك الاختبار',cheque_book:'دفتر اختبار',cheque_number:key('CHQ'),beneficiary:supplier.name,cheque_date:'2026-04-06',due_date:'2026-04-07'}))await page.locator(PF('details.'+field)).fill(value);
  }
  await page.locator(PA('withholding-code')).click();
  await page.locator(`#picker-window [data-pick="${code.id}"]`).click();
  const preview=await clickResponse(page,PA('preview'),'payment-preview');
  assert.equal(preview.allocated_cents,5700);
  assert.equal(preview.withholding_base_cents,5000);
  assert.equal(preview.withholding_cents,150);
  assert.equal(preview.net_cash_cents,5550);
  for(const amount of [5000,150,5550])assert.ok((await page.locator('#ap-withholding-summary').textContent()).includes(money(amount)));
  const saved=await clickResponse(page,PA('save-payment'),'supplier-payments');
  assert.equal(saved.withholding_cents,150);
  const posted=await clickResponse(page,PA('post-payment'),`supplier-payments/${saved.id}/post`);
  assert.equal(posted.net_cash_cents,5550);
  return posted;
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.__withholdingPrints=0;window.print=()=>window.__withholdingPrints++;});
  try {
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    assert.deepEqual((await api(page,'withholding-config')).codes,[],'No default WHT rates or codes');
    const cash=chart.accounts.find(a=>a.code==='111002'),bank=chart.accounts.find(a=>a.code==='112001');
    const expense=chart.accounts.find(a=>a.posting&&a.active&&a.normal_side==='debit'&&a.code.startsWith('6'));
    const whMapping=chart.mappings.find(m=>m.module==='الضرائب'&&m.role==='withholding_payable');
    for(const m of chart.mappings.filter(m=>['payable_control','supplier_advances','issued_cheques','recoverable_input_vat','withholding_payable'].includes(m.role)))await api(page,`account-mappings/${m.id}`,'PUT',{revision:m.revision,status:'confirmed',account_id:m.account_id});

    // Configuration is entered by the operator, with no assumed statutory rate.
    await page.locator('#modules [data-page="accounting"]').click();
    await page.locator('[data-withholding-setup]').click();
    await page.waitForSelector(A('new-code'));await page.locator(A('new-code')).click();
    await page.locator(F('name')).fill('خصم اختبار 3% '+stamp);
    await page.locator(F('rate')).fill('3');
    await page.locator(`${W} [data-withholding-flag="confirmed"]`).check();
    page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');
    assert.ok(await page.locator(W).evaluate(e=>e.open),'Dirty Escape preserves code form');
    assert.equal(await page.locator(F('rate')).inputValue(),'3');
    const code=await clickResponse(page,A('save-code'),'withholding-codes');
    assert.equal(code.rate,'3.00');await close(page);

    const supplier=await api(page,'suppliers','POST',{name:'مورد خصم اختبار '+stamp,tax_number:'TEST-TAX-'+stamp,terms_days:0});
    const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:boot.departments[0].id,name:'خدمة خصم تجريبية '+stamp,unit:'خدمة',price:'100',expense_account_id:expense.id});
    const taxConfig=await api(page,'tax-config');
    await api(page,'tax-settings','PUT',{revision:taxConfig.settings.revision,registration_status:'registered',tax_number:'TEST-VAT-'+stamp});
    const vat=await api(page,'tax-codes','POST',{name:'VAT TEST14 '+stamp,kind:'vat',rate:'14',recoverable_percent:'100',active:true,confirmed:true});
    const draft=await api(page,'invoices','POST',{request_key:key('INV'),supplier_id:supplier.id,department_id:boot.departments[0].id,reference:key('REF'),business_date:'2026-04-01',due_date:'2026-04-01',kind:'service',tax_code_id:vat.id,lines:[{product_id:product.id,quantity:'1',price:'100'}]});
    const invoice=await api(page,`invoices/${draft.id}/post`,'POST',{revision:draft.revision});
    assert.equal(invoice.total_cents,11400);await page.evaluate(()=>refresh());

    const first=await paymentForm(page,supplier,invoice,code,cash);
    const journal=await api(page,'journals/'+first.journal_entry_id);
    assert.equal(journal.lines.find(l=>l.account_id===whMapping.account_id).credit_cents,150);
    assert.equal(journal.lines.find(l=>l.account_id===cash.id).credit_cents,5550);
    const voucher=await printed(page,PA('print-payment'),[first.code,money(5700),money(150),money(5550),'إقرار استلام']);
    assert.ok(voucher.includes('باستلام مبلغ '+money(5550)),'Cash acknowledgement uses actual NET received');
    await printed(page,PA('print-withholding'),[first.withholding.certificate_code,supplier.name,supplier.tax_number,money(5000),money(150),money(5550),'3.00']);
    await page.locator(AP).screenshot({path:require('node:path').join(__dirname,'withholding-payment.png')});
    await close(page,PA('close'));

    // Editing current config cannot rewrite the saved rate or supplier certificate.
    await api(page,`withholding-codes/${code.id}`,'PUT',{revision:code.revision,rate:'4'});
    const original=await api(page,'supplier-payments/'+first.id);
    assert.equal(original.withholding.rate,'3.00');assert.equal(original.withholding_cents,150);
    const chequeCode=await api(page,'withholding-codes','POST',{name:'خصم شيك اختبار 3% '+stamp,rate:'3',active:true,confirmed:true});
    const second=await paymentForm(page,supplier,invoice,chequeCode,bank,true);
    await printed(page,PA('print-payment'),[second.code,money(5550),money(150)]);
    await page.locator(PA('clear-cheque')).click();
    assert.ok((await page.locator('#ap-body').textContent()).includes(money(5550)),'Cheque clearance shows NET cheque value');
    await page.locator(PF('clearance.clearance_date')).fill('2026-04-07');
    await page.locator(PF('clearance.reference')).fill(key('CLEAR'));
    const cleared=await clickResponse(page,PA('post-clearance'),`supplier-payments/${second.id}/clear`);
    const clearance=await api(page,'journals/'+cleared.clearance_journal_entry_id);
    assert.equal(clearance.credit_cents,5550);
    assert.equal(clearance.lines.find(l=>l.account_id===bank.id).credit_cents,5550);
    assert.equal(clearance.lines.filter(l=>l.account_id===whMapping.account_id).length,0,'Clearance does not debit tax liability');
    await close(page,PA('close'));
    const bounced=await api(page,`supplier-payments/${second.id}/cheque-event`,'POST',{request_key:key('BOUNCE'),action:'bounce',revision:cleared.revision,event_revision:cleared.adjustment_revision||0,business_date:'2026-04-08',reference:key('BOUNCE-REF'),reason:'اختبار شيك صافي مرتد'});
    assert.equal(bounced.effective_status,'reversed');
    const bounceJournal=await api(page,'journals/'+bounced.reversal.journal_id);
    assert.equal(bounceJournal.lines.find(l=>l.account_id===whMapping.account_id).debit_cents,150);
    assert.ok((await api(page,'supplier-reconciliation?as_of=2026-04-08')).matched);

    // A later cash correction stays out of the earlier quarter.
    const reversed=await api(page,`supplier-payments/${first.id}/reverse`,'POST',{request_key:key('REVERSE'),revision:first.revision,event_revision:first.adjustment_revision||0,business_date:'2026-07-01',reference:key('REVERSE-REF'),reason:'اختبار تاريخ تصحيح الخصم'});
    assert.equal(reversed.effective_status,'reversed');
    await page.evaluate(id=>WithholdingSetup.report('2026-04-01','2026-04-30',id),supplier.id);
    await page.waitForSelector(`${W} .wh-report-table`);
    await page.locator(`${A('quarter')}[data-quarter="2"]`).click();
    await page.waitForFunction(()=>document.querySelector('[data-withholding-date="to"]')?.value==='2026-06-30');
    const report=await api(page,`withholding-report?from_date=2026-04-01&to_date=2026-06-30&supplier_id=${supplier.id}`);
    assert.equal(report.rows.length,3);assert.equal(report.totals.withholding_cents,150);
    assert.equal(await page.locator(`${W} .wh-report-table tbody tr`).count(),3);
    assert.equal(await page.locator(A('journal')).count(),3,'Each report row links its journal');
    await printed(page,A('print-report'),[supplier.name,supplier.tax_number,first.code,second.code,'2026-06-30',money(150)]);
    await page.locator(W).screenshot({path:require('node:path').join(__dirname,'withholding-report.png')});
    await page.locator(A('journal')).first().click();await page.waitForSelector(A('back-report'));
    await printed(page,A('print-journal'),['قيد الخصم','نيو بانوراما']);
    await page.locator(A('back-report')).click();
    await page.locator(`${W} [data-withholding-date="to"]`).fill('2026-07-31');
    assert.ok(await page.locator(A('print-report')).isDisabled(),'Changed filters block stale print');
    await page.locator(`${A('quarter')}[data-quarter="3"]`).click();
    await page.waitForFunction(()=>document.querySelector('[data-withholding-date="from"]')?.value==='2026-07-01');
    const q3=await api(page,`withholding-report?from_date=2026-07-01&to_date=2026-09-30&supplier_id=${supplier.id}`);
    assert.equal(q3.rows.length,1);assert.equal(q3.totals.withholding_cents,-150);
    await page.setViewportSize({width:392,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Report fits phone dialog');
    await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);
    assert.deepEqual(errors,[]);
    console.log('PASS withholding UI: explicit TEST rate, NET cash acknowledgement/certificate, VAT partial settlement, original snapshot, NET cheque clear/bounce, signed dated quarter reports and print/journal/mobile/dirty guards.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
