"""Apply supplier advances and return credits without changing their source documents.

All money is integer cents. The caller owns the transaction; approval and reversal
create sealed, balanced journals and immutable source/allocation snapshots.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import payables

ALL_TIME = '9999-12-31'
SOURCE_TYPES = ('advance', 'return_credit')


def _has(db, table):
    return payables._has_table(db, table)


def initialize(db):
    for statement in [
        '''CREATE TABLE IF NOT EXISTS supplier_settlements(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            source_type TEXT NOT NULL CHECK(source_type IN('advance','return_credit')),source_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,reference TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            source_account_id INTEGER REFERENCES accounts(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS settlement_allocations(
            settlement_id INTEGER NOT NULL REFERENCES supplier_settlements(id),
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            PRIMARY KEY(settlement_id,invoice_id))''',
        '''CREATE TABLE IF NOT EXISTS supplier_settlement_reversals(
            settlement_id INTEGER PRIMARY KEY REFERENCES supplier_settlements(id),
            business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS active_settlement_reference ON supplier_settlements(supplier_id,reference)
            WHERE status<>'cancelled' ''',
        'CREATE INDEX IF NOT EXISTS settlements_source ON supplier_settlements(source_type,source_id,business_date)',
        'CREATE INDEX IF NOT EXISTS settlements_target ON settlement_allocations(invoice_id)',
    ]:
        db.execute(statement)
    for table, alias, condition in [
        ('supplier_settlements', 'OLD', "OLD.status='posted'"),
        ('settlement_allocations', 'OLD', "EXISTS(SELECT 1 FROM supplier_settlements WHERE id=OLD.settlement_id AND status='posted')"),
        ('supplier_settlement_reversals', 'OLD', '1'),
    ]:
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS settlements_immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'posted supplier settlement is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS settlements_immutable_allocation_{operation.lower()}_parent
            BEFORE {operation} ON settlement_allocations
            WHEN EXISTS(SELECT 1 FROM supplier_settlements WHERE id=NEW.settlement_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted supplier settlement allocations are immutable'); END''')


def _active_clause(alias='s'):
    return f"{alias}.status='posted' AND {alias}.business_date<=? AND NOT EXISTS(SELECT 1 FROM supplier_settlement_reversals sr WHERE sr.settlement_id={alias}.id AND sr.business_date<=?)"


def source_used(db, source_type, source_id, as_of=None):
    """Posted source consumption net of dated reversal documents."""
    if not _has(db, 'supplier_settlements'):
        return 0
    as_of = payables._as_of(as_of)
    return db.execute(f'''SELECT COALESCE(SUM(s.amount_cents),0) FROM supplier_settlements s
        WHERE s.source_type=? AND s.source_id=? AND {_active_clause()}''',
                      (source_type, source_id, as_of, as_of)).fetchone()[0]


def _refund_used(db, source_type, source_id, as_of):
    if not _has(db, 'supplier_refunds'):
        return 0
    # Refund documents are append-only; a later refund correction is another source
    # operation and is reflected explicitly by the adjustments module.
    return db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM supplier_refunds
        WHERE source_type=? AND source_id=? AND status='posted' AND business_date<=?""",
                      (source_type, source_id, as_of)).fetchone()[0]


def _payment_reversed(db, payment_id, as_of):
    return _has(db, 'supplier_payment_reversals') and db.execute(
        'SELECT 1 FROM supplier_payment_reversals WHERE payment_id=? AND business_date<=?',
        (payment_id, as_of)).fetchone() is not None


def _advance_account(db, payment):
    journal_id = payment.get('journal_entry_id')
    if not journal_id:
        journal = db.execute("SELECT id FROM journal_entries WHERE source_type='supplier_payment' AND source_id=? AND sealed=1", (payment['id'],)).fetchone()
        journal_id = journal['id'] if journal else None
    if not journal_id:
        raise ValueError('قيد الدفعة المقدمة الأصلي غير موجود')
    rows = db.execute('''SELECT l.account_id,SUM(l.debit_cents) amount_cents FROM journal_lines l
        JOIN accounts a ON a.id=l.account_id WHERE l.journal_id=? AND l.debit_cents>0 AND a.code LIKE '1%'
        GROUP BY l.account_id''', (journal_id,)).fetchall()
    if len(rows) != 1:
        raise ValueError('تعذر تحديد حساب الدفعة المقدمة من قيدها الأصلي')
    return rows[0]['account_id']


