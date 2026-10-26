/**
 * Visual acceptance checks for the dark reference dashboard and light entry window.
 * Requires Playwright + Chromium and an application server backed by an ISOLATED
 * temporary database. Run with AM_TEST_URL when the test server is not on 8091.
 * This file tests presentation and UI refresh, not accounting/posting rules.
 */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');
const url = process.env.AM_TEST_URL || 'http://127.0.0.1:8091';
const output = __dirname;
const formatMoney = n => new Intl.NumberFormat('ar-EG', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
}).format(n / 100);

async function api(page, route, method = 'GET', body) {
  const response = await page.evaluate(async ({ route, method, body }) => {
    const bootstrapResponse = await fetch('/api/bootstrap');
    if (!bootstrapResponse.ok) throw new Error('Bootstrap request failed');
    const bootstrap = await bootstrapResponse.json();
    const result = await fetch('/api/' + route, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-Madar-CSRF': bootstrap.csrf,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: result.status, body: await result.json() };
  }, { route, method, body });
  assert.ok(response.status >= 200 && response.status < 300,
    `${method} ${route}: ${response.status} ${response.body.error || ''}`);
  return response.body;
}

async function waitForAmount(page, cents) {
  await page.waitForFunction(expected => {
    const value = document.querySelector('#stats .stat .value');
    return value && value.textContent.trim() === expected;
  }, formatMoney(cents));
}

async function perceivedBackground(page, selector) {
  return page.locator(selector).evaluate(element => {
    // Walk through transparent ancestors rather than treating transparent as black.
    let current = element;
    while (current) {
      const color = getComputedStyle(current).backgroundColor;
      const rgba = color.match(/[\d.]+/g)?.map(Number);
      if (rgba && (rgba.length === 3 || rgba[3] > 0.8)) {
        return 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2];
      }
      current = current.parentElement;
    }
    return 255;
  });
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.AM_CHROMIUM || '/usr/bin/chromium',
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(url);
    await page.waitForSelector('body.reference-theme');
    await page.waitForSelector('#loading[hidden]', { state: 'attached' });
    await page.waitForSelector('.connection-status[data-state="connected"]');
    assert.ok(await page.locator('.reference-workbench').isVisible(), 'Reference workbench is visible');
    assert.ok(await page.locator('.hotel-poster').isVisible(), 'Hotel poster is visible');
    assert.match(await page.locator('.hotel-copy h1').innerText(), /نيو بانوراما/);
    assert.match(await page.locator('.hotel-copy h1').innerText(), /رمسيس/);
    await page.waitForFunction(() => {
      const image = document.querySelector('.hotel-photo');
      return image && image.complete && image.naturalWidth > 100;
    });
    assert.equal(await page.locator('aside:visible').count(), 0, 'There is no sidebar');
    assert.equal(await page.locator('select').count(), 0, 'There are no dropdown selects');
    assert.equal(await page.locator('#modules [data-page="invoices"]').count(), 0,
      'Supplier invoices are not a separate root module');
    assert.equal(await page.locator('.topnav [data-page="supplier"]').count(), 0,
      'Supplier navigation is not duplicated beside Home');
    assert.equal(await page.locator('#stats .stat').count(), 3,
      'The dashboard uses three consolidated indicators');
    assert.equal(await page.locator('#stats [data-page="supplier"]').count(), 1,
      'There is one consolidated supplier indicator');
    assert.equal(await page.locator('#stats [data-page="products"]').count(), 0,
      'Supplier items do not duplicate the supplier indicator');
    assert.equal(await page.locator('[data-supplier-tab="products"]').count(), 0,
      'Saved supplier items are managed in the supplier file');
    assert.ok(await perceivedBackground(page, 'body') < 75, 'Dashboard has a dark background');

    const stamp = Date.now().toString(36);
    const bootstrap = await api(page, 'bootstrap');
    const department = bootstrap.departments[0];
    assert.ok(department, 'A department exists in the isolated test database');
    const supplier = await api(page, 'suppliers', 'POST', { name: 'مورد اختبار المظهر ' + stamp });
    const product = await api(page, 'products', 'POST', {
      supplier_id: supplier.id,
      department_id: department.id,
      name: 'خدمة اختبار المظهر ' + stamp,
      unit: 'زيارة',
      price: '100.05',
    });
    const invoiceData = {
      supplier_id: supplier.id,
      department_id: department.id,
      reference: 'VISUAL-' + stamp,
      request_key: 'visual-' + stamp,
      business_date: '2026-10-26',
      kind: 'service',
      tax_rate: '0',
      lines: [{ product_id: product.id, quantity: '1.5', price: '100.05' }],
    };
    await api(page, 'invoices', 'POST', invoiceData);
    const updatedDashboard = await api(page, 'dashboard');
    await page.locator('#refresh-data').click();
    await waitForAmount(page, updatedDashboard.total_cents);
    await page.waitForSelector('.connection-status[data-state="connected"]');
    // The displayed amount comes from the API, including the invoice created out of band.
    assert.equal(await page.locator('#stats .stat .value').first().innerText(),
      formatMoney(updatedDashboard.total_cents));
    await page.screenshot({ path: path.join(output, 'reference-dashboard.png'), fullPage: true });

    await page.locator('#modules [data-page="supplier"]').click();
    await page.locator(`[data-supplier-file="${supplier.id}"]`).click();
    await page.waitForSelector('#modal[open]');
    assert.ok((await page.locator('#modal-body').innerText()).includes(product.name),
      'The supplier file keeps the existing saved item visible');
    await page.locator(`#modal [data-product-supplier="${supplier.id}"]`).click();
    await page.waitForSelector('#picker-window[open]');
    assert.match(await page.locator('#picker-title').innerText(), /القسم الافتراضي/,
      'Adding from a supplier file skips the supplier picker');
    await page.locator(`[data-pick="${department.id}"]`).click();
    await page.waitForSelector('#record-window[open]');
    assert.ok((await page.locator('#record-title').innerText()).includes(supplier.name),
      'The new item form identifies the preselected supplier');
    const addedItemName = 'خدمة من ملف المورد ' + stamp;
    await page.locator('#record-form [name="name"]').fill(addedItemName);
    await page.locator('#record-form [name="unit"]').fill('شهر');
    await page.locator('#record-form [name="price"]').fill('250.00');
    await page.locator('#record-form button[type="submit"]').click();
    await page.waitForFunction(name => document.querySelector('#modal').open &&
      document.querySelector('#modal-body').innerText.includes(name), addedItemName);
    assert.ok((await page.locator('#modal-body').innerText()).includes(product.name),
      'Adding a new item preserves the previously saved item');
    const afterItemSave = await api(page, 'bootstrap');
    const addedItem = afterItemSave.products.find(item => item.name === addedItemName);
    assert.ok(addedItem, 'The added supplier item was persisted');
    assert.equal(addedItem.supplier_id, supplier.id, 'The item keeps the preselected supplier');
    assert.equal(addedItem.department_id, department.id, 'The selected department was persisted');
    assert.equal(addedItem.price_cents, 25000, 'The saved item price is accurate');
    await page.locator('#modal [data-close="modal"]').click();
    await page.locator('#supplier [data-new-invoice]').click();
    await page.waitForSelector('#entry-window[open]');
    assert.equal(await page.locator('#cancel-invoice').isVisible(), false,
      'A new unsaved draft does not expose persisted-record cancellation');
    assert.ok(await perceivedBackground(page, '#entry-window') > 205,
      'Entry window uses a light background over the dark dashboard');
    await page.locator('#vendor').click();
    await page.locator('[data-pick]').filter({ hasText: supplier.name }).click();
    await page.locator('[data-kind="service"]').click();
    await page.locator('#reference').fill('UNSAVED-VISUAL-' + stamp);
    await page.locator('#add-line').click();
    await page.locator('[data-pick]').filter({ hasText: product.name }).click();
    assert.match(await page.locator('#cost').innerText(), new RegExp(department.cost_code));
    assert.equal(await page.locator('select').count(), 0, 'Entry choices do not introduce dropdowns');
    await page.screenshot({ path: path.join(output, 'reference-invoice.png'), fullPage: true });

    // Escape must keep unsaved work when discard is declined, then close on acceptance.
    page.once('dialog', dialog => dialog.dismiss());
    await page.keyboard.press('Escape');
    assert.ok(await page.locator('#entry-window').evaluate(element => element.open),
      'Declining discard preserves the entry window');
    page.once('dialog', dialog => dialog.accept());
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('#entry-window').open);

    await page.locator('[data-page="dashboard"]').first().click();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
      'The dashboard does not overflow the phone viewport');
    await page.locator('#modules [data-page="supplier"]').click();
    await page.locator('#supplier [data-new-invoice]').click();
    assert.equal(await page.locator('#entry-window').evaluate(element => element.scrollWidth > element.clientWidth), false,
      'The input window does not overflow on a phone');
    await page.keyboard.press('Escape');
    assert.deepEqual(errors, [], 'No JavaScript page errors');
    console.log('PASS reference UI: consolidated supplier navigation and metrics, supplier-file item save, API-backed refresh, light entry, discard Escape and phone width.');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
