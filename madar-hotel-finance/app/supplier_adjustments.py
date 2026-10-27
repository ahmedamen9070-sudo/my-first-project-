"""Append-only supplier refunds, payment reversals and cheque operational history.

Callers own the SQLite transaction. Approved source documents and their allocation
rows stay unchanged; dated reversal rows make their effect cease at that date.
"""
from __future__ import annotations

import hashlib
import json

import accounting
import accounting_periods
import payables


def _has(db, table):
    return payables._has_table(db, table)


def _require(db, table, ident):
    return payables._require(db, table, ident)


def _text(data, key, maximum=200, required=False):
    return payables._text(data, key, maximum, required)


def _date(value):
    return payables._date(value, 'تاريخ المستند')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _amount(data):
    # The existing payment parser performs exact decimal-string validation.
    import supplier_payments
    return supplier_payments._amount(data)


def initialize(db):
    for sql in [
        '''CREATE TABLE IF NOT EXISTS supplier_refunds(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,supplier_id INTEGER NOT NULL REFERENCES suppliers(id),source_type TEXT NOT NULL CHECK(source_type IN('advance','return_credit')),source_id INTEGER NOT NULL,business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',method TEXT NOT NULL CHECK(method IN('cash','bank_transfer')),funding_account_id INTEGER NOT NULL REFERENCES accounts(id),details TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),status TEXT NOT NULL CHECK(status IN('draft','posted','cancelled')),revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER REFERENCES journal_entries(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS supplier_payment_reversals(id INTEGER PRIMARY KEY,payment_id INTEGER NOT NULL UNIQUE REFERENCES supplier_payments(id),business_date TEXT NOT NULL,reason TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',event_type TEXT NOT NULL CHECK(event_type IN('payment_reverse','cheque_cancel','cheque_bounce')),journal_id INTEGER NOT NULL REFERENCES journal_entries(id),clearance_journal_id INTEGER REFERENCES journal_entries(id),request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS supplier_cheque_events(id INTEGER PRIMARY KEY,payment_id INTEGER NOT NULL REFERENCES supplier_payments(id),action TEXT NOT NULL CHECK(action IN('stop','resume','cancel','bounce')),business_date TEXT NOT NULL,reason TEXT NOT NULL,reference TEXT NOT NULL,reversal_id INTEGER REFERENCES supplier_payment_reversals(id),request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS supplier_refunds_source ON supplier_refunds(source_type,source_id,status,business_date)',
        'CREATE INDEX IF NOT EXISTS supplier_cheque_history ON supplier_cheque_events(payment_id,business_date,id)',
    ]:
        db.execute(sql)
    if 'reference' not in {r['name'] for r in db.execute('PRAGMA table_info(supplier_payment_reversals)')}:
        db.execute("ALTER TABLE supplier_payment_reversals ADD COLUMN reference TEXT NOT NULL DEFAULT ''")
    for table, condition in [('supplier_refunds', "OLD.status='posted'"), ('supplier_payment_reversals', '1'), ('supplier_cheque_events', '1')]:
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS adjustment_immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'approved supplier adjustment is immutable'); END''')


def refund_consumption(db, source_type, source_id, as_of=None):
    if not _has(db, 'supplier_refunds'):
        return 0
    as_of = payables._as_of(as_of)
    return db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM supplier_refunds WHERE source_type=? AND source_id=? AND status='posted' AND business_date<=?", (source_type, source_id, as_of)).fetchone()[0]


def payment_reversed(db, payment_id, as_of=None):
    if not _has(db, 'supplier_payment_reversals'):
        return False
    return db.execute('SELECT 1 FROM supplier_payment_reversals WHERE payment_id=? AND business_date<=?', (payment_id, payables._as_of(as_of))).fetchone() is not None


def _active_settlement_usage(db, source_type, source_id, as_of):
    if not _has(db, 'supplier_settlements'):
        return 0
    reverse_clause = ' AND NOT EXISTS(SELECT 1 FROM supplier_settlement_reversals r WHERE r.settlement_id=s.id AND r.business_date<=?)' if _has(db, 'supplier_settlement_reversals') else ''
    params = (source_type, source_id, as_of, as_of) if reverse_clause else (source_type, source_id, as_of)
    return db.execute("SELECT COALESCE(SUM(s.amount_cents),0) FROM supplier_settlements s WHERE s.source_type=? AND s.source_id=? AND s.status='posted' AND s.business_date<=?" + reverse_clause, params).fetchone()[0]