def source_balances(db, supplier_id, as_of=None):
    supplier = payables._require(db, 'suppliers', supplier_id)
    as_of = payables._as_of(as_of)
    sources = []
    if _has(db, 'supplier_payments'):
        for record in db.execute("SELECT * FROM supplier_payments WHERE supplier_id=? AND status='posted' AND payment_date<=? ORDER BY payment_date,id", (supplier_id, as_of)).fetchall():
            payment = dict(record)
            if _payment_reversed(db, payment['id'], as_of):
                continue
            allocated = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payment_allocations WHERE payment_id=?', (payment['id'],)).fetchone()[0]
            original = payment['amount_cents'] - allocated
            if original <= 0:
                continue
            used = source_used(db, 'advance', payment['id'], as_of)
            refunded = _refund_used(db, 'advance', payment['id'], as_of)
            available = max(original - used - refunded, 0)
            account_id = _advance_account(db, payment)
            sources.append({'source_type': 'advance', 'source_id': payment['id'], 'supplier_id': supplier_id,
                            'code': payment['code'], 'reference': payment['code'], 'source_date': payment['payment_date'],
                            'business_date': payment['payment_date'], 'original_cents': original,
                            'settled_cents': used, 'refunded_cents': refunded, 'available_cents': available,
                            'account_id': account_id})
    # The invoice report already includes incoming/outgoing settlements, refunds
    # and dated payment reversals. Reading its remaining credit avoids consuming a
    # credit again and lets a later return create a new credit after settlement.
    for invoice in payables.open_invoices(db, supplier_id, as_of):
        used = source_used(db, 'return_credit', invoice['id'], as_of)
        refunded = _refund_used(db, 'return_credit', invoice['id'], as_of)
        available = invoice['return_credit_cents']
        original = available + used + refunded
        if original <= 0:
            continue
        posting = db.execute('SELECT payable_account_id FROM invoice_postings WHERE invoice_id=?', (invoice['id'],)).fetchone()
        sources.append({'source_type': 'return_credit', 'source_id': invoice['id'], 'supplier_id': supplier_id,
                        'code': invoice['code'], 'reference': invoice['reference'], 'source_date': invoice['business_date'],
                        'business_date': invoice['business_date'], 'original_cents': original,
                        'settled_cents': used, 'refunded_cents': refunded, 'available_cents': available,
                        'account_id': posting['payable_account_id']})
    for source in sources:
        account = payables._require(db, 'accounts', source['account_id'])
        source.update(account_code=account['code'], account_name=account['name'])
    return {'supplier_id': supplier_id, 'supplier_name': supplier['name'], 'as_of': as_of,
            'currency': accounting.get_settings(db)['base_currency'], 'sources': sources}


def _cents(value):
    if isinstance(value, bool):
        raise ValueError('مبلغ التسوية غير صحيح')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= Decimal('100000000000') or number != number.quantize(Decimal('.01')):
            raise ValueError()
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('مبلغ التسوية موجب وبحد أقصى منزلتين عشريتين') from None


def _payload(data):
    if not isinstance(data, dict):
        raise ValueError('بيانات التسوية غير صحيحة')
    supplier_id = payables._id(data.get('supplier_id'))
    source_type = data.get('source_type')
    if source_type not in SOURCE_TYPES:
        raise ValueError('اختر دفعة مقدمة أو رصيد مرتجع')
    source_id = payables._id(data.get('source_id'))
    business_date = payables._date(data.get('business_date'), 'تاريخ التسوية')
    reference = payables._text(data, 'reference', 100, True)
    notes = payables._text(data, 'notes', 2000)
    incoming = data.get('allocations')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 500:
        raise ValueError('حدد الفواتير والمبالغ المطلوب تسويتها')
    allocations, seen = [], set()
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError('بند التسوية غير صحيح')
        invoice_id = payables._id(item.get('invoice_id'))
        if invoice_id in seen:
            raise ValueError('الفاتورة مكررة في التسوية')
        seen.add(invoice_id)
        amount = _cents(item['amount']) if 'amount' in item else payables._money(item.get('amount_cents'), allow_zero=False)
        allocations.append({'invoice_id': invoice_id, 'amount_cents': amount})
    amount = sum(row['amount_cents'] for row in allocations)
    payables._money(amount, allow_zero=False)
    return {'supplier_id': supplier_id, 'source_type': source_type, 'source_id': source_id,
            'business_date': business_date, 'reference': reference, 'notes': notes,
            'amount_cents': amount, 'allocations': sorted(allocations, key=lambda item: item['invoice_id'])}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def capacity_dates(db, from_date):
    """Every future balance boundary, including movements later reversed.

    Checking only the document date and final balance can miss an intervening
    overdraft. Source and invoice capacities must survive every dated movement.
    """
    from_date = payables._date(from_date)
    dates = {from_date, ALL_TIME}
    dates.update(row[0] for row in db.execute(
        "SELECT DISTINCT entry_date FROM journal_entries WHERE sealed=1 AND entry_date>=?", (from_date,)))
    return sorted(dates)


