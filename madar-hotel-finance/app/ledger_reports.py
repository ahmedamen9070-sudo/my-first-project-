"""Read-only general-ledger reports, in exact cents from sealed journals only.

Opening balances in a report mean activity before its selected start date. This
module never creates an opening entry or infers an unrecorded bank balance.
Posting rows and hierarchy rollups are separate, so totals cannot double count.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import accounting


AMOUNTS = ('opening_debit_cents', 'opening_credit_cents', 'debit_cents',
           'credit_cents', 'closing_debit_cents', 'closing_credit_cents')


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _date(value):
    if not isinstance(value, str):
        raise ValueError('حدد التاريخ بصيغة سنة-شهر-يوم')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('التاريخ غير صحيح') from None
    if parsed.isoformat() != value:
        raise ValueError('حدد التاريخ بصيغة سنة-شهر-يوم')
    return value


def _range(from_date, to_date):
    if to_date is not None:
        to_date = _date(to_date)
    else:
        try:
            to_date = datetime.now(ZoneInfo('Africa/Cairo')).date().isoformat()
        except ZoneInfoNotFoundError:
            # Windows Python may not ship IANA timezone files. The hotel device's
            # local date remains usable without an extra runtime dependency.
            to_date = date.today().isoformat()
    from_date = _date(from_date) if from_date is not None else to_date[:4] + '-01-01'
    if from_date > to_date:
        raise ValueError('تاريخ بداية التقرير يجب ألا يلي تاريخ نهايته')
    return from_date, to_date


def _id(value, label='الحساب'):
    if type(value) is not int or value <= 0:
        raise ValueError('معرف ' + label + ' غير صحيح')
    return value


def _account(row):
    return {'account_id': row['id'], 'id': row['id'], 'code': row['code'], 'name': row['name'],
            'parent_id': row['parent_id'], 'posting': bool(row['posting']),
            'active': bool(row['active']), 'normal_side': row['normal_side'], 'statement': row['statement']}


def _amounts(opening=0, debit=0, credit=0):
    closing = opening + debit - credit
    return {'opening_balance_cents': opening,
            'opening_debit_cents': max(opening, 0), 'opening_credit_cents': max(-opening, 0),
            'debit_cents': debit, 'credit_cents': credit,
            'closing_balance_cents': closing,
            'closing_debit_cents': max(closing, 0), 'closing_credit_cents': max(-closing, 0)}


def _used(row):
    return any(row[key] for key in AMOUNTS)


def _exceptions(db, to_date, account_id=None, supplier_id=None):
    if not _has(db, 'journal_entries') or not _has(db, 'journal_lines'):
        return []
    values = [to_date]
    scope = ''
    predicates, scoped_values = [], []
    if account_id is not None:
        predicates.append('sc.account_id=?')
        scoped_values.append(account_id)
    if supplier_id is not None:
        predicates.append('sc.supplier_id=?')
        scoped_values.append(supplier_id)
    if predicates:
        scope = ' AND EXISTS(SELECT 1 FROM journal_lines sc WHERE sc.journal_id=j.id AND ' + ' AND '.join(predicates) + ')'
        values.extend(scoped_values)
    exceptions = []
    for row in db.execute('''SELECT j.id journal_id,j.code journal_code,j.entry_date,j.sealed,
            COUNT(l.id) line_count,COALESCE(SUM(l.debit_cents),0) debit_cents,
            COALESCE(SUM(l.credit_cents),0) credit_cents
            FROM journal_entries j LEFT JOIN journal_lines l ON l.journal_id=j.id
            WHERE j.entry_date<=?''' + scope + ' GROUP BY j.id ORDER BY j.entry_date,j.id', values):
        if not row['sealed']:
            exceptions.append({'type': 'unsealed_journal', 'message': 'قيد غير مكتمل مستبعد من الأرصدة.', **dict(row)})
        elif row['debit_cents'] != row['credit_cents'] or row['line_count'] < 2 or row['debit_cents'] <= 0:
            exceptions.append({'type': 'unbalanced_journal', 'message': 'قيد مرحل غير متوازن أو بلا أطراف مكتملة؛ راجع المصدر.', **dict(row)})
    # Validate the whole journal involved in an account/supplier view, including
    # its counterparty lines. A valid bank line cannot hide a missing AP account.
    where, values = 'j.sealed=1 AND j.entry_date<=?' + scope, [to_date, *scoped_values]
    for row in db.execute('''SELECT l.id line_id,l.account_id,j.id journal_id,j.code journal_code,
            a.posting,a.code account_code,l.debit_cents,l.credit_cents
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            LEFT JOIN accounts a ON a.id=l.account_id WHERE ''' + where +
            ' AND (a.id IS NULL OR a.posting<>1 OR l.debit_cents<0 OR l.credit_cents<0 OR (l.debit_cents>0)=(l.credit_cents>0)) ORDER BY j.id,l.id', values):
        kind = 'missing_account' if row['posting'] is None else 'nonposting_account' if not row['posting'] else 'invalid_journal_line'
        exceptions.append({'type': kind, 'message': 'طرف قيد على حساب مفقود أو تجميعي أو بمبلغ غير صحيح.', **dict(row)})
    return exceptions


def trial_balance(db, from_date=None, to_date=None, include_zero=False):
    """Full posting-account trial balance and independent hierarchy rollups."""
    from_date, to_date = _range(from_date, to_date)
    if type(include_zero) is not bool:
        raise ValueError('اختيار إظهار الحسابات الصفرية غير صحيح')
    accounts = {row['id']: _account(row) for row in db.execute('SELECT * FROM accounts ORDER BY code')}
    activity = {}
    if _has(db, 'journal_entries') and _has(db, 'journal_lines'):
        activity = {row['account_id']: dict(row) for row in db.execute('''SELECT l.account_id,
            SUM(CASE WHEN j.entry_date<? THEN l.debit_cents-l.credit_cents ELSE 0 END) opening_balance_cents,
            SUM(CASE WHEN j.entry_date>=? THEN l.debit_cents ELSE 0 END) debit_cents,
            SUM(CASE WHEN j.entry_date>=? THEN l.credit_cents ELSE 0 END) credit_cents
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.sealed=1 AND j.entry_date<=? GROUP BY l.account_id''', (from_date, from_date, from_date, to_date))}
    rows = []
    for ident in sorted(set(aid for aid, account in accounts.items() if account['posting']) | set(activity),
                        key=lambda aid: accounts.get(aid, {'code': '~' + str(aid)})['code']):
        account = accounts.get(ident, {'account_id': ident, 'id': ident, 'code': '', 'name': 'حساب مفقود',
                                     'parent_id': None, 'posting': True, 'active': False, 'normal_side': None, 'statement': None})
        amount = activity.get(ident, {})
        row = {**account, **_amounts(amount.get('opening_balance_cents', 0), amount.get('debit_cents', 0), amount.get('credit_cents', 0))}
        if include_zero or _used(row):
            rows.append(row)
    # Ancestors are accumulated from direct account balances, never from other
    # rollups. Debit/credit columns preserve each child's side, without netting.
    groups = {aid: {**account, **dict.fromkeys(AMOUNTS, 0), 'opening_balance_cents': 0,
                    'closing_balance_cents': 0, 'child_account_count': 0}
              for aid, account in accounts.items() if not account['posting']}
    hierarchy_exceptions = []
    for row in rows:
        parent, seen = row['parent_id'], {row['account_id']}
        while parent is not None:
            if parent in seen or parent not in groups:
                hierarchy_exceptions.append({'type': 'invalid_account_hierarchy', 'account_id': row['account_id'],
                                             'parent_id': parent, 'message': 'تسلسل الحسابات غير صحيح؛ راجع شجرة الحسابات.'})
                break
            seen.add(parent)
            group = groups[parent]
            for key in (*AMOUNTS, 'opening_balance_cents', 'closing_balance_cents'):
                group[key] += row[key]
            group['child_account_count'] += 1
            parent = group['parent_id']
    totals = {key: sum(row[key] for row in rows) for key in AMOUNTS}
    totals.update(opening_difference_cents=totals['opening_debit_cents'] - totals['opening_credit_cents'],
                  movement_difference_cents=totals['debit_cents'] - totals['credit_cents'],
                  closing_difference_cents=totals['closing_debit_cents'] - totals['closing_credit_cents'])
    exceptions = _exceptions(db, to_date) + hierarchy_exceptions
    return {'from_date': from_date, 'to_date': to_date, 'currency': accounting.get_settings(db)['base_currency'],
            'include_zero': include_zero, 'rows': rows,
            'groups': [row for row in sorted(groups.values(), key=lambda item: item['code']) if include_zero or _used(row)],
            'totals': totals, 'balanced': not exceptions and totals['opening_difference_cents'] == totals['movement_difference_cents'] == totals['closing_difference_cents'] == 0,
            'exceptions': exceptions, 'account_count': len(rows),
            'notes': ['الأرصدة من القيود المرحّلة فقط؛ رصيد أول المدة هو صافي القيود السابقة لتاريخ البداية.',
                      'إجماليات التقرير للحسابات التفصيلية مباشرة؛ التجميعات معروضة منفصلة ولا تضاف مرة أخرى.',
                      'الرصيد الموجب مدين والسالب دائن، بغض النظر عن الطبيعة المعتادة للحساب.']}


def _document(db, source_type, source_id, fallback):
    tables = {
        'supplier_invoice': 'invoices', 'supplier_return': 'supplier_returns',
        'supplier_payment': 'supplier_payments', 'supplier_cheque_clearance': 'supplier_payments',
        'supplier_settlement': 'supplier_settlements', 'supplier_refund': 'supplier_refunds',
    }
    table = tables.get(source_type)
    if table and _has(db, table):
        cols = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        fields = 'code' + (',reference' if 'reference' in cols else '')
        result = db.execute('SELECT ' + fields + ' FROM ' + table + ' WHERE id=?', (source_id,)).fetchone()
        if result:
            return {'document_code': result['code'], 'reference': result['reference'] if 'reference' in result.keys() else ''}
    if source_type in ('supplier_payment_reversal', 'supplier_cheque_bounce') and _has(db, 'supplier_payment_reversals'):
        result = db.execute('''SELECT r.id,p.code,r.reference FROM supplier_payment_reversals r
            JOIN supplier_payments p ON p.id=r.payment_id WHERE r.id=?''', (source_id,)).fetchone()
        if result:
            return {'document_code': f"SPR-{result['id']:06d}", 'reference': result['reference'],
                    'original_document_code': result['code']}
    if source_type == 'supplier_settlement_reversal' and _has(db, 'supplier_settlement_reversals'):
        result = db.execute('''SELECT s.code,r.reference FROM supplier_settlement_reversals r
            JOIN supplier_settlements s ON s.id=r.settlement_id WHERE r.settlement_id=?''', (source_id,)).fetchone()
        if result:
            return {'document_code': result['code'] + ' / عكس', 'reference': result['reference'],
                    'original_document_code': result['code']}
    if source_type == 'supplier_refund_reversal' and _has(db, 'supplier_refund_reversals'):
        result = db.execute('''SELECT r.id,r.reference,f.code original_document_code
            FROM supplier_refund_reversals r JOIN supplier_refunds f ON f.id=r.refund_id
            WHERE r.id=?''', (source_id,)).fetchone()
        if result:
            return {'document_code': f"SRR-{result['id']:06d}", 'reference': result['reference'],
                    'original_document_code': result['original_document_code']}
    return {'document_code': fallback, 'reference': ''}


def account_ledger(db, account_id, from_date=None, to_date=None, supplier_id=None):
    """A posting account's dated lines and exact running Dr-minus-Cr balance."""
    from_date, to_date = _range(from_date, to_date)
    account_id = _id(account_id)
    found = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if found is None or not found['posting']:
        raise ValueError('اختر حسابًا تفصيليًا موجودًا لعرض دفتر الأستاذ')
    supplier = None
    if supplier_id is not None:
        supplier_id = _id(supplier_id, 'المورد')
        if not _has(db, 'suppliers'):
            raise ValueError('المورد غير موجود')
        found_supplier = db.execute('SELECT id,code,name FROM suppliers WHERE id=?', (supplier_id,)).fetchone()
        if found_supplier is None:
            raise ValueError('المورد غير موجود')
        supplier = dict(found_supplier)
    values, extra = [account_id], ''
    if supplier_id is not None:
        extra = ' AND l.supplier_id=?'
        values.append(supplier_id)
    opening, rows = 0, []
    if _has(db, 'journal_entries') and _has(db, 'journal_lines'):
        opening = db.execute('''SELECT COALESCE(SUM(l.debit_cents-l.credit_cents),0)
            FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
            WHERE j.sealed=1 AND l.account_id=?''' + extra + ' AND j.entry_date<?', (*values, from_date)).fetchone()[0]
        # Real installations have both dimension tables. The optional joins
        # also keep a pristine chart-only database readable before AP setup.
        supplier_select = 's.code supplier_code,s.name supplier_name' if _has(db, 'suppliers') else 'NULL supplier_code,NULL supplier_name'
        supplier_join = ' LEFT JOIN suppliers s ON s.id=l.supplier_id' if _has(db, 'suppliers') else ''
        department_select = 'd.name department_name,d.cost_code' if _has(db, 'departments') else 'NULL department_name,NULL cost_code'
        department_join = ' LEFT JOIN departments d ON d.id=l.department_id' if _has(db, 'departments') else ''
        raw = db.execute('''SELECT l.id line_id,l.journal_id,l.debit_cents,l.credit_cents,
            l.supplier_id,l.department_id,l.source_line_id,j.code journal_code,
            j.entry_date,j.source_type,j.source_id,''' + supplier_select + ',' + department_select +
            ' FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id' + supplier_join + department_join +
            ' WHERE j.sealed=1 AND l.account_id=?' + extra +
            ' AND j.entry_date>=? AND j.entry_date<=? ORDER BY j.entry_date,j.id,l.id', (*values, from_date, to_date)).fetchall()
        running, documents = opening, {}
        for line in raw:
            row = dict(line)
            running += row['debit_cents'] - row['credit_cents']
            key = (row['source_type'], row['source_id'])
            if key not in documents:
                documents[key] = _document(db, *key, row['journal_code'])
            row.update(documents[key], date=row['entry_date'], running_balance_cents=running,
                       balance_cents=running, running_debit_cents=max(running, 0), running_credit_cents=max(-running, 0))
            rows.append(row)
    debit, credit = sum(row['debit_cents'] for row in rows), sum(row['credit_cents'] for row in rows)
    totals = _amounts(opening, debit, credit)
    exceptions = _exceptions(db, to_date, account_id, supplier_id)
    return {'account': _account(found), 'supplier': supplier, 'from_date': from_date, 'to_date': to_date,
            'currency': accounting.get_settings(db)['base_currency'], 'rows': rows, 'totals': totals, **totals,
            'exceptions': exceptions, 'complete': not exceptions,
            'notes': ['الأستاذ يعرض الحساب التفصيلي المختار فقط من القيود المرحّلة.',
                      'الرصيد الموجب مدين والسالب دائن؛ التصفية بالمورد تشمل أطراف القيد المسندة إليه فقط.']}
