"""EGP bank masters, immutable movements and ledger-backed bank books.

The caller owns the transaction. Opening balances are entered through settings,
and supplier/tax journals are read once rather than copied into bank vouchers.
"""
from __future__ import annotations

import json

import accounting
import accounting_periods
import ledger_reports
import payables
import supplier_payments
import treasury
import treasury_reports


KINDS = {'receipt': ('BRC', 'إيداع أو متحصل بنكي'),
         'payment': ('BPY', 'سداد بنكي مباشر'),
         'fee': ('BFE', 'مصاريف وعمولات بنكية'),
         'transfer': ('BTR', 'تحويل بين البنك والخزينة أو بنك آخر')}
FIELDS = ('kind', 'business_date', 'value_date', 'reference', 'bank_reference',
          'amount_cents', 'currency', 'account_id', 'counter_account_id',
          'destination_account_id', 'party_name', 'department_id', 'notes')


def initialize(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS bank_accounts(
            id INTEGER PRIMARY KEY,chart_account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
            bank_name TEXT NOT NULL,branch TEXT NOT NULL DEFAULT '',
            account_number TEXT NOT NULL DEFAULT '',iban TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS bank_documents(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,prepared_fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN('receipt','payment','fee','transfer')),
            business_date TEXT NOT NULL,value_date TEXT NOT NULL,reference TEXT NOT NULL,
            bank_reference TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            currency TEXT NOT NULL DEFAULT 'EGP' CHECK(currency='EGP'),
            account_id INTEGER NOT NULL REFERENCES accounts(id),counter_account_id INTEGER REFERENCES accounts(id),
            destination_account_id INTEGER REFERENCES accounts(id),party_name TEXT NOT NULL DEFAULT '',
            department_id INTEGER REFERENCES departments(id),notes TEXT NOT NULL DEFAULT '',
            account_snapshot TEXT NOT NULL,destination_snapshot TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS bank_reversals(
            id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL UNIQUE REFERENCES bank_documents(id),
            business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS opening_bank_cheques(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,opening_line_id INTEGER NOT NULL UNIQUE,
            journal_id INTEGER NOT NULL REFERENCES journal_entries(id),
            liability_account_id INTEGER NOT NULL REFERENCES accounts(id),
            bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
            beneficiary TEXT NOT NULL,supplier_id INTEGER REFERENCES suppliers(id),
            cheque_number TEXT NOT NULL,bank_name TEXT NOT NULL,source_date TEXT NOT NULL,
            due_date TEXT NOT NULL,business_date TEXT NOT NULL,reference TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS bank_cheque_clearances(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,cheque_id INTEGER NOT NULL UNIQUE REFERENCES opening_bank_cheques(id),
            business_date TEXT NOT NULL,value_date TEXT NOT NULL,reference TEXT NOT NULL,bank_reference TEXT NOT NULL,
            bank_account_id INTEGER NOT NULL REFERENCES accounts(id),amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS bank_documents_date ON bank_documents(status,business_date)',
        'CREATE INDEX IF NOT EXISTS bank_documents_reference ON bank_documents(kind,account_id,reference)',
    ):
        db.execute(statement)
    for table, condition in (('bank_documents', "OLD.status='posted'"), ('bank_reversals', '1'),
                             ('opening_bank_cheques', '1'), ('bank_cheque_clearances', '1')):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'posted bank document is immutable'); END''')


def _is_local_bank(account):
    return (account['code'].startswith('112') and account['normal_side'] == 'debit'
            and account['code'] != '112002' and account.get('role') != 'foreign_bank')


def _bank_account(db, ident):
    account = payables._posting_account(db, ident)
    if not _is_local_bank(account):
        raise ValueError('اختر حساب بنك محلي بالجنيه المصري؛ العملات الأجنبية لها إعداد مستقل')
    return account


def _liquid_account(db, ident):
    account = payables._posting_account(db, ident)
    if account['code'].startswith('111'):
        return treasury._cash_account(db, ident)
    return _bank_account(db, ident)


def _snapshot(db, account):
    row = db.execute('SELECT * FROM bank_accounts WHERE chart_account_id=?', (account['id'],)).fetchone()
    return {'id': account['id'], 'code': account['code'], 'name': account['name'],
            **({key: row[key] for key in ('bank_name', 'branch', 'account_number', 'iban')} if row else {})}


def save_account(db, data, account_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات الحساب البنكي غير صحيحة')
    if data.get('currency', 'EGP') != 'EGP':
        raise ValueError('ملف البنك المحلي بالجنيه المصري فقط')
    old = payables._require(db, 'bank_accounts', account_id) if account_id else None
    if old:
        payables._revision(data, old)
        chart_id = data.get('chart_account_id', old['chart_account_id'])
        if chart_id != old['chart_account_id']:
            raise ValueError('لا ينقل ملف البنك إلى حساب أستاذ آخر؛ أنشئ ملفًا مستقلًا')
    else:
        chart_id = data.get('chart_account_id')
    name = payables._text(data, 'bank_name', 150, True)
    branch = payables._text(data, 'branch', 150)
    number = payables._text(data, 'account_number', 100)
    iban = payables._text(data, 'iban', 50).replace(' ', '').upper()
    if iban and (not iban.isascii() or not iban.isalnum() or not 15 <= len(iban) <= 34):
        raise ValueError('راجع صيغة IBAN؛ الحقل اختياري ولا يتحقق النظام من ملكية الحساب خارجيًا')
    if chart_id is None and data.get('create_account') is True and not old:
        parent = db.execute("SELECT id FROM accounts WHERE code='112000' AND posting=0").fetchone()
        if parent is None:
            raise ValueError('تجميع الحسابات البنكية غير موجود')
        created = accounting.create_account(db, {'parent_id': parent['id'], 'posting': True,
            'normal_side': 'debit', 'name': name + (' • ' + number[-4:] if number else ''),
            'notes': 'حساب بنك محلي؛ الرصيد الافتتاحي من إعدادات الأرصدة فقط.'})
        chart_id = created['id']
    _bank_account(db, chart_id)
    other = db.execute('SELECT id FROM bank_accounts WHERE chart_account_id=?', (chart_id,)).fetchone()
    if other and (not old or other['id'] != old['id']):
        raise accounting.ConflictError('حساب الأستاذ مرتبط بملف بنك بالفعل')
    stamp = payables._now()
    if old:
        db.execute('UPDATE bank_accounts SET bank_name=?,branch=?,account_number=?,iban=?,revision=revision+1,updated_at=? WHERE id=?',
                   (name, branch, number, iban, stamp, old['id']))
    else:
        account_id = db.execute('''INSERT INTO bank_accounts(chart_account_id,bank_name,branch,account_number,iban,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)''', (chart_id, name, branch, number, iban, stamp, stamp)).lastrowid
    accounting._audit(db, 'bank_account', account_id, 'update' if old else 'create', {'chart_account_id': chart_id})
    return _master(db, account_id)


def _master(db, ident):
    row = db.execute('''SELECT b.*,a.code account_code,a.name account_name,a.active
        FROM bank_accounts b JOIN accounts a ON a.id=b.chart_account_id WHERE b.id=?''', (ident,)).fetchone()
    if row is None:
        raise ValueError('ملف البنك غير موجود')
    return dict(row)


def config(db):
    base = treasury.config(db)
    accounts = [dict(row) for row in db.execute('SELECT * FROM accounts WHERE active=1 AND posting=1 ORDER BY code')
                if _is_local_bank(dict(row))]
    return {'bank_accounts': accounts, 'cash_accounts': base['cash_accounts'],
            'liquid_accounts': accounts + base['cash_accounts'],
            'counter_accounts_by_kind': {**base['counter_accounts_by_kind'], 'fee': base['counter_accounts_by_kind']['payment']},
            'departments': base['departments'],
            'masters': [_master(db, row['id']) for row in db.execute('SELECT id FROM bank_accounts ORDER BY id')],
            'currency': accounting.get_settings(db)['base_currency'],
            'opening_status': accounting.opening_status(db),
            'notes': ['الأرصدة الافتتاحية تدخل من إعدادات الأرصدة؛ ملف البنك لا ينشئ رصيدًا.',
                      'سداد الموردين والشيكات والتوريدات لها قيودها الأصلية وتظهر في كشف البنك مرة واحدة.',
                      'المتاح حركات محلية بالجنيه وكشف دفتري؛ مطابقة كشف البنك الخارجي مرحلة مستقلة.',
                      'الحسابات الأجنبية لا تستخدم في حركة محلية دون إعداد العملة وسعر الصرف.']}


def _payload(data):
    if not isinstance(data, dict) or data.get('kind') not in KINDS:
        raise ValueError('اختر نوع المستند البنكي')
    if data.get('currency', 'EGP') != 'EGP':
        raise ValueError('المستند البنكي في هذه المرحلة بالجنيه المصري فقط')
    kind = data['kind']
    counter = data.get('counter_account_id')
    destination = data.get('destination_account_id')
    department = data.get('department_id')
    if kind == 'transfer':
        if counter is not None:
            raise ValueError('التحويل يحدد الحساب المستلم دون حساب مقابل آخر')
        destination = payables._id(destination)
        department = None
        party = ''
    else:
        if destination is not None:
            raise ValueError('اختر التحويل لنقل مبلغ بين حسابين')
        counter = payables._id(counter)
        party = payables._text(data, 'party_name', 200, True)
        if kind in ('payment', 'fee'):
            department = payables._id(department)
        elif department is not None:
            payables._id(department)
    return {'kind': kind, 'business_date': payables._date(data.get('business_date'), 'تاريخ القيد'),
            'value_date': payables._date(data.get('value_date'), 'تاريخ القيمة البنكية'),
            'reference': payables._text(data, 'reference', 100, True),
            'bank_reference': payables._text(data, 'bank_reference', 150, True),
            'amount_cents': treasury._amount(data), 'currency': 'EGP',
            'account_id': payables._id(data.get('account_id')), 'counter_account_id': counter,
            'destination_account_id': destination, 'party_name': party,
            'department_id': department, 'notes': payables._text(data, 'notes', 2000)}


def _preview(db, value):
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('راجع العملة الأساسية EGP قبل حفظ حركة بنكية بالجنيه')
    kind, amount = value['kind'], value['amount_cents']
    source = _liquid_account(db, value['account_id']) if kind == 'transfer' else _bank_account(db, value['account_id'])
    target = (_liquid_account(db, value['destination_account_id']) if kind == 'transfer'
              else treasury._counter_account(db, value['counter_account_id'], 'payment' if kind in ('payment', 'fee') else 'receipt'))
    if source['id'] == target['id']:
        raise ValueError('الحساب المرسل والمستلم يجب أن يكونا مختلفين')
    if kind == 'transfer' and not any(a['code'].startswith('112') for a in (source, target)):
        raise ValueError('التحويل البنكي يتضمن بنكًا واحدًا على الأقل؛ تحويل الخزائن من مساره')
    department = payables._require(db, 'departments', value['department_id']) if value['department_id'] else None
    dr, cr = (source, target) if kind == 'receipt' else (target, source)
    lines = []
    for account, debit, credit in ((dr, amount, 0), (cr, 0, amount)):
        dep = department if kind in ('payment', 'fee') and account['id'] == target['id'] else None
        lines.append({'account_id': account['id'], 'account_code': account['code'], 'account_name': account['name'],
                      'debit_cents': debit, 'credit_cents': credit, 'department_id': dep['id'] if dep else None,
                      'department_name': dep['name'] if dep else None})
    before = supplier_payments.account_balance(db, source['id'], value['business_date'])
    after = before + amount if kind == 'receipt' else before - amount
    warnings = []
    if after < 0:
        warnings.append('رصيد الحركات المسجلة سالب بعد العملية؛ راجع الأرصدة الافتتاحية والحركات قبل الاعتماد.')
    result = {**value, 'lines': lines, 'journal': {'lines': lines, 'debit_cents': amount, 'credit_cents': amount},
              'account_code': source['code'], 'account_name': source['name'],
              'counter_account_code': target['code'] if kind != 'transfer' else None,
              'counter_account_name': target['name'] if kind != 'transfer' else None,
              'destination_account_code': target['code'] if kind == 'transfer' else None,
              'destination_account_name': target['name'] if kind == 'transfer' else None,
              'account_snapshot': _snapshot(db, source),
              'destination_snapshot': _snapshot(db, target) if kind == 'transfer' else {},
              'balance_before_cents': before, 'balance_after_cents': after,
              'warnings': warnings, 'can_approve': True}
    if kind == 'transfer':
        receiving = supplier_payments.account_balance(db, target['id'], value['business_date'])
        result.update(destination_balance_before_cents=receiving, destination_balance_after_cents=receiving + amount)
    return result


def _prepared(plan):
    return treasury._digest({'account': plan['account_snapshot'], 'destination': plan['destination_snapshot'],
                             'lines': [{key: row[key] for key in ('account_id', 'debit_cents', 'credit_cents', 'department_id')} for row in plan['lines']]})


def preview(db, data):
    return _preview(db, _payload(data))


def _unique_reference(db, value, exclude=0):
    if db.execute('''SELECT 1 FROM bank_documents d WHERE d.kind=? AND d.account_id=? AND d.reference=?
        AND d.status<>'cancelled' AND d.id<>? AND NOT EXISTS(SELECT 1 FROM bank_reversals r
        WHERE r.document_id=d.id AND r.business_date<=?)''',
        (value['kind'], value['account_id'], value['reference'], exclude, value['business_date'])).fetchone():
        raise accounting.ConflictError('مرجع المستند مستخدم لنفس نوع الحركة والحساب البنكي')


def save(db, data, document_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات المستند البنكي غير صحيحة')
    old = payables._require(db, 'bank_documents', document_id) if document_id else None
    if old:
        if old['status'] == 'cancelled' and data.get('action') == 'cancel':
            return detail(db, old['id'])
        payables._revision(data, old)
        if old['status'] != 'draft':
            raise ValueError('المستند المعتمد أو الملغى لا يعدل؛ التصحيح بمستند مستقل')
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE bank_documents SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), old['id']))
            accounting._audit(db, 'bank_document', old['id'], 'cancel_draft', {})
            return detail(db, old['id'])
    value = _payload(data)
    digest = treasury._digest(value)
    key = old['request_key'] if old else payables._text(data, 'request_key', 100, True)
    prior = db.execute('SELECT id,request_fingerprint FROM bank_documents WHERE request_key=?', (key,)).fetchone()
    if old is None and prior:
        if prior['request_fingerprint'] != digest:
            raise accounting.ConflictError('مفتاح الحفظ مستخدم لمستند بنكي مختلف')
        return detail(db, prior['id'])
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    plan = _preview(db, value)
    _unique_reference(db, value, old['id'] if old else 0)
    stamp = payables._now()
    snapshots = (json.dumps(plan['account_snapshot'], ensure_ascii=False), json.dumps(plan['destination_snapshot'], ensure_ascii=False))
    if old:
        if value['kind'] != old['kind']:
            raise ValueError('لا يتغير نوع المستند المحفوظ؛ أنشئ مسودة من النوع المطلوب')
        db.execute(f"UPDATE bank_documents SET request_fingerprint=?,prepared_fingerprint=?,{','.join(f+'=?' for f in FIELDS)},account_snapshot=?,destination_snapshot=?,revision=revision+1,updated_at=? WHERE id=?",
                   (digest, _prepared(plan), *(value[f] for f in FIELDS), *snapshots, stamp, old['id']))
        document_id = old['id']
    else:
        code = payables._code(db, 'bank_' + value['kind'], KINDS[value['kind']][0])
        document_id = db.execute(f"INSERT INTO bank_documents(code,request_key,request_fingerprint,prepared_fingerprint,{','.join(FIELDS)},account_snapshot,destination_snapshot,created_at,updated_at) VALUES(?,?,?,?,{','.join('?' for _ in FIELDS)},?,?,?,?)",
                                (code, key, digest, _prepared(plan), *(value[f] for f in FIELDS), *snapshots, stamp, stamp)).lastrowid
    accounting._audit(db, 'bank_document', document_id, 'update_draft' if old else 'create_draft', {'kind': value['kind'], 'amount_cents': value['amount_cents']})
    return detail(db, document_id)


def approve(db, document_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات اعتماد البنك غير صحيحة')
    old = payables._require(db, 'bank_documents', document_id)
    if old['status'] == 'posted':
        return detail(db, old['id'])
    payables._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('اعتماد البنك متاح للمسودة فقط')
    accounting_periods.assert_open(db, old['business_date'])
    value = {f: old[f] for f in FIELDS}
    if treasury._digest(value) != old['request_fingerprint']:
        raise accounting.ConflictError('بيانات المسودة تغيرت؛ أعد حفظها ومراجعتها')
    plan = _preview(db, value)
    if _prepared(plan) != old['prepared_fingerprint']:
        raise accounting.ConflictError('بيانات الحساب البنكي تغيرت؛ أعد حفظ المسودة ومراجعتها')
    _unique_reference(db, value, old['id'])
    journal_id = payables.create_journal(db, 'bank_' + old['kind'], old['id'], old['business_date'],
        [{key: row[key] for key in ('account_id', 'debit_cents', 'credit_cents', 'department_id')} for row in plan['lines']])
    db.execute("UPDATE bank_documents SET status='posted',journal_id=?,revision=revision+1,updated_at=? WHERE id=?", (journal_id, payables._now(), old['id']))
    accounting._audit(db, 'bank_document', old['id'], 'post', {'journal_id': journal_id, 'amount_cents': old['amount_cents']})
    return detail(db, old['id'])


def reverse(db, document_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات تصحيح البنك غير صحيحة')
    old = payables._require(db, 'bank_documents', document_id)
    value = {'document_id': old['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ العكس'),
             'reference': payables._text(data, 'reference', 100, True), 'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    event = data.get('event_revision', data.get('adjustment_revision', 0))
    if type(data.get('revision')) is not int or type(event) is not int:
        raise accounting.ConflictError('أعد فتح المستند قبل عكسه')
    if 'event_revision' in data and 'adjustment_revision' in data and data['event_revision'] != data['adjustment_revision']:
        raise accounting.ConflictError('حالة تصحيح البنك تغيرت')
    prior = db.execute('SELECT * FROM bank_reversals WHERE document_id=? OR request_key=?', (document_id, key)).fetchone()
    if prior:
        if prior['document_id'] != document_id or prior['request_key'] != key or prior['request_fingerprint'] != treasury._digest(value):
            raise accounting.ConflictError('المستند معكوس أو مفتاح التصحيح مستخدم لبيانات أخرى')
        return detail(db, document_id)
    if old['status'] != 'posted':
        raise ValueError('العكس متاح للمستند البنكي المعتمد فقط')
    payables._revision(data, old)
    if event != 0:
        raise accounting.ConflictError('حالة المستند تغيرت؛ أعد فتحه')
    accounting_periods.assert_open(db, value['business_date'])
    if value['business_date'] < old['business_date']:
        raise ValueError('تاريخ العكس لا يسبق تاريخ المستند الأصلي')
    original = payables.journal_detail(db, old['journal_id'])
    if original['source_type'] != 'bank_' + old['kind'] or original['source_id'] != document_id:
        raise ValueError('القيد الأصلي لا يطابق المستند البنكي')
    reversal_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM bank_reversals').fetchone()[0]
    journal_id = payables.create_journal(db, 'bank_reversal', reversal_id, value['business_date'],
        [{'account_id': row['account_id'], 'department_id': row['department_id'], 'supplier_id': row['supplier_id'],
          'source_line_id': row['source_line_id'], 'debit_cents': row['credit_cents'], 'credit_cents': row['debit_cents']} for row in original['lines']])
    db.execute('''INSERT INTO bank_reversals(id,document_id,business_date,reference,reason,request_key,request_fingerprint,journal_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (reversal_id, document_id, value['business_date'], value['reference'], value['reason'], key, treasury._digest(value), journal_id, payables._now()))
    accounting._audit(db, 'bank_reversal', reversal_id, 'reverse', {'document_id': document_id, 'journal_id': journal_id, 'reason': value['reason']})
    return detail(db, document_id)


def detail(db, document_id, as_of=None):
    document = payables._require(db, 'bank_documents', document_id)
    document.pop('request_fingerprint', None)
    document.pop('prepared_fingerprint', None)
    source = json.loads(document['account_snapshot'])
    destination = json.loads(document['destination_snapshot'])
    document['account_snapshot'], document['destination_snapshot'] = source, destination
    document.update(account_code=source.get('code'), account_name=source.get('name'),
                    bank_name=source.get('bank_name'), account_number=source.get('account_number'), iban=source.get('iban'),
                    destination_account_code=destination.get('code'), destination_account_name=destination.get('name'))
    counter = payables._require(db, 'accounts', document['counter_account_id']) if document['counter_account_id'] else None
    department = payables._require(db, 'departments', document['department_id']) if document['department_id'] else None
    document.update(counter_account_code=counter['code'] if counter else None,
                    counter_account_name=counter['name'] if counter else None,
                    department_name=department['name'] if department else None,
                    cost_code=department['cost_code'] if department else None)
    document['journal'] = payables.journal_detail(db, document['journal_id']) if document['journal_id'] else None
    end = payables._date(as_of if as_of is not None else '9999-12-31', 'تاريخ التقرير')
    reversal = db.execute('SELECT * FROM bank_reversals WHERE document_id=? AND business_date<=?', (document_id, end)).fetchone()
    document['reversal'] = dict(reversal) if reversal else None
    if reversal:
        document['reversal'].pop('request_fingerprint', None)
        document['reversal'].update(code=f"BRV-{reversal['id']:06d}", journal=payables.journal_detail(db, reversal['journal_id']))
    document.update(effective_status='reversed' if reversal else document['status'], adjustment_revision=1 if reversal else 0,
                    voucher_title=KINDS[document['kind']][1])
    return document


def list_documents(db, as_of=None):
    end = payables._date(as_of, 'تاريخ التقرير') if as_of is not None else '9999-12-31'
    return [detail(db, row['id'], end) for row in db.execute('SELECT id FROM bank_documents WHERE business_date<=? ORDER BY business_date DESC,id DESC', (end,))]


def _report_account(db, account_id=None):
    accounts = [dict(row) for row in db.execute("SELECT * FROM accounts WHERE posting=1 AND code LIKE '112%' ORDER BY code")]
    if account_id is None:
        selected = next((a for a in accounts if a['active'] and _is_local_bank(a)), None)
        if selected is None:
            raise ValueError('لا يوجد حساب بنكي لعرض حركته')
        return selected
    account_id = payables._id(account_id)
    selected = next((a for a in accounts if a['id'] == account_id), None)
    if selected is None:
        raise ValueError('اختر حساب بنك تفصيليًا موجودًا')
    return selected


def book(db, account_id=None, from_date=None, to_date=None):
    account = _report_account(db, account_id)
    result = ledger_reports.account_ledger(db, account['id'], from_date, to_date)
    for row in result['rows']:
        metadata = treasury_reports._origin(db, row['source_type'], row['source_id'], row['journal_id'])
        row.update({key: value for key, value in metadata.items() if value is not None})
    return {**result, 'account_id': account['id'], 'account_code': account['code'], 'account_name': account['name'],
            'notes': ['كشف دفتري من القيود المرحّلة مرة واحدة، وليس مطابقة كشف البنك الخارجي.',
                      'رصيد قبل الفترة يشمل القيود السابقة ومنها الافتتاح المعتمد من الإعدادات.',
                      'فتح المستند يعرض حالته الحالية مع بقاء الحركة بالتاريخ المختار.',
                      'الحساب الأجنبي التاريخي يعرض بمبالغ القيود الدفترية المسجلة؛ لا يجري هذا الكشف تحويل عملات.']}


def position(db, as_of=None):
    _, end = ledger_reports._range(None, as_of)
    accounts, exceptions, seen = [], [], set()
    for row in db.execute("SELECT * FROM accounts WHERE posting=1 AND code LIKE '112%' ORDER BY code"):
        balance = supplier_payments.account_balance(db, row['id'], end)
        accounts.append({'id': row['id'], 'account_id': row['id'], 'code': row['code'], 'name': row['name'],
                         'active': bool(row['active']), 'local_operations': _is_local_bank(dict(row)), 'balance_cents': balance})
    for row in db.execute("SELECT id FROM accounts WHERE code LIKE '112%'"):
        for issue in ledger_reports._exceptions(db, end, row['id']):
            identity = issue['type'], issue.get('journal_id'), issue.get('line_id')
            if identity not in seen:
                seen.add(identity)
                exceptions.append(issue)
    return {'as_of': end, 'currency': accounting.get_settings(db)['base_currency'], 'accounts': accounts,
            'totals': {'balance_cents': sum(a['balance_cents'] for a in accounts)},
            'opening_status': accounting.opening_status(db, end),
            'complete': not exceptions, 'exceptions': exceptions,
            'notes': ['أرصدة دفتريّة من القيود المسجلة؛ التسوية مع كشف البنك الخارجي مستقلة.',
                      'الأرصدة الافتتاحية من إعدادات الأرصدة، ولا يضيف ملف البنك أو هذا الكشف رصيدًا افتراضيًا.']}


def issued_cheque_account_ids(db):
    """All known issued-cheque controls stay protected after a mapping change."""
    result = {r['id'] for r in db.execute("SELECT id FROM accounts WHERE code='217004'")}
    result.update(r['account_id'] for r in db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role='issued_cheques'"))
    if ledger_reports._has(db, 'opening_bank_cheques'):
        result.update(r['liability_account_id'] for r in db.execute('SELECT DISTINCT liability_account_id FROM opening_bank_cheques'))
    if ledger_reports._has(db, 'supplier_payments'):
        has_tax = ledger_reports._has(db, 'payment_withholding')
        tax_join = ' LEFT JOIN payment_withholding w ON w.payment_id=p.id' if has_tax else ''
        tax_guard = ' AND (w.account_id IS NULL OR w.account_id<>l.account_id) AND l.credit_cents=COALESCE(w.net_cash_cents,p.amount_cents)' if has_tax else ' AND l.credit_cents=p.amount_cents'
        result.update(r['account_id'] for r in db.execute('''SELECT DISTINCT l.account_id FROM supplier_payments p
            JOIN journal_entries j ON j.id=p.journal_entry_id AND j.source_type='supplier_payment' AND j.source_id=p.id AND j.sealed=1
            JOIN journal_lines l ON l.journal_id=j.id JOIN accounts a ON a.id=l.account_id''' + tax_join + '''
            WHERE p.method='cheque' AND p.status='posted' AND l.credit_cents>0 AND a.code LIKE '2%' ''' + tax_guard))
    return result


def _other_control_ids(db):
    from supplier_reconciliation import _account_ids
    payable, advance = _account_ids(db)
    result = payable | advance
    result.update(r['account_id'] for r in db.execute("""SELECT account_id FROM account_mappings WHERE role IN
        ('payable_control','capital_payable_control','supplier_advances','operating_custodies','withholding_payable','opening_clearing')"""))
    result.update(r['id'] for r in db.execute("SELECT id FROM accounts WHERE role IN('withholding_payable','operating_custodies','opening_balance_clearing') OR code IN('213004','115003')"))
    for table, column in (('payment_withholding', 'account_id'), ('withholding_remittance_allocations', 'account_id'),
                          ('opening_withholding_sources', 'account_id'), ('opening_remittance_allocations', 'account_id'),
                          ('custody_documents', 'custody_account_id')):
        if ledger_reports._has(db, table):
            result.update(r[0] for r in db.execute(f'SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL'))
    return result


def _cheque_unique(db, bank_id, bank_name, number, opening_line_id=None):
    name_key, number_key = bank_name.casefold(), number.casefold()
    for row in db.execute('SELECT * FROM opening_bank_cheques'):
        if row['opening_line_id'] == opening_line_id:
            continue
        if (row['bank_account_id'] == bank_id or row['bank_name'].casefold() == name_key) and row['cheque_number'].casefold() == number_key:
            raise accounting.ConflictError('رقم الشيك مسجل بالفعل على نفس البنك ضمن الشيكات الافتتاحية')
    if ledger_reports._has(db, 'supplier_payments'):
        for row in db.execute("SELECT funding_account_id,cheque_bank_key,cheque_number_key FROM supplier_payments WHERE method='cheque' AND status<>'cancelled'"):
            if (row['funding_account_id'] == bank_id or row['cheque_bank_key'] == name_key) and row['cheque_number_key'] == number_key:
                raise accounting.ConflictError('رقم الشيك مسجل بالفعل في دورة الموردين؛ لا يعاد إدخاله افتتاحيًا')


def prepare_opening_cheque(db, line, business_date):
    """Normalize an outstanding NET cheque; opening core owns its balanced GL."""
    if not isinstance(line, dict):
        raise ValueError('بيانات الشيك الافتتاحي غير صحيحة')
    extra = line.get('extra')
    if extra is None:
        extra = json.loads(line.get('extra_json') or '{}')
    if not isinstance(extra, dict):
        raise ValueError('تفاصيل الشيك الافتتاحي غير صحيحة')
    business_date = payables._date(business_date, 'تاريخ الافتتاح')
    bank_id = payables._id(extra.get('bank_account_id', line.get('bank_account_id')))
    bank = _bank_account(db, bank_id)
    snapshot = _snapshot(db, bank)
    details = {key: payables._text({key: extra.get(key, line.get(key, snapshot.get(key, '')))}, key, limit, True)
               for key, limit in (('beneficiary', 200), ('cheque_number', 100), ('bank_name', 150))}
    source_date = payables._date(line.get('source_date', extra.get('source_date')), 'تاريخ إصدار الشيك')
    due_date = payables._date(line.get('due_date', extra.get('due_date')), 'استحقاق الشيك')
    if source_date > business_date or due_date < source_date:
        raise ValueError('إصدار الشيك لا يتجاوز الافتتاح، والاستحقاق لا يسبق الإصدار')
    supplier_id = line.get('supplier_id')
    if supplier_id is not None:
        supplier_id = payables._id(supplier_id)
        payables._require(db, 'suppliers', supplier_id)
    mapped = payables.mapping_account(db, 'الموردون', 'issued_cheques', require_confirmed=False)
    liability_id = line.get('account_id') or mapped['id']
    liability = payables._posting_account(db, liability_id)
    if liability_id not in issued_cheque_account_ids(db) or liability_id in _other_control_ids(db) or liability['normal_side'] != 'credit' or not liability['code'].startswith('2'):
        raise ValueError('الشيك الافتتاحي على حساب التزام الشيكات الصادرة فقط')
    if line.get('side', 'credit') != 'credit':
        raise ValueError('الشيك الصادر الافتتاحي التزام دائن بصافي قيمة الشيك')
    _cheque_unique(db, bank_id, details['bank_name'], details['cheque_number'], line.get('id'))
    return {**line, 'kind': 'issued_cheque', 'account_id': liability_id, 'side': 'credit',
            'amount_cents': treasury._amount(line), 'source_date': source_date, 'due_date': due_date,
            'supplier_id': supplier_id, 'extra': {**details, 'bank_account_id': bank_id}}


def post_opening_cheque(db, line, journal_id):
    """Attach an immutable opening source after its original GL was sealed."""
    prior = db.execute('SELECT id,journal_id FROM opening_bank_cheques WHERE opening_line_id=?', (line['id'],)).fetchone()
    if prior:
        if prior['journal_id'] != journal_id:
            raise accounting.ConflictError('الشيك الافتتاحي مرتبط بقيد آخر')
        return {**opening_cheque_detail(db, prior['id']), 'document_type': 'opening_bank_cheque',
                'document_id': prior['id'], 'cheque_id': prior['id']}
    journal = payables.journal_detail(db, journal_id)
    if journal['source_type'] != 'opening_balance' or journal['source_id'] != line['id'] or not journal['sealed']:
        raise ValueError('الشيك الافتتاحي يحتاج قيد الافتتاح الأصلي المعتمد')
    value = prepare_opening_cheque(db, line, journal['entry_date'])
    liabilities = [r for r in journal['lines'] if r['account_id'] == value['account_id'] and r['credit_cents'] == value['amount_cents']]
    if len(liabilities) != 1:
        raise ValueError('قيد الافتتاح لا يطابق صافي الشيك الصادر')
    extra = value['extra']
    cheque_id = db.execute('''INSERT INTO opening_bank_cheques(code,opening_line_id,journal_id,liability_account_id,
        bank_account_id,beneficiary,supplier_id,cheque_number,bank_name,source_date,due_date,business_date,reference,amount_cents,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (payables._code(db, 'opening_bank_cheque', 'OCH'), line['id'], journal_id, value['account_id'], extra['bank_account_id'],
         extra['beneficiary'], value['supplier_id'], extra['cheque_number'], extra['bank_name'], value['source_date'],
         value['due_date'], journal['entry_date'], value.get('reference') or extra['cheque_number'], value['amount_cents'], payables._now())).lastrowid
    return {**opening_cheque_detail(db, cheque_id), 'document_type': 'opening_bank_cheque',
            'document_id': cheque_id, 'cheque_id': cheque_id}


def opening_cheque_detail(db, cheque_id, as_of=None):
    row = payables._require(db, 'opening_bank_cheques', cheque_id)
    end = payables._date(as_of or '9999-12-31', 'تاريخ الموقف')
    cleared = db.execute('SELECT * FROM bank_cheque_clearances WHERE cheque_id=? AND business_date<=?', (cheque_id, end)).fetchone()
    clearance = dict(cleared) if cleared else None
    if clearance:
        clearance.pop('request_fingerprint', None)
        clearance['journal'] = payables.journal_detail(db, clearance['journal_id'])
    bank = payables._require(db, 'accounts', row['bank_account_id'])
    liability = payables._require(db, 'accounts', row['liability_account_id'])
    return {**row, 'party_name': row['beneficiary'], 'bank_account_code': bank['code'], 'bank_account_name': bank['name'],
            'liability_account_code': liability['code'], 'liability_account_name': liability['name'],
            'journal': payables.journal_detail(db, row['journal_id']), 'clearance': clearance,
            'effective_status': 'cleared' if clearance else 'outstanding', 'revision': 1,
            'notes': 'شيك سابق للافتتاح بصافي القيمة؛ لا ينشئ سداد مورد أو خصمًا ضريبيًا جديدًا.'}


def list_opening_cheques(db, as_of=None):
    end = payables._date(as_of or '9999-12-31', 'تاريخ الموقف')
    return [opening_cheque_detail(db, row['id'], end) for row in db.execute(
        'SELECT id FROM opening_bank_cheques WHERE business_date<=? ORDER BY due_date,id', (end,))]


def _validate_opening_cheque_source(db, row):
    """Do not clear an orphaned, borrowed or mismatched opening source."""
    if not ledger_reports._has(db, 'opening_lines') or not ledger_reports._has(db, 'opening_batches'):
        raise ValueError('الشيك يحتاج بندًا تفصيليًا ضمن افتتاح معتمد')
    line = db.execute('SELECT * FROM opening_lines WHERE id=?', (row['opening_line_id'],)).fetchone()
    batch = db.execute('SELECT * FROM opening_batches WHERE id=?', (line['batch_id'],)).fetchone() if line else None
    if line is None or batch is None or batch['status'] != 'posted' or not batch['posted_at']:
        raise ValueError('الشيك يحتاج بندًا تفصيليًا ضمن افتتاح معتمد')
    expected = {'kind': 'issued_cheque', 'side': 'credit', 'account_id': row['liability_account_id'],
                'amount_cents': row['amount_cents'], 'supplier_id': row['supplier_id'],
                'business_date': row['business_date'], 'source_date': row['source_date'],
                'due_date': row['due_date'], 'reference': row['reference'], 'journal_id': row['journal_id'],
                'document_type': 'opening_bank_cheque', 'document_id': row['id']}
    if any(line[key] != value for key, value in expected.items()) or batch['business_date'] != row['business_date']:
        raise ValueError('هوية الشيك لا تطابق بند الافتتاح الأصلي')
    try:
        prepared, extra = json.loads(line['prepared_json']), json.loads(line['extra_json'])
    except (ValueError, TypeError):
        raise ValueError('تفاصيل مصدر الشيك غير صحيحة') from None
    if not isinstance(prepared, dict) or not isinstance(extra, dict) or not isinstance(prepared.get('extra'), dict):
        raise ValueError('تفاصيل اعتماد الشيك الافتتاحي غير صحيحة')
    for key in ('kind', 'side', 'account_id', 'amount_cents', 'supplier_id', 'business_date', 'source_date', 'due_date', 'reference'):
        if prepared.get(key) != expected[key]:
            raise ValueError('بيانات اعتماد بند الافتتاح لا تطابق الشيك')
    for key in ('bank_account_id', 'beneficiary', 'cheque_number', 'bank_name'):
        if extra.get(key) != row[key] or prepared['extra'].get(key) != row[key]:
            raise ValueError('تفاصيل البنك والمستفيد لا تطابق الشيك الافتتاحي')
    original = payables.journal_detail(db, row['journal_id'])
    if not original['sealed'] or (original['source_type'], original['source_id'], original['entry_date']) != ('opening_balance', row['opening_line_id'], batch['business_date']):
        raise ValueError('هوية قيد الافتتاح لا تطابق الشيك')
    clearing = payables._require(db, 'accounts', batch['clearing_account_id'])
    if not clearing['posting'] or not clearing['code'].startswith('3') or clearing['normal_side'] != 'credit' or clearing['id'] == row['liability_account_id']:
        raise ValueError('حساب الافتتاح الوسيط غير صحيح')
    expected_lines = [(row['liability_account_id'], 0, row['amount_cents'], line['supplier_id'], line['department_id'], line['id']),
                      (batch['clearing_account_id'], row['amount_cents'], 0, None, None, None)]
    actual_lines = [tuple(l[key] for key in ('account_id', 'debit_cents', 'credit_cents', 'supplier_id', 'department_id', 'source_line_id')) for l in original['lines']]
    if len(actual_lines) != 2 or any(actual_lines.count(expected) != 1 for expected in expected_lines):
        raise ValueError('أطراف قيد الافتتاح لا تطابق التزام الشيك وحساب الافتتاح الوسيط')
    return original


def clear_opening_cheque(db, cheque_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات صرف الشيك غير صحيحة')
    row = payables._require(db, 'opening_bank_cheques', cheque_id)
    value = {'cheque_id': row['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ الصرف'),
             'value_date': payables._date(data.get('value_date'), 'تاريخ القيمة البنكية'),
             'reference': payables._text(data, 'reference', 100, True), 'bank_reference': payables._text(data, 'bank_reference', 150, True),
             'bank_account_id': payables._id(data.get('bank_account_id', row['bank_account_id'])), 'amount_cents': row['amount_cents']}
    if any(key in data and treasury._amount({key: data[key]}) != row['amount_cents'] for key in ('amount', 'amount_cents')):
        raise ValueError('صرف الشيك بكامل صافي قيمته المثبتة فقط')
    key = payables._text(data, 'request_key', 100, True)
    digest = treasury._digest(value)
    prior = db.execute('SELECT * FROM bank_cheque_clearances WHERE cheque_id=? OR request_key=?', (cheque_id, key)).fetchone()
    if prior:
        if prior['cheque_id'] != cheque_id or prior['request_key'] != key or prior['request_fingerprint'] != digest:
            raise accounting.ConflictError('الشيك مصروف أو مفتاح الصرف مستخدم ببيانات أخرى')
        return opening_cheque_detail(db, cheque_id)
    if type(data.get('revision')) is not int or data['revision'] != 1:
        raise accounting.ConflictError('أعد فتح الشيك الافتتاحي قبل إثبات الصرف')
    if value['business_date'] < row['business_date'] or value['business_date'] < row['source_date']:
        raise ValueError('صرف الشيك لا يسبق تاريخ الافتتاح أو الإصدار')
    if value['bank_account_id'] != row['bank_account_id']:
        raise ValueError('الصرف على البنك المسحوب عليه في الشيك الافتتاحي نفسه')
    accounting_periods.assert_open(db, value['business_date'])
    _bank_account(db, value['bank_account_id'])
    _validate_opening_cheque_source(db, row)
    ident = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM bank_cheque_clearances').fetchone()[0]
    journal_id = payables.create_journal(db, 'opening_cheque_clearance', ident, value['business_date'], [
        {'account_id': row['liability_account_id'], 'debit_cents': row['amount_cents'], 'credit_cents': 0},
        {'account_id': value['bank_account_id'], 'debit_cents': 0, 'credit_cents': row['amount_cents']}])
    db.execute('''INSERT INTO bank_cheque_clearances(id,code,cheque_id,business_date,value_date,reference,bank_reference,
        bank_account_id,amount_cents,request_key,request_fingerprint,journal_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (ident, payables._code(db, 'opening_cheque_clearance', 'OCC'), cheque_id, value['business_date'], value['value_date'],
         value['reference'], value['bank_reference'], value['bank_account_id'], row['amount_cents'], key, digest, journal_id, payables._now()))
    accounting._audit(db, 'opening_bank_cheque', cheque_id, 'clear', {'journal_id': journal_id, **value})
    return opening_cheque_detail(db, cheque_id)
