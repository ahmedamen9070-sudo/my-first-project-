"""Read-only reconciliation of dated supplier subledgers and control accounts.

The document subledger is computed independently by payables. Ledger figures come
only from sealed journal lines. A zero grand-total difference is insufficient:
individual supplier differences and unidentified control lines still fail review.
"""
from __future__ import annotations

import accounting
import payables


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(db, table):
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')} if _has_table(db, table) else set()


def _account_ids(db):
    """Keep original control accounts after a user changes today's mappings."""
    payable = set()
    advance = set()
    if _has_table(db, 'account_mappings'):
        for row in db.execute("SELECT role,account_id FROM account_mappings WHERE module='الموردون' AND role IN('payable_control','capital_payable_control','supplier_advances')"):
            (advance if row['role'] == 'supplier_advances' else payable).add(row['account_id'])
    if _has_table(db, 'invoice_postings'):
        payable.update(row[0] for row in db.execute('SELECT DISTINCT payable_account_id FROM invoice_postings'))
    if _has_table(db, 'journal_entries'):
        # These original source journals provide historical accounts even if the
        # current mapping no longer points to them. Cash funding is a credit in
        # a payment journal, so it cannot be mistaken for an advance asset here.
        payable.update(row[0] for row in db.execute("""SELECT DISTINCT l.account_id FROM journal_lines l
            JOIN journal_entries j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
            WHERE j.sealed=1 AND j.source_type='supplier_invoice' AND l.credit_cents>0 AND a.code LIKE '2%'"""))
        advance.update(row[0] for row in db.execute("""SELECT DISTINCT l.account_id FROM journal_lines l
            JOIN journal_entries j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id
            WHERE j.sealed=1 AND j.source_type='supplier_payment' AND l.debit_cents>0 AND a.code LIKE '1%'"""))
    for table in ('supplier_settlements', 'supplier_refunds'):
        columns = _columns(db, table)
        if {'source_type', 'source_account_id'} <= columns:
            advance.update(row[0] for row in db.execute(f"SELECT DISTINCT source_account_id FROM {table} WHERE source_type='advance' AND source_account_id IS NOT NULL"))
            payable.update(row[0] for row in db.execute(f"SELECT DISTINCT source_account_id FROM {table} WHERE source_type='return_credit' AND source_account_id IS NOT NULL"))
    return payable, advance


def _source_supplier(db, source_type, source_id):
    """Expected supplier when the journal links to a known source document."""
    direct = {
        'supplier_invoice': ('invoices', 'id'),
        'supplier_return': ('supplier_returns', 'id'),
        'supplier_payment': ('supplier_payments', 'id'),
        'supplier_cheque_clearance': ('supplier_payments', 'id'),
        'supplier_settlement': ('supplier_settlements', 'id'),
        'supplier_settlement_reversal': ('supplier_settlements', 'id'),
        'supplier_refund': ('supplier_refunds', 'id'),
    }
    if source_type == 'supplier_refund_reversal':
        if not _has_table(db, 'supplier_refund_reversals'):
            return None
        row = db.execute('SELECT r.supplier_id FROM supplier_refund_reversals rr JOIN supplier_refunds r ON r.id=rr.refund_id WHERE rr.id=?', (source_id,)).fetchone()
        return row['supplier_id'] if row else None
    if source_type in ('supplier_payment_reversal', 'supplier_cheque_bounce'):
        if not _has_table(db, 'supplier_payment_reversals'):
            return None
        row = db.execute('SELECT p.supplier_id FROM supplier_payment_reversals r JOIN supplier_payments p ON p.id=r.payment_id WHERE r.id=?', (source_id,)).fetchone()
        return row['supplier_id'] if row else None
    target = direct.get(source_type)
    if target is None:
        return None
    table, column = target
    if not {'supplier_id', column} <= _columns(db, table):
        return None
    row = db.execute(f'SELECT supplier_id FROM {table} WHERE {column}=?', (source_id,)).fetchone()
    return row['supplier_id'] if row else None



