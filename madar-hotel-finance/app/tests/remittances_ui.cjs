/** Tax remittances through real UI. Use a disposable, freshly initialized MADAR_DB. */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');
const base = process.env.AM_TEST_URL || 'http://127.0.0.1:8132';
const W = '#withholding-remittance-window';
const A = action => `${W} [data-remittance-action="${action}"]`;
const F = field => `${W} [data-remittance-field="${field}"]`;
const stamp = Date.now().toString(36);
let serial = 0;
const key = label => `REMIT-UI-${stamp}-${label}-${++serial}`;
const money = cents => new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function api(page, route, method='GET', data, expected=200) {
  const result = await page.evaluate(async ({route,method,data}) => {
    const boot=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/'+route,{method,
      headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},
      body:data===undefined?undefined:JSON.stringify(data)});
    return {status:response.status,body:await response.json()};
  },{route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);
  return result.body;
}
async function clickResponse(page, selector, route, method='POST') {
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();
  const response=await pending,result=await response.json();
  assert.equal(response.status(),200,`${method} ${route}: ${JSON.stringify(result)}`);
  return result;
}
async function printed(page, selector, words) {
  const before=await page.evaluate(()=>window.__remittancePrints);
  await page.locator(selector).click();
  await page.waitForFunction(n=>window.__remittancePrints>n,before);
  const content=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(content.includes(word),'Printed document contains '+word);
  assert.match(content,/اعتمده|راجعه|توقيع/);
  await page.emulateMedia({media:'print'});
  assert.equal(await page.locator('dialog:visible').count(),0,'Entry dialogs hidden during print');
  assert.ok(await page.locator('#print-document').isVisible());
  await page.emulateMedia({media:'screen'});
}
async function close(page) {
  await page.locator(A('close')).last().click();
  await page.waitForFunction(()=>!document.querySelector('#withholding-remittance-window').open);
}
async function balances(page,asOf='2026-01-31') {return api(page,'withholding-balances?as_of='+asOf);}
const source = (report,id) => report.sources.find(item=>item.payment_id===id);

