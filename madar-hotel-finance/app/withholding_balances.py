"""Dated withholding liabilities, remittances and independent ledger comparison.

Amounts are signed credit-minus-debit cents. A source reversed after remittance
keeps its remitted amount and becomes an over-remittance credit. Reports never
erase that credit or apply it across different historical tax accounts.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import accounting
import payables

ALL_TIME = '9999-12-31'


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _id(value, label='المستند'):
    if type(value) is not int or value < 1:
        raise ValueError('معرف ' + label + ' غير صحيح')
    return value


def _as_of(value=None):
    if value is None:
        try:
            value = datetime.now(ZoneInfo('Africa/Cairo')).date().isoformat()
        except ZoneInfoNotFoundError:
            value = date.today().isoformat()
    return payables._date(value, 'تاريخ التقرير')


def _period(period_from, period_to):
    start = payables._date(period_from, 'بداية فترة الخصم') if period_from is not None else None
    end = payables._date(period_to, 'نهاية فترة الخصم') if period_to is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError('بداية فترة الخصم لا تتجاوز نهايتها')
    return start, end


def _reversed_payment(db, payment_id, as_of):
    if not _has(db, 'supplier_payment_reversals'):
        return None
    row = db.execute('SELECT * FROM supplier_payment_reversals WHERE payment_id=? AND business_date<=?',
                     (payment_id, as_of)).fetchone()
    return dict(row) if row else None


def _remittance_reversal(db, remittance_id, as_of):
    if not _has(db, 'withholding_remittance_reversals'):
        return None
    row = db.execute('SELECT * FROM withholding_remittance_reversals WHERE remittance_id=? AND business_date<=?',
                     (remittance_id, as_of)).fetchone()
    return dict(row) if row else None


def _source_rows(db, as_of):
    if not (_has(db, 'payment_withholding') and _has(db, 'supplier_payments')):
        return []
    return [dict(row) for row in db.execute('''SELECT w.*,p.code payment_code,p.payment_date,p.journal_entry_id
        FROM payment_withholding w JOIN supplier_payments p ON p.id=w.payment_id
        WHERE p.status='posted' AND p.payment_date<=? ORDER BY p.payment_date,p.id''', (as_of,))]


def _remitted(db, as_of, payment_id=None, account_id=None, exclude_remittance_id=None):
    if not (_has(db, 'withholding_remittances') and _has(db, 'withholding_remittance_allocations')):
        return 0
    predicates, values = ["r.status='posted'", 'r.business_date<=?'], [as_of]
    if payment_id is not None:
        predicates.append('a.payment_id=?')
        values.append(payment_id)
    if account_id is not None:
        predicates.append('a.account_id=?')
        values.append(account_id)
    if exclude_remittance_id is not None:
        predicates.append('r.id<>?')
        values.append(exclude_remittance_id)
    if _has(db, 'withholding_remittance_reversals'):
        predicates.append('NOT EXISTS(SELECT 1 FROM withholding_remittance_reversals rr WHERE rr.remittance_id=r.id AND rr.business_date<=?)')
        values.append(as_of)
    return db.execute('''SELECT COALESCE(SUM(a.amount_cents),0) FROM withholding_remittance_allocations a
        JOIN withholding_remittances r ON r.id=a.remittance_id WHERE ''' + ' AND '.join(predicates), values).fetchone()[0]


def capacity_dates(db, business_date):
    """All document and accounting boundaries, including intermediate changes."""
    start = payables._date(business_date, 'تاريخ التوريد')
    dates = {start, ALL_TIME}
    if _has(db, 'journal_entries'):
        dates.update(row[0] for row in db.execute('SELECT DISTINCT entry_date FROM journal_entries WHERE sealed=1 AND entry_date>=?', (start,)))
    for table, field, clause in (
        ('supplier_payments', 'payment_date', " AND status='posted'"),
        ('supplier_payment_reversals', 'business_date', ''),
        ('withholding_remittances', 'business_date', " AND status='posted'"),
        ('withholding_remittance_reversals', 'business_date', ''),
    ):
        if _has(db, table):
            dates.update(row[0] for row in db.execute(f'SELECT DISTINCT {field} FROM {table} WHERE {field}>=?' + clause, (start,)))
    return sorted(dates)


def capacity(db, payment_id, business_date, exclude_remittance_id=None):
    payment_id = _id(payment_id, 'سداد مصدر الخصم')
    if exclude_remittance_id is not None:
        exclude_remittance_id = _id(exclude_remittance_id, 'التوريد المستبعد')
    row = db.execute('''SELECT w.withholding_cents,p.payment_date,p.status FROM payment_withholding w
        JOIN supplier_payments p ON p.id=w.payment_id WHERE w.payment_id=?''', (payment_id,)).fetchone() if _has(db, 'payment_withholding') else None
    if row is None or row['status'] != 'posted':
        raise ValueError('مصدر الخصم ليس سدادًا معتمدًا')
    amounts = []
    for day in capacity_dates(db, business_date):
        withheld = row['withholding_cents'] if row['payment_date'] <= day and not _reversed_payment(db, payment_id, day) else 0
        amounts.append(withheld - _remitted(db, day, payment_id=payment_id, exclude_remittance_id=exclude_remittance_id))
    return min(amounts)


def account_capacity(db, account_id, business_date, exclude_remittance_id=None):
    account_id = _id(account_id, 'حساب الخصم')
    if not db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
        raise ValueError('حساب الخصم غير موجود')
    if exclude_remittance_id is not None:
        exclude_remittance_id = _id(exclude_remittance_id, 'التوريد المستبعد')
    amounts = []
    for day in capacity_dates(db, business_date):
        withheld = sum(row['withholding_cents'] for row in _source_rows(db, day)
                       if row['account_id'] == account_id and not _reversed_payment(db, row['payment_id'], day))
        amounts.append(withheld - _remitted(db, day, account_id=account_id, exclude_remittance_id=exclude_remittance_id))
    return min(amounts)


def _account_ids(db):
    ids = set()
    if _has(db, 'account_mappings'):
        ids.update(row[0] for row in db.execute("SELECT account_id FROM account_mappings WHERE module='الضرائب' AND role='withholding_payable'"))
    if _has(db, 'payment_withholding'):
        ids.update(row[0] for row in db.execute('SELECT DISTINCT account_id FROM payment_withholding'))
    if _has(db, 'withholding_remittance_allocations'):
        ids.update(row[0] for row in db.execute('SELECT DISTINCT account_id FROM withholding_remittance_allocations'))
    return sorted(ids)


def _totals(sources):
    keys = ('original_cents', 'withheld_cents', 'remitted_cents', 'remaining_cents', 'due_cents', 'overpaid_cents')
    result = {key: sum(row[key] for row in sources) for key in keys}
    result.update(source_count=len(sources), original_withholding_cents=result['original_cents'],
                  effective_withholding_cents=result['withheld_cents'])
    balances = {}
    for row in sources:
        balances[row['account_id']] = balances.get(row['account_id'], 0) + row['remaining_cents']
    result['net_due_cents'] = sum(max(value, 0) for value in balances.values())
    result['account_credit_cents'] = sum(max(-value, 0) for value in balances.values())
    return result


def balance_report(db, as_of=None, period_from=None, period_to=None):
    """Independent source-document balances, with dated correction credits intact."""
    as_of = _as_of(as_of)
    period_from, period_to = _period(period_from, period_to)
    ids = _account_ids(db)
    account_rows = {row['id']: dict(row) for row in db.execute('SELECT id,code,name,posting FROM accounts')}
    suppliers = {row[0] for row in db.execute('SELECT id FROM suppliers')} if _has(db, 'suppliers') else set()
    exceptions, expected, events = [], {}, {}

    def add_event(journal_id, source_type, source_id, lines):
        if not lines:
            return
        if journal_id is None:
            exceptions.append({'type': 'missing_source_journal', 'message': 'مستند خصم أو توريد معتمد بلا قيد مصدر.', 'source_type': source_type, 'source_id': source_id, 'account_ids': sorted({line[0] for line in lines})})
        else:
            if journal_id in events and (events[journal_id]['source_type'], events[journal_id]['source_id']) != (source_type, source_id):
                exceptions.append({'type': 'shared_source_journal', 'message': 'قيد واحد مرتبط بأكثر من مصدر خصم أو توريد.', 'journal_id': journal_id})
            event = events.setdefault(journal_id, {'source_type': source_type, 'source_id': source_id, 'lines': {}})
            for account_id, supplier_id, debit, credit in lines:
                pair = (account_id, supplier_id)
                old = event['lines'].get(pair, (0, 0))
                event['lines'][pair] = (old[0] + debit, old[1] + credit)
        for account_id, supplier_id, debit, credit in lines:
            pair = (account_id, supplier_id)
            expected[pair] = expected.get(pair, 0) + credit - debit

    sources = {}
    for raw in _source_rows(db, as_of):
        payment_id, original = raw['payment_id'], raw['withholding_cents']
        reversal = _reversed_payment(db, payment_id, as_of)
        withheld = 0 if reversal else original
        source = {'payment_id': payment_id, 'payment_code': raw['payment_code'], 'code': raw['payment_code'],
                  'payment_date': raw['payment_date'], 'source_date': raw['payment_date'],
                  'account_id': raw['account_id'], 'supplier_id': raw['supplier_id'],
                  'supplier_name': raw['supplier_name'], 'supplier_code': raw['supplier_code'],
                  'supplier_tax_number': raw['supplier_tax_number'], 'withholding_code': raw['code'],
                  'withholding_name': raw['name'], 'rate_basis': raw['rate_basis'], 'rate': format(raw['rate_basis'] / 100, '.2f'),
                  'original_cents': original, 'original_withholding_cents': original,
                  'withheld_cents': withheld, 'effective_withholding_cents': withheld, 'remitted_cents': 0,
                  'source_reversed': reversal is not None, 'reversal_date': reversal['business_date'] if reversal else None,
                  'journal_id': raw['journal_entry_id'], 'remittance_ids': [], 'remittance_codes': []}
        account = account_rows.get(raw['account_id'], {})
        source.update(account_code=account.get('code', ''), account_name=account.get('name', 'حساب مفقود'))
        sources[payment_id] = source
        if original:
            add_event(raw['journal_entry_id'], 'supplier_payment', payment_id, [(raw['account_id'], raw['supplier_id'], 0, original)])
            if reversal:
                add_event(reversal['journal_id'], 'supplier_payment_reversal', reversal['id'], [(raw['account_id'], raw['supplier_id'], original, 0)])
        if raw['supplier_id'] not in suppliers:
            exceptions.append({'type': 'missing_source_supplier', 'message': 'مورد مصدر الخصم غير موجود.', 'payment_id': payment_id, 'supplier_id': raw['supplier_id']})

    orphan_remitted = 0
    if _has(db, 'withholding_remittances') and _has(db, 'withholding_remittance_allocations'):
        for raw in db.execute("SELECT * FROM withholding_remittances WHERE status='posted' AND business_date<=? ORDER BY business_date,id", (as_of,)):
            remittance = dict(raw)
            reversal = _remittance_reversal(db, remittance['id'], as_of)
            allocations = [dict(row) for row in db.execute('SELECT * FROM withholding_remittance_allocations WHERE remittance_id=? ORDER BY payment_id', (remittance['id'],))]
            if not allocations or sum(row['amount_cents'] for row in allocations) != remittance['amount_cents']:
                exceptions.append({'type': 'remittance_allocation_difference', 'message': 'إجمالي تخصيصات التوريد لا يطابق مستنده.', 'remittance_id': remittance['id'], 'journal_id': remittance['journal_id']})
            lines = []
            for allocation in allocations:
                amount = allocation['amount_cents']
                lines.append((allocation['account_id'], allocation['supplier_id'], amount, 0))
                source = sources.get(allocation['payment_id'])
                if source is None:
                    exceptions.append({'type': 'orphan_remittance_source', 'message': 'تخصيص توريد بلا مصدر خصم معتمد في تاريخ التقرير.', 'remittance_id': remittance['id'], 'payment_id': allocation['payment_id'], 'journal_id': remittance['journal_id'], 'account_id': allocation['account_id']})
                    if not reversal:
                        orphan_remitted += amount
                    continue
                if (allocation['account_id'], allocation['supplier_id']) != (source['account_id'], source['supplier_id']):
                    exceptions.append({'type': 'remittance_source_mismatch', 'message': 'حساب أو مورد تخصيص التوريد يختلف عن مصدر الخصم الأصلي.', 'remittance_id': remittance['id'], 'payment_id': source['payment_id'], 'account_id': allocation['account_id'], 'supplier_id': allocation['supplier_id']})
                if not reversal:
                    source['remitted_cents'] += amount
                    source['remittance_ids'].append(remittance['id'])
                    source['remittance_codes'].append(remittance['code'])
            add_event(remittance['journal_id'], 'withholding_remittance', remittance['id'], lines)
            if reversal:
                add_event(reversal['journal_id'], 'withholding_remittance_reversal', reversal['id'],
                          [(account_id, supplier_id, credit, debit) for account_id, supplier_id, debit, credit in lines])

    for row in sources.values():
        remaining = row['withheld_cents'] - row['remitted_cents']
        row.update(remaining_cents=remaining, due_cents=max(remaining, 0), overpaid_cents=max(-remaining, 0))
    accounts = {ident: {'id': ident, 'account_id': ident, 'code': account_rows.get(ident, {}).get('code', ''),
                        'name': account_rows.get(ident, {}).get('name', 'حساب مفقود'),
                        'debit_cents': 0, 'credit_cents': 0, 'ledger_balance_cents': 0,
                        'book_balance_cents': sum(amount for (account_id, _), amount in expected.items() if account_id == ident),
                        'journal_ids': set(), 'supplier_differences': []} for ident in ids}
    ledger, actual_events = {}, {}
    if ids and _has(db, 'journal_entries') and _has(db, 'journal_lines'):
        marks = ','.join('?' for _ in ids)
        rows = db.execute(f'''SELECT l.*,j.code journal_code,j.source_type,j.source_id,j.entry_date,j.sealed
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.entry_date<=? AND l.account_id IN({marks}) ORDER BY j.entry_date,j.id,l.id''', (as_of, *ids)).fetchall()
        for raw in rows:
            row = dict(raw)
            account = accounts[row['account_id']]
            account['journal_ids'].add(row['journal_id'])
            if not row['sealed']:
                exceptions.append({'type': 'unsealed_journal', 'message': 'قيد غير مكتمل على حساب الخصم مستبعد من الأرصدة.', 'journal_id': row['journal_id'], 'journal_code': row['journal_code'], 'account_id': row['account_id']})
                continue
            debit, credit = row['debit_cents'], row['credit_cents']
            account['debit_cents'] += debit
            account['credit_cents'] += credit
            pair = (row['account_id'], row['supplier_id'])
            ledger[pair] = ledger.get(pair, 0) + credit - debit
            actual = actual_events.setdefault(row['journal_id'], {'source_type': row['source_type'], 'source_id': row['source_id'], 'lines': {}})
            old = actual['lines'].get(pair, (0, 0))
            actual['lines'][pair] = (old[0] + debit, old[1] + credit)
            if row['supplier_id'] not in suppliers:
                exceptions.append({'type': 'missing_ledger_supplier', 'message': 'طرف حساب الخصم بلا مورد مسجل.', 'journal_id': row['journal_id'], 'line_id': row['id'], 'account_id': row['account_id'], 'supplier_id': row['supplier_id']})
            if row['account_id'] not in account_rows or not account_rows[row['account_id']]['posting'] or debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
                exceptions.append({'type': 'invalid_withholding_line', 'message': 'طرف خصم على حساب مفقود أو تجميعي أو بمبلغ غير صحيح.', 'journal_id': row['journal_id'], 'line_id': row['id'], 'account_id': row['account_id']})
        for journal_id in sorted(set(events) | set(actual_events)):
            expected_event, actual = events.get(journal_id), actual_events.get(journal_id)
            if expected_event is None:
                exceptions.append({'type': 'untracked_withholding_journal', 'message': 'قيد على حساب الخصم غير مرتبط بمستند خصم أو توريد مسجل.', 'journal_id': journal_id})
            elif actual is None or expected_event != actual:
                exceptions.append({'type': 'source_journal_difference', 'message': 'أطراف حساب الخصم في القيد تختلف عن مستند مصدرها.', 'journal_id': journal_id, 'source_type': expected_event['source_type'], 'source_id': expected_event['source_id']})
            journal = db.execute('''SELECT j.code,j.sealed,j.source_type,j.source_id,COUNT(l.id) line_count,
                COALESCE(SUM(l.debit_cents),0) debit_cents,COALESCE(SUM(l.credit_cents),0) credit_cents
                FROM journal_entries j LEFT JOIN journal_lines l ON l.journal_id=j.id WHERE j.id=? GROUP BY j.id''', (journal_id,)).fetchone()
            if journal is None or not journal['sealed'] or journal['line_count'] < 2 or journal['debit_cents'] <= 0 or journal['debit_cents'] != journal['credit_cents']:
                exceptions.append({'type': 'invalid_source_journal', 'message': 'قيد مصدر الخصم أو التوريد غير موجود أو غير مكتمل أو غير متوازن.', 'journal_id': journal_id})

    for ident, account in accounts.items():
        if ident not in account_rows:
            exceptions.append({'type': 'missing_withholding_account', 'message': 'حساب خصم تاريخي غير موجود في الشجرة.', 'account_id': ident})
        account['ledger_balance_cents'] = account['credit_cents'] - account['debit_cents']
        account['subledger_balance_cents'] = account['book_balance_cents']
        account['difference_cents'] = account['ledger_balance_cents'] - account['book_balance_cents']
        for supplier_id in sorted({supplier for account_id, supplier in set(expected) | set(ledger) if account_id == ident}, key=lambda value: (-1 if value is None else value)):
            book, actual = expected.get((ident, supplier_id), 0), ledger.get((ident, supplier_id), 0)
            account['supplier_differences'].append({'supplier_id': supplier_id, 'book_balance_cents': book,
                                                    'ledger_balance_cents': actual, 'difference_cents': actual - book,
                                                    'matched': actual == book})
        # A zero balance difference must not paint an affected account as matched
        # when offsetting untracked/corrupt journal sides still need review.
        related_journals = account['journal_ids'] | {journal_id for journal_id, event in events.items()
                                                    if any(pair[0] == ident for pair in event['lines'])}
        has_exception = any(item.get('account_id') == ident or ident in item.get('account_ids', [])
                            or item.get('journal_id') in related_journals for item in exceptions)
        account['journal_ids'] = sorted(related_journals)
        account['matched'] = account['difference_cents'] == 0 and all(item['matched'] for item in account['supplier_differences']) and not has_exception

    all_sources = list(sources.values())
    selected = [row for row in all_sources if (period_from is None or row['payment_date'] >= period_from) and (period_to is None or row['payment_date'] <= period_to)]
    totals, all_totals = _totals(selected), _totals(all_sources)
    all_totals['remitted_cents'] += orphan_remitted
    all_totals['remaining_cents'] -= orphan_remitted
    all_totals['net_due_cents'] = sum(max(account['book_balance_cents'], 0) for account in accounts.values())
    all_totals['account_credit_cents'] = sum(max(-account['book_balance_cents'], 0) for account in accounts.values())
    all_totals['ledger_balance_cents'] = sum(account['ledger_balance_cents'] for account in accounts.values())
    all_totals['book_balance_cents'] = sum(account['book_balance_cents'] for account in accounts.values())
    all_totals['difference_cents'] = all_totals['ledger_balance_cents'] - all_totals['book_balance_cents']
    matched = not exceptions and all(account['matched'] for account in accounts.values())
    return {'as_of': as_of, 'period_from': period_from, 'period_to': period_to,
            'currency': accounting.get_settings(db)['base_currency'], 'sources': selected, 'totals': totals,
            'all_totals': all_totals, 'accounts': sorted(accounts.values(), key=lambda row: row['code']),
            'matched': matched, 'exceptions': exceptions,
            'notes': ['الرصيد موجب عند وجود خصم مستحق، وسالب عند وجود توريد زائد بعد تصحيح المصدر.',
                      'محددات الفترة ترشح تاريخ سداد مصدر الخصم؛ المطابقة والحد الأقصى للتوريد يراجعان جميع الفترات.',
                      'الأرصدة الدائنة والمديونيات على حسابين تاريخيين مختلفين لا تسوى تلقائيًا بينهما.']}
