"""Direct EGP cash vouchers and transfers, separate from supplier source documents.

Callers own transactions. Saving reserves no cash; posting creates one immutable
journal and corrections append its exact opposite. Cash sufficiency warnings
describe recorded movements, since actual opening balances are not assumed.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import payables
import supplier_payments


KINDS = {'receipt': ('TRC', 'سند قبض نقدي'),
         'payment': ('TPY', 'سند صرف نقدي'),
         'transfer': ('TTR', 'تحويل بين الخزائن')}
FIELDS = ('kind', 'business_date', 'reference', 'amount_cents', 'cash_account_id',
          'counter_account_id', 'destination_cash_account_id', 'party_name',
          'department_id', 'notes', 'currency')


def initialize(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS treasury_documents(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN('receipt','payment','transfer')),
            business_date TEXT NOT NULL,reference TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            currency TEXT NOT NULL DEFAULT 'EGP' CHECK(currency='EGP'),
            cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
            counter_account_id INTEGER REFERENCES accounts(id),
            destination_cash_account_id INTEGER REFERENCES accounts(id),
            party_name TEXT NOT NULL DEFAULT '',department_id INTEGER REFERENCES departments(id),
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1,
            journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            CHECK((kind='transfer' AND counter_account_id IS NULL
                AND destination_cash_account_id IS NOT NULL
                AND cash_account_id<>destination_cash_account_id)
                OR (kind IN('receipt','payment') AND counter_account_id IS NOT NULL
                AND destination_cash_account_id IS NULL AND cash_account_id<>counter_account_id)))''',
        '''CREATE TABLE IF NOT EXISTS treasury_reversals(
            id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL UNIQUE REFERENCES treasury_documents(id),
            business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS treasury_documents_date ON treasury_documents(status,business_date)',
        'CREATE INDEX IF NOT EXISTS treasury_document_reference ON treasury_documents(kind,cash_account_id,reference)',
    ):
        db.execute(sql)
    for table, when in (('treasury_documents', "OLD.status='posted'"),
                        ('treasury_reversals', '1')):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {when}
                BEGIN SELECT RAISE(ABORT,'posted treasury document is immutable'); END''')


def _columns(db, table):
    return {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _amount(data):
    if 'amount' not in data:
        return payables._money(data.get('amount_cents'), False)
    if isinstance(data['amount'], bool):
        raise ValueError('المبلغ موجب وبحد أقصى منزلتين عشريتين')
    try:
        value = Decimal(str(data['amount']))
        if not value.is_finite() or not 0 < value <= Decimal(payables.MAX_CENTS) / 100:
            raise ValueError()
        if value != value.quantize(Decimal('.01')):
            raise ValueError()
        amount = int(value * 100)
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('المبلغ موجب وبحد أقصى منزلتين عشريتين') from None
    if 'amount_cents' in data and payables._money(data['amount_cents'], False) != amount:
        raise ValueError('المبالغ المدخلة غير متطابقة')
    return amount


def _payload(data):
    if not isinstance(data, dict):
        raise ValueError('بيانات سند الخزينة غير صحيحة')
    kind = data.get('kind')
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('اختر قبضًا أو صرفًا أو تحويلًا بين الخزائن')
    if data.get('currency', 'EGP') != 'EGP':
        raise ValueError('سند الخزينة بالجنيه المصري فقط')
    department_id = data.get('department_id')
    if department_id is not None:
        payables._id(department_id)
    if kind == 'payment' and department_id is None:
        raise ValueError('اختر القسم ومركز التكلفة للصرف')
    counter_id = data.get('counter_account_id')
    destination_id = data.get('destination_cash_account_id')
    if kind == 'transfer':
        if counter_id is not None:
            raise ValueError('التحويل يحدد الخزينة المستلمة دون حساب مقابل آخر')
        destination_id = payables._id(destination_id)
        party = ''
        department_id = None
    else:
        if destination_id is not None:
            raise ValueError('حدد التحويل بين الخزائن كنوع مستقل')
        counter_id = payables._id(counter_id)
        party = payables._text(data, 'party_name', 200, True)
    return {'kind': kind, 'business_date': payables._date(data.get('business_date'), 'تاريخ السند'),
            'reference': payables._text(data, 'reference', 100, True),
            'amount_cents': _amount(data), 'currency': 'EGP',
            'cash_account_id': payables._id(data.get('cash_account_id')),
            'counter_account_id': counter_id, 'destination_cash_account_id': destination_id,
            'party_name': party, 'department_id': department_id,
            'notes': payables._text(data, 'notes', 2000)}


def blocked_counter_account_ids(db):
    """Keep direct vouchers out of current and historical subsidiary controls.

    The initial direct-cash workflow conservatively excludes all module mappings.
    It also retains original controls after mappings move, so a cash voucher
    cannot bypass supplier, tax, custody or another protected source workflow.
    """
    blocked = {row[0] for row in db.execute('SELECT account_id FROM account_mappings')}
    from supplier_reconciliation import _account_ids
    payable, advances = _account_ids(db)
    blocked.update(payable | advances)
    for table, field in (
        ('payment_withholding', 'account_id'),
        ('invoice_tax_settings', 'recoverable_account_id'),
        ('withholding_remittance_allocations', 'account_id'),
        ('opening_withholding_sources', 'account_id'),
        ('opening_remittance_allocations', 'account_id'),
        ('custody_documents', 'custody_account_id'),
    ):
        if field in _columns(db, table):
            blocked.update(row[0] for row in db.execute(
                f'SELECT DISTINCT {field} FROM {table} WHERE {field} IS NOT NULL'))
    import banks
    blocked.update(banks.issued_cheque_account_ids(db))
    for row in db.execute("SELECT id FROM accounts WHERE code LIKE '111%' OR code LIKE '112%' OR code='115003'"):
        blocked.add(row[0])
    return blocked


def _cash_account(db, account_id):
    account = payables._posting_account(db, account_id)
    clearing = {row[0] for row in db.execute(
        "SELECT account_id FROM account_mappings WHERE module='الخزينة' AND role='cash_transfer_clearing'")}
    if (account['normal_side'] != 'debit' or not account['code'].startswith('111')
            or account['code'] == '111005' or account['id'] in clearing):
        raise ValueError('اختر خزينة نقدية بالجنيه المصري؛ التحويلات بالطريق خارج هذا المسار')
    return account


def _counter_account(db, account_id, kind, blocked=None):
    account = payables._posting_account(db, account_id)
    blocked = blocked_counter_account_ids(db) if blocked is None else blocked
    if account['id'] in blocked:
        raise ValueError('هذا حساب مرتبط بدورة مستقلة؛ استخدم مستند المورد أو الضريبة أو العهدة من مساره الأصلي')
    classes, side = (('5', '6', '8'), 'debit') if kind == 'payment' else (('2', '3', '7'), 'credit')
    if not account['code'].startswith(classes) or account['normal_side'] != side:
        raise ValueError('اختر حساب مصروف مدين للصرف، أو التزام أو حقوق ملكية أو إيراد آخر دائن للقبض')
    return account


def config(db):
    blocked = blocked_counter_account_ids(db)
    rows = [dict(row) for row in db.execute("SELECT * FROM accounts WHERE active=1 AND posting=1 ORDER BY code")]
    clearing = {row[0] for row in db.execute(
        "SELECT account_id FROM account_mappings WHERE module='الخزينة' AND role='cash_transfer_clearing'")}
    cash = [row for row in rows if row['normal_side'] == 'debit' and row['code'].startswith('111')
            and row['code'] != '111005' and row['id'] not in clearing]
    counter = {kind: [row for row in rows if row['id'] not in blocked
                     and row['normal_side'] == side and row['code'].startswith(classes)]
               for kind, classes, side in (('receipt', ('2', '3', '7'), 'credit'),
                                          ('payment', ('5', '6', '8'), 'debit'))}
    return {'cash_accounts': cash, 'counter_accounts_by_kind': counter,
            'departments': [dict(row) for row in db.execute('SELECT * FROM departments ORDER BY code,id')],
            'currency': accounting.get_settings(db)['base_currency'],
            'opening_status': accounting.opening_status(db),
            'notes': ['الموردون والضرائب والعهد لهم مستندات مستقلة؛ لا تعِد تسجيل حركة خزينة سبق ترحيلها من تلك المسارات.',
                      'الحسابات المرتبطة بالموديولات لا تظهر كحساب مقابل للسند المباشر؛ اختر تصنيفًا مباشرًا مناسبًا.',
                      accounting.opening_status(db)['message'],
                      'السند المباشر لا يحسب ضريبة فاتورة أو خصمًا؛ الفواتير والمصروفات الضريبية من دورة الموردين.']}


def _preview(db, value):
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('راجع العملة الأساسية قبل ترحيل سند بالجنيه المصري')
    cash = _cash_account(db, value['cash_account_id'])
    department = payables._require(db, 'departments', value['department_id']) if value['department_id'] is not None else None
    amount = value['amount_cents']
    kind = value['kind']
    counter = (_cash_account(db, value['destination_cash_account_id']) if kind == 'transfer'
               else _counter_account(db, value['counter_account_id'], kind))
    if counter['id'] == cash['id']:
        raise ValueError('الخزينة المرسلة والمستلمة يجب أن تكونا مختلفتين')
    if kind == 'receipt':
        raw = [{'account_id': cash['id'], 'debit_cents': amount, 'credit_cents': 0},
               {'account_id': counter['id'], 'debit_cents': 0, 'credit_cents': amount}]
    else:
        raw = [{'account_id': counter['id'], 'debit_cents': amount, 'credit_cents': 0},
               {'account_id': cash['id'], 'debit_cents': 0, 'credit_cents': amount}]
    accounts = {cash['id']: cash, counter['id']: counter}
    lines = []
    for line in raw:
        account = accounts[line['account_id']]
        # The expense bears the cost centre, while the physical cash side does
        # not create a second departmental expense dimension.
        dep = department if department and line['account_id'] == counter['id'] else None
        lines.append({**line, 'account_code': account['code'], 'account_name': account['name'],
                      'department_id': dep['id'] if dep else None,
                      'department_name': dep['name'] if dep else None})
    before = supplier_payments.account_balance(db, cash['id'], value['business_date'])
    after = before + amount if kind == 'receipt' else before - amount
    warnings = []
    if after < 0:
        warnings.append('رصيد الحركات المسجلة للخزينة سالب بعد السند؛ راجع أرصدة البداية والحركات، ولا يُفترض رصيد افتتاحي تلقائيًا.')
    result = {**value, 'lines': lines, 'journal': {'lines': lines, 'debit_cents': amount, 'credit_cents': amount},
              'cash_account_code': cash['code'], 'cash_account_name': cash['name'],
              'cash_balance_before_cents': before, 'cash_balance_after_cents': after,
              'warnings': warnings, 'can_approve': True}
    prefix = 'destination_cash_account' if kind == 'transfer' else 'counter_account'
    result.update({prefix + '_code': counter['code'], prefix + '_name': counter['name']})
    if kind == 'transfer':
        before_to = supplier_payments.account_balance(db, counter['id'], value['business_date'])
        result.update(destination_balance_before_cents=before_to,
                      destination_balance_after_cents=before_to + amount)
    return result


def preview(db, data):
    return _preview(db, _payload(data))


def _reference_unique(db, value, exclude=0):
    if db.execute('''SELECT 1 FROM treasury_documents d WHERE d.kind=? AND d.cash_account_id=?
        AND d.reference=? AND d.status<>'cancelled' AND d.id<>?
        AND NOT EXISTS(SELECT 1 FROM treasury_reversals r WHERE r.document_id=d.id AND r.business_date<=?)''',
        (value['kind'], value['cash_account_id'], value['reference'], exclude, value['business_date'])).fetchone():
        raise ValueError('مرجع السند مستخدم لنفس نوع الحركة والخزينة؛ راجع المستند الأصلي')


def save(db, data, document_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات سند الخزينة غير صحيحة')
    old = payables._require(db, 'treasury_documents', document_id) if document_id is not None else None
    if old:
        if data.get('action') == 'cancel' and old['status'] == 'cancelled':
            return detail(db, old['id'])
        payables._revision(data, old)
        if old['status'] != 'draft':
            raise ValueError('السند المعتمد أو الملغى لا يعدّل؛ استخدم مستند تصحيح مستقل')
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE treasury_documents SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?",
                       (payables._now(), old['id']))
            payables._audit(db, 'treasury_document', old['id'], 'cancel_draft', {})
            return detail(db, old['id'])
    value = _payload(data)
    digest = _digest(value)
    key = old['request_key'] if old else payables._text(data, 'request_key', 100, True)
    if old is None:
        prior = db.execute('SELECT id,request_fingerprint FROM treasury_documents WHERE request_key=?', (key,)).fetchone()
        if prior:
            if prior['request_fingerprint'] != digest:
                raise accounting.ConflictError('مفتاح الطلب مستخدم لسند خزينة ببيانات أخرى')
            return detail(db, prior['id'])
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    _preview(db, value)
    _reference_unique(db, value, old['id'] if old else 0)
    stamp = payables._now()
    if old:
        # A document's kind identifies its code series throughout its life.
        if value['kind'] != old['kind']:
            raise ValueError('نوع السند محفوظ؛ ألغِ المسودة وأنشئ سندًا من النوع المطلوب')
        db.execute(f"UPDATE treasury_documents SET request_fingerprint=?,{','.join(field+'=?' for field in FIELDS)},revision=revision+1,updated_at=? WHERE id=?",
                   (digest, *(value[field] for field in FIELDS), stamp, old['id']))
        document_id = old['id']
    else:
        code = payables._code(db, 'treasury_' + value['kind'], KINDS[value['kind']][0])
        document_id = db.execute(f"INSERT INTO treasury_documents(code,request_key,request_fingerprint,{','.join(FIELDS)},created_at,updated_at) VALUES(?,?,?,{','.join('?' for _ in FIELDS)},?,?)",
                                 (code, key, digest, *(value[field] for field in FIELDS), stamp, stamp)).lastrowid
    payables._audit(db, 'treasury_document', document_id, 'update_draft' if old else 'create_draft',
                    {'kind': value['kind'], 'amount_cents': value['amount_cents']})
    return detail(db, document_id)


def approve(db, document_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات اعتماد سند الخزينة غير صحيحة')
    old = payables._require(db, 'treasury_documents', document_id)
    if old['status'] == 'posted':
        return detail(db, old['id'])
    payables._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('السند الملغى لا يعتمد')
    accounting_periods.assert_open(db, old['business_date'])
    value = {field: old[field] for field in FIELDS}
    plan = _preview(db, value)
    _reference_unique(db, value, old['id'])
    lines = [{key: row[key] for key in ('account_id', 'debit_cents', 'credit_cents', 'department_id')}
             for row in plan['lines']]
    journal_id = payables.create_journal(db, 'treasury_' + old['kind'], old['id'], old['business_date'], lines)
    db.execute("UPDATE treasury_documents SET status='posted',journal_id=?,revision=revision+1,updated_at=? WHERE id=?",
               (journal_id, payables._now(), old['id']))
    payables._audit(db, 'treasury_document', old['id'], 'post',
                    {'journal_id': journal_id, 'amount_cents': old['amount_cents'], 'warnings': plan['warnings']})
    return detail(db, old['id'])


def reverse(db, document_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات تصحيح سند الخزينة غير صحيحة')
    old = payables._require(db, 'treasury_documents', document_id)
    value = {'document_id': old['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ التصحيح'),
             'reference': payables._text(data, 'reference', 100, True),
             'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    event_revision = data.get('event_revision', data.get('adjustment_revision', 0))
    if type(data.get('revision')) is not int or type(event_revision) is not int:
        raise accounting.ConflictError('أعد فتح سند الخزينة قبل التصحيح')
    if 'event_revision' in data and 'adjustment_revision' in data and (type(data['adjustment_revision']) is not int or data['adjustment_revision'] != event_revision):
        raise accounting.ConflictError('حالة تصحيح السند تغيرت؛ أعد فتح المستند')
    prior = db.execute('SELECT * FROM treasury_reversals WHERE document_id=? OR request_key=?',
                       (old['id'], key)).fetchone()
    if prior:
        if prior['document_id'] != old['id'] or prior['request_key'] != key or prior['request_fingerprint'] != _digest(value):
            raise accounting.ConflictError('السند معكوس بالفعل أو مفتاح التصحيح مستخدم لبيانات أخرى')
        return detail(db, old['id'])
    if old['status'] != 'posted':
        raise ValueError('التصحيح متاح لسند الخزينة المعتمد فقط')
    payables._revision(data, old)
    if event_revision != 0:
        raise accounting.ConflictError('حالة تصحيح سند الخزينة تغيرت؛ أعد فتح المستند')
    accounting_periods.assert_open(db, value['business_date'])
    if value['business_date'] < old['business_date']:
        raise ValueError('تاريخ التصحيح لا يسبق تاريخ السند الأصلي')
    original = payables.journal_detail(db, old['journal_id'])
    if original['source_type'] != 'treasury_' + old['kind'] or original['source_id'] != old['id']:
        raise ValueError('قيد السند الأصلي لا يطابق المستند')
    reversal_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM treasury_reversals').fetchone()[0]
    lines = [{'account_id': row['account_id'], 'debit_cents': row['credit_cents'],
              'credit_cents': row['debit_cents'], 'supplier_id': row['supplier_id'],
              'department_id': row['department_id'], 'source_line_id': row['source_line_id']}
             for row in original['lines']]
    journal_id = payables.create_journal(db, 'treasury_reversal', reversal_id, value['business_date'], lines)
    db.execute('''INSERT INTO treasury_reversals(id,document_id,business_date,reference,reason,
        request_key,request_fingerprint,journal_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (reversal_id, old['id'], value['business_date'], value['reference'], value['reason'],
         key, _digest(value), journal_id, payables._now()))
    payables._audit(db, 'treasury_reversal', reversal_id, 'reverse',
                    {'document_id': old['id'], 'journal_id': journal_id, 'reason': value['reason']})
    return detail(db, old['id'])


def detail(db, document_id, as_of=None):
    result = payables._require(db, 'treasury_documents', document_id)
    result.pop('request_fingerprint', None)
    for field in ('cash_account', 'counter_account', 'destination_cash_account'):
        account_id = result[field + '_id']
        account = payables._require(db, 'accounts', account_id) if account_id else None
        result.update({field + '_code': account['code'] if account else None,
                       field + '_name': account['name'] if account else None})
    department = payables._require(db, 'departments', result['department_id']) if result['department_id'] else None
    result.update(department_name=department['name'] if department else None,
                  cost_code=department['cost_code'] if department else None)
    result['journal'] = payables.journal_detail(db, result['journal_id']) if result['journal_id'] else None
    as_of = payables._date(as_of if as_of is not None else '9999-12-31', 'تاريخ التقرير')
    row = db.execute('SELECT * FROM treasury_reversals WHERE document_id=? AND business_date<=?',
                     (result['id'], as_of)).fetchone()
    result['reversal'] = dict(row) if row else None
    if result['reversal']:
        result['reversal'].pop('request_fingerprint', None)
        result['reversal'].update(code=f"TRV-{row['id']:06d}", journal=payables.journal_detail(db, row['journal_id']))
    result.update(effective_status='reversed' if row else result['status'],
                  adjustment_revision=1 if row else 0, voucher_title=KINDS[result['kind']][1])
    return result


def list_documents(db, as_of=None):
    if as_of is None:
        rows = db.execute('SELECT id FROM treasury_documents ORDER BY business_date DESC,id DESC').fetchall()
    else:
        as_of = payables._date(as_of, 'تاريخ التقرير')
        rows = db.execute('SELECT id FROM treasury_documents WHERE business_date<=? ORDER BY business_date DESC,id DESC', (as_of,)).fetchall()
    return [detail(db, row['id'], as_of) for row in rows]