def _advance_account(db, payment):
    allocated_accounts = {r[0] for r in db.execute('SELECT DISTINCT p.payable_account_id FROM payment_allocations a JOIN invoice_postings p ON p.invoice_id=a.invoice_id WHERE a.payment_id=?', (payment['id'],))}
    lines = [dict(r) for r in db.execute('SELECT account_id,debit_cents FROM journal_lines WHERE journal_id=? AND debit_cents>0', (payment['journal_entry_id'],)) if r['account_id'] not in allocated_accounts]
    if len(lines) != 1 or lines[0]['debit_cents'] != payment['advance_cents']:
        raise ValueError('تعذر تحديد حساب الدفعة المقدمة من قيدها الأصلي')
    return lines[0]['account_id']


def _source(db, supplier_id, source_type, source_id, as_of):
    source_id = payables._id(source_id)
    if source_type == 'advance':
        payment = _require(db, 'supplier_payments', source_id)
        if payment['supplier_id'] != supplier_id or payment['status'] != 'posted' or not payment['advance_cents']:
            raise ValueError('الدفعة المقدمة ليست معتمدة لهذا المورد')
        available = 0
        if payment['payment_date'] <= as_of and not payment_reversed(db, source_id, as_of):
            available = payment['advance_cents'] - _active_settlement_usage(db, source_type, source_id, as_of) - refund_consumption(db, source_type, source_id, as_of)
        return {'source_type': source_type, 'source_id': source_id, 'source_code': payment['code'], 'source_date': payment['payment_date'], 'account_id': _advance_account(db, payment), 'available_cents': max(available, 0)}
    if source_type == 'return_credit':
        invoice = _require(db, 'invoices', source_id)
        posting = db.execute('SELECT * FROM invoice_postings WHERE invoice_id=?', (source_id,)).fetchone()
        if invoice['supplier_id'] != supplier_id or not posting:
            raise ValueError('رصيد المرتجع لا يتبع فاتورة معتمدة لهذا المورد')
        row = next((r for r in payables.open_invoices(db, supplier_id, as_of) if r['id'] == source_id), {})
        return {'source_type': source_type, 'source_id': source_id, 'source_code': invoice['code'], 'source_date': invoice['business_date'], 'account_id': posting['payable_account_id'], 'available_cents': row.get('return_credit_cents', 0)}
    raise ValueError('اختر دفعة مقدمة أو رصيد مرتجع')


def _refund_details(method, incoming):
    if not isinstance(incoming, dict):
        raise ValueError('تفاصيل استرداد المبلغ مطلوبة')
    allowed = {'received_by', 'receipt_reference'} if method == 'cash' else {'bank_name', 'transaction_reference', 'value_date'}
    if set(incoming) - allowed:
        raise ValueError('تفاصيل لا تخص طريقة الاسترداد المختارة')
    result = {}
    for key in allowed:
        result[key] = _text(incoming, key, 200, key != 'receipt_reference')
    if method == 'bank_transfer':
        _date(result['value_date'])
    return result


def _refund_payload(db, data):
    supplier_id = payables._id(data.get('supplier_id'))
    _require(db, 'suppliers', supplier_id)
    method = data.get('method')
    if method not in ('cash', 'bank_transfer'):
        raise ValueError('الاسترداد نقدي أو بتحويل بنكي')
    import supplier_payments
    funding = supplier_payments._funding(db, data.get('funding_account_id'), method)
    value = {'supplier_id': supplier_id, 'source_type': data.get('source_type'), 'source_id': payables._id(data.get('source_id')),
             'business_date': _date(data.get('business_date')), 'reference': _text(data, 'reference', 100, True),
             'reason': _text(data, 'reason', 2000, True), 'notes': _text(data, 'notes', 2000), 'method': method,
             'funding_account_id': funding['id'], 'details': _refund_details(method, data.get('details', {})), 'amount_cents': _amount(data)}
    return value


