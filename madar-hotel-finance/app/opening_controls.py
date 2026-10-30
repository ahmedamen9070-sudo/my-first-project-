"""Opening custody and withholding sources; neither source fabricates cash flow."""
from __future__ import annotations

import hashlib
import json

import accounting
import payables


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def initialize(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS opening_custody_sources(
            document_id INTEGER PRIMARY KEY REFERENCES custody_documents(id),
            opening_line_id INTEGER NOT NULL UNIQUE REFERENCES opening_lines(id),
            opening_batch_id INTEGER NOT NULL REFERENCES opening_batches(id),
            clearing_account_id INTEGER NOT NULL REFERENCES accounts(id))''',
        '''CREATE TABLE IF NOT EXISTS opening_withholding_sources(
            id INTEGER PRIMARY KEY,opening_line_id INTEGER NOT NULL UNIQUE REFERENCES opening_lines(id),
            opening_batch_id INTEGER NOT NULL REFERENCES opening_batches(id),
            code TEXT NOT NULL UNIQUE,business_date TEXT NOT NULL,source_date TEXT NOT NULL,
            reference TEXT NOT NULL,account_id INTEGER NOT NULL REFERENCES accounts(id),
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            supplier_name TEXT NOT NULL,supplier_code TEXT NOT NULL,supplier_tax_number TEXT NOT NULL DEFAULT '',
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS opening_remittance_allocations(
            remittance_id INTEGER NOT NULL REFERENCES withholding_remittances(id),
            opening_source_id INTEGER NOT NULL REFERENCES opening_withholding_sources(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),payment_code TEXT NOT NULL,
            supplier_name TEXT NOT NULL,supplier_code TEXT NOT NULL,supplier_tax_number TEXT NOT NULL DEFAULT '',
            source_date TEXT NOT NULL,reference TEXT NOT NULL,
            PRIMARY KEY(remittance_id,opening_source_id))''',
        'CREATE INDEX IF NOT EXISTS opening_withholding_dates ON opening_withholding_sources(business_date,source_date)',
    ):
        db.execute(statement)
    for table in ('opening_custody_sources', 'opening_withholding_sources'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'opening source is immutable'); END''')
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_opening_remittance_{operation.lower()}
            BEFORE {operation} ON opening_remittance_allocations
            WHEN EXISTS(SELECT 1 FROM withholding_remittances WHERE id=OLD.remittance_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted opening remittance is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_opening_remittance_parent_{operation.lower()}
            BEFORE {operation} ON opening_remittance_allocations
            WHEN EXISTS(SELECT 1 FROM withholding_remittances WHERE id=NEW.remittance_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted opening remittance is immutable'); END''')


def prepare(db, line, business_date):
    result = dict(line)
    kind = line['kind']
    payables._money(line['amount_cents'], False)
    if line.get('department_id') is not None:
        raise ValueError('رصيد العهدة أو الخصم الافتتاحي ليس مصروف قسم؛ اترك مركز التكلفة فارغًا')
    if kind == 'custody':
        import custodies
        person = payables._require(db, 'custodians', line.get('custodian_id'))
        if not person['active']:
            raise ValueError('اختر صاحب عهدة نشطًا للرصيد الافتتاحي')
        account = custodies._custody_account(db, require_confirmed=False)
        side, supplier_id = 'debit', None
        result.update(custodian_id=person['id'], custodian_name=person['name'], custodian_code=person['code'])
        source_date = payables._date(line.get('source_date') or business_date, 'تاريخ العهدة السابق')
        if source_date > business_date:
            raise ValueError('تاريخ العهدة السابقة لا يلي تاريخ الافتتاح')
        result['source_date'] = source_date
    elif kind == 'withholding':
        import withholding
        supplier = payables._require(db, 'suppliers', line.get('supplier_id'))
        account = withholding._account(db, {}, require_confirmed=False)
        side, supplier_id = 'credit', supplier['id']
        source_date = payables._date(line.get('source_date'), 'تاريخ الخصم السابق')
        if source_date > business_date:
            raise ValueError('تاريخ مصدر الخصم الافتتاحي لا يلي تاريخ الافتتاح')
        result.update(custodian_id=None, supplier_name=supplier['name'], supplier_code=supplier['code'],
                      supplier_tax_number=supplier['tax_number'], source_date=source_date)
    else:
        raise ValueError('نوع رصيد افتتاحي غير مدعوم في هذا المسار')
    if line.get('side') not in (None, '', side):
        raise ValueError('جانب الرصيد الافتتاحي لا يناسب مصدره التفصيلي')
    if line.get('account_id') not in (None, account['id']):
        raise ValueError('حساب الرصيد الافتتاحي يحدد من الربط المعتمد')
    result.update(account_id=account['id'], side=side, supplier_id=supplier_id,
                  reference=payables._text(line, 'reference', 100, True))
    return result


def post_line(db, line, journal_id):
    if line['kind'] == 'custody':
        person = payables._require(db, 'custodians', line['custodian_id'])
        journal = payables.journal_detail(db, journal_id)
        clearing = [row['account_id'] for row in journal['lines'] if row['credit_cents'] == line['amount_cents']]
        if len(clearing) != 1:
            raise ValueError('قيد العهدة الافتتاحية لا يتضمن الحساب الوسيط المقابل')
        stamp = payables._now()
        code = payables._code(db, 'opening_custody', 'OCU')
        digest = hashlib.sha256(json.dumps(line, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        document_id = db.execute('''INSERT INTO custody_documents(code,request_key,request_fingerprint,
            prepared_fingerprint,kind,business_date,reference,custodian_id,cash_account_id,custody_account_id,
            amount_cents,purpose,notes,payload,custodian_name_snapshot,custodian_code_snapshot,status,
            journal_id,created_at,updated_at) VALUES(?,?,?,?,'issue',?,?,?,NULL,?,?,?,?,?,?,?,'posted',?,?,?)''',
            (code, 'opening-custody:' + str(line['id']), digest, digest, line['business_date'], line['reference'],
             person['id'], line['account_id'], line['amount_cents'], 'رصيد عهدة افتتاحي تحت التسوية',
             '', '{}', person['name'], person['code'], journal_id, stamp, stamp)).lastrowid
        db.execute('''INSERT INTO opening_custody_sources(document_id,opening_line_id,opening_batch_id,clearing_account_id)
            VALUES(?,?,?,?)''', (document_id, line['id'], line['batch_id'], clearing[0]))
        return {'document_type': 'custody_document', 'document_id': document_id}
    supplier = payables._require(db, 'suppliers', line['supplier_id'])
    code = payables._code(db, 'opening_withholding', 'OWH')
    ident = db.execute('''INSERT INTO opening_withholding_sources(opening_line_id,opening_batch_id,code,
        business_date,source_date,reference,account_id,supplier_id,amount_cents,supplier_name,
        supplier_code,supplier_tax_number,journal_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (line['id'], line['batch_id'], code, line['business_date'], line['source_date'], line['reference'],
         line['account_id'], supplier['id'], line['amount_cents'], supplier['name'], supplier['code'],
         supplier['tax_number'], journal_id, payables._now())).lastrowid
    return {'document_type': 'opening_withholding', 'document_id': ident}


def custody_origin(db, document_id):
    if not _has(db, 'opening_custody_sources'):
        return None
    row = db.execute('SELECT * FROM opening_custody_sources WHERE document_id=?', (document_id,)).fetchone()
    return dict(row) if row else None


def source_matches(db, kind, source):
    """Verify a detailed opening source against the immutable batch and line."""
    if not (_has(db, 'opening_lines') and _has(db, 'opening_batches')):
        return False
    row = db.execute('''SELECT l.*,b.status batch_status,b.clearing_account_id FROM opening_lines l
        JOIN opening_batches b ON b.id=l.batch_id WHERE l.id=?''', (source['opening_line_id'],)).fetchone()
    if row is None:
        return False
    row = dict(row)
    fields = ('amount_cents', 'business_date', 'reference', 'journal_id')
    expected = {field: source[field] for field in fields}
    expected['account_id'] = source['custody_account_id' if kind == 'custody' else 'account_id']
    if any(row[field] != value for field, value in expected.items()):
        return False
    if row['batch_status'] != 'posted' or row['kind'] != kind or row['batch_id'] != source['opening_batch_id']:
        return False
    if row['department_id'] is not None:
        return False
    journal = db.execute('SELECT * FROM journal_entries WHERE id=?', (source['journal_id'],)).fetchone()
    if journal is None or not journal['sealed'] or journal['source_type'] != 'opening_balance' or journal['source_id'] != row['id'] or journal['entry_date'] != row['business_date']:
        return False
    debit, credit = (row['amount_cents'], 0) if kind == 'custody' else (0, row['amount_cents'])
    expected_lines = [(row['account_id'], debit, credit, row['supplier_id'], None),
                      (row['clearing_account_id'], credit, debit, None, None)]
    actual_lines = [tuple(line) for line in db.execute('''SELECT account_id,debit_cents,credit_cents,supplier_id,department_id
        FROM journal_lines WHERE journal_id=?''', (source['journal_id'],))]
    sorter = lambda line: tuple(-1 if value is None else value for value in line)
    if sorted(actual_lines, key=sorter) != sorted(expected_lines, key=sorter):
        return False
    if kind == 'custody':
        return (row['side'] == 'debit' and row['custodian_id'] == source['custodian_id']
                and row['supplier_id'] is None and source['cash_account_id'] is None
                and row['clearing_account_id'] == source['clearing_account_id']
                and row['document_type'] == 'custody_document' and row['document_id'] == source['id'])
    return (row['side'] == 'credit' and row['supplier_id'] == source['supplier_id']
            and row['source_date'] == source['source_date'] and row['document_type'] == 'opening_withholding'
            and row['document_id'] == source['id'])


def withholding_sources(db, as_of):
    if not _has(db, 'opening_withholding_sources'):
        return []
    return [dict(row) for row in db.execute('''SELECT * FROM opening_withholding_sources
        WHERE business_date<=? ORDER BY business_date,id''', (as_of,))]