def _preview(db, value):
    supplier_id, business_date = value['supplier_id'], value['business_date']
    source_key = (value['source_type'], value['source_id'])
    historical = { (s['source_type'], s['source_id']): s for s in source_balances(db, supplier_id, business_date)['sources'] }
    boundaries = capacity_dates(db, business_date)
    future_sources = [{(s['source_type'], s['source_id']): s for s in source_balances(db, supplier_id, day)['sources']} for day in boundaries]
    source = historical.get(source_key)
    if source is None:
        raise ValueError('لا يوجد رصيد متاح لهذا المصدر والمورد في تاريخ التسوية')
    available = min(source['available_cents'], *(sources.get(source_key, {}).get('available_cents', 0) for sources in future_sources))
    if value['amount_cents'] > available:
        raise ValueError('مبلغ التسوية يتجاوز الرصيد المتاح من الدفعة المقدمة أو المرتجع')
    historical_invoices = {row['id']: row for row in payables.open_invoices(db, supplier_id, business_date)}
    future_invoices = [{row['id']: row for row in payables.open_invoices(db, supplier_id, day)} for day in boundaries]
    allocations = []
    for item in value['allocations']:
        invoice = historical_invoices.get(item['invoice_id'])
        if invoice is None:
            raise ValueError('الفاتورة ليست معتمدة لهذا المورد في تاريخ التسوية')
        if value['source_type'] == 'return_credit' and invoice['id'] == value['source_id']:
            raise ValueError('اختر فاتورة أخرى لتسوية رصيد المرتجع')
        outstanding = min(invoice['outstanding_cents'], *(invoices.get(invoice['id'], {}).get('outstanding_cents', 0) for invoices in future_invoices))
        if item['amount_cents'] > outstanding:
            raise ValueError('التسوية تتجاوز المتبقي على الفاتورة ' + invoice['code'])
        allocations.append({**item, 'code': invoice['code'], 'reference': invoice['reference'],
                            'due_date': invoice['due_date'], 'outstanding_cents': outstanding})
    return {'supplier_id': supplier_id, 'source_type': source_key[0], 'source_id': source_key[1],
            'source': source, 'business_date': business_date, 'amount_cents': value['amount_cents'],
            'available_cents': available, 'remaining_source_cents': available - value['amount_cents'],
            'allocations': allocations, 'open_invoices': list(historical_invoices.values()), 'warnings': []}


def preview(db, data):
    return _preview(db, _payload(data))