def _refund_preview(db, value):
    dated = _source(db, value['supplier_id'], value['source_type'], value['source_id'], value['business_date'])
    # A future consumption followed by reversal can have the same endpoints.
    # Check every approved accounting boundary, not only today and all-time.
    boundaries = {value['business_date'], '9999-12-31'}
    boundaries.update(r[0] for r in db.execute("SELECT DISTINCT entry_date FROM journal_entries WHERE sealed=1 AND entry_date>=?", (value['business_date'],)))
    available = min(_source(db, value['supplier_id'], value['source_type'], value['source_id'], day)['available_cents'] for day in boundaries)
    if value['business_date'] < dated['source_date']:
        raise ValueError('تاريخ الاسترداد لا يسبق مصدر الرصيد')
    if value['amount_cents'] > available:
        raise ValueError('مبلغ الاسترداد يتجاوز الرصيد المتاح؛ راجع التسويات والاستردادات السابقة')
    payables._posting_account(db, dated['account_id'])
    import supplier_payments
    before = supplier_payments.account_balance(db, value['funding_account_id'], value['business_date'])
    return {**dated, 'available_cents': available, 'amount_cents': value['amount_cents'], 'remaining_cents': available - value['amount_cents'],
            'funding_balance_before_cents': before, 'funding_balance_after_cents': before + value['amount_cents'], 'currency': 'EGP'}


def refund_preview(db, data):
    return _refund_preview(db, _refund_payload(db, data))


def save_refund(db, data, refund_id=None):
    if refund_id is not None:
        old = _require(db, 'supplier_refunds', refund_id)
        payables._revision(data, old)
        if old['status'] != 'draft':
            raise ValueError('استرداد معتمد أو ملغى لا يعدل')
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE supplier_refunds SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), refund_id))
            payables._audit(db, 'supplier_refund', refund_id, 'cancel_draft', {})
            return get_refund(db, refund_id)
    value = _refund_payload(db, data)
    digest = _fingerprint(value)
    key = _text(data, 'request_key', 100, True) if refund_id is None else old['request_key']
    if refund_id is None:
        prior = db.execute('SELECT * FROM supplier_refunds WHERE request_key=?', (key,)).fetchone()
        if prior:
            if prior['request_fingerprint'] != digest:
                raise accounting.ConflictError('مفتاح الحفظ مستخدم لاسترداد ببيانات أخرى')
            return get_refund(db, prior['id'])
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if refund_id is not None else None)
    _refund_preview(db, value)
    fields = ('supplier_id','source_type','source_id','business_date','reference','reason','notes','method','funding_account_id','details','amount_cents')
    values = [value[k] if k != 'details' else _json(value[k]) for k in fields]
    stamp = payables._now()
    if refund_id is None:
        code = payables._code(db, 'supplier_refund', 'SRF')
        refund_id = db.execute(f"INSERT INTO supplier_refunds(code,request_key,request_fingerprint,{','.join(fields)},status,created_at,updated_at) VALUES(?,?,?,{','.join('?' for _ in fields)},'draft',?,?)", (code, key, digest, *values, stamp, stamp)).lastrowid
    else:
        db.execute(f"UPDATE supplier_refunds SET {','.join(k+'=?' for k in fields)},revision=revision+1,updated_at=? WHERE id=?", (*values, stamp, refund_id))
    payables._audit(db, 'supplier_refund', refund_id, 'save_draft', {'amount_cents': value['amount_cents']})
    return get_refund(db, refund_id)


def approve_refund(db, refund_id, data):
    old = _require(db, 'supplier_refunds', refund_id)
    if old['status'] == 'posted':
        return get_refund(db, refund_id)
    payables._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('استرداد ملغى لا يعتمد')
    accounting_periods.assert_open(db, old['business_date'])
    value = {**old, 'details': json.loads(old['details'])}
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('سند الاسترداد محفوظ بالجنيه المصري؛ راجع العملة الأساسية')
    _refund_payload(db, value)
    preview = _refund_preview(db, value)
    journal = payables.create_journal(db, 'supplier_refund', refund_id, old['business_date'], [
        {'account_id': old['funding_account_id'], 'debit_cents': old['amount_cents'], 'supplier_id': old['supplier_id']},
        {'account_id': preview['account_id'], 'credit_cents': old['amount_cents'], 'supplier_id': old['supplier_id']},
    ])
    db.execute("UPDATE supplier_refunds SET status='posted',journal_id=?,revision=revision+1,updated_at=? WHERE id=?", (journal, payables._now(), refund_id))
    payables._audit(db, 'supplier_refund', refund_id, 'post', {'journal_id': journal})
    return get_refund(db, refund_id)