def _advance_documents(db, supplier_id, as_of, advance_ids):
    """Remaining document advances grouped by their original posting account."""
    balances, exceptions = {}, []
    if not _has_table(db, 'supplier_payments'):
        return balances, exceptions
    for payment in db.execute("SELECT * FROM supplier_payments WHERE supplier_id=? AND status='posted' AND payment_date<=?", (supplier_id, as_of)).fetchall():
        payment = dict(payment)
        if _has_table(db, 'supplier_payment_reversals') and db.execute('SELECT 1 FROM supplier_payment_reversals WHERE payment_id=? AND business_date<=?', (payment['id'], as_of)).fetchone():
            continue
        allocated = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment_allocations WHERE payment_id=?', (payment['id'],)).fetchone()[0]
        original = payment['amount_cents'] - allocated
        if original <= 0:
            continue
        used = 0
        if _has_table(db, 'supplier_settlements'):
            reverse_clause, values = '', [payment['id'], as_of]
            if _has_table(db, 'supplier_settlement_reversals'):
                reverse_clause = ' AND NOT EXISTS(SELECT 1 FROM supplier_settlement_reversals r WHERE r.settlement_id=s.id AND r.business_date<=?)'
                values.append(as_of)
            used = db.execute("SELECT COALESCE(SUM(s.amount_cents),0) FROM supplier_settlements s WHERE s.source_type='advance' AND s.source_id=? AND s.status='posted' AND s.business_date<=?" + reverse_clause, values).fetchone()[0]
        refunded = 0
        if _has_table(db, 'supplier_refunds'):
            refund_clause, refund_values = payables._refund_effective_filter(db, 'r', as_of)
            refunded = db.execute("SELECT COALESCE(SUM(r.amount_cents),0) FROM supplier_refunds r WHERE r.source_type='advance' AND r.source_id=? AND r.status='posted' AND r.business_date<=?" + refund_clause, (payment['id'], as_of, *refund_values)).fetchone()[0]
        rows = db.execute("""SELECT DISTINCT l.account_id,j.id journal_id FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.sealed=1 AND j.source_type='supplier_payment' AND j.source_id=? AND l.debit_cents>0""", (payment['id'],)).fetchall()
        account_rows = [row for row in rows if row['account_id'] in advance_ids]
        if len(account_rows) != 1:
            exceptions.append({'type': 'unresolved_advance_account', 'message': 'تعذر مطابقة حساب الدفعة المقدمة في قيد المصدر الأصلي.', 'supplier_id': supplier_id, 'payment_id': payment['id'], 'journal_id': rows[0]['journal_id'] if rows else None})
            continue
        remaining = original - used - refunded
        if remaining < 0:
            exceptions.append({'type': 'overused_advance_source', 'message': 'تسويات واستردادات الدفعة المقدمة تتجاوز قيمتها الأصلية.', 'supplier_id': supplier_id, 'payment_id': payment['id'], 'journal_id': account_rows[0]['journal_id']})
        account_id = account_rows[0]['account_id']
        balances[account_id] = balances.get(account_id, 0) + remaining
    return balances, exceptions


