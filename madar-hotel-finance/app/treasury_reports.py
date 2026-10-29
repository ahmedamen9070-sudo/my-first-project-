"""Physical EGP cash reports, read only and counted once from sealed journals.

Supplier disbursements, refunds and tax remittances already own their cash
journals.  These views read those lines alongside treasury and custody lines;
they never copy a source document or manufacture an opening cash balance.
"""
from __future__ import annotations

import accounting
import ledger_reports


SOURCE_LABELS = {
    'treasury_receipt': 'سند قبض خزينة',
    'treasury_payment': 'سند صرف خزينة',
    'treasury_transfer': 'تحويل بين الخزائن',
    'treasury_reversal': 'عكس مستند خزينة',
    'custody_issue': 'صرف عهدة تشغيلية',
    'custody_settlement': 'تسوية عهدة تشغيلية',
    'custody_return': 'رد متبقي عهدة',
    'custody_reversal': 'عكس مستند عهدة',
    'supplier_payment': 'سداد مورد',
    'supplier_payment_reversal': 'عكس سداد مورد',
    'supplier_cheque_clearance': 'صرف شيك مورد',
    'supplier_cheque_bounce': 'ارتداد شيك مورد',
    'supplier_refund': 'استرداد من مورد',
    'supplier_refund_reversal': 'عكس استرداد مورد',
    'withholding_remittance': 'توريد خصم ضريبي',
    'withholding_remittance_reversal': 'عكس توريد خصم ضريبي',
}


def _has(db, table):
    return ledger_reports._has(db, table)


def _excluded_cash_ids(db):
    # The account for money in transit is not a physical safe.  POS/outlet
    # cash (111004) is physical EGP cash and remains included.
    excluded = {row['id'] for row in db.execute(
        "SELECT id FROM accounts WHERE code='111005'")}
    if _has(db, 'account_mappings'):
        excluded.update(row['account_id'] for row in db.execute("""
            SELECT account_id FROM account_mappings
            WHERE module='الخزينة' AND role='cash_transfer_clearing'"""))
    return excluded


def _cash_accounts(db):
    excluded = _excluded_cash_ids(db)
    return [dict(row) for row in db.execute(
        "SELECT * FROM accounts WHERE code LIKE '111%' AND posting=1 ORDER BY code")
        if row['id'] not in excluded]


def _cash_account(db, account_id):
    accounts = {row['id']: row for row in _cash_accounts(db)}
    if account_id is None:
        configured = None
        if _has(db, 'account_mappings'):
            configured = db.execute("""SELECT account_id FROM account_mappings
                WHERE module='الخزينة' AND role='main_expense_safe' AND status='confirmed'""").fetchone()
        if configured and configured['account_id'] in accounts and accounts[configured['account_id']]['active']:
            return accounts[configured['account_id']]
        account = next((row for row in accounts.values() if row['code'] == '111002'), None)
        if account is None:
            account = next((row for row in accounts.values() if row['active']), None)
        if account is None:
            raise ValueError('لا توجد خزينة نقدية بالجنيه متاحة لعرض الحركة')
        return account
    account_id = ledger_reports._id(account_id, 'الخزينة')
    if account_id not in accounts:
        raise ValueError('اختر حساب خزينة نقدية بالجنيه؛ البنك والنقدية بالطريق خارج هذا الكشف')
    return accounts[account_id]