def get_refund(db, refund_id):
    result = _require(db, 'supplier_refunds', refund_id)
    result['details'] = json.loads(result['details'])
    supplier = _require(db, 'suppliers', result['supplier_id'])
    source_table = 'supplier_payments' if result['source_type'] == 'advance' else 'invoices'
    source = _require(db, source_table, result['source_id'])
    account = _require(db, 'accounts', result['funding_account_id'])
    result.update(supplier_name=supplier['name'], supplier_code=supplier['code'], source_code=source['code'], funding_account_name=account['name'], funding_account_code=account['code'],
                  voucher_title='إذن قبض من المورد' if result['method']=='cash' else 'إشعار استرداد بنكي من المورد')
    result['journal'] = payables.journal_detail(db, result['journal_id']) if result['journal_id'] else None
    result.pop('request_fingerprint', None)
    return result


def list_refunds(db, supplier_id=None):
    if supplier_id is not None:
        payables._id(supplier_id)
    return [get_refund(db, r[0]) for r in db.execute('SELECT id FROM supplier_refunds WHERE (? IS NULL OR supplier_id=?) ORDER BY business_date DESC,id DESC LIMIT 500', (supplier_id, supplier_id))]


def cheque_events(db, payment_id, as_of=None):
    if not _has(db, 'supplier_cheque_events'):
        return []
    return [dict(r) for r in db.execute('SELECT * FROM supplier_cheque_events WHERE payment_id=? AND business_date<=? ORDER BY business_date,id', (payment_id, payables._as_of(as_of)))]


def payment_overlay(db, payment, as_of=None):
    result = dict(payment)
    as_of = payables._as_of(as_of)
    reversal = dict(r) if (r := db.execute('SELECT * FROM supplier_payment_reversals WHERE payment_id=? AND business_date<=?', (result['id'], as_of)).fetchone()) else None
    if reversal:
        reversal.pop('request_fingerprint', None)
        reversal['code'] = f"SPR-{reversal['id']:06d}"
    events = cheque_events(db, result['id'], as_of)
    state = result.get('cheque_status') if result.get('method') == 'cheque' else None
    if state == 'cleared' and result.get('clearance_date') and result['clearance_date'] > as_of:
        state = 'issued'
    for event in events:
        event['code'] = f"SCE-{event['id']:06d}"
        if result.get('cheque_status') == 'cleared' and result.get('clearance_date') and result['clearance_date'] <= as_of and event['action'] in ('stop','resume') and event['business_date'] <= result['clearance_date']:
            event.pop('request_fingerprint', None)
            continue
        state = {'stop':'stopped','resume':'issued','cancel':'cancelled','bounce':'bounced'}[event['action']]
        event.pop('request_fingerprint', None)
    result.pop('request_fingerprint', None)
    result.update(effective_status='reversed' if reversal else result.get('status'), effective_cheque_status=state,
                  adjustment_revision=len(events), event_history=events, reversal=reversal)
    return result


def _event_revision(db, payment, data):
    payables._revision(data, payment)
    revision = db.execute('SELECT COUNT(*) FROM supplier_cheque_events WHERE payment_id=?', (payment['id'],)).fetchone()[0]
    if type(data.get('event_revision', 0)) is not int or data.get('event_revision', 0) != revision:
        raise accounting.ConflictError('حالة الشيك تغيرت؛ أعد فتح مستند السداد')