def save(db, data, settlement_id=None):
    if settlement_id is None:
        settlement_id = data.get('id')
    old = payables._require(db, 'supplier_settlements', settlement_id) if settlement_id is not None else None
    if old:
        payables._revision(data, old)
        if old['status'] != 'draft':
            raise ValueError('التسوية المعتمدة أو الملغاة لا تعدل')
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE supplier_settlements SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), settlement_id))
            payables._audit(db, 'supplier_settlement', settlement_id, 'cancel_draft', {})
            return detail(db, settlement_id)
    value = _payload(data)
    digest = _fingerprint(value)
    key = payables._text(data, 'request_key', 100, True) if not old else old['request_key']
    if not old:
        existing = db.execute('SELECT id,request_fingerprint FROM supplier_settlements WHERE request_key=?', (key,)).fetchone()
        if existing:
            if existing['request_fingerprint'] != digest:
                raise accounting.ConflictError('مفتاح الحفظ مستخدم لتسوية ببيانات أخرى')
            return detail(db, existing['id'])
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    _preview(db, value)
    stamp = payables._now()
    fields = (digest, value['supplier_id'], value['source_type'], value['source_id'], value['business_date'],
              value['reference'], value['notes'], value['amount_cents'])
    if old:
        db.execute('''UPDATE supplier_settlements SET request_fingerprint=?,supplier_id=?,source_type=?,source_id=?,
            business_date=?,reference=?,notes=?,amount_cents=?,revision=revision+1,updated_at=? WHERE id=?''',
                   (*fields, stamp, settlement_id))
        db.execute('DELETE FROM settlement_allocations WHERE settlement_id=?', (settlement_id,))
    else:
        code = payables._code(db, 'supplier_settlement', 'SST')
        settlement_id = db.execute('''INSERT INTO supplier_settlements(code,request_key,request_fingerprint,supplier_id,
            source_type,source_id,business_date,reference,notes,amount_cents,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (code, key, *fields, stamp, stamp)).lastrowid
    db.executemany('INSERT INTO settlement_allocations(settlement_id,invoice_id,amount_cents) VALUES(?,?,?)',
                   [(settlement_id, row['invoice_id'], row['amount_cents']) for row in value['allocations']])
    payables._audit(db, 'supplier_settlement', settlement_id, 'update_draft' if old else 'create_draft',
                    {'amount_cents': value['amount_cents'], 'source_type': value['source_type'], 'source_id': value['source_id']})
    return detail(db, settlement_id)


def _stored_value(db, old):
    return {key: old[key] for key in ('supplier_id', 'source_type', 'source_id', 'business_date', 'reference', 'notes', 'amount_cents')} | {
        'allocations': [dict(row) for row in db.execute('SELECT invoice_id,amount_cents FROM settlement_allocations WHERE settlement_id=? ORDER BY invoice_id', (old['id'],))]}


def approve(db, settlement_id, data):
    old = payables._require(db, 'supplier_settlements', settlement_id)
    if old['status'] == 'posted':
        return detail(db, settlement_id)
    payables._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('التسوية الملغاة لا تعتمد')
    accounting_periods.assert_open(db, old['business_date'])
    value = _stored_value(db, old)
    if sum(row['amount_cents'] for row in value['allocations']) != old['amount_cents']:
        raise ValueError('إجمالي التسوية لا يساوي توزيعها')
    report = _preview(db, value)
    lines = []
    for allocation in report['allocations']:
        original = db.execute('''SELECT p.payable_account_id,i.department_id FROM invoice_postings p
            JOIN invoices i ON i.id=p.invoice_id WHERE p.invoice_id=?''', (allocation['invoice_id'],)).fetchone()
        lines.append({'account_id': original['payable_account_id'], 'debit_cents': allocation['amount_cents'],
                      'supplier_id': old['supplier_id'], 'department_id': original['department_id']})
    source_account = report['source']['account_id']
    lines.append({'account_id': source_account, 'credit_cents': old['amount_cents'], 'supplier_id': old['supplier_id']})
    journal_id = payables.create_journal(db, 'supplier_settlement', settlement_id, old['business_date'], lines)
    db.execute("UPDATE supplier_settlements SET status='posted',source_account_id=?,journal_id=?,revision=revision+1,updated_at=? WHERE id=?",
               (source_account, journal_id, payables._now(), settlement_id))
    payables._audit(db, 'supplier_settlement', settlement_id, 'post_settlement', {'journal_id': journal_id, 'amount_cents': old['amount_cents']})
    return detail(db, settlement_id)


def _assert_reversal_dependencies_unused(db, settlement_id, business_date):
    """An incoming settlement can itself fund a later return-credit operation."""
    targets = [row[0] for row in db.execute('SELECT invoice_id FROM settlement_allocations WHERE settlement_id=?', (settlement_id,))]
    for invoice_id in targets:
        if _refund_used(db, 'return_credit', invoice_id, ALL_TIME):
            raise ValueError('توجد عملية استرداد مرتبطة برصيد إحدى فواتير التسوية؛ راجع المستند المرتبط أولًا')
        rows = db.execute("""SELECT r.business_date reversal_date FROM supplier_settlements s
            LEFT JOIN supplier_settlement_reversals r ON r.settlement_id=s.id
            WHERE s.source_type='return_credit' AND s.source_id=? AND s.status='posted' AND s.id<>?""",
                          (invoice_id, settlement_id)).fetchall()
        if any(row['reversal_date'] is None or row['reversal_date'] > business_date for row in rows):
            raise ValueError('توجد تسوية لاحقة تستخدم رصيد مرتجع إحدى الفواتير؛ اعكسها أولًا واختر تاريخًا بعدها')


def reverse(db, settlement_id, data):
    old = payables._require(db, 'supplier_settlements', settlement_id)
    if old['status'] != 'posted':
        raise ValueError('عكس التسوية متاح للتسوية المعتمدة فقط')
    business_date = payables._date(data.get('business_date'), 'تاريخ عكس التسوية')
    reference = payables._text(data, 'reference', 100, True)
    reason = payables._text(data, 'reason', 2000, True)
    existing = db.execute('SELECT * FROM supplier_settlement_reversals WHERE settlement_id=?', (settlement_id,)).fetchone()
    if existing:
        if (business_date, reference, reason) != (existing['business_date'], existing['reference'], existing['reason']):
            raise accounting.ConflictError('التسوية معكوسة بالفعل ببيانات أخرى')
        return detail(db, settlement_id)
    payables._revision(data, old)
    accounting_periods.assert_open(db, business_date)
    if business_date < old['business_date']:
        raise ValueError('تاريخ العكس لا يسبق تاريخ التسوية')
    _assert_reversal_dependencies_unused(db, settlement_id, business_date)
    lines = [dict(row) for row in db.execute('''SELECT account_id,credit_cents debit_cents,debit_cents credit_cents,
        supplier_id,department_id,source_line_id FROM journal_lines WHERE journal_id=? ORDER BY id''', (old['journal_id'],))]
    journal_id = payables.create_journal(db, 'supplier_settlement_reversal', settlement_id, business_date, lines)
    db.execute('''INSERT INTO supplier_settlement_reversals(settlement_id,business_date,reference,reason,journal_id,created_at)
        VALUES(?,?,?,?,?,?)''', (settlement_id, business_date, reference, reason, journal_id, payables._now()))
    payables._audit(db, 'supplier_settlement', settlement_id, 'reverse_settlement', {'journal_id': journal_id, 'reason': reason})
    return detail(db, settlement_id)


def detail(db, settlement_id):
    result = payables._require(db, 'supplier_settlements', settlement_id)
    result.pop('request_fingerprint', None)
    result['stored_status'] = result['status']
    supplier = payables._require(db, 'suppliers', result['supplier_id'])
    result.update(supplier_name=supplier['name'], supplier_code=supplier['code'])
    result['allocations'] = [dict(row) for row in db.execute('''SELECT a.invoice_id,a.amount_cents,i.code,i.reference,
        i.business_date,t.due_date FROM settlement_allocations a JOIN invoices i ON i.id=a.invoice_id
        LEFT JOIN invoice_terms t ON t.invoice_id=i.id WHERE a.settlement_id=? ORDER BY i.id''', (settlement_id,))]
    result['journal_code'] = None
    if result['journal_id']:
        result['journal_code'] = db.execute('SELECT code FROM journal_entries WHERE id=?', (result['journal_id'],)).fetchone()[0]
    reversal = db.execute('''SELECT r.*,j.code journal_code FROM supplier_settlement_reversals r
        JOIN journal_entries j ON j.id=r.journal_id WHERE r.settlement_id=?''', (settlement_id,)).fetchone()
    result['reversal'] = dict(reversal) if reversal else None
    if reversal:
        result['status'] = 'reversed'
    sources = source_balances(db, result['supplier_id'], ALL_TIME)['sources']
    result['source'] = next((source for source in sources if (source['source_type'], source['source_id']) == (result['source_type'], result['source_id'])), None)
    if result['source'] is None:
        table = 'supplier_payments' if result['source_type'] == 'advance' else 'invoices'
        source = payables._require(db, table, result['source_id'])
        result['source'] = {'source_type': result['source_type'], 'source_id': result['source_id'], 'code': source['code'],
                            'reference': source.get('reference', source['code']), 'available_cents': 0,
                            'account_id': result['source_account_id']}
    return result


def list_settlements(db, supplier_id=None):
    if supplier_id is not None:
        payables._require(db, 'suppliers', supplier_id)
    return [detail(db, row[0]) for row in db.execute('''SELECT id FROM supplier_settlements
        WHERE (? IS NULL OR supplier_id=?) ORDER BY business_date DESC,id DESC LIMIT 500''', (supplier_id, supplier_id)).fetchall()]
