/** Trial balance and GL drilldown E2E. Use a fresh DISPOSABLE database only. */
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8110';
const W='#ledger-reports-window',B=action=>`${W} [data-ledger-action="${action}"]`;
const stamp=Date.now().toString(36);let sequence=0;
const key=kind=>kind+'-'+stamp+'-'+(++sequence);
async function api(page,route,method='GET',body,expected=200){
 const result=await page.evaluate(async({route,method,body})=>{const boot=await(await fetch('/api/bootstrap')).json();const response=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:body===undefined?undefined:JSON.stringify(body)});return {status:response.status,body:await response.json()};},{route,method,body});
 assert.equal(result.status,expected,method+' '+route+': '+JSON.stringify(result.body));return result.body;
}
async function invoice(page,supplier,product,date,amount,post=true){const d=await api(page,'invoices','POST',{request_key:key('invoice'),supplier_id:supplier.id,department_id:1,reference:key('REF'),business_date:date,due_date:date,kind:'service',notes:'بيانات اختبار منفصلة',tax_rate:'0',lines:[{product_id:product.id,quantity:'1',price:amount}]});return post?api(page,`invoices/${d.id}/post`,'POST',{revision:d.revision}):d;}
async function payment(page,supplier,cash,target,date,amount){const d=await api(page,'supplier-payments','POST',{request_key:key('payment'),supplier_id:supplier.id,payment_date:date,amount,method:'cash',funding_account_id:cash.id,allocation_mode:'invoice',target_invoice_id:target.id,allow_advance:false,details:{received_by:'مندوب الاختبار'}});return api(page,`supplier-payments/${d.id}/post`,'POST',{revision:d.revision});}
async function reload(page,kind){const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+kind);await page.locator(B('reload')).click();assert.ok((await pending).ok());await page.waitForFunction(()=>!document.querySelector('#ledger-reports-body').textContent.includes('جاري قراءة التقرير'));}
async function print(page,words){const before=await page.evaluate(()=>window.__ledgerPrints||0);await page.locator(B('print')).click();await page.waitForFunction(n=>(window.__ledgerPrints||0)>n,before);const text=await page.locator('#print-document').textContent();for(const word of words)assert.ok(text.includes(word),'Missing printed '+word);assert.match(text,/راجعه/);return text;}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{window.__ledgerPrints=0;window.print=()=>window.__ledgerPrints++;});
 try{
  await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
  const chart=await api(page,'accounting'),mapping=chart.mappings.find(m=>m.role==='payable_control');
  await api(page,'account-mappings/'+mapping.id,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
  const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6')),cash=chart.accounts.find(a=>a.code==='111002');
  const supplier=await api(page,'suppliers','POST',{name:'اختبار دفتر الأستاذ '+stamp});
  const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:1,name:'خدمة أستاذ '+stamp,unit:'مرة',price:'100',expense_account_id:expense.id});
  const jan=await invoice(page,supplier,product,'2026-01-05','100');await payment(page,supplier,cash,jan,'2026-01-10','30');
  const feb=await invoice(page,supplier,product,'2026-02-10','50');await payment(page,supplier,cash,feb,'2026-02-12','20');
  await invoice(page,supplier,product,'2026-02-15','999',false);
  await page.reload();await page.waitForSelector('#loading[hidden]',{state:'attached'});
  await page.locator('#modules [data-page="accounting"]').click();await page.locator('#accounting [data-ledger-trial]').click();
  await page.waitForSelector(W+'[open]');await page.waitForFunction(()=>document.querySelector('#ledger-reports-title').textContent==='ميزان المراجعة');
  await page.locator(W+' [data-ledger-date="from"]').fill('2026-01-01');await page.locator(W+' [data-ledger-date="to"]').fill('2026-02-28');
  assert.ok(await page.locator(B('print')).isDisabled(),'Changing date blocks printing old report with new controls');
  await reload(page,'trial-balance');
  const report=await api(page,'trial-balance?from_date=2026-01-01&to_date=2026-02-28');
  assert.equal(report.rows.length,3);assert.equal(report.totals.debit_cents,20000);assert.equal(report.totals.credit_cents,20000);assert.equal(report.totals.closing_debit_cents,15000);assert.ok(report.balanced);
  assert.equal(await page.locator('#lr-table-wrap tbody tr').count(),3,'Draft999 excluded, only expense/AP/cash');
  assert.match(await page.locator(W+' .lr-status').textContent(),/متوازن/);
  await page.locator(W).screenshot({path:path.join(__dirname,'trial-balance.png')});
  await page.locator(W+' [data-ledger-search]').fill(expense.code);assert.equal(await page.locator('#lr-table-wrap tbody tr').count(),1);
  const filtered=await print(page,['ميزان المراجعة',expense.code,expense.name,'2026-02-28']);assert.ok(!filtered.includes(cash.name),'Print matches searched accounts');
  await page.locator(W+' [data-ledger-search]').fill('');await page.locator(B('groups')).click();assert.equal(await page.locator('#lr-table-wrap tfoot').count(),0,'No invalid grand total of nested groups');assert.match(await page.locator(W+' .lr-note.warning').textContent(),/تتداخل/);
  await page.locator(B('groups')).click();
  await page.locator(`${B('account')}[data-id="${mapping.account_id}"]`).click();await page.waitForFunction(()=>document.querySelector('#ledger-reports-title').textContent==='كشف حركة الحساب');
  const ledger=await api(page,`account-ledger?account_id=${mapping.account_id}&from_date=2026-01-01&to_date=2026-02-28`);assert.deepEqual(ledger.rows.map(r=>r.running_balance_cents),[-10000,-7000,-12000,-10000]);
  assert.equal(await page.locator(W+' .lr-table tbody tr').count(),5,'Opening row plus four ledger entries');
  await page.locator(W+' [data-ledger-date="from"]').fill('2026-02-01');await reload(page,'account-ledger');
  const febLedger=await api(page,`account-ledger?account_id=${mapping.account_id}&from_date=2026-02-01&to_date=2026-02-28`);assert.equal(febLedger.opening_balance_cents,-7000);assert.equal(febLedger.closing_balance_cents,-10000);assert.equal(febLedger.rows.length,2);
  assert.equal(await page.locator(W+' .lr-table tbody tr').count(),3);assert.match(await page.locator(W+' .lr-opening').textContent(),/دائن/);
  await page.locator(B('journal')).first().click();await page.waitForFunction(()=>document.querySelector('#ledger-reports-title').textContent==='تفاصيل القيد المحاسبي');
  await print(page,['القيد المحاسبي',feb.journal_code,expense.name]);await page.locator(B('back')).click();assert.match(await page.locator('#ledger-reports-title').textContent(),/كشف حركة/);
  await page.locator(B('supplier')).click();await page.locator(`#picker-list [data-pick="${supplier.id}"]`).click();await page.waitForFunction(name=>document.querySelector('#ledger-reports-body [data-ledger-action="supplier"]')?.textContent===name,supplier.name);
  await print(page,['كشف حركة الحساب',supplier.name,'2026-02-01','2026-02-28']);
  await page.locator(W+' [data-ledger-date="from"]').fill('2026-03-01');await page.locator(B('reload')).click();assert.match(await page.locator('#ledger-reports-error').textContent(),/البداية لا تتجاوز/);assert.ok(await page.locator(B('print')).isDisabled());
  await page.locator(W+' [data-ledger-date="from"]').fill('2026-02-01');await reload(page,'account-ledger');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.locator(W).evaluate(el=>el.scrollWidth>el.clientWidth),false,'Modal fits phone viewport');assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'Page fits phone viewport');
  assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);await page.keyboard.press('Escape');await page.waitForSelector(W+':not([open])',{state:'attached'});
  // A response arriving after closure must never reopen the report.
  await page.route('**/api/trial-balance?**',async route=>{await new Promise(resolve=>setTimeout(resolve,180));await route.continue();});
  await page.evaluate(()=>{LedgerReports.trialBalance();document.querySelector('#ledger-reports-window [data-ledger-action="close"]').click();});
  await page.waitForTimeout(350);assert.equal(await page.locator(W).evaluate(el=>el.open),false);await page.unroute('**/api/trial-balance?**');
  assert.deepEqual(errors,[]);console.log('PASS ledger UI: exact trial balance, draft exclusion, hierarchy without duplicate totals, searched print, historical opening movement, account/supplier/journal drill, invalid dates, mobile/Escape and late-response closure.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