def assert_cheque_can_clear(db, payment_id, clearance_date):
    payment = _require(db, 'supplier_payments', payment_id)
    state = payment_overlay(db, payment, '9999-12-31')
    if state['effective_status'] == 'reversed' or state['effective_cheque_status'] in ('stopped','cancelled','bounced'):
        raise ValueError('الشيك موقوف أو ملغى أو مرتد؛ لا يمكن إثبات صرفه')
    events = cheque_events(db, payment_id, '9999-12-31')
    if events and clearance_date < events[-1]['business_date']:
        raise ValueError('تاريخ صرف الشيك لا يسبق آخر إجراء عليه')


def _assert_dependencies_unused(db, payment, business_date):
    sources = [('advance', payment['id'])]
    sources += [('return_credit', r[0]) for r in db.execute('SELECT invoice_id FROM payment_allocations WHERE payment_id=?', (payment['id'],))]
    for kind, ident in sources:
        if refund_consumption(db, kind, ident, '9999-12-31'):
            raise ValueError('توجد عملية استرداد مرتبطة برصيد السداد؛ راجع المستند المرتبط قبل عكس السداد')
        if not _has(db, 'supplier_settlements'):
            continue
        if not _has(db, 'supplier_settlement_reversals'):
            if _active_settlement_usage(db, kind, ident, '9999-12-31'):
                raise ValueError('توجد تسوية مرتبطة برصيد السداد؛ اعكس التسوية أولًا')
            continue
        # Reversing before a dependency's later reversal would invalidate the
        # historical interval in between even if all-time consumption is zero.
        rows = db.execute("SELECT s.business_date,r.business_date reversal_date FROM supplier_settlements s LEFT JOIN supplier_settlement_reversals r ON r.settlement_id=s.id WHERE s.source_type=? AND s.source_id=? AND s.status='posted'", (kind, ident))
        if any(r['reversal_date'] is None or r['reversal_date'] > business_date for r in rows):
            raise ValueError('توجد تسوية مرتبطة؛ اعكسها أولًا واختر تاريخ عكس السداد بعد عكس التسوية')


def _opposite_lines(db, journal_id):
    return [{'account_id': r['account_id'], 'debit_cents': r['credit_cents'], 'credit_cents': r['debit_cents'], 'supplier_id': r['supplier_id'], 'department_id': r['department_id'], 'source_line_id': r['source_line_id']}
            for r in db.execute('SELECT * FROM journal_lines WHERE journal_id=? ORDER BY id', (journal_id,))]


def _reverse_payload(payment_id, data, event_type):
    return {'payment_id': payment_id, 'business_date': _date(data.get('business_date')), 'reason': _text(data, 'reason', 2000, True), 'reference': _text(data, 'reference', 200), 'event_type': event_type}


def _existing_reversal(db, payment_id, data, event_type):
    key = _text(data, 'request_key', 100, True)
    value = _reverse_payload(payment_id, data, event_type)
    old = db.execute('SELECT * FROM supplier_payment_reversals WHERE payment_id=? OR request_key=?', (payment_id, key)).fetchone()
    if old:
        if old['request_key'] != key or old['request_fingerprint'] != _fingerprint(value):
            raise accounting.ConflictError('السداد معكوس بالفعل أو مفتاح الطلب مستخدم لإجراء آخر')
        return dict(old)
    return None