async function withholdingFixture(page,boot,chart,rate,date,label) {
  const supplier=await api(page,'suppliers','POST',{name:'مورد توريد الضريبة '+label+' '+stamp,
    tax_number:'TEST-TAX-'+stamp+'-'+label,terms_days:0});
  const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6'));
  const product=await api(page,'products','POST',{supplier_id:supplier.id,
    department_id:boot.departments[0].id,name:'خدمة توريد اختبار '+label,unit:'خدمة',price:'100',expense_account_id:expense.id});
  const code=await api(page,'withholding-codes','POST',{name:'TEST WHT '+rate+' '+label+' '+stamp,
    rate:String(rate),active:true,confirmed:true});
  const draft=await api(page,'invoices','POST',{request_key:key('invoice'),supplier_id:supplier.id,
    department_id:boot.departments[0].id,reference:key('invoice-ref'),business_date:date,due_date:date,
    kind:'service',tax_rate:'0',lines:[{product_id:product.id,quantity:'1',price:'100'}]});
  const invoice=await api(page,`invoices/${draft.id}/post`,'POST',{revision:draft.revision});
  const cash=chart.accounts.find(a=>a.code==='111002');
  const paymentDraft=await api(page,'supplier-payments','POST',{request_key:key('payment'),
    supplier_id:supplier.id,payment_date:date,amount:'100',method:'cash',funding_account_id:cash.id,
    allocation_mode:'invoice',target_invoice_id:invoice.id,withholding_code_id:code.id,
    details:{received_by:'مندوب اختبار '+label}});
  const payment=await api(page,`supplier-payments/${paymentDraft.id}/post`,'POST',{revision:paymentDraft.revision});
  return {supplier,invoice,payment,code};
}
function remitData(payment,funding,amount,date,method='cash') {
  return {request_key:key('remit'),business_date:date,period_from:'2026-01-01',period_to:'2026-01-11',
    authority_name:'جهة ضريبية للاختبار فقط',reference:key('receipt'),method,funding_account_id:funding.id,
    details:method==='cash'?{received_by:'مستلم الجهة في الاختبار',receipt_reference:key('cash-receipt')}:
      {bank_name:'بنك الاختبار',transaction_reference:key('bank-ref'),value_date:date},
    allocations:[{payment_id:payment.id,amount:String(amount)}]};
}
async function form(page,payment,funding,amount,date,method='cash') {
  await page.evaluate(preset=>WithholdingRemittances.newDocument(preset),{
    business_date:date,period_from:'2026-01-01',period_to:payment.payment_date});
  await page.waitForSelector(F('authority_name'));
  await page.locator(F('authority_name')).fill('جهة ضريبية للاختبار فقط');
  await page.locator(F('reference')).fill(key('UI-RECEIPT'));
  if(method!=='cash')await page.locator(`${A('method')}[data-method="${method}"]`).click();
  await page.locator(`${A('funding')}[data-account-id="${funding.id}"]`).click();
  if(method==='cash') {
    await page.locator(F('details.received_by')).fill('مستلم الجهة في الاختبار');
    await page.locator(F('details.receipt_reference')).fill(key('CASH-RECEIPT'));
    assert.equal(await page.locator(F('details.bank_name')).count(),0,'Cash displays cash fields only');
  } else {
    for(const [name,value] of Object.entries({bank_name:'بنك الاختبار',transaction_reference:key('BANK'),value_date:date}))
      await page.locator(F('details.'+name)).fill(value);
    assert.equal(await page.locator(F('details.received_by')).count(),0,'Bank displays bank fields only');
  }
  const input=page.locator(`${W} [data-remittance-allocation="${payment.id}"]`);
  if(await input.isDisabled())await page.locator(`${A('toggle-source')}[data-payment-id="${payment.id}"]`).click();
  await input.fill(String(amount));
  return page.locator(F('reference')).inputValue();
}
async function saveAndPost(page) {
  const saved=await clickResponse(page,A('save'),'withholding-remittances');
  const posted=await clickResponse(page,A('post'),`withholding-remittances/${saved.id}/post`);
  assert.equal(posted.status,'posted');
  return posted;
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{window.__remittancePrints=0;window.print=()=>window.__remittancePrints++;});
  try {
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    const cash=chart.accounts.find(a=>a.code==='111002'),bank=chart.accounts.find(a=>a.code==='112001');
    const wh=chart.mappings.find(m=>m.module==='الضرائب'&&m.role==='withholding_payable');
    for(const mapping of chart.mappings.filter(m=>['payable_control','supplier_advances','issued_cheques','withholding_payable'].includes(m.role)))
      await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    const empty=await balances(page);assert.equal(empty.sources.length,0);assert.ok(empty.matched,'Empty fresh ledger reconciles');
    const first=await withholdingFixture(page,boot,chart,3,'2026-01-02','الأول');
    assert.equal(first.payment.net_cash_cents,9700);await page.evaluate(()=>refresh());
    assert.equal(source(await balances(page,'2026-01-04'),first.payment.id).remaining_cents,300);

    // A partial cash remittance is drafted, reviewed, and approved separately.
    const cashReference=await form(page,first.payment,cash,1,'2026-01-05');
    page.once('dialog',dialog=>dialog.dismiss());await page.keyboard.press('Escape');
    assert.ok(await page.locator(W).evaluate(e=>e.open),'Dirty Escape preserves the document');
    assert.equal(await page.locator(F('reference')).inputValue(),cashReference);
    await clickResponse(page,A('preview'),'withholding-remittance-preview');
    const cashRemit=await saveAndPost(page);assert.equal(cashRemit.amount_cents,100);
    let position=await balances(page,'2026-01-05');
    assert.equal(source(position,first.payment.id).remitted_cents,100);
    assert.equal(source(position,first.payment.id).remaining_cents,200);assert.ok(position.matched);
    const cashJournal=await api(page,'journals/'+cashRemit.journal_id);
    assert.equal(cashJournal.lines.find(l=>l.account_id===wh.account_id).debit_cents,100);
    assert.equal(cashJournal.lines.find(l=>l.account_id===cash.id).credit_cents,100);
    await printed(page,A('print-document'),[cashRemit.code,cashReference,'جهة ضريبية للاختبار فقط',first.payment.code,money(100)]);
    await page.locator(W).screenshot({path:path.join(__dirname,'remittance-document.png')});
    await close(page);

    // The remaining amount is transferred from the bank, with no second supplier deduction.
    await form(page,first.payment,bank,2,'2026-01-06','bank_transfer');
    const bankRemit=await saveAndPost(page);assert.equal(bankRemit.amount_cents,200);
    await printed(page,A('print-document'),[bankRemit.code,'بنك الاختبار',money(200)]);
    await close(page);
    position=await balances(page,'2026-01-06');
    assert.equal(source(position,first.payment.id).remaining_cents,0);assert.ok(position.matched);
    const supplier=(await api(page,`suppliers/${first.supplier.id}/statement?as_of=2026-01-06`)).totals;
    assert.equal(supplier.payable_balance_cents,0);assert.equal(supplier.advance_cents,0);
    assert.ok((await api(page,'supplier-reconciliation?as_of=2026-01-06')).matched);

    // A real later supplier correction retains the tax already remitted as a credit.
    await api(page,`supplier-payments/${first.payment.id}/reverse`,'POST',{
      request_key:key('SOURCE-REVERSE'),revision:first.payment.revision,event_revision:0,
      business_date:'2026-01-10',reference:key('SOURCE-REF'),reason:'تصحيح سداد المورد بعد التوريد'});
    position=await balances(page,'2026-01-10');
    const credit=source(position,first.payment.id);
    assert.equal(credit.withheld_cents,0);assert.equal(credit.remitted_cents,300);
    assert.equal(credit.remaining_cents,-300);assert.equal(credit.overpaid_cents,300);
    assert.equal(position.all_totals.net_due_cents,0);assert.ok(position.matched);
    const second=await withholdingFixture(page,boot,chart,5,'2026-01-11','الثاني');
    position=await balances(page,'2026-01-11');assert.equal(position.all_totals.net_due_cents,200);
    await api(page,'withholding-remittances','POST',remitData(second.payment,cash,5,'2026-01-12'),400);
    const cappedDraft=await api(page,'withholding-remittances','POST',remitData(second.payment,cash,2,'2026-01-12'));
    await api(page,`withholding-remittances/${cappedDraft.id}/post`,'POST',{revision:cappedDraft.revision});
    assert.equal((await balances(page,'2026-01-12')).all_totals.net_due_cents,0);

    // A remittance correction restores capacity on its own date, retaining original receipt/journal.
    await page.evaluate(id=>WithholdingRemittances.open(id),bankRemit.id);
    await page.waitForSelector(A('reverse'));await page.locator(A('reverse')).click();
    await page.locator(F('business_date')).fill('2026-01-20');
    await page.locator(F('reference')).fill(key('REV-REF'));
    const reason='تصحيح توريد بنكي مسجل بالخطأ';await page.locator(F('reason')).fill(reason);
    const reversed=await clickResponse(page,A('confirm-reverse'),`withholding-remittances/${bankRemit.id}/reverse`);
    assert.equal(reversed.effective_status,'reversed');assert.equal(reversed.journal_id,bankRemit.journal_id);
    assert.match(reversed.reversal.code,/^WTRR-/);
    await printed(page,A('print-reversal'),[bankRemit.code,reversed.reversal.code,reason,money(200)]);
    await close(page);
    assert.equal((await balances(page,'2026-01-19')).all_totals.net_due_cents,0);
    position=await balances(page,'2026-01-20');assert.equal(position.all_totals.net_due_cents,200);assert.ok(position.matched);
    assert.equal(source(position,first.payment.id).remaining_cents,-100);

    // Pending remittances participate in monthly closing checks.
    const pending=await api(page,'withholding-remittances','POST',remitData(second.payment,cash,2,'2026-01-21'));
    const preview=await api(page,'period-preview?month=2026-01');
    assert.equal(preview.can_close,false);
    assert.ok(preview.pending_drafts.some(row=>row.code===pending.code),'Close preview includes tax remittance draft');
    await api(page,`withholding-remittances/${pending.id}`,'PUT',{revision:pending.revision,action:'cancel'});

    await page.evaluate(()=>WithholdingRemittances.balances('2026-01-20'));
    await page.waitForSelector(A('print-balances'));
    assert.ok((await page.locator('#remittance-body').textContent()).includes(money(100)),'Excess credit remains visible');
    await printed(page,A('print-balances'),[first.payment.code,second.payment.code,money(100),money(200),'2026-01-20']);
    await page.locator(W).screenshot({path:path.join(__dirname,'remittance-balances.png')});
    await page.locator(F('filter.as_of')).fill('2026-01-21');
    assert.ok(await page.locator(A('print-balances')).isDisabled(),'Changed dates cannot print the old result');
    await page.locator(A('reload-balances')).click();await page.waitForSelector(`${A('print-balances')}:not([disabled])`);
    await page.locator(A('journal')).first().click();await page.waitForSelector(A('back'));
    await printed(page,A('print-journal'),['قيد توريد الخصم','نيو بانوراما']);
    await page.locator(A('back')).click();
    await page.setViewportSize({width:392,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Balance dialog fits mobile');
    await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);
    assert.deepEqual(errors,[]);
    console.log('PASS remittance UI: partial cash/bank, gross AP unchanged, retained excess after source reversal, net-account cap, dated remittance correction, draft closing checks, saved print and mobile.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
