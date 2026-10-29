"""Nonposting EGP cash-count evidence with frozen and current book positions.

The caller owns the transaction. Confirming a count certifies its captured facts;
it never creates a cash adjustment, opening balance or journal entry.
"""
from __future__ import annotations

import hashlib
import json

import accounting
import accounting_periods
import ledger_reports
import payables
import treasury


DENOMINATIONS = (20000, 10000, 5000, 2000, 1000, 500, 100, 50, 25)
NOTES = (
    'محضر الجرد مستند إثبات ومراجعة فقط؛ حفظه أو اعتماده لا ينشئ قيدًا أو تسوية نقدية.',
    'الأرصدة الافتتاحية الفعلية مؤجلة؛ الفرق يحتاج مراجعة بيانات البداية والمستندات قبل أي تسوية مستقلة.',
    'الرصيد الدفتري يجمع القيود المرحّلة حتى تاريخ الجرد كما كانت مسجلة وقت التقاط المحضر؛ ليس إعادة بناء لحظة تاريخية داخل اليوم.',
    'الفئات قيم للعد يراجعها القائم بالجرد، ويمكن إدخال فئة أخرى بالجنيه والقرش؛ ليست إقرارًا بشأن تداول كل فئة.',
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def initialize(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS cash_counts(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            business_date TEXT NOT NULL,reference TEXT NOT NULL,
            cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
            cash_account_code_snapshot TEXT NOT NULL,cash_account_name_snapshot TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EGP' CHECK(currency='EGP'),
            counted_by TEXT NOT NULL,reviewed_by TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','confirmed','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
            actual_cents INTEGER NOT NULL CHECK(actual_cents>=0),book_cents INTEGER NOT NULL,
            variance_cents INTEGER NOT NULL,lines_json TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,
            captured_at TEXT NOT NULL,confirmed_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS cash_count_voids(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,
            count_id INTEGER NOT NULL UNIQUE REFERENCES cash_counts(id),
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,
            created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS cash_counts_date ON cash_counts(business_date,id)',
        'CREATE INDEX IF NOT EXISTS cash_counts_reference ON cash_counts(cash_account_id,reference)',
    ):
        db.execute(statement)
    for table, condition in (('cash_counts', "OLD.status='confirmed'"), ('cash_count_voids', '1')):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'confirmed cash count is immutable'); END''')


def config(db):
    return {'cash_accounts': treasury.config(db)['cash_accounts'],
            'denominations_cents': list(DENOMINATIONS), 'currency': 'EGP', 'notes': list(NOTES)}


def _normalize(data):
    if not isinstance(data, dict):
        raise ValueError('بيانات محضر الجرد غير صحيحة')
    if data.get('currency', 'EGP') != 'EGP':
        raise ValueError('جرد النقد في هذه المرحلة بالجنيه المصري EGP فقط')
    rows = data.get('lines')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError('أدخل من فئة نقد واحدة إلى عشرين فئة')
    lines, seen, actual = [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('فئة نقد غير صحيحة')
        denomination, quantity = row.get('denomination_cents'), row.get('quantity')
        if type(denomination) is not int or not 0 < denomination <= payables.MAX_CENTS:
            raise ValueError('قيمة الفئة موجبة وبالقرش الصحيح دون كسور')
        if type(quantity) is not int or not 0 <= quantity <= 1000000:
            raise ValueError('عدد القطع عدد صحيح من صفر إلى مليون')
        if denomination in seen:
            raise ValueError('الفئة مكررة؛ عدّل عدد القطع في السطر الموجود')
        seen.add(denomination)
        total = denomination * quantity
        actual += total
        payables._money(actual)
        if 'line_total_cents' in row and (type(row['line_total_cents']) is not int or row['line_total_cents'] != total):
            raise ValueError('إجمالي الفئة المدخل لا يطابق عدد القطع وقيمتها')
        lines.append({'denomination_cents': denomination, 'quantity': quantity})
    for key in ('actual_cents', 'total_cents'):
        if key in data and (type(data[key]) is not int or data[key] != actual):
            raise ValueError('إجمالي الجرد المدخل لا يطابق الفئات')
    return {'business_date': payables._date(data.get('business_date'), 'تاريخ الجرد'),
            'reference': payables._text(data, 'reference', 100, True),
            'cash_account_id': payables._id(data.get('cash_account_id')),
            'counted_by': payables._text(data, 'counted_by', 150, True),
            'reviewed_by': payables._text(data, 'reviewed_by', 150, True),
            'notes': payables._text(data, 'notes', 2000), 'currency': 'EGP',
            'lines': sorted(lines, key=lambda line: line['denomination_cents'], reverse=True)}


def _book(db, account_id, business_date):
    """Read even inactive/historically reclassified accounts without validating routing."""
    cash_scope = '''j.entry_date<=? AND EXISTS(
        SELECT 1 FROM journal_lines cash WHERE cash.journal_id=j.id AND cash.account_id=?)'''
    headers = [dict(row) for row in db.execute(
        'SELECT j.* FROM journal_entries j WHERE ' + cash_scope + ' ORDER BY j.entry_date,j.id',
        (business_date, account_id))]
    lines = [dict(row) for row in db.execute('''SELECT l.* FROM journal_lines l
        JOIN journal_entries j ON j.id=l.journal_id WHERE ''' + cash_scope +
        ' ORDER BY j.entry_date,j.id,l.id', (business_date, account_id))]
    sealed = {row['id'] for row in headers if row['sealed']}
    cash_lines = [row for row in lines if row['account_id'] == account_id]
    balance = sum(row['debit_cents'] - row['credit_cents']
                  for row in cash_lines if row['journal_id'] in sealed)
    fingerprint = _hash({'cash_account_id': account_id, 'business_date': business_date,
                         'journals': headers, 'lines': lines})
    evidence = {'journal_count': len(headers), 'sealed_journal_count': len(sealed),
                'cash_line_count': len(cash_lines),
                'last_journal_id': max((row['id'] for row in headers), default=None),
                'last_line_id': max((row['id'] for row in cash_lines), default=None)}
    return {'book_cents': balance, 'evidence_fingerprint': fingerprint,
            'evidence': evidence,
            'exceptions': ledger_reports._exceptions(db, business_date, account_id)}


def _plan(db, value):
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('يلزم ضبط العملة الأساسية EGP قبل جرد النقد بالجنيه المصري')
    account = treasury._cash_account(db, value['cash_account_id'])
    book = _book(db, account['id'], value['business_date'])
    lines = [{**line, 'line_total_cents': line['denomination_cents'] * line['quantity']}
             for line in value['lines']]
    actual = sum(line['line_total_cents'] for line in lines)
    return {**value, **book, 'cash_account_code': account['code'], 'cash_account_name': account['name'],
            'lines': lines, 'actual_cents': actual, 'variance_cents': actual - book['book_cents'],
            'can_confirm': not book['exceptions'], 'warnings': list(NOTES)}


def preview(db, data):
    return _plan(db, _normalize(data))


def _unique_reference(db, value, exclude=0):
    for old in db.execute('''SELECT c.id,v.business_date void_date FROM cash_counts c
            LEFT JOIN cash_count_voids v ON v.count_id=c.id
            WHERE c.cash_account_id=? AND lower(c.reference)=lower(?) AND c.status<>'cancelled' AND c.id<>?''',
                          (value['cash_account_id'], value['reference'], exclude)):
        if old['void_date'] is None or value['business_date'] < old['void_date']:
            raise accounting.ConflictError('مرجع الجرد مستخدم لهذه الخزينة؛ افتح المحضر المحفوظ أو استخدم مرجعًا آخر')


def save(db, data, count_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات محضر الجرد غير صحيحة')
    old = payables._require(db, 'cash_counts', count_id) if count_id is not None else None
    if old:
        if old['status'] == 'cancelled' and data.get('action') == 'cancel':
            return detail(db, count_id)
        if old['status'] != 'draft':
            raise ValueError('المحضر المعتمد أو الملغى لا يعدل؛ إبطال المعتمد له مستند مستقل')
        payables._revision(data, old)
        accounting_periods.assert_open(db, old['business_date'])
        if data.get('action') == 'cancel':
            db.execute("UPDATE cash_counts SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?",
                       (payables._now(), count_id))
            accounting._audit(db, 'cash_count', count_id, 'cancel_draft', {})
            return detail(db, count_id)
    value = _normalize(data)
    key = payables._text(data, 'request_key', 100, old is None) or old['request_key']
    fingerprint = _hash(value)
    prior = db.execute('SELECT id,request_fingerprint FROM cash_counts WHERE request_key=?', (key,)).fetchone()
    if not old and prior:
        if prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('مفتاح الطلب مستخدم لمحضر جرد ببيانات مختلفة')
        return detail(db, prior['id'])
    if old and key != old['request_key']:
        raise accounting.ConflictError('لا يتغير مفتاح طلب المحضر المحفوظ')
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    _unique_reference(db, value, old['id'] if old else 0)
    plan = _plan(db, value)
    stamp = payables._now()
    fields = (fingerprint, value['business_date'], value['reference'], value['cash_account_id'],
              plan['cash_account_code'], plan['cash_account_name'], value['counted_by'], value['reviewed_by'],
              value['notes'], plan['actual_cents'], plan['book_cents'], plan['variance_cents'],
              _json(value['lines']), plan['evidence_fingerprint'], _json(plan['evidence']), stamp, stamp)
    if old:
        db.execute('''UPDATE cash_counts SET request_fingerprint=?,business_date=?,reference=?,cash_account_id=?,
            cash_account_code_snapshot=?,cash_account_name_snapshot=?,counted_by=?,reviewed_by=?,notes=?,
            actual_cents=?,book_cents=?,variance_cents=?,lines_json=?,evidence_fingerprint=?,evidence_json=?,
            captured_at=?,updated_at=?,revision=revision+1 WHERE id=?''', (*fields, count_id))
    else:
        code = payables._code(db, 'cash_count', 'CCT')
        count_id = db.execute('''INSERT INTO cash_counts(request_key,code,request_fingerprint,business_date,reference,
            cash_account_id,cash_account_code_snapshot,cash_account_name_snapshot,counted_by,reviewed_by,notes,
            actual_cents,book_cents,variance_cents,lines_json,evidence_fingerprint,evidence_json,captured_at,updated_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (key, code, *fields, stamp)).lastrowid
    accounting._audit(db, 'cash_count', count_id, 'update_draft' if old else 'save_draft',
                      {'actual_cents': plan['actual_cents'], 'book_cents': plan['book_cents'],
                       'variance_cents': plan['variance_cents'], 'financial_posting': False})
    return detail(db, count_id)


def confirm(db, count_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات اعتماد الجرد غير صحيحة')
    old = payables._require(db, 'cash_counts', count_id)
    if old['status'] == 'confirmed':
        if type(data.get('revision')) is not int or data['revision'] not in (old['revision'], old['revision'] - 1):
            raise accounting.ConflictError('أعد فتح محضر الجرد قبل الاعتماد')
        return detail(db, count_id)
    if old['status'] != 'draft':
        raise ValueError('الاعتماد متاح لمسودة الجرد فقط')
    payables._revision(data, old)
    accounting_periods.assert_open(db, old['business_date'])
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('يلزم ضبط العملة الأساسية EGP قبل اعتماد جرد النقد')
    try:
        stored_value = _normalize({**old, 'lines': json.loads(old['lines_json'])})
    except (ValueError, TypeError):
        raise accounting.ConflictError('بيانات المسودة لا تطابق محضر العد؛ أعد حفظها ومراجعتها') from None
    if (_hash(stored_value) != old['request_fingerprint']
            or old['variance_cents'] != old['actual_cents'] - old['book_cents']):
        raise accounting.ConflictError('بيانات المسودة تغيرت؛ أعد حفظها ومراجعتها')
    treasury._cash_account(db, old['cash_account_id'])
    book = _book(db, old['cash_account_id'], old['business_date'])
    try:
        saved_evidence = json.loads(old['evidence_json'])
    except (ValueError, TypeError):
        raise accounting.ConflictError('بيانات لقطة الجرد غير مكتملة؛ أعد حفظ المسودة ومراجعتها') from None
    if old['book_cents'] != book['book_cents'] or saved_evidence != book['evidence']:
        raise accounting.ConflictError('بيانات لقطة الجرد لا تطابق سجل الخزينة؛ أعد حفظ المسودة ومراجعتها')
    if book['evidence_fingerprint'] != old['evidence_fingerprint']:
        raise accounting.ConflictError('حركة الخزينة تغيرت منذ حفظ المحضر، حتى إن تعادل الرصيد؛ أعد حفظ المسودة ومراجعتها قبل الاعتماد')
    if book['exceptions']:
        raise ValueError('توجد قيود خزينة غير مكتملة أو غير متوازنة؛ راجعها قبل اعتماد محضر الجرد')
    _unique_reference(db, old, old['id'])
    stamp = payables._now()
    db.execute("UPDATE cash_counts SET status='confirmed',revision=revision+1,confirmed_at=?,updated_at=? WHERE id=?",
               (stamp, stamp, count_id))
    accounting._audit(db, 'cash_count', count_id, 'confirm_count',
                      {'actual_cents': old['actual_cents'], 'book_cents': old['book_cents'],
                       'variance_cents': old['variance_cents'], 'financial_posting': False})
    return detail(db, count_id)


def void(db, count_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات إبطال محضر الجرد غير صحيحة')
    old = payables._require(db, 'cash_counts', count_id)
    value = {'count_id': old['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ الإبطال'),
             'reference': payables._text(data, 'reference', 100, True),
             'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    fingerprint = _hash(value)
    event = data.get('event_revision', data.get('adjustment_revision', 0))
    if type(data.get('revision')) is not int or type(event) is not int:
        raise accounting.ConflictError('أعد فتح المحضر قبل إبطاله')
    if 'event_revision' in data and 'adjustment_revision' in data and (
            type(data['adjustment_revision']) is not int or data['event_revision'] != data['adjustment_revision']):
        raise accounting.ConflictError('حالة إبطال المحضر تغيرت')
    prior = db.execute('SELECT * FROM cash_count_voids WHERE count_id=? OR request_key=?', (count_id, key)).fetchone()
    if prior:
        if prior['count_id'] != count_id or prior['request_key'] != key or prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('المحضر مبطل بالفعل أو مفتاح الإبطال مستخدم لبيانات أخرى')
        return detail(db, count_id)
    if old['status'] != 'confirmed':
        raise ValueError('الإبطال بمستند مستقل متاح للجرد المعتمد فقط')
    payables._revision(data, old)
    if event != 0:
        raise accounting.ConflictError('حالة إبطال المحضر تغيرت؛ أعد فتحه')
    if value['business_date'] < old['business_date']:
        raise ValueError('تاريخ الإبطال لا يسبق تاريخ الجرد الأصلي')
    accounting_periods.assert_open(db, value['business_date'])
    code = payables._code(db, 'cash_count_void', 'CCV')
    db.execute('''INSERT INTO cash_count_voids(code,count_id,request_key,request_fingerprint,business_date,
        reference,reason,created_at) VALUES(?,?,?,?,?,?,?,?)''',
               (code, count_id, key, fingerprint, value['business_date'], value['reference'], value['reason'], payables._now()))
    accounting._audit(db, 'cash_count', count_id, 'void_count', {**value, 'financial_posting': False})
    return detail(db, count_id)


def detail(db, count_id):
    row = payables._require(db, 'cash_counts', count_id)
    row.pop('request_fingerprint', None)
    lines = json.loads(row.pop('lines_json'))
    row['lines'] = [{**line, 'line_total_cents': line['denomination_cents'] * line['quantity']} for line in lines]
    row['evidence'] = json.loads(row.pop('evidence_json'))
    live = _book(db, row['cash_account_id'], row['business_date'])
    row.update(cash_account_code=row['cash_account_code_snapshot'], cash_account_name=row['cash_account_name_snapshot'],
               live_book_cents=live['book_cents'], live_variance_cents=row['actual_cents'] - live['book_cents'],
               live_evidence=live['evidence'], live_exceptions=live['exceptions'],
               book_changed=live['evidence_fingerprint'] != row['evidence_fingerprint'],
               warnings=list(NOTES))
    void_row = db.execute('SELECT * FROM cash_count_voids WHERE count_id=?', (count_id,)).fetchone()
    row['void'] = dict(void_row) if void_row else None
    if row['void']:
        row['void'].pop('request_fingerprint', None)
    row['effective_status'] = 'voided' if void_row else row['status']
    row['adjustment_revision'] = 1 if void_row else 0
    return row


def list_counts(db):
    return [detail(db, row['id']) for row in db.execute('SELECT id FROM cash_counts ORDER BY business_date DESC,id DESC')]
