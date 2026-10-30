"""Nonposting bank statements, dated reconciliation and immutable certificates.

The caller owns the transaction. Matching never records bank movements; missing
transactions must be recorded in their original workflow before certification.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import banks
import ledger_reports
import payables
import treasury_reports

MAX_LINES = 5000


def _has(db, table):
    return ledger_reports._has(db, table)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('صيغة بيانات التسوية البنكية غير صحيحة') from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def initialize(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS bank_reconciliations(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,account_id INTEGER NOT NULL REFERENCES accounts(id),
            from_date TEXT NOT NULL,to_date TEXT NOT NULL,business_date TEXT NOT NULL,
            reference TEXT NOT NULL,payload_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,
            snapshot_fingerprint TEXT NOT NULL,evidence_fingerprint TEXT NOT NULL,
            account_code_snapshot TEXT NOT NULL,account_name_snapshot TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','confirmed','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),captured_at TEXT NOT NULL,
            confirmed_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS bank_reconciliation_voids(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,reconciliation_id INTEGER NOT NULL UNIQUE
            REFERENCES bank_reconciliations(id),request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,business_date TEXT NOT NULL,reference TEXT NOT NULL,
            reason TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS bank_reconciliation_matches(
            reconciliation_id INTEGER NOT NULL REFERENCES bank_reconciliations(id),
            book_line_id INTEGER NOT NULL REFERENCES journal_lines(id),group_no INTEGER NOT NULL,
            PRIMARY KEY(reconciliation_id,book_line_id))''',
        "CREATE UNIQUE INDEX IF NOT EXISTS one_bank_reconciliation_draft ON bank_reconciliations(account_id) WHERE status='draft'",
        'CREATE INDEX IF NOT EXISTS bank_reconciliations_dates ON bank_reconciliations(account_id,to_date,status)',
    ):
        db.execute(sql)
    for table, condition in (('bank_reconciliations', "OLD.status='confirmed'"),
                             ('bank_reconciliation_voids', '1')):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'confirmed bank reconciliation is immutable'); END''')
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_bank_match_{operation.lower()}
            BEFORE {operation} ON bank_reconciliation_matches
            WHEN EXISTS(SELECT 1 FROM bank_reconciliations WHERE id=OLD.reconciliation_id AND status='confirmed')
            BEGIN SELECT RAISE(ABORT,'confirmed bank match is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_bank_match_parent_{operation.lower()}
            BEFORE {operation} ON bank_reconciliation_matches
            WHEN EXISTS(SELECT 1 FROM bank_reconciliations WHERE id=NEW.reconciliation_id AND status='confirmed')
            BEGIN SELECT RAISE(ABORT,'confirmed bank match is immutable'); END''')


def _signed(data, key, cents_key=None, optional=False):
    cents_key = cents_key or key + '_cents'
    raw, cents = data.get(key), data.get(cents_key)
    if raw in (None, '') and cents is None:
        if optional:
            return None
        raise ValueError('أدخل القيمة: ' + key)
    if raw not in (None, ''):
        try:
            if isinstance(raw, (bool, float)):
                raise ValueError()
            number = Decimal(str(raw))
            if not number.is_finite() or abs(number) > Decimal(payables.MAX_CENTS) / 100 or number != number.quantize(Decimal('.01')):
                raise ValueError()
            parsed = int(number * 100)
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            raise ValueError('أدخل مبلغًا صحيحًا بدقة القرش، دون تقريب: ' + key) from None
        if cents is not None and (type(cents) is not int or cents != parsed):
            raise ValueError('قيمة المبلغ لا تطابق القروش: ' + key)
        cents = parsed
    if type(cents) is not int or abs(cents) > payables.MAX_CENTS:
        raise ValueError('المبلغ خارج النطاق أو ليس عدد قروش صحيحًا: ' + key)
    return cents


def _ids(value, label):
    if not isinstance(value, list) or len(value) > MAX_LINES:
        raise ValueError('قائمة ' + label + ' غير صحيحة')
    result = [payables._id(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError('عنصر مكرر في ' + label)
    return sorted(result)


def _alias(data, first, second, default):
    if first in data and second in data and data[first] != data[second]:
        raise ValueError('قيم متعارضة للحقلين ' + first + ' و' + second)
    return data.get(first, data.get(second, default))


def _normalize(data):
    if not isinstance(data, dict) or data.get('currency', 'EGP') != 'EGP':
        raise ValueError('التسوية البنكية الحالية بالجنيه المصري فقط')
    start = payables._date(data.get('from_date'), 'بداية كشف البنك')
    end = payables._date(data.get('to_date'), 'نهاية كشف البنك')
    if start > end:
        raise ValueError('بداية كشف البنك لا تلي نهايته')
    raw_lines = data.get('lines', [])
    if not isinstance(raw_lines, list) or len(raw_lines) > MAX_LINES:
        raise ValueError('كشف البنك يقبل حتى خمسة آلاف بند')
    lines, keys = [], set()
    for index, raw in enumerate(raw_lines, 1):
        if not isinstance(raw, dict):
            raise ValueError('بند كشف البنك غير صحيح')
        line_key = payables._text(raw, 'line_key', 100) or str(index)
        if line_key in keys:
            raise ValueError('معرف بند كشف البنك مكرر')
        keys.add(line_key)
        ds = payables._date(raw.get('date'), 'تاريخ بند الكشف')
        if not start <= ds <= end:
            raise ValueError('تاريخ بند الكشف خارج فترة كشف البنك')
        value_date = payables._date(raw.get('value_date') or ds, 'تاريخ قيمة بند الكشف')
        cents = _signed(raw, 'amount')
        if not cents:
            raise ValueError('بند كشف البنك يحتاج حركة غير صفرية')
        line = dict(line_key=line_key, date=ds, value_date=value_date,
                    reference=payables._text(raw, 'reference', 200), description=payables._text(raw, 'description', 1000),
                    amount_cents=cents)
        balance = _signed(raw, 'balance', optional=True)
        if balance is not None:
            line['balance_cents'] = balance
        lines.append(line)
    if (sum(max(row['amount_cents'], 0) for row in lines) > payables.MAX_CENTS
            or sum(max(-row['amount_cents'], 0) for row in lines) > payables.MAX_CENTS):
        raise ValueError('إجمالي حركات الكشف المدينة أو الدائنة يتجاوز النطاق المسموح')
    key_by_id = {i: row['line_key'] for i, row in enumerate(lines, 1)}
    raw_groups = _alias(data, 'matches', 'groups', [])
    if not isinstance(raw_groups, list) or len(raw_groups) > MAX_LINES:
        raise ValueError('مجموعات المطابقة غير صحيحة')
    groups, seen_statement, seen_book = [], set(), set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError('مجموعة المطابقة غير صحيحة')
        if 'statement_line_keys' in raw:
            statement_keys = raw['statement_line_keys']
            if not isinstance(statement_keys, list) or any(not isinstance(k, str) or k not in keys for k in statement_keys):
                raise ValueError('بند الكشف المختار للمطابقة غير موجود')
            if 'statement_line_ids' in raw:
                stated_ids = _ids(raw['statement_line_ids'], 'بنود الكشف')
                if any(i not in key_by_id for i in stated_ids) or sorted(statement_keys) != sorted(key_by_id[i] for i in stated_ids):
                    raise ValueError('معرفات بنود الكشف متعارضة')
        else:
            statement_ids = _ids(raw.get('statement_line_ids', []), 'بنود الكشف')
            if any(i not in key_by_id for i in statement_ids):
                raise ValueError('بند الكشف المختار للمطابقة غير موجود')
            statement_keys = [key_by_id[i] for i in statement_ids]
        book_ids = _ids(_alias(raw, 'book_line_ids', 'ledger_line_ids', []), 'الحركات الدفترية')
        if not statement_keys or not book_ids or len(statement_keys) != len(set(statement_keys)):
            raise ValueError('كل مجموعة مطابقة تحتاج بنود كشف وحركات دفترية غير مكررة')
        if seen_statement.intersection(statement_keys) or seen_book.intersection(book_ids):
            raise ValueError('الحركة لا تستخدم في أكثر من مجموعة مطابقة')
        seen_statement.update(statement_keys); seen_book.update(book_ids)
        groups.append({'statement_line_keys': sorted(statement_keys), 'book_line_ids': book_ids,
                       'note': payables._text(raw, 'note', 1000) or payables._text(raw, 'notes', 1000)})
    prior_ids = _ids(_alias(data, 'opening_outstanding_line_ids', 'prior_ledger_line_ids', []), 'الحركات القائمة أول الفترة')
    return {'account_id': payables._id(data.get('account_id')), 'from_date': start, 'to_date': end,
            'reference': payables._text(data, 'reference', 100, True), 'currency': 'EGP',
            'opening_balance_cents': _signed(data, 'opening_balance'), 'closing_balance_cents': _signed(data, 'closing_balance'),
            'prepared_by': payables._text(data, 'prepared_by', 150), 'reviewed_by': payables._text(data, 'reviewed_by', 150),
            'notes': payables._text(data, 'notes', 2000), 'lines': lines, 'matches': groups,
            'opening_outstanding_line_ids': prior_ids}


def _active(db, account_id, exclude=0):
    return [dict(row) for row in db.execute('''SELECT r.* FROM bank_reconciliations r
        WHERE r.account_id=? AND r.status='confirmed' AND r.id<>?
        AND NOT EXISTS(SELECT 1 FROM bank_reconciliation_voids v WHERE v.reconciliation_id=r.id)
        ORDER BY r.to_date,r.id''', (account_id, exclude))]


def _book(db, account_id, end):
    scope = '''j.entry_date<=? AND EXISTS(SELECT 1 FROM journal_lines b WHERE b.journal_id=j.id AND b.account_id=?)'''
    headers = [dict(row) for row in db.execute('SELECT j.* FROM journal_entries j WHERE ' + scope + ' ORDER BY j.entry_date,j.id', (end, account_id))]
    full_lines = [dict(row) for row in db.execute('SELECT l.* FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id WHERE ' + scope + ' ORDER BY j.entry_date,j.id,l.id', (end, account_id))]
    sealed = {row['id']: row for row in headers if row['sealed']}
    rows = []
    for raw in full_lines:
        if raw['account_id'] != account_id or raw['journal_id'] not in sealed:
            continue
        journal = sealed[raw['journal_id']]
        row = {**raw, 'line_id': raw['id'], 'entry_date': journal['entry_date'], 'date': journal['entry_date'],
               'journal_code': journal['code'], 'source_type': journal['source_type'], 'source_id': journal['source_id'],
               'amount_cents': raw['debit_cents'] - raw['credit_cents']}
        row.update(ledger_reports._document(db, row['source_type'], row['source_id'], journal['code']))
        row.update(treasury_reports._origin(db, row['source_type'], row['source_id'], row['journal_id']))
        rows.append(row)
    return {'rows': rows, 'book_cents': sum(row['amount_cents'] for row in rows),
            'evidence_fingerprint': _hash({'account_id': account_id, 'to_date': end, 'journals': headers, 'lines': full_lines}),
            'exceptions': ledger_reports._exceptions(db, end, account_id),
            'evidence': {'journal_count': len(headers), 'line_count': len(full_lines), 'bank_line_count': len(rows)}}


def _snapshot(row):
    try:
        snapshot = json.loads(row['snapshot_json'])
    except (ValueError, TypeError):
        raise accounting.ConflictError('بيانات محضر التسوية غير مكتملة؛ أعد مراجعتها') from None
    if not isinstance(snapshot, dict) or _hash(snapshot) != row['snapshot_fingerprint']:
        raise accounting.ConflictError('بيانات محضر التسوية تغيرت؛ أعد مراجعتها')
    return snapshot


def _pending_cheques(db, account_id, end):
    rows = []
    if _has(db, 'opening_bank_cheques'):
        for row in banks.list_opening_cheques(db, end):
            if row['bank_account_id'] == account_id and row['effective_status'] == 'outstanding':
                rows.append({'kind': 'opening_cheque', 'id': row['id'], 'code': row['code'], 'number': row['cheque_number'],
                             'amount_cents': row['amount_cents'], 'due_date': row['due_date'], 'party_name': row['beneficiary'], 'beneficiary': row['beneficiary'],
                             'cheque_number': row['cheque_number'], 'action': {'type': 'opening_bank_cheque', 'id': row['id']}})
    if _has(db, 'supplier_payments'):
        import supplier_adjustments
        import supplier_payments
        import withholding
        for raw in db.execute("SELECT id,payment_date,code,details,amount_cents FROM supplier_payments WHERE method='cheque' AND status='posted' AND funding_account_id=? AND payment_date<=?", (account_id, end)):
            row = dict(raw)
            payment = supplier_adjustments.payment_overlay(db, supplier_payments.get_payment(db, row['id']), end)
            if payment['effective_status'] != 'posted' or payment['effective_cheque_status'] not in ('issued', 'stopped'):
                continue
            details = json.loads(row['details'])
            rows.append({'kind': 'supplier_cheque', 'id': row['id'], 'code': row['code'],
                         'number': details.get('cheque_number', ''), 'amount_cents': withholding.net_cash(db, row['id'], row['amount_cents']),
                         'due_date': details.get('due_date'), 'party_name': details.get('beneficiary', ''),
                         'cheque_number': details.get('cheque_number', ''), 'beneficiary': details.get('beneficiary', ''),
                         'action': {'type': 'payment', 'id': row['id']}})
    return rows


def _plan(db, value, reconciliation_id=0):
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('اضبط العملة الأساسية EGP قبل التسوية البنكية')
    account = banks._bank_account(db, value['account_id'])
    start, end = value['from_date'], value['to_date']
    book = _book(db, account['id'], end)
    blockers = []
    def block(code, message, count=1):
        blockers.append({'code': code, 'message': message, 'count': count})
    opening = accounting.opening_status(db, end)
    if opening['status'] != 'posted':
        block('opening_not_confirmed', 'اعتمد كشف الأرصدة الافتتاحية من الإعدادات قبل اعتماد أول تسوية بنكية.')
    elif start < opening['business_date']:
        block('before_opening', 'بداية كشف البنك لا تسبق تاريخ الافتتاح المعتمد: ' + opening['business_date'])
    active = _active(db, account['id'], reconciliation_id)
    previous = active[-1] if active else None
    used = {row['book_line_id'] for row in db.execute('''SELECT m.book_line_id FROM bank_reconciliation_matches m
        JOIN bank_reconciliations r ON r.id=m.reconciliation_id WHERE r.status='confirmed' AND r.id<>?
        AND NOT EXISTS(SELECT 1 FROM bank_reconciliation_voids v WHERE v.reconciliation_id=r.id)''', (reconciliation_id,))}
    by_id = {row['line_id']: row for row in book['rows']}
    previous_info, carry_ids, previous_snapshot = None, [], None
    if previous:
        prior_review = review(db, previous['to_date'])
        if not prior_review['matched']:
            block('previous_integrity', 'محاضر التسوية السابقة أو روابطها تحتاج مراجعة قبل اعتماد فترة تالية.', len(prior_review['exceptions']))
        previous_snapshot = _snapshot(previous)
        previous_info = {'id': previous['id'], 'code': previous['code'], 'from_date': previous['from_date'],
                         'to_date': previous['to_date'], 'closing_balance_cents': previous_snapshot['totals']['statement_closing_cents']}
        try:
            next_start = (date.fromisoformat(previous['to_date']) + timedelta(days=1)).isoformat()
        except OverflowError:
            raise ValueError('لا توجد فترة تالية بعد آخر تاريخ مسموح') from None
        if start != next_start:
            block('non_contiguous', 'الفترة التالية تبدأ مباشرة بعد آخر تسوية معتمدة: ' + next_start)
        if value['opening_balance_cents'] != previous_info['closing_balance_cents']:
            block('opening_not_carried', 'رصيد أول الكشف يجب أن يساوي رصيد نهاية كشف البنك السابق.')
        if previous['evidence_fingerprint'] != _book(db, account['id'], previous['to_date'])['evidence_fingerprint']:
            block('previous_changed', 'قيود فترة التسوية السابقة تغيرت؛ راجع وأبطل آخر محضر قبل متابعة التسوية.')
        carry_ids = [row['line_id'] for row in previous_snapshot['unmatched_book_lines']]
        if value['opening_outstanding_line_ids'] and value['opening_outstanding_line_ids'] != sorted(carry_ids):
            block('carry_changed', 'الحركات القائمة أول الفترة ترحل تلقائيًا من التسوية السابقة دون تغيير.')
    else:
        carry_ids = value['opening_outstanding_line_ids']
    prior_pool = [row for row in book['rows'] if row['entry_date'] < start and row['source_type'] != 'opening_balance' and row['line_id'] not in used]
    prior_by_id = {row['line_id']: row for row in prior_pool}
    if any(ident not in prior_by_id for ident in carry_ids):
        block('invalid_carry', 'إحدى الحركات القائمة أول الفترة ليست حركة دفترية متاحة لهذا البنك.')
    carry = [prior_by_id[ident] for ident in carry_ids if ident in prior_by_id]
    current = [row for row in book['rows'] if row['entry_date'] >= start and row['source_type'] != 'opening_balance']
    candidates = carry + current
    candidate_by_id = {row['line_id']: row for row in candidates}
    opening_book = sum(row['amount_cents'] for row in book['rows'] if row['entry_date'] < start or (row['entry_date'] == start and row['source_type'] == 'opening_balance'))
    opening_outstanding = sum(row['amount_cents'] for row in carry)
    opening_difference = opening_book - opening_outstanding - value['opening_balance_cents']
    if opening_difference:
        block('opening_difference', 'رصيد بداية الدفاتر بعد الحركات القائمة لا يطابق رصيد بداية كشف البنك.')
    statement_lines = [{**row, 'id': index, 'line_no': index} for index, row in enumerate(value['lines'], 1)]
    statement_by_key = {row['line_key']: row for row in statement_lines}
    statement_close = value['opening_balance_cents'] + sum(row['amount_cents'] for row in statement_lines)
    if statement_close != value['closing_balance_cents']:
        block('statement_arithmetic', 'رصيد أول كشف البنك وحركاته لا يساوي رصيد نهايته المدخل.')
    running = value['opening_balance_cents']
    running_errors = []
    for row in statement_lines:
        running += row['amount_cents']
        if 'balance_cents' in row and row['balance_cents'] != running:
            running_errors.append(row['id'])
    if running_errors:
        block('running_balance', 'الرصيد الجاري ببنود الكشف لا يطابق اتجاه الحركات ومبالغها.', len(running_errors))
    matches, matched_statement, matched_book = [], set(), set()
    for index, group in enumerate(value['matches'], 1):
        statement = [statement_by_key[key] for key in group['statement_line_keys']]
        selected = [candidate_by_id[ident] for ident in group['book_line_ids'] if ident in candidate_by_id]
        if len(selected) != len(group['book_line_ids']) or any(ident in used for ident in group['book_line_ids']):
            block('invalid_match', 'حركة مطابقة غير متاحة أو سبق مطابقتها في محضر آخر.')
        amounts = [row['amount_cents'] for row in statement + selected]
        same_direction = amounts and (all(n > 0 for n in amounts) or all(n < 0 for n in amounts))
        statement_sum, book_sum = sum(row['amount_cents'] for row in statement), sum(row['amount_cents'] for row in selected)
        valid = same_direction and statement_sum == book_sum and len(selected) == len(group['book_line_ids']) and not used.intersection(group['book_line_ids'])
        if not valid:
            block('match_difference', 'كل مجموعة تحتاج حركات بالاتجاه نفسه وإجمالي كشف يساوي إجمالي الدفاتر.')
        matched_statement.update(group['statement_line_keys']); matched_book.update(group['book_line_ids'])
        matches.append({**group, 'group_no': index, 'statement_line_ids': [row['id'] for row in statement],
                        'ledger_line_ids': group['book_line_ids'], 'amount_cents': statement_sum,
                        'statement_cents': statement_sum, 'book_cents': book_sum, 'matched': bool(valid)})
    unmatched_statement = [row for row in statement_lines if row['line_key'] not in matched_statement]
    unmatched_book = [row for row in candidates if row['line_id'] not in matched_book]
    if unmatched_statement:
        block('unmatched_statement', 'راجع كل بند بكشف البنك وسجل الحركة الناقصة من مصدرها ثم طابقها قبل الاعتماد.', len(unmatched_statement))
    unmatched_book_cents = sum(row['amount_cents'] for row in unmatched_book)
    unmatched_statement_cents = sum(row['amount_cents'] for row in unmatched_statement)
    adjusted = value['closing_balance_cents'] + unmatched_book_cents
    difference = adjusted - book['book_cents']
    if difference:
        block('closing_difference', 'رصيد كشف البنك المعدل بالحركات الدفترية القائمة لا يطابق الأستاذ العام.')
    if book['exceptions']:
        block('ledger_exceptions', 'توجد قيود بنكية غير مكتملة أو غير متوازنة تحتاج مراجعة.', len(book['exceptions']))
    if not value['prepared_by'] or not value['reviewed_by']:
        block('review_names', 'أدخل اسم معد التسوية ومراجعها قبل الاعتماد.')
    try:
        accounting_periods.assert_open(db, end)
    except ValueError as error:
        block('closed_period', str(error))
    suggestions = []
    for statement in unmatched_statement:
        options = [row for row in unmatched_book if row['amount_cents'] == statement['amount_cents']]
        if len(options) == 1:
            suggestions.append({'statement_line_ids': [statement['id']], 'statement_line_keys': [statement['line_key']],
                                'book_line_ids': [options[0]['line_id']], 'ledger_line_ids': [options[0]['line_id']],
                                'amount_cents': statement['amount_cents'], 'note': 'نفس المبلغ والاتجاه؛ راجع التاريخ والمرجع قبل المطابقة.'})
    totals = {'book_opening_cents': opening_book, 'statement_opening_cents': value['opening_balance_cents'],
              'opening_outstanding_cents': opening_outstanding, 'opening_difference_cents': opening_difference,
              'book_closing_cents': book['book_cents'], 'statement_closing_cents': value['closing_balance_cents'],
              'statement_calculated_closing_cents': statement_close, 'statement_movement_cents': statement_close-value['opening_balance_cents'],
              'unmatched_book_cents': unmatched_book_cents, 'unmatched_statement_cents': unmatched_statement_cents,
              'adjusted_statement_cents': adjusted, 'difference_cents': difference,
              'statement_count': len(statement_lines), 'book_count': len(candidates), 'match_count': len(matches),
              'matched_statement_count': len(matched_statement), 'matched_book_count': len(matched_book),
              'unmatched_statement_count': len(unmatched_statement), 'unmatched_book_count': len(unmatched_book),
              'pending_deposit_cents': sum(max(row['amount_cents'], 0) for row in unmatched_book),
              'pending_withdrawal_cents': sum(max(-row['amount_cents'], 0) for row in unmatched_book)}
    return {**value, 'lines': statement_lines, 'account_code': account['code'], 'account_name': account['name'],
            'book_lines': candidates, 'ledger_candidates': candidates, 'available_prior_lines': prior_pool if previous is None else [],
            'matches': matches, 'groups': matches, 'previous': previous_info,
            'unmatched_statement_lines': unmatched_statement, 'unmatched_book_lines': unmatched_book,
            'suggestions': suggestions, 'totals': totals, 'blockers': blockers, 'can_confirm': not blockers,
            'evidence_fingerprint': book['evidence_fingerprint'], 'evidence': book['evidence'],
            'exceptions': book['exceptions'], 'opening_status': opening, 'pending_cheques': _pending_cheques(db, account['id'], end),
            'warnings': ['المطابقة ومحضرها لا ينشئان قيدًا أو تسوية مالية تلقائية.',
                         'الحركات الدفترية القائمة ترحل للتسوية التالية؛ الشيك الصادر قبل صرفه معلومة مستقلة ولا يخصم من الرصيد البنكي مرة ثانية.']}


def preview(db, data):
    ident = data.get('reconciliation_id', data.get('id', 0)) if isinstance(data, dict) else 0
    if ident:
        ident = payables._id(ident)
    return _plan(db, _normalize(data), ident)


def config(db):
    accounts = banks.config(db)['bank_accounts']
    latest = {}
    for account in accounts:
        active = _active(db, account['id'])
        if active:
            old = active[-1]
            latest[str(account['id'])] = {'id': old['id'], 'code': old['code'], 'from_date': old['from_date'],
                                        'to_date': old['to_date'], 'closing_balance_cents': _snapshot(old)['totals']['statement_closing_cents']}
    return {'bank_accounts': accounts, 'accounts': accounts, 'currency': 'EGP',
            'opening_status': accounting.opening_status(db), 'latest_by_account': latest,
            'notes': ['ابدأ ببيانات كشف البنك الفعلي، ثم طابق جميع بنوده بالقيود المسجلة.',
                      'أي مصروف أو إيداع مفقود يسجل أولًا في دورته الأصلية؛ المطابقة لا تكرر الحركة.']}


def _ensure_slot(db, account_id, ident=0):
    if db.execute("SELECT 1 FROM bank_reconciliations WHERE account_id=? AND status='draft' AND id<>?", (account_id, ident)).fetchone():
        raise accounting.ConflictError('توجد مسودة تسوية لهذا البنك؛ افتحها واستكملها أو ألغها أولًا')


def save(db, data, reconciliation_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات التسوية البنكية غير صحيحة')
    old = payables._require(db, 'bank_reconciliations', reconciliation_id) if reconciliation_id else None
    if old:
        if old['status'] == 'cancelled' and data.get('action') == 'cancel':
            return detail(db, old['id'])
        if old['status'] != 'draft':
            raise ValueError('محضر التسوية المعتمد أو الملغى لا يعدل؛ الإبطال بمستند مستقل')
        payables._revision(data, old)
        accounting_periods.assert_open(db, old['to_date'])
        if data.get('action') == 'cancel':
            db.execute("UPDATE bank_reconciliations SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), old['id']))
            accounting._audit(db, 'bank_reconciliation', old['id'], 'cancel_draft', {'financial_posting': False})
            return detail(db, old['id'])
    value = _normalize(data)
    key = payables._text(data, 'request_key', 100, old is None) or old['request_key']
    fingerprint = _hash(value)
    prior = db.execute('SELECT * FROM bank_reconciliations WHERE request_key=?', (key,)).fetchone()
    if prior and old is None:
        if prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('مفتاح الطلب مستخدم لكشف بنك ببيانات مختلفة')
        return detail(db, prior['id'])
    if old and key != old['request_key']:
        raise accounting.ConflictError('مفتاح المسودة المحفوظة لا يتغير')
    if old and value['account_id'] != old['account_id']:
        raise ValueError('حساب البنك لا يتغير بعد حفظ المسودة؛ ألغها وابدأ مسودة للبنك الصحيح')
    _ensure_slot(db, value['account_id'], old['id'] if old else 0)
    accounting_periods.assert_document_dates(db, value['to_date'], old['to_date'] if old else None)
    plan = _plan(db, value, old['id'] if old else 0)
    active = _active(db, value['account_id'], old['id'] if old else 0)
    if active and value['from_date'] <= active[-1]['to_date']:
        raise ValueError('كشف البنك يتداخل مع فترة تسوية معتمدة؛ أبطل آخر محضر أولًا للتصحيح')
    for prior in db.execute('''SELECT r.id FROM bank_reconciliations r WHERE r.account_id=? AND lower(r.reference)=lower(?)
            AND r.status<>'cancelled' AND r.id<>? AND NOT EXISTS(SELECT 1 FROM bank_reconciliation_voids v WHERE v.reconciliation_id=r.id)''',
            (value['account_id'], value['reference'], old['id'] if old else 0)):
        raise accounting.ConflictError('مرجع كشف البنك مستخدم؛ افتح محضره المحفوظ')
    stamp = payables._now()
    fields = (fingerprint, value['account_id'], value['from_date'], value['to_date'], value['to_date'], value['reference'],
              _json(value), _json(plan), _hash(plan), plan['evidence_fingerprint'], plan['account_code'], plan['account_name'], stamp, stamp)
    if old:
        db.execute('''UPDATE bank_reconciliations SET request_fingerprint=?,account_id=?,from_date=?,to_date=?,business_date=?,reference=?,
            payload_json=?,snapshot_json=?,snapshot_fingerprint=?,evidence_fingerprint=?,account_code_snapshot=?,account_name_snapshot=?,captured_at=?,updated_at=?,revision=revision+1 WHERE id=?''', (*fields, old['id']))
        ident = old['id']
    else:
        ident = db.execute('''INSERT INTO bank_reconciliations(code,request_key,request_fingerprint,account_id,from_date,to_date,business_date,reference,
            payload_json,snapshot_json,snapshot_fingerprint,evidence_fingerprint,account_code_snapshot,account_name_snapshot,captured_at,updated_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (payables._code(db, 'bank_reconciliation', 'BRS'), key, *fields, stamp)).lastrowid
    accounting._audit(db, 'bank_reconciliation', ident, 'save_draft' if old is None else 'update_draft', {'financial_posting': False})
    return detail(db, ident)


def confirm(db, reconciliation_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات اعتماد التسوية غير صحيحة')
    old = payables._require(db, 'bank_reconciliations', reconciliation_id)
    if old['status'] == 'confirmed':
        if type(data.get('revision')) is not int or data['revision'] not in (old['revision'], old['revision'] - 1):
            raise accounting.ConflictError('أعد فتح محضر التسوية المعتمد')
        return detail(db, old['id'])
    if old['status'] != 'draft':
        raise ValueError('اعتماد التسوية متاح للمسودة فقط')
    payables._revision(data, old)
    accounting_periods.assert_open(db, old['to_date'])
    value = _normalize(json.loads(old['payload_json']))
    if _hash(value) != old['request_fingerprint']:
        raise accounting.ConflictError('بيانات مسودة كشف البنك تغيرت؛ أعد حفظها')
    saved = _snapshot(old)
    plan = _plan(db, value, old['id'])
    if _hash(plan) != old['snapshot_fingerprint'] or old['evidence_fingerprint'] != plan['evidence_fingerprint']:
        raise accounting.ConflictError('تغيرت القيود أو أرصدة التسوية؛ حدّث المسودة واحفظها قبل الاعتماد')
    if not plan['can_confirm']:
        raise ValueError(' '.join(blocker['message'] for blocker in plan['blockers']))
    for group in plan['matches']:
        db.executemany('INSERT INTO bank_reconciliation_matches(reconciliation_id,book_line_id,group_no) VALUES(?,?,?)',
                       [(old['id'], ident, group['group_no']) for ident in group['book_line_ids']])
    stamp = payables._now()
    db.execute("UPDATE bank_reconciliations SET status='confirmed',revision=revision+1,confirmed_at=?,updated_at=? WHERE id=?", (stamp, stamp, old['id']))
    accounting._audit(db, 'bank_reconciliation', old['id'], 'confirm', {'financial_posting': False, 'book_cents': saved['totals']['book_closing_cents']})
    return detail(db, old['id'])


def void(db, reconciliation_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات إبطال التسوية غير صحيحة')
    old = payables._require(db, 'bank_reconciliations', reconciliation_id)
    value = {'reconciliation_id': old['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ الإبطال'),
             'reference': payables._text(data, 'reference', 100, True), 'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    fingerprint = _hash(value)
    prior = db.execute('SELECT * FROM bank_reconciliation_voids WHERE reconciliation_id=? OR request_key=?', (reconciliation_id, key)).fetchone()
    if prior:
        if prior['reconciliation_id'] != reconciliation_id or prior['request_key'] != key or prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('المحضر مبطل أو مفتاح الطلب مستخدم لبيانات أخرى')
        return detail(db, reconciliation_id)
    if old['status'] != 'confirmed':
        raise ValueError('الإبطال لمحضر تسوية معتمد فقط')
    payables._revision(data, old)
    latest = _active(db, old['account_id'])
    if not latest or latest[-1]['id'] != old['id']:
        raise ValueError('أبطل أحدث تسوية بنكية لهذا الحساب أولًا')
    if value['business_date'] < old['to_date']:
        raise ValueError('إبطال المحضر لا يسبق نهاية فترة تسويته')
    accounting_periods.assert_open(db, value['business_date'])
    stamp = payables._now()
    db.execute('''INSERT INTO bank_reconciliation_voids(code,reconciliation_id,request_key,request_fingerprint,business_date,reference,reason,created_at)
        VALUES(?,?,?,?,?,?,?,?)''', (payables._code(db, 'bank_reconciliation_void', 'BRSV'), old['id'], key, fingerprint,
                                  value['business_date'], value['reference'], value['reason'], stamp))
    accounting._audit(db, 'bank_reconciliation', old['id'], 'void', {**value, 'financial_posting': False})
    return detail(db, old['id'])


def detail(db, reconciliation_id, as_of=None):
    if as_of is not None:
        as_of = payables._date(as_of, 'تاريخ الموقف')
    row = payables._require(db, 'bank_reconciliations', reconciliation_id)
    value = json.loads(row['payload_json'])
    snapshot = _snapshot(row)
    live = _book(db, row['account_id'], row['to_date'])
    void_row = db.execute('SELECT * FROM bank_reconciliation_voids WHERE reconciliation_id=?', (row['id'],)).fetchone()
    event = dict(void_row) if void_row else None
    if event:
        event.pop('request_fingerprint', None)
    effective_void = bool(event and (as_of is None or event['business_date'] <= payables._date(as_of, 'تاريخ الموقف')))
    result = {**value, **{key: row[key] for key in ('id', 'code', 'request_key', 'status', 'revision', 'from_date', 'to_date', 'business_date', 'reference', 'captured_at', 'confirmed_at', 'created_at', 'updated_at')},
              'account_code': row['account_code_snapshot'], 'account_name': row['account_name_snapshot'],
              'lines': snapshot['lines'], 'matches': snapshot['matches'], 'groups': snapshot['matches'],
              'book_lines': snapshot['book_lines'], 'ledger_candidates': snapshot['book_lines'],
              'snapshot': snapshot, 'totals': snapshot['totals'], 'void': event,
              'effective_status': 'voided' if effective_void else row['status'],
              'live_book_cents': live['book_cents'], 'book_changed': live['evidence_fingerprint'] != row['evidence_fingerprint'],
              'live_exceptions': live['exceptions'], 'evidence_fingerprint': row['evidence_fingerprint']}
    if row['status'] == 'draft':
        try:
            result['preview'] = _plan(db, _normalize(value), row['id'])
        except ValueError as error:
            result['preview_error'] = str(error)
    return result


def list_reconciliations(db, account_id=None, as_of=None):
    end = payables._date(as_of, 'تاريخ الموقف') if as_of else None
    values, where = [], []
    if account_id is not None:
        where.append('r.account_id=?'); values.append(payables._id(account_id))
    if end:
        where.append('r.to_date<=?'); values.append(end)
    sql = 'SELECT r.* FROM bank_reconciliations r' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY r.to_date DESC,r.id DESC'
    result = []
    for row in db.execute(sql, values):
        event = db.execute('SELECT * FROM bank_reconciliation_voids WHERE reconciliation_id=?', (row['id'],)).fetchone()
        effective_void = event and (end is None or event['business_date'] <= end)
        snapshot = _snapshot(dict(row))
        result.append({key: row[key] for key in ('id', 'code', 'account_id', 'from_date', 'to_date', 'business_date', 'reference', 'status', 'revision', 'captured_at', 'confirmed_at')}
                      | {'account_code': row['account_code_snapshot'], 'account_name': row['account_name_snapshot'],
                         'effective_status': 'voided' if effective_void else row['status'], 'totals': snapshot['totals'],
                         'void': {'id': event['id'], 'code': event['code'], 'business_date': event['business_date']} if event else None})
    return result


def assert_journal_open(db, entry_date, lines):
    """Account-scoped lock; journal idempotent replay must be checked first."""
    if not (_has(db, 'bank_reconciliations') and _has(db, 'bank_reconciliation_voids')):
        return
    accounts = {row.get('account_id') for row in lines if isinstance(row, dict)}
    for account_id in accounts:
        row = db.execute('''SELECT r.code,r.to_date FROM bank_reconciliations r
            WHERE r.account_id=? AND r.status='confirmed' AND r.to_date>=?
            AND NOT EXISTS(SELECT 1 FROM bank_reconciliation_voids v WHERE v.reconciliation_id=r.id)
            ORDER BY r.to_date DESC,r.id DESC LIMIT 1''', (account_id, entry_date)).fetchone()
        if row:
            raise ValueError('حساب البنك مقفل بمحضر التسوية ' + row['code'] + ' حتى ' + row['to_date'] + '؛ أبطل أحدث محضر أولًا للتصحيح')


def review(db, as_of=None):
    """Compact diagnostics for monthly review, never a mandatory bank schedule."""
    end = payables._date(as_of or '9999-12-31', 'تاريخ المراجعة')
    if not _has(db, 'bank_reconciliations'):
        return {'matched': True, 'certificate_count': 0, 'exceptions': []}
    exceptions, count, seen = [], 0, {}
    for row in db.execute('''SELECT r.* FROM bank_reconciliations r WHERE r.status='confirmed' AND r.to_date<=?
            AND NOT EXISTS(SELECT 1 FROM bank_reconciliation_voids v WHERE v.reconciliation_id=r.id)
            ORDER BY r.account_id,r.to_date,r.id''', (end,)):
        row = dict(row); count += 1
        try:
            snapshot = _snapshot(row)
            payload = _normalize(json.loads(row['payload_json']))
            if (_hash(payload) != row['request_fingerprint'] or snapshot.get('account_id') != row['account_id']
                    or snapshot.get('from_date') != row['from_date'] or snapshot.get('to_date') != row['to_date']
                    or not snapshot.get('can_confirm') or snapshot.get('evidence_fingerprint') != row['evidence_fingerprint']):
                raise ValueError('هوية المحضر أو بيانات اعتماده لا تطابق النسخة المحفوظة')
            book = _book(db, row['account_id'], row['to_date'])
            if book['evidence_fingerprint'] != row['evidence_fingerprint'] or book['exceptions']:
                raise ValueError('قيود فترة المحضر تغيرت أو بها استثناءات تحتاج مراجعة')
            expected = {(ident, group['group_no']) for group in snapshot['matches'] for ident in group['book_line_ids']}
            actual = {(match['book_line_id'], match['group_no']) for match in db.execute('SELECT * FROM bank_reconciliation_matches WHERE reconciliation_id=?', (row['id'],))}
            if expected != actual:
                raise ValueError('روابط المطابقة لا تطابق بنود المحضر المعتمد')
            for ident, _ in expected:
                if ident in seen:
                    raise ValueError('حركة دفترية مرتبطة بمحضرين معتمدين')
                seen[ident] = row['id']
        except (ValueError, TypeError, KeyError) as error:
            exceptions.append({'type': 'bank_reconciliation_integrity', 'reconciliation_id': row['id'],
                               'code': row['code'], 'account_id': row['account_id'], 'to_date': row['to_date'],
                               'message': str(error)})
    return {'matched': not exceptions, 'certificate_count': count, 'exceptions': exceptions}