def _reverse(db, payment, data, event_type):
    value = _reverse_payload(payment['id'], data, event_type)
    accounting_periods.assert_open(db, value['business_date'])
    if value['business_date'] < payment['payment_date']:
        raise ValueError('تاريخ العكس لا يسبق تاريخ السداد')
    if event_type == 'cheque_bounce' and value['business_date'] < payment['clearance_date']:
        raise ValueError('تاريخ ارتداد الشيك لا يسبق صرفه')
    _assert_dependencies_unused(db, payment, value['business_date'])
    if event_type == 'cheque_bounce':
        payables._posting_account(db, payment['clearance_bank_account_id'])
    key = _text(data, 'request_key', 100, True)
    # BEGIN IMMEDIATE serializes callers. Reserve the next reversal ID without
    # inserting a mutable placeholder; the immutable row is written only once.
    reversal_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM supplier_payment_reversals').fetchone()[0]
    journal = payables.create_journal(db, 'supplier_payment_reversal', reversal_id, value['business_date'], _opposite_lines(db, payment['journal_entry_id']))
    clearance_journal = payables.create_journal(db, 'supplier_cheque_bounce', reversal_id, value['business_date'], _opposite_lines(db, payment['clearance_journal_entry_id'])) if event_type == 'cheque_bounce' else None
    db.execute('INSERT INTO supplier_payment_reversals(id,payment_id,business_date,reason,reference,event_type,journal_id,clearance_journal_id,request_key,request_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (reversal_id, payment['id'], value['business_date'], value['reason'], value['reference'], event_type, journal, clearance_journal, key, _fingerprint(value), payables._now()))
    payables._audit(db, 'supplier_payment_reversal', reversal_id, event_type, {'payment_id': payment['id'], 'journal_id': journal, 'clearance_journal_id': clearance_journal})
    return reversal_id


def _payment_detail(db, payment_id):
    import supplier_payments
    return supplier_payments.get_payment(db, payment_id)


def reverse_payment(db, payment_id, data):
    payment = _require(db, 'supplier_payments', payment_id)
    if _existing_reversal(db, payment_id, data, 'payment_reverse'):
        return _payment_detail(db, payment['id'])
    if payment['status'] != 'posted':
        raise ValueError('العكس متاح للسداد المعتمد فقط')
    if payment['method'] == 'cheque':
        raise ValueError('استخدم إجراء إلغاء أو ارتداد الشيك حسب حالته')
    _event_revision(db, payment, data)
    _reverse(db, payment, data, 'payment_reverse')
    return _payment_detail(db, payment['id'])


def cheque_event(db, payment_id, data):
    payment = _require(db, 'supplier_payments', payment_id)
    if payment['method'] != 'cheque' or payment['status'] != 'posted':
        raise ValueError('الإجراء متاح لشيك معتمد فقط')
    action = data.get('action')
    if action not in ('stop', 'resume', 'cancel', 'bounce'):
        raise ValueError('إجراء الشيك غير صحيح')
    key = _text(data, 'request_key', 100, True)
    value = {'payment_id': payment_id, 'action': action, 'business_date': _date(data.get('business_date')), 'reason': _text(data, 'reason', 2000, True), 'reference': _text(data, 'reference', 200, True)}
    old = db.execute('SELECT * FROM supplier_cheque_events WHERE request_key=?', (key,)).fetchone()
    if old:
        if old['request_fingerprint'] != _fingerprint(value):
            raise accounting.ConflictError('مفتاح الطلب مستخدم لإجراء شيك آخر')
        return _payment_detail(db, payment['id'])
    accounting_periods.assert_open(db, value['business_date'])
    _event_revision(db, payment, data)
    state = payment_overlay(db, payment, '9999-12-31')
    allowed = {'issued': {'stop','cancel'}, 'stopped': {'resume','cancel'}, 'cleared': {'bounce'}}
    if state['effective_status'] == 'reversed' or action not in allowed.get(state['effective_cheque_status'], set()):
        raise ValueError('إجراء غير مسموح في حالة الشيك الحالية')
    events = cheque_events(db, payment_id, '9999-12-31')
    earliest = max(payment['payment_date'], json.loads(payment['details'])['cheque_date'], events[-1]['business_date'] if events else payment['payment_date'])
    if payment['clearance_date']:
        earliest = max(earliest, payment['clearance_date'])
    if value['business_date'] < earliest:
        raise ValueError('تاريخ الإجراء لا يسبق إصدار الشيك أو آخر حركة عليه')
    reversal_id = None
    if action in ('cancel','bounce'):
        # Preserve the event request identity, but the reversal fingerprint covers financial facts only.
        reversal_id = _reverse(db, payment, data, 'cheque_cancel' if action == 'cancel' else 'cheque_bounce')
    db.execute('INSERT INTO supplier_cheque_events(payment_id,action,business_date,reason,reference,reversal_id,request_key,request_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (payment_id, action, value['business_date'], value['reason'], value['reference'], reversal_id, key, _fingerprint(value), payables._now()))
    payables._audit(db, 'supplier_payment', payment_id, 'cheque_' + action, {'business_date': value['business_date'], 'reference': value['reference'], 'reason': value['reason']})
    return _payment_detail(db, payment['id'])
