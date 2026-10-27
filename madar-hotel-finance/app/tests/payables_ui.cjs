/**
 * Supplier accounting acceptance flow. Run ONLY against a disposable MADAR_DB.
 * AM_TEST_URL defaults to http://127.0.0.1:8096. Requires Playwright + Chromium.
 * Exercises linked masters, saved/posted invoices, AP settlement/return windows,
 * contextual method fields, reports, saved-document printing and discard behavior.
 */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');
const base = process.env.AM_TEST_URL || 'http://127.0.0.1:8096';
const fmt = cents => new Intl.NumberFormat('ar-EG', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(cents / 100);
const AP = '#supplier-ap-window';
const action = name => `${AP} [data-ap-action="${name}"]`;
const field = name => `${AP} [data-ap-field="${name}"]`;
const stamp = Date.now().toString(36);

async function api(page, route, method = 'GET', payload) {
  const result = await page.evaluate(async ({ route, method, payload }) => {
    const boot = await (await fetch('/api/bootstrap')).json();
    const response = await fetch('/api/' + route, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Madar-CSRF': boot.csrf },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    return { status: response.status, body: await response.json() };
  }, { route, method, payload });
  assert.ok(result.status >= 200 && result.status < 300,
    `${method} ${route}: ${result.status} ${result.body.error || ''}`);
  return result.body;
}
async function clickResponse(page, selector, route, method = 'POST') {
  const pending = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/' + route && response.request().method() === method);
  await page.locator(selector).click();
  const response = await pending;
  const body = await response.json();
  assert.ok(response.ok(), `${method} ${route}: ${response.status()} ${body.error || ''}`);
  return body;
}
async function connected(page) {
  await page.waitForSelector('.connection-status[data-state="connected"]');
}
async function toastContains(page, text) {
  await page.waitForFunction(text => document.querySelector('#toast').textContent.includes(text), text);
}
async function outstanding(page, supplierId, invoiceId) {
  const rows = await api(page, 'suppliers/' + supplierId + '/open-invoices?as_of=2026-12-31');
  return rows.find(row => row.id === invoiceId || row.invoice_id === invoiceId)?.outstanding_cents;
}
async function closeEntry(page) {
  await page.locator('#entry-close').click();
  await page.waitForFunction(() => !document.querySelector('#entry-window').open);
}
async function closeAP(page) {
  await page.locator(action('close')).first().click();
  await page.waitForFunction(() => !document.querySelector('#supplier-ap-window').open);
}
async function assertAPHealthy(page) {
  assert.equal((await page.locator(`${AP} #ap-error`).textContent()).trim(), '', 'No AP form error');
  assert.equal(await page.locator('select').count(), 0, 'No dropdown controls');
}
async function printDocument(page, selector, required, label) {
  const before = await page.evaluate(() => window.__amPrintCount || 0);
  await page.locator(selector).click();
  await page.waitForFunction(before => (window.__amPrintCount || 0) > before, before);
  const text = await page.locator('#print-document').textContent();
  for (const expected of required) assert.ok(text.includes(expected), `${label}: missing ${expected}`);
  assert.match(text, /اعتمده|اعتماد|المستلم|استلم|توقيع|التوقيع/, `${label}: signature fields`);
}
async function createInvoice(page, supplier, product, reference, businessDate, quantity, price) {
  if (!await page.locator('#supplier').isVisible()) await page.locator('[data-page="supplier"]:visible').first().click();
  await page.locator('#supplier [data-new-invoice]').click();
  await page.locator('#vendor').click();
  await page.locator(`[data-pick="${supplier.id}"]`).click();
  const originalDate = await page.locator('#date').inputValue();
  const autoDue = new Date(originalDate + 'T12:00:00Z'); autoDue.setUTCDate(autoDue.getUTCDate() + Number(supplier.terms_days));
  assert.equal(await page.locator('#due-date').inputValue(), autoDue.toISOString().slice(0, 10), 'Supplier terms automatically set the initial due date');
  await page.locator('[data-kind="service"]').click();
  await page.locator('#date').fill(businessDate);
  const changedDue = new Date(businessDate + 'T12:00:00Z'); changedDue.setUTCDate(changedDue.getUTCDate() + Number(supplier.terms_days));
  assert.equal(await page.locator('#due-date').inputValue(), changedDue.toISOString().slice(0, 10), 'Changing invoice date recomputes an automatic due date');
  await page.locator('#reference').fill(reference);
  await page.locator('#add-line').click();
  await page.locator(`[data-pick="${product.id}"]`).click();
  await page.locator('[data-qty]').fill(quantity);
  await page.locator('[data-qty]').dispatchEvent('change');
  await page.locator('[data-price]').fill(price);
  await page.locator('[data-price]').dispatchEvent('change');
  return { save: async () => {
    const saved = await clickResponse(page, '#save', 'invoices');
    await toastContains(page, 'تم حفظ ' + saved.code);
    return saved;
  } };
}
async function postInvoice(page, saved) {
  const posted = await clickResponse(page, '#post-invoice', `invoices/${saved.id}/post`);
  await page.waitForFunction(() => document.querySelector('#invoice-status').textContent.includes('مرحّلة') && document.querySelector('#save').hidden);
  await connected(page);
  assert.equal(posted.status, 'posted', 'Invoice becomes posted');
  assert.ok(posted.journal_id || posted.journal_entry_id, 'Posted invoice has a journal');
  assert.ok(await page.locator('#reference').isDisabled(), 'Posted reference is immutable');
  assert.ok(await page.locator('[data-qty]').isDisabled(), 'Posted quantities are immutable');
  assert.ok(await page.locator('[data-price]').isDisabled(), 'Posted prices are immutable');
  assert.equal(await page.locator('#save').isVisible(), false, 'No save action after posting');
  assert.equal(await page.locator('#cancel-invoice').isVisible(), false, 'No cancellation action after posting');
  return posted;
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.AM_CHROMIUM || '/usr/bin/chromium', args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__amPrintCount = 0;
    window.print = () => { window.__amPrintCount += 1; };
  });
  try {
    await page.goto(base);
    await page.waitForSelector('#loading[hidden]', { state: 'attached' });
    await connected(page);
    assert.equal(await page.locator('aside:visible').count(), 0, 'No sidebar');
    assert.equal(await page.locator('#modules [data-page="invoices"]').count(), 0, 'One supplier root module');
    const boot = await api(page, 'bootstrap');
    const category = boot.categories.find(c => c.name === 'خدمات') || boot.categories[0];
    assert.ok(category && boot.departments.length, 'Master categories and departments available');
    const chart = await api(page, 'accounting');
    const expense = chart.accounts.find(a => a.active && a.posting && a.normal_side === 'debit' && /^[568]/.test(a.code));
    const cash = chart.accounts.find(a => a.active && a.posting && a.normal_side === 'debit' && a.code.startsWith('111'));
    const bank = chart.accounts.find(a => a.active && a.posting && a.normal_side === 'debit' && a.code.startsWith('112'));
    assert.ok(expense && cash && bank, 'Active expense and cash/bank source accounts exist');
    const payable = chart.mappings.find(m => m.module === 'الموردون' && m.role === 'payable_control');
    assert.ok(payable, 'Supplier control mapping exists');
    await api(page, 'account-mappings/' + payable.id, 'PUT', { revision: payable.revision, status: 'confirmed', account_id: payable.account_id });

    // Real master UI: supplier categories and terms, then a linked expense/service item.
    await page.locator('[data-new-supplier]:visible').first().click();
    const supplierName = 'مورد دورة الحسابات ' + stamp;
    await page.locator('#record-form [name="name"]').fill(supplierName);
    await page.locator('#record-form [name="terms_days"]').fill('30');
    await page.locator(`[data-category="${category.id}"]`).click();
    const supplier = await clickResponse(page, '#record-form button[type="submit"]', 'suppliers');
    await toastContains(page, 'تم الحفظ • ' + supplier.code);
    assert.equal(supplier.terms_days, 30);
    assert.ok(supplier.category_ids.includes(category.id));
    if (!await page.locator('#supplier').isVisible()) await page.locator('[data-page="supplier"]:visible').first().click();
    await page.locator(`[data-supplier-file="${supplier.id}"]`).click();
    await page.locator(`#modal [data-new-product][data-product-supplier="${supplier.id}"]`).click();
    await page.locator(`[data-pick="${boot.departments[0].id}"]`).click();
    const productName = 'خدمة فندقية تجريبية ' + stamp;
    await page.locator('#record-form [name="name"]').fill(productName);
    await page.locator('#record-form [name="unit"]').fill('زيارة');
    await page.locator('#record-form [name="price"]').fill('100');
    await page.locator(`[data-item-category="${category.id}"]`).click();
    await page.locator('.master-account').click();
    await page.locator(`[data-pick="${expense.id}"]`).click();
    const product = await clickResponse(page, '#record-form button[type="submit"]', 'products');
    await toastContains(page, 'تم الحفظ • ' + product.code);
    assert.equal(product.expense_account_id, expense.id);
    assert.equal(product.category_id, category.id);
    await page.locator('#modal [data-close="modal"]').click();

    // First invoice establishes the posted purchase price. Terms automatically supply a due date.
    const firstForm = await createInvoice(page, supplier, product, 'UI-FIRST-' + stamp, '2026-09-01', '2', '100');
    // A deliberately overridden due date must survive later invoice-date edits.
    await page.locator('#due-date').fill('2026-11-15');
    await page.locator('#date').fill('2026-09-02');
    assert.equal(await page.locator('#due-date').inputValue(), '2026-11-15', 'Manual due-date override is preserved');
    await page.locator('#date').fill('2026-09-01');
    assert.equal(await page.locator('#due-date').inputValue(), '2026-11-15', 'Manual due date remains explicit after another date edit');
    await page.locator('#due-date').fill('2026-10-01');
    const firstDraft = await firstForm.save();
    assert.equal(firstDraft.due_date, '2026-10-01');
    assert.equal(firstDraft.total_cents, 20000);
    const first = await postInvoice(page, firstDraft);
    let latest = (await api(page, 'bootstrap')).products.find(p => p.id === product.id);
    assert.equal(latest.last_purchase_price_cents, 10000);
    await closeEntry(page);

    const secondForm = await createInvoice(page, supplier, product, 'UI-SECOND-' + stamp, '2026-10-01', '1', '120');
    await page.locator('#due-date').fill('2026-10-31');
    assert.ok(await page.locator('.price-comparison.higher').isVisible(), 'Above-last-price warning visible');
    const secondDraft = await secondForm.save();
    latest = (await api(page, 'bootstrap')).products.find(p => p.id === product.id);
    assert.equal(latest.last_purchase_price_cents, 10000, 'Saving a draft does not change latest posted price');
    const second = await postInvoice(page, secondDraft);
    latest = (await api(page, 'bootstrap')).products.find(p => p.id === product.id);
    assert.equal(latest.last_purchase_price_cents, 12000, 'Approval updates latest purchase price');
    assert.equal(latest.price_cents, 12000);
    await closeEntry(page);

    // Lower-price warning and unsaved Escape confirmation retain/reject the draft correctly.
    await createInvoice(page, supplier, latest, 'UI-DISCARD-' + stamp, '2026-10-27', '1', '80');
    assert.ok(await page.locator('.price-comparison.lower').isVisible(), 'Below-last-price warning visible');
    page.once('dialog', dialog => dialog.dismiss());
    await page.keyboard.press('Escape');
    assert.ok(await page.locator('#entry-window').evaluate(e => e.open), 'Declining discard retains invoice draft');
    page.once('dialog', dialog => dialog.accept());
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('#entry-window').open);
    assert.equal((await api(page, 'bootstrap')).products.find(p => p.id === product.id).price_cents, 12000);

    // A targeted payment: contextual fields must clear safely when changing methods.
    await page.evaluate(([sid, iid]) => SupplierAP.payment(sid, iid), [supplier.id, second.id]);
    await page.waitForSelector(`${AP}[open]`);
    await page.locator(`${action('method')}[data-method="cheque"]`).click();
    await page.locator(field('details.cheque_number')).fill('UNUSED-' + stamp);
    await page.locator(`${action('method')}[data-method="bank_transfer"]`).click();
    assert.equal(await page.locator(field('details.cheque_number')).count(), 0, 'Cheque field disappears on transfer');
    await page.locator(field('details.transaction_reference')).fill('UNUSED-TRANSFER-' + stamp);
    await page.locator(`${action('method')}[data-method="bank_card"]`).click();
    assert.equal(await page.locator('select').count(), 0, 'Card type is a button choice');
    assert.equal(await page.locator('[data-ap-field="details.card_number"], [data-ap-field="details.cvv"]').count(), 0, 'No full card or CVV inputs');
    await page.locator(`${action('method')}[data-method="cash"]`).click();
    assert.equal(await page.locator(field('details.transaction_reference')).count(), 0, 'Transfer fields disappear on cash');
    await page.locator(`${action('funding')}[data-account-id="${cash.id}"]`).click();
    await page.locator(field('amount')).fill('30');
    await page.locator(field('details.received_by')).fill('مستلم اختبار ' + stamp);
    const paymentDraft = await clickResponse(page, action('save-payment'), 'supplier-payments');
    await toastContains(page, 'تم حفظ ' + paymentDraft.code + ' كمسودة');
    assert.equal(paymentDraft.status, 'draft');
    assert.equal(await outstanding(page, supplier.id, second.id), 12000, 'Draft payment has no balance effect');
    const payment = await clickResponse(page, action('post-payment'), 'supplier-payments/' + paymentDraft.id + '/post');
    await toastContains(page, 'تم اعتماد السداد');
    assert.equal(payment.status, 'posted');
    assert.equal(payment.allocations.length, 1);
    assert.equal(payment.allocations[0].invoice_id, second.id);
    assert.equal(payment.allocations[0].amount_cents, 3000);
    assert.equal(await outstanding(page, supplier.id, second.id), 9000);
    assert.equal(payment.details.cheque_number, undefined, 'Irrelevant cheque details were not saved');
    assert.equal(payment.details.transaction_reference, undefined, 'Irrelevant transfer details were not saved');
    await printDocument(page, action('print-payment'), [payment.code, supplierName], 'Cash payment voucher');
    await closeAP(page);

    // Partial return uses the original posted invoice line and original unit price.
    await page.evaluate(id => SupplierAP.returns(id), second.id);
    await page.locator(field('reference')).fill('RETURN-' + stamp);
    const line = second.lines[0];
    await page.locator(`[data-ap-return-line="${line.id}"]`).fill('0.25');
    const returnDraft = await clickResponse(page, action('save-return'), 'supplier-returns');
    await toastContains(page, 'تم حفظ ' + returnDraft.code + ' كمسودة');
    assert.equal(returnDraft.total_cents, 3000);
    assert.equal(await outstanding(page, supplier.id, second.id), 9000, 'Draft return has no effect');
    const returned = await clickResponse(page, action('post-return'), 'supplier-returns/' + returnDraft.id + '/post');
    await toastContains(page, 'تم اعتماد المرتجع');
    assert.equal(returned.status, 'posted');
    assert.equal(await outstanding(page, supplier.id, second.id), 6000);
    assert.equal((await api(page, 'bootstrap')).products.find(p => p.id === product.id).price_cents, 12000,
      'A return does not overwrite last posted purchase price');
    await printDocument(page, action('print-return'), [returned.code, second.code, supplierName], 'Partial return voucher');
    await closeAP(page);

    // General settlement explicitly chooses FIFO; the older invoice receives the allocation.
    await page.evaluate(id => SupplierAP.payment(id), supplier.id);
    assert.ok(await page.locator(`${action('allocation-mode')}[data-mode="manual"]`).isVisible());
    await page.locator(`${action('allocation-mode')}[data-mode="fifo"]`).click();
    await page.locator(`${action('funding')}[data-account-id="${cash.id}"]`).click();
    await page.locator(field('amount')).fill('10');
    const preview = await clickResponse(page, action('preview'), 'payment-preview');
    assert.equal(preview.allocations[0].invoice_id, first.id, 'FIFO allocates to earliest due invoice');
    assert.equal(preview.allocations[0].amount_cents, 1000);
    const generalDraft = await clickResponse(page, action('save-payment'), 'supplier-payments');
    await toastContains(page, 'تم حفظ ' + generalDraft.code + ' كمسودة');
    const general = await clickResponse(page, action('post-payment'), 'supplier-payments/' + generalDraft.id + '/post');
    await toastContains(page, 'تم اعتماد السداد');
    assert.equal(general.allocations[0].invoice_id, first.id);
    await closeAP(page);

    // Reports show the posted invoice/return/payment chain, with due-date aging.
    const reportDate = '2026-12-31';
    const statement = await api(page, `suppliers/${supplier.id}/statement?as_of=${reportDate}`);
    assert.equal(statement.totals.balance_cents, 25000);
    await page.evaluate(([id, ds]) => SupplierAP.statement(id, ds), [supplier.id, reportDate]);
    for (const code of [first.code, second.code, payment.code, returned.code, general.code]) {
      assert.ok((await page.locator(`${AP} #ap-body`).textContent()).includes(code), 'Statement includes ' + code);
    }
    assert.ok((await page.locator(`${AP} #ap-body`).textContent()).includes(fmt(25000)));
    await printDocument(page, action('print-statement'), [supplierName, first.code, payment.code, fmt(25000)], 'Supplier statement');
    await closeAP(page);
    await page.evaluate(ds => SupplierAP.aging(ds), reportDate);
    await assertAPHealthy(page);
    const aging = await api(page, 'supplier-aging?as_of=' + reportDate);
    const aged = aging.suppliers.find(s => s.supplier_id === supplier.id);
    assert.equal(aged.net_payable_cents, 25000);
    assert.equal(aged.current_cents, 0, 'Both due dates are overdue at the report date');
    assert.equal(aged.days_over_90_cents, 19000, 'The older invoice is aged from its due date');
    assert.equal(aged.days_61_90_cents, 6000, 'The newer invoice retains its net residual in the correct bucket');
    const agingRow = page.locator(`${AP} .ap-aging-table tbody tr`).filter({ hasText: supplierName });
    assert.equal((await agingRow.locator('td').nth(4).innerText()).trim(), fmt(6000), 'UI 61–90-day bucket matches the dated residual');
    assert.equal((await agingRow.locator('td').nth(5).innerText()).trim(), fmt(19000), 'UI over-90-day bucket matches the older invoice');
    assert.ok((await page.locator(`${AP} #ap-body`).textContent()).includes(supplierName));
    await printDocument(page, action('print-aging'), [supplierName, fmt(25000)], 'Supplier aging');
    await page.screenshot({ path: path.join(__dirname, 'payables-aging.png'), fullPage: true });
    await closeAP(page);

    // An unfinished AP draft requires confirmation on Escape, including on narrow screens.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(id => SupplierAP.payment(id), supplier.id);
    await page.locator(field('amount')).fill('5');
    assert.equal(await page.locator(AP).evaluate(e => e.scrollWidth > e.clientWidth), false, 'AP window has no mobile overflow');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Page has no mobile overflow');
    page.once('dialog', dialog => dialog.dismiss());
    await page.keyboard.press('Escape');
    assert.ok(await page.locator(AP).evaluate(e => e.open), 'Declining discard retains AP draft');
    page.once('dialog', dialog => dialog.accept());
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('#supplier-ap-window').open);
    assert.deepEqual(errors, [], 'No JavaScript page errors');
    console.log('PASS supplier accounting UI: linked categorized masters, due-date draft and immutable posting, price baselines/warnings, conditional payment fields, partial cash settlement + voucher, partial return + voucher, general FIFO, posted statement/aging reports, Escape and mobile.');
  } catch (error) {
    await page.screenshot({ path: path.join(__dirname, 'payables-failure.png'), fullPage: true }).catch(() => {});
    if (errors.length) console.error('PAGE_ERRORS', errors);
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
