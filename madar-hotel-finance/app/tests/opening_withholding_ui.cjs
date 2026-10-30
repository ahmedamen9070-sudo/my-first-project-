/** Collision-safe opening/current withholding UI, on its own disposable server. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const net=require('node:net');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const W='#withholding-remittance-window';
const A=name=>`${W} [data-remittance-action="${name}"]`;
const F=name=>`${W} [data-remittance-field="${name}"]`;
const stamp=Date.now().toString(36);let serial=0;
const key=label=>`OPEN-WH-UI-${stamp}-${label}-${++serial}`;

async function api(page,route,method='GET',data){
  const response=await page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const result=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:result.status,body:await result.json()};
  },{route,method,data});
  assert.equal(response.status,200,`${method} ${route}: ${JSON.stringify(response.body)}`);return response.body;
}
async function clickResponse(page,selector,route){
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()==='POST');
  await page.locator(selector).click();const result=await pending,body=await result.json();
  assert.equal(result.status(),200,`${route}: ${JSON.stringify(body)}`);return body;
}
async function post(page,route,data){
  const draft=await api(page,route,'POST',{request_key:key(route),...data});
  return api(page,`${route}/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function close(page){
  await page.locator(A('close')).last().click();
  await page.waitForFunction(()=>!document.querySelector('#withholding-remittance-window').open);
}
async function freePort(){
  const listener=net.createServer();await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));
  const port=listener.address().port;await new Promise(resolve=>listener.close(resolve));return port;
}

(async()=>{
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'madar-opening-wh-ui-'));
  const port=await freePort(),base=`http://127.0.0.1:${port}`;
  const server=spawn('python3',[path.join(__dirname,'..','server.py'),'--port',String(port)],{
    env:{...process.env,MADAR_DB:path.join(temporary,'isolated.sqlite')},stdio:['ignore','ignore','pipe']});
  let serverErrors='';server.stderr.on('data',chunk=>{serverErrors=(serverErrors+chunk).slice(-16000);});
  let browser;
  try{
    let ready=false;
    for(let attempt=0;attempt<120;attempt++){
      if(server.exitCode!==null)throw Error('Isolated server failed: '+serverErrors);
      try{ready=(await fetch(base+'/api/bootstrap')).ok;}catch{}
      if(ready)break;await new Promise(resolve=>setTimeout(resolve,50));
    }
    assert.ok(ready,'Fresh isolated server starts');
    browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
    const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{window.__openingWhPrints=0;window.print=()=>window.__openingWhPrints++;});
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    const cash=chart.accounts.find(a=>a.code==='111002'),equity=chart.accounts.find(a=>a.code==='300001');
    for(const mapping of chart.mappings.filter(m=>['payable_control','supplier_advances','issued_cheques','withholding_payable','opening_clearing'].includes(m.role)))
      await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    const supplier=await api(page,'suppliers','POST',{name:'مورد خصم افتتاح وتشغيل '+stamp,terms_days:0,tax_number:'OLD-WH-TAX-'+stamp});
    const opening=await post(page,'opening-balances',{business_date:'2026-01-01',reference:key('opening'),lines:[
      {kind:'general',account_id:cash.id,side:'debit',amount:'1000'},
      {kind:'withholding',supplier_id:supplier.id,amount:'30',reference:'PRIOR-WH-DECEMBER',source_date:'2025-12-15'},
      {kind:'general',account_id:equity.id,side:'credit',amount:'970'}]});
    const openingId=opening.lines.find(line=>line.kind==='withholding').document_id;
    assert.equal((await api(page,'supplier-payments')).length,0,'Opening does not fabricate a payment');
    const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6'));
    const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:boot.departments[0].id,
      name:'خدمة اختبار الخصم الحالي',unit:'خدمة',price:'100',expense_account_id:expense.id});
    const invoice=await post(page,'invoices',{supplier_id:supplier.id,department_id:boot.departments[0].id,
      reference:key('invoice'),business_date:'2026-01-02',due_date:'2026-01-02',kind:'service',tax_rate:'0',
      lines:[{product_id:product.id,quantity:'1',price:'100'}]});
    const code=await api(page,'withholding-codes','POST',{name:'خصم تشغيل 3 بالمئة '+stamp,rate:'3',active:true,confirmed:true});
    const payment=await post(page,'supplier-payments',{supplier_id:supplier.id,payment_date:'2026-01-02',amount:'100',
      method:'cash',funding_account_id:cash.id,allocation_mode:'invoice',target_invoice_id:invoice.id,
      withholding_code_id:code.id,details:{received_by:'مندوب المورد'}});
    assert.equal(openingId,payment.id,'Fixture deliberately collides numeric source IDs');
    const before=await api(page,'withholding-balances?as_of=2026-01-10');
    assert.equal(before.sources.length,2);assert.ok(before.matched);
    const oldSource=before.sources.find(row=>row.source_type==='opening_withholding');
    const cashBefore=(await api(page,`cash-book?account_id=${cash.id}&from_date=2026-01-01&to_date=2026-01-31`)).closing_balance_cents;

    await page.evaluate(()=>WithholdingRemittances.newDocument({business_date:'2026-01-10',period_from:'2025-12-01',period_to:'2026-01-09'}));
    await page.waitForSelector(F('authority_name'));
    await page.locator(F('authority_name')).fill('جهة ضريبية لاختبار الرصيد السابق');
    await page.locator(F('reference')).fill(key('receipt'));
    await page.locator(`${A('funding')}[data-account-id="${cash.id}"]`).click();
    await page.locator(F('details.received_by')).fill('مندوب الجهة الضريبية');
    const priorKey='opening:'+openingId,currentKey=String(payment.id);
    const priorInput=()=>page.locator(`${W} [data-remittance-allocation="${priorKey}"]`);
    const currentInput=()=>page.locator(`${W} [data-remittance-allocation="${currentKey}"]`);
    assert.ok(await priorInput().isDisabled());assert.ok(await currentInput().isDisabled());
    await page.locator(`${A('toggle-source')}[data-payment-id="${priorKey}"]`).click();
    await priorInput().fill('10');
    await page.locator(`${A('toggle-source')}[data-payment-id="${currentKey}"]`).click();
    assert.equal(Number(await priorInput().inputValue()),10,'Selecting same numeric payment ID preserves opening allocation');
    await currentInput().fill('1');
    await page.locator(`${A('toggle-source')}[data-payment-id="${priorKey}"]`).click();
    assert.ok(await priorInput().isDisabled());assert.equal(Number(await currentInput().inputValue()),1,'Deselecting opening keeps payment independently selected');
    await page.locator(`${A('toggle-source')}[data-payment-id="${priorKey}"]`).click();await priorInput().fill('10');
    const preview=await clickResponse(page,A('preview'),'withholding-remittance-preview');
    assert.equal(preview.amount_cents,1100);assert.ok(preview.can_approve);assert.equal(preview.allocations.length,2);
    const draft=await clickResponse(page,A('save'),'withholding-remittances');
    assert.equal(draft.allocations.length,2);await close(page);
    await page.evaluate(id=>WithholdingRemittances.open(id),draft.id);await page.waitForSelector(F('authority_name'));
    assert.equal(Number(await priorInput().inputValue()),10);assert.equal(Number(await currentInput().inputValue()),1,'Saved mixed draft reloads each distinct allocation');
    const posted=await clickResponse(page,A('post'),`withholding-remittances/${draft.id}/post`);
    assert.equal(posted.amount_cents,1100);
    const prints=await page.evaluate(()=>window.__openingWhPrints);
    await page.locator(A('print-document')).click();await page.waitForFunction(n=>window.__openingWhPrints>n,prints);
    const content=await page.locator('#print-document').textContent();
    assert.ok(content.includes('رصيد خصم افتتاحي'));assert.ok(content.includes(oldSource.payment_code));assert.ok(content.includes(payment.code));
    assert.ok(!content.includes('شهادة'),'Remittance evidence does not claim an opening deduction certificate');
    const printedPrior=await page.locator('#print-document tbody tr').filter({hasText:oldSource.payment_code}).textContent();
    assert.ok(printedPrior.includes('2025-12-15'),'Original prior tax date is retained');
    assert.ok(!printedPrior.includes('%'),'Opening amount has no fabricated tax rate');
    await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);await page.emulateMedia({media:'screen'});
    const after=await api(page,'withholding-balances?as_of=2026-01-10');assert.ok(after.matched);
    assert.equal(after.sources.find(row=>row.source_type==='opening_withholding').remaining_cents,2000);
    assert.equal(after.sources.find(row=>row.source_type==='payment_withholding').remaining_cents,200);
    const cashAfter=(await api(page,`cash-book?account_id=${cash.id}&from_date=2026-01-01&to_date=2026-01-31`)).closing_balance_cents;
    assert.equal(cashAfter,cashBefore-1100);assert.ok((await api(page,'supplier-reconciliation?as_of=2026-01-10')).matched);
    assert.equal((await api(page,'supplier-payments')).length,1,'Tax remittance does not append a supplier deduction');
    assert.deepEqual(errors,[]);
    console.log('PASS opening WHT UI: colliding IDs independently selected, mixed partial draft reload/post, prior-date evidence without rate/certificate, matched GL/AP and exact cash effect.');
  }finally{
    if(browser)await browser.close();server.kill('SIGTERM');
    if(server.exitCode===null)await new Promise(resolve=>server.once('exit',resolve));
    fs.rmSync(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