def report(db, as_of=None):
    """Return exact integer-cent differences without changing any document."""
    as_of = payables._as_of(as_of)
    payable_ids, advance_ids = _account_ids(db)
    all_ids = sorted(payable_ids | advance_ids)
    supplier_data = {row['id']: dict(row) for row in db.execute('SELECT id,code,name FROM suppliers ORDER BY name,id')}
    exceptions = []
    for account_id in sorted(payable_ids & advance_ids):
        exceptions.append({'type': 'overlapping_control_account', 'message': 'الحساب مستخدم لمراقبة الموردين والدفعات المقدمة معًا؛ راجع الربط.', 'account_id': account_id})
    account_rows = {row['id']: dict(row) for row in db.execute('SELECT id,code,name FROM accounts')}
    account_totals = {}
    for account_id in all_ids:
        account = account_rows.get(account_id)
        if account is None:
            exceptions.append({'type': 'missing_control_account', 'message': 'حساب مراقبة تاريخي غير موجود في الشجرة.', 'account_id': account_id})
            continue
        account_totals[account_id] = {**account, 'kind': 'advance' if account_id in advance_ids else 'payable', 'debit_cents': 0, 'credit_cents': 0, 'balance_cents': 0, 'subledger_balance_cents': 0, 'difference_cents': 0, 'matched': True, 'journal_ids': set()}
    ledger_by_supplier = {}
    control_journal_ids = set()
    lines = []
    if all_ids:
        marks = ','.join('?' for _ in all_ids)
        lines = db.execute(f'''SELECT l.*,j.code journal_code,j.source_type,j.source_id,j.entry_date
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.sealed=1 AND j.entry_date<=? AND l.account_id IN({marks})
            ORDER BY j.entry_date,j.id,l.id''', (as_of, *all_ids)).fetchall()
    totals = dict.fromkeys(('ledger_payable_cents', 'subledger_payable_cents', 'payable_difference_cents',
                           'ledger_advance_cents', 'subledger_advance_cents', 'advance_difference_cents',
                           'ledger_net_cents', 'subledger_net_cents', 'net_difference_cents',
                           'payable_debit_cents', 'payable_credit_cents', 'advance_debit_cents', 'advance_credit_cents',
                           'unassigned_payable_cents', 'unassigned_advance_cents'), 0)
    expected_cache = {}
    for raw in lines:
        line = dict(raw)
        control_journal_ids.add(line['journal_id'])
        is_advance = line['account_id'] in advance_ids
        kind = 'advance' if is_advance else 'payable'
        signed = (line['debit_cents'] - line['credit_cents']) if is_advance else (line['credit_cents'] - line['debit_cents'])
        totals['ledger_' + kind + '_cents'] += signed
        totals[kind + '_debit_cents'] += line['debit_cents']
        totals[kind + '_credit_cents'] += line['credit_cents']
        account = account_totals.get(line['account_id'])
        if account:
            account['debit_cents'] += line['debit_cents']
            account['credit_cents'] += line['credit_cents']
            account['balance_cents'] += signed
            account['journal_ids'].add(line['journal_id'])
        supplier_id = line['supplier_id']
        key = (line['source_type'], line['source_id'])
        if key not in expected_cache:
            expected_cache[key] = _source_supplier(db, *key)
        expected_supplier = expected_cache[key]
        if supplier_id not in supplier_data:
            totals['unassigned_' + kind + '_cents'] += signed
            exceptions.append({'type': 'missing_supplier' if supplier_id is None else 'unknown_supplier',
                               'message': 'طرف حساب المراقبة غير مربوط بمورد مسجل.',
                               'journal_id': line['journal_id'], 'journal_code': line['journal_code'],
                               'line_id': line['id'], 'account_id': line['account_id'], 'supplier_id': supplier_id,
                               'kind': kind, 'debit_cents': line['debit_cents'], 'credit_cents': line['credit_cents']})
            continue
        row = ledger_by_supplier.setdefault(supplier_id, {'payable_cents': 0, 'advance_cents': 0, 'journal_ids': set(), 'account_ids': set(), 'account_balances': {}, 'has_exception': False})
        row[kind + '_cents'] += signed
        row['journal_ids'].add(line['journal_id'])
        row['account_ids'].add(line['account_id'])
        row['account_balances'][line['account_id']] = row['account_balances'].get(line['account_id'], 0) + signed
        if expected_supplier is not None and expected_supplier != supplier_id:
            row['has_exception'] = True
            exceptions.append({'type': 'source_supplier_mismatch', 'message': 'مورد طرف القيد يختلف عن المورد في المستند الأصلي.',
                               'journal_id': line['journal_id'], 'journal_code': line['journal_code'], 'line_id': line['id'],
                               'account_id': line['account_id'], 'supplier_id': supplier_id, 'expected_supplier_id': expected_supplier})
    # A damaged sealed journal is never reported as reconciled just because its
    # control-account side happens to agree with documents.
    if control_journal_ids:
        marks = ','.join('?' for _ in control_journal_ids)
        for row in db.execute(f'''SELECT j.id journal_id,j.code journal_code,SUM(l.debit_cents) debit_cents,SUM(l.credit_cents) credit_cents
            FROM journal_entries j JOIN journal_lines l ON l.journal_id=j.id
            WHERE j.id IN({marks}) GROUP BY j.id HAVING SUM(l.debit_cents)<>SUM(l.credit_cents)''', tuple(sorted(control_journal_ids))):
            exceptions.append({'type': 'unbalanced_journal', 'message': 'قيد مرحّل مرتبط بالمطابقة غير متوازن.', **dict(row)})
    suppliers = []
    for supplier_id, supplier in supplier_data.items():
        balance = payables.supplier_balances(db, supplier_id, as_of)
        expected_accounts = {}
        for invoice in payables.open_invoices(db, supplier_id, as_of):
            original_account = db.execute('SELECT payable_account_id FROM invoice_postings WHERE invoice_id=?', (invoice['id'],)).fetchone()[0]
            expected_accounts[original_account] = expected_accounts.get(original_account, 0) + invoice['raw_outstanding_cents']
        expected_advances, source_exceptions = _advance_documents(db, supplier_id, as_of, advance_ids)
        exceptions.extend(source_exceptions)
        for account_id, amount in expected_advances.items():
            expected_accounts[account_id] = expected_accounts.get(account_id, 0) + amount
        ledger = ledger_by_supplier.get(supplier_id, {'payable_cents': 0, 'advance_cents': 0, 'journal_ids': set(), 'account_ids': set(), 'account_balances': {}, 'has_exception': False})
        payable = balance['payable_balance_cents']
        advance = balance['advance_cents']
        row = {'supplier_id': supplier_id, 'code': supplier['code'], 'name': supplier['name'],
               'ledger_payable_cents': ledger['payable_cents'], 'subledger_payable_cents': payable,
               'payable_difference_cents': ledger['payable_cents'] - payable,
               'ledger_advance_cents': ledger['advance_cents'], 'subledger_advance_cents': advance,
               'advance_difference_cents': ledger['advance_cents'] - advance,
               'ledger_net_cents': ledger['payable_cents'] - ledger['advance_cents'],
               'subledger_net_cents': payable - advance,
               'net_difference_cents': ledger['payable_cents'] - ledger['advance_cents'] - payable + advance,
               'journal_ids': sorted(ledger['journal_ids']), 'account_ids': sorted(ledger['account_ids'])}
        row['account_differences'] = []
        for account_id in sorted(set(expected_accounts) | ledger['account_ids']):
            actual = ledger['account_balances'].get(account_id, 0)
            expected = expected_accounts.get(account_id, 0)
            difference = actual - expected
            account = account_rows.get(account_id, {'id': account_id, 'code': '', 'name': ''})
            row['account_differences'].append({**account, 'kind': 'advance' if account_id in advance_ids else 'payable',
                'ledger_balance_cents': actual, 'subledger_balance_cents': expected, 'difference_cents': difference})
            if account_id in account_totals:
                account_totals[account_id]['subledger_balance_cents'] += expected
        row['matched'] = row['payable_difference_cents'] == row['advance_difference_cents'] == 0 and not ledger['has_exception'] and not source_exceptions and all(a['difference_cents'] == 0 for a in row['account_differences'])
        if ledger['journal_ids'] or payable or advance:
            suppliers.append(row)
        totals['subledger_payable_cents'] += payable
        totals['subledger_advance_cents'] += advance
    totals['payable_difference_cents'] = totals['ledger_payable_cents'] - totals['subledger_payable_cents']
    totals['advance_difference_cents'] = totals['ledger_advance_cents'] - totals['subledger_advance_cents']
    totals['ledger_net_cents'] = totals['ledger_payable_cents'] - totals['ledger_advance_cents']
    totals['subledger_net_cents'] = totals['subledger_payable_cents'] - totals['subledger_advance_cents']
    totals['net_difference_cents'] = totals['ledger_net_cents'] - totals['subledger_net_cents']
    for account in account_totals.values():
        account['journal_ids'] = sorted(account['journal_ids'])
        account['difference_cents'] = account['balance_cents'] - account['subledger_balance_cents']
        account['matched'] = account['difference_cents'] == 0
    source_rows = db.execute("SELECT id,source_type,source_id FROM journal_entries WHERE sealed=1 AND entry_date<=?", (as_of,)).fetchall()
    supplier_sources = [row for row in source_rows if row['source_type'].startswith('supplier_') or row['id'] in control_journal_ids]
    mismatched = sum(not row['matched'] for row in suppliers)
    matched = not exceptions and not mismatched and totals['payable_difference_cents'] == totals['advance_difference_cents'] == 0
    return {'as_of': as_of, 'currency': accounting.get_settings(db)['base_currency'], 'matched': matched,
            'totals': totals, 'suppliers': suppliers,
            'accounts': sorted(account_totals.values(), key=lambda row: (row['kind'], row['code'])),
            'exceptions': exceptions,
            'counts': {'supplier_count': len(suppliers), 'mismatched_suppliers': mismatched,
                       'journal_count': len(supplier_sources), 'control_journal_count': len(control_journal_ids),
                       'source_document_count': len({(row['source_type'], row['source_id']) for row in supplier_sources}),
                       'exception_count': len(exceptions), 'mismatched_accounts': sum(not a['matched'] for a in account_totals.values())},
            'notes': ['مراقبة الموردين: الرصيد الدائن موجب، والدفعات المقدمة: الرصيد المدين موجب.',
                      'الصافي يساوي مراقبة الموردين ناقص الدفعات المقدمة، مع الحركات المرحّلة حتى تاريخ المستند المحدد.',
                      'المطابقة تخص القيود المسجلة والأستاذ المساعد؛ لا تثبت مطابقة البنك أو اكتمال الأرصدة الافتتاحية.']}
