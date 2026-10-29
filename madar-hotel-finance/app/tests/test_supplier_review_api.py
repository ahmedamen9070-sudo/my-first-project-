"""Supplier review is a dated, read-only view alongside explicitly current drafts."""
import sqlite3
import unittest

import test_payables_api as base_api


class SupplierReviewAPI(unittest.TestCase):
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    invoice = base_api.PayablesAPI.invoice
    payment = base_api.PayablesAPI.payment

    def setUp(self):
        base_api.PayablesAPI.setUpClass.__func__(self.__class__)

    def tearDown(self):
        base_api.PayablesAPI.tearDownClass.__func__(self.__class__)

    def review(self, ds='2026-01-20', horizon=7):
        return self.request(f'supplier-review?as_of={ds}&horizon_days={horizon}')

    def stored_counts(self):
        with sqlite3.connect(self.tmp.name + '/api.sqlite') as db:
            return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                    for name in ('audit', 'journal_entries', 'journal_lines', 'invoices',
                                 'supplier_payments', 'supplier_settlements',
                                 'supplier_refunds', 'withholding_remittances')}

    def test_dated_liability_and_current_future_draft_do_not_write(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2026-01-01')
        self.payment(supplier, '30', '2026-01-15', mode='invoice', target=bill['id'])
        future = self.invoice(supplier, product, '9', '2026-02-01', post=False)
        before = self.stored_counts()
        early = self.review('2026-01-10')
        later = self.review('2026-01-20')
        self.assertEqual(early['position']['overdue_cents'], 10000)
        self.assertEqual(later['position']['overdue_cents'], 7000)
        self.assertEqual(later['position']['net_payable_cents'], 7000)
        self.assertTrue(all(early['checks'][key]['matched']
                            for key in ('suppliers', 'withholding')))
        self.assertTrue(early['checks']['trial_balance']['balanced'])
        drafts = later['queues']['drafts_current']
        self.assertEqual([(r['kind'], r['id']) for r in drafts], [('invoice', future['id'])])
        self.assertTrue(drafts[0]['future_dated'])
        self.assertTrue(later['notes'])
        self.assertEqual(before, self.stored_counts())

    def test_due_today_and_horizon_do_not_change_full_open_position(self):
        supplier, product = self.fixture(terms=0)
        self.invoice(supplier, product, '10', '2026-01-01', due_date='2026-01-20')
        self.invoice(supplier, product, '20', '2026-01-02', due_date='2026-01-22')
        self.invoice(supplier, product, '30', '2026-01-03', due_date='2026-02-10')
        zero = self.review(horizon=0)
        seven = self.review(horizon=7)
        self.assertEqual(zero['position']['due_today_cents'], 1000)
        self.assertEqual(zero['position']['due_soon_cents'], 0)
        self.assertEqual(seven['position']['due_soon_cents'], 2000)
        self.assertEqual(zero['position']['open_invoice_cents'], 6000)
        self.assertEqual(seven['position']['open_invoice_cents'], 6000)
        self.assertEqual(seven['counts']['due_later_count'], 1)

    def test_strict_filter_validation_and_valid_extremes(self):
        for query in ('horizon_days=-1', 'horizon_days=91', 'horizon_days=7.5',
                      'horizon_days=true', 'horizon_days=', 'horizon_days=007',
                      'horizon_days=999999999999999999999999999999',
                      'as_of=2026-02-30', 'as_of=20260120', 'as_of='):
            with self.subTest(query=query):
                result = self.request('supplier-review?' + query, expected=400)
                self.assertIn('error', result)
        for days in (0, 90):
            self.assertEqual(self.review(horizon=days)['horizon_days'], days)
        self.assertEqual(self.request('supplier-review')['currency'], 'EGP')

    def test_review_entry_and_assets_are_bundled(self):
        from pathlib import Path
        static = Path(__file__).resolve().parents[1] / 'static'
        html = (static / 'index.html').read_text()
        self.assertIn('data-supplier-review', html)
        self.assertIn('/supplier_review.js', html)
        self.assertIn('/supplier-review.css', html)
        for asset in ('supplier_review.js', 'supplier-review.css'):
            import urllib.request
            with urllib.request.urlopen(self.url.replace('/api/', '/') + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)


if __name__ == '__main__':
    unittest.main()