def _source_row(db, table, ident):
    if not _has(db, table):
        return None
    row = db.execute('SELECT * FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
    return dict(row) if row else None


def _origin(db, source_type, source_id, journal_id):
    """Return the current source's identity, without using its amount twice."""
    direct = {
        'treasury_receipt': ('treasury_documents', 'treasury_document'),
        'treasury_payment': ('treasury_documents', 'treasury_document'),
        'treasury_transfer': ('treasury_documents', 'treasury_document'),
        'custody_issue': ('custody_documents', 'custody_document'),
        'custody_settlement': ('custody_documents', 'custody_document'),
        'custody_return': ('custody_documents', 'custody_document'),
        'supplier_payment': ('supplier_payments', 'payment'),
        'supplier_cheque_clearance': ('supplier_payments', 'payment'),
        'supplier_refund': ('supplier_refunds', 'refund'),
        'withholding_remittance': ('withholding_remittances', 'remittance'),
    }
    reversals = {
        'treasury_reversal': ('treasury_reversals', 'document_id', 'treasury_documents', 'treasury_document'),
        'custody_reversal': ('custody_reversals', 'document_id', 'custody_documents', 'custody_document'),
        'supplier_payment_reversal': ('supplier_payment_reversals', 'payment_id', 'supplier_payments', 'payment'),
        'supplier_cheque_bounce': ('supplier_payment_reversals', 'payment_id', 'supplier_payments', 'payment'),
        'supplier_refund_reversal': ('supplier_refund_reversals', 'refund_id', 'supplier_refunds', 'refund'),
        'withholding_remittance_reversal': ('withholding_remittance_reversals', 'remittance_id', 'withholding_remittances', 'remittance'),
    }
    original, action = None, {'type': 'journal', 'id': journal_id}
    if source_type in direct:
        table, action_type = direct[source_type]
        original = _source_row(db, table, source_id)
        if original:
            action = {'type': action_type, 'id': original['id']}
    elif source_type in reversals:
        table, key, original_table, action_type = reversals[source_type]
        reversal = _source_row(db, table, source_id)
        original = _source_row(db, original_table, reversal[key]) if reversal and key in reversal else None
        if original:
            action = {'type': action_type, 'id': original['id']}
    original = original or {}
    supplier = _source_row(db, 'suppliers', original['supplier_id']) if original.get('supplier_id') else None
    custodian = _source_row(db, 'custodians', original['custodian_id']) if original.get('custodian_id') else None
    custodian_name = original.get('custodian_name_snapshot') or (custodian or {}).get('name')
    custodian_code = original.get('custodian_code_snapshot') or (custodian or {}).get('code')
    party = original.get('party_name') or original.get('authority_name') or (supplier or {}).get('name') or custodian_name or ''
    return {'party_name': party, 'document_id': original.get('id'),
            'supplier_name': (supplier or {}).get('name'), 'supplier_code': (supplier or {}).get('code'),
            'custodian_id': original.get('custodian_id'),
            'custodian_name': custodian_name, 'custodian_code': custodian_code,
            'source_label': SOURCE_LABELS.get(source_type, 'قيد محاسبي'), 'action': action}


def cash_book(db, account_id=None, from_date=None, to_date=None):
    """One cash account's exact running balance, including every source once."""
    account = _cash_account(db, account_id)
    ledger = ledger_reports.account_ledger(db, account['id'], from_date, to_date)
    origins = {}
    for row in ledger['rows']:
        key = row['source_type'], row['source_id']
        if key not in origins:
            origins[key] = _origin(db, *key, row['journal_id'])
        # Cash journal sides often have no supplier dimension; identity comes
        # from the source header, while amounts remain the actual cash line.
        metadata = origins[key]
        row.update({key: value for key, value in metadata.items() if value is not None})
    return {**ledger, 'account_id': account['id'], 'account_code': account['code'],
            'account_name': account['name'],
            'notes': [
                'الحركة من أطراف القيود المرحّلة على حساب الخزينة المختار مرة واحدة، بما فيها سداد الموردين واسترداداتهم وتوريد الخصم.',
                'سداد المورد يظهر بصافي النقد الفعلي بعد الخصم الضريبي؛ لا ينشأ سند خزينة إضافي للحركة نفسها.',
                'رصيد أول المدة هو صافي القيود السابقة للبداية؛ الأرصدة الافتتاحية الفعلية لم تدخل ولم يفترض رصيد نقدي.',
                'التحويل بين الخزائن يظهر خروجًا من المصدر ودخولًا للمستلم؛ كل كشف يخص حسابه فقط.',
                'فتح المستند يعرض حالته الحالية، بينما تظل الحركة والأرصدة حتى تاريخ التقرير.',
            ]}


def position(db, as_of=None):
    """Recorded balances of physical EGP safes, excluding bank/in-transit."""
    _, as_of = ledger_reports._range(None, as_of)
    accounts = _cash_accounts(db)
    balances = {}
    if _has(db, 'journal_entries') and _has(db, 'journal_lines'):
        balances = {row['account_id']: row['balance_cents'] for row in db.execute('''
            SELECT l.account_id,SUM(l.debit_cents-l.credit_cents) balance_cents
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.sealed=1 AND j.entry_date<=? GROUP BY l.account_id''', (as_of,))}
    rows, exceptions, seen = [], [], set()
    for account in accounts:
        rows.append({'id': account['id'], 'account_id': account['id'], 'code': account['code'],
                     'name': account['name'], 'active': bool(account['active']),
                     'balance_cents': balances.get(account['id'], 0)})
    # Inspect every cash-scoped journal, including a malformed journal posted
    # to a cash group. Such a group is not a physical account in the total.
    excluded = _excluded_cash_ids(db)
    for account in db.execute("SELECT id FROM accounts WHERE code LIKE '111%'"):
        if account['id'] in excluded:
            continue
        for issue in ledger_reports._exceptions(db, as_of, account['id']):
            identity = (issue['type'], issue.get('journal_id'), issue.get('line_id'))
            if identity not in seen:
                exceptions.append(issue)
                seen.add(identity)
    return {'as_of': as_of, 'currency': accounting.get_settings(db)['base_currency'],
            'accounts': rows, 'totals': {'balance_cents': sum(row['balance_cents'] for row in rows)},
            'exceptions': exceptions, 'complete': not exceptions,
            'notes': [
                'الأرصدة من القيود المسجلة فقط، وليست إثباتًا لرصيد فعلي قبل إدخال الأرصدة الافتتاحية والجرد.',
                'حساب النقدية بالطريق والبنوك والعهد التشغيلية خارج مجموع الخزائن النقدية بالجنيه؛ خزائن نقاط البيع ضمنه.',
                'الحسابات الموقوفة تظل ظاهرة للمراجعة التاريخية؛ اختيارها لا يسمح بصرف أو قبض جديد.',
            ]}
