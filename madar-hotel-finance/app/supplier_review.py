"""Read-only dated supplier position alongside explicitly current preparation work.

The caller owns the read transaction.  This module neither approves documents nor
reserves credit, and it does not infer opening or actual bank balances.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import accounting
import ledger_reports
import payables
import supplier_adjustments
import supplier_reconciliation
import supplier_review_preparation
import supplier_settlements
import withholding
import withholding_balances


def _invoice_row(invoice, supplier):
    return {
        'kind': 'invoice', 'id': invoice['id'], 'code': invoice['code'],
        'business_date': invoice['business_date'], 'due_date': invoice['due_date'],
        'supplier_id': supplier['id'], 'supplier_name': supplier['name'],
        'supplier_code': supplier['code'], 'reference': invoice['reference'],
        'amount_cents': invoice['total_cents'],
        'outstanding_cents': invoice['outstanding_cents'],
        'days_overdue': invoice['days_overdue'],
        'action': {'type': 'invoice', 'id': invoice['id']},
    }


def _financial_issues(supplier_report, tax_report, trial, as_of):
    """Retain dimensional differences even when their combined total is zero."""
    issues = []

    def add(kind, row, action):
        issues.append({**row, 'kind': kind, 'action': {'type': action, 'as_of': as_of}})

    for row in supplier_report['exceptions']:
        add('supplier', row, 'reconciliation')
    for row in supplier_report['suppliers']:
        if not row['matched']:
            add('supplier', {
                'type': 'supplier_balance_difference', 'supplier_id': row['supplier_id'],
                'supplier_name': row['name'], 'supplier_code': row['code'],
                'message': 'رصيد المورد أو توزيعه على الحسابات لا يطابق الأستاذ المساعد.',
                'payable_difference_cents': row['payable_difference_cents'],
                'advance_difference_cents': row['advance_difference_cents'],
                'account_ids': row['account_ids'], 'journal_ids': row['journal_ids'],
            }, 'reconciliation')
    for row in supplier_report['accounts']:
        if not row['matched']:
            add('supplier', {
                'type': 'supplier_account_difference', 'account_id': row['id'],
                'account_code': row['code'], 'account_name': row['name'],
                'message': 'رصيد حساب مراقبة الموردين أو الدفعات المقدمة يحتاج مطابقة.',
                'difference_cents': row['difference_cents'], 'journal_ids': row['journal_ids'],
            }, 'reconciliation')
    for row in tax_report['exceptions']:
        add('withholding', row, 'withholding_balances')
    for row in tax_report['accounts']:
        if not row['matched']:
            add('withholding', {
                'type': 'withholding_account_difference', 'account_id': row['account_id'],
                'account_code': row['code'], 'account_name': row['name'],
                'message': 'حساب الخصم الضريبي أو توزيع الموردين لا يطابق مستندات المصدر.',
                'difference_cents': row['difference_cents'], 'journal_ids': row['journal_ids'],
            }, 'withholding_balances')
    for row in trial['exceptions']:
        add('trial_balance', row, 'trial_balance')
    if not trial['balanced'] and not trial['exceptions']:
        add('trial_balance', {
            'type': 'trial_balance_difference',
            'message': 'إجماليات ميزان المراجعة غير متوازنة؛ راجع القيود.',
            **{key: trial['totals'][key] for key in (
                'opening_difference_cents', 'movement_difference_cents', 'closing_difference_cents')},
        }, 'trial_balance')
    return issues


def snapshot(db, as_of=None, horizon_days=7):
    """Return every matching row, with historical money and current drafts apart."""
    as_of = withholding_balances._as_of(as_of)
    if type(horizon_days) is not int or not 0 <= horizon_days <= 90:
        raise ValueError('مدة متابعة الاستحقاق عدد صحيح من صفر إلى ٩٠ يومًا')
    day = date.fromisoformat(as_of)
    through = date.fromordinal(min(day.toordinal() + horizon_days, date.max.toordinal())).isoformat()
    suppliers = {row['id']: dict(row) for row in db.execute('SELECT id,code,name FROM suppliers ORDER BY name,id')}
    queues = {key: [] for key in ('overdue', 'due_today', 'due_soon', 'cheques', 'credits',
                                  'drafts_current', 'setup_current', 'financial_issues', 'negative_funding')}
    position = dict.fromkeys((
        'open_invoice_cents', 'open_invoice_count', 'overdue_cents', 'overdue_count',
        'due_today_cents', 'due_today_count', 'due_soon_cents', 'due_soon_count',
        'return_credit_cents', 'advance_cents', 'net_payable_cents',
        'open_cheque_net_cents', 'open_cheque_count'), 0)
    due_later_count, credit_failures = 0, []
    for supplier in suppliers.values():
        for invoice in payables.open_invoices(db, supplier['id'], as_of):
            if invoice['outstanding_cents'] <= 0:
                continue
            row = _invoice_row(invoice, supplier)
            amount = row['outstanding_cents']
            position['open_invoice_cents'] += amount
            position['open_invoice_count'] += 1
            if row['due_date'] < as_of:
                queue = 'overdue'
            elif row['due_date'] == as_of:
                queue = 'due_today'
            elif row['due_date'] <= through:
                queue = 'due_soon'
            else:
                due_later_count += 1
                continue
            queues[queue].append(row)
            position[queue + '_cents'] += amount
            position[queue + '_count'] += 1
        try:
            sources = supplier_settlements.source_balances(db, supplier['id'], as_of)['sources']
        except ValueError as error:
            # A damaged advance journal must not prevent the independent
            # reconciliation from being shown. Keep document totals intact and
            # explicitly withhold source-level actions whose account is unknown.
            balances = payables.supplier_balances(db, supplier['id'], as_of)
            position['return_credit_cents'] += balances['unapplied_return_credit_cents']
            position['advance_cents'] += balances['advance_cents']
            credit_failures.append({
                'kind': 'supplier', 'type': 'unavailable_credit_sources',
                'supplier_id': supplier['id'], 'supplier_code': supplier['code'],
                'supplier_name': supplier['name'],
                'message': 'تعذر تجهيز تفاصيل الأرصدة المتاحة لهذا المورد؛ الإجمالي من مستنداته محفوظ دون إتاحة تسوية مصدر غير محدد. ' + str(error),
                'action': {'type': 'reconciliation', 'as_of': as_of},
            })
            sources = []
        for source in sources:
            if source['available_cents'] <= 0:
                continue
            row = {**source, 'kind': source['source_type'], 'id': source['source_id'],
                   'supplier_name': supplier['name'], 'supplier_code': supplier['code'],
                   'amount_cents': source['available_cents'],
                   'action': {'type': 'settlement_new', 'supplier_id': supplier['id']}}
            queues['credits'].append(row)
            position['advance_cents' if source['source_type'] == 'advance' else 'return_credit_cents'] += source['available_cents']
    position['net_payable_cents'] = position['open_invoice_cents'] - position['return_credit_cents'] - position['advance_cents']

    # Raw rows are essential: get_payment's all-time overlay would hide a cheque
    # cleared after this report date.  Every event is instead evaluated as of it.
    for record in db.execute("""SELECT * FROM supplier_payments WHERE status='posted'
            AND method='cheque' AND payment_date<=? ORDER BY payment_date,id""", (as_of,)):
        payment = supplier_adjustments.payment_overlay(db, dict(record), as_of)
        if payment['effective_status'] != 'posted' or payment['effective_cheque_status'] not in ('issued', 'stopped'):
            continue
        supplier = suppliers.get(payment['supplier_id'], {'name': 'مورد غير موجود', 'code': ''})
        details = json.loads(payment['details'])
        net = withholding.net_cash(db, payment['id'], payment['amount_cents'])
        queues['cheques'].append({
            'kind': 'payment', 'id': payment['id'], 'code': payment['code'],
            'business_date': payment['payment_date'], 'due_date': details.get('due_date'),
            'supplier_id': payment['supplier_id'], 'supplier_name': supplier['name'],
            'supplier_code': supplier['code'], 'reference': details.get('cheque_number', ''),
            'amount_cents': net, 'gross_cents': payment['amount_cents'], 'net_cash_cents': net,
            'effective_cheque_status': payment['effective_cheque_status'], 'details': details,
            'action': {'type': 'payment', 'id': payment['id']},
        })
        position['open_cheque_net_cents'] += net
        position['open_cheque_count'] += 1
    for key in ('overdue', 'due_today', 'due_soon', 'cheques'):
        queues[key].sort(key=lambda row: (row.get('due_date') or '', row['business_date'], row['id']))

    supplier_report = supplier_reconciliation.report(db, as_of)
    tax_report = withholding_balances.balance_report(db, as_of)
    trial = ledger_reports.trial_balance(db, as_of[:4] + '-01-01', as_of)
    queues['financial_issues'] = credit_failures + _financial_issues(supplier_report, tax_report, trial, as_of)
    for row in trial['rows']:
        if row['code'].startswith(('111', '112')) and row['closing_balance_cents'] < 0:
            queues['negative_funding'].append({
                'kind': 'funding', 'id': row['account_id'], 'code': row['code'], 'name': row['name'],
                'amount_cents': row['closing_balance_cents'], 'active': row['active'],
                'message': 'الرصيد الدفتري دائن؛ راجع كشف الأرصدة الافتتاحية ومطابقة الرصيد الفعلي.',
                'action': {'type': 'ledger', 'id': row['account_id'],
                           'from_date': trial['from_date'], 'to_date': as_of},
            })
    preparation = supplier_review_preparation.current_work(db, as_of)
    queues['drafts_current'] = preparation['drafts']
    queues['setup_current'] = preparation['setup_issues']
    return {
        'as_of': as_of, 'horizon_days': horizon_days, 'opening_status': accounting.opening_status(db, as_of),
        'currency': accounting.get_settings(db)['base_currency'],
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'position': position, 'queues': queues, 'credits_complete': not credit_failures,
        'checks': {
            'suppliers': {key: supplier_report[key] for key in ('matched', 'totals', 'counts', 'exceptions')},
            'withholding': {'matched': tax_report['matched'], 'totals': tax_report['all_totals'],
                            'counts': {'source_count': len(tax_report['sources']),
                                       'account_count': len(tax_report['accounts']),
                                       'exception_count': len(tax_report['exceptions']),
                                       'mismatched_accounts': sum(not row['matched'] for row in tax_report['accounts'])},
                            'exceptions': tax_report['exceptions']},
            'trial_balance': {key: trial[key] for key in ('balanced', 'totals', 'account_count', 'exceptions')},
        },
        'tax_position': {key: tax_report['all_totals'][key] for key in (
            'net_due_cents', 'account_credit_cents', 'remaining_cents', 'source_count')},
        'counts': {**preparation['counts'], 'due_later_count': due_later_count,
                   'financial_issues': len(queues['financial_issues']),
                   'negative_funding': len(queues['negative_funding'])},
        'notes': (['تفاصيل مصادر الأرصدة المتاحة غير مكتملة لبعض الموردين؛ إجماليات مستنداتهم ظاهرة، وسبب التعذر مدرج ضمن فروق المراجعة.'] if credit_failures else []) + [
            'الأرصدة والاستحقاقات والشيكات والمطابقات محسوبة من المستندات المرحّلة حتى تاريخ التقرير.',
            'المسودات وتجهيز الربط تعرض حالتها الحالية وقت إعداد التقرير، بما فيها المسودات اللاحقة لتاريخه؛ ليست لقطة تاريخية للمسودات.',
            'الشيكات المعروضة صافي المبلغ المستحق للبنك بعد الخصم الضريبي، وقد تكون حالة المستند عند فتحه تغيرت بعد تاريخ التقرير.',
            'المطابقة تخص القيود المسجلة؛ لا تثبت اكتمال الأرصدة الافتتاحية أو الرصيد الفعلي للبنك والخزينة أو جاهزية باقي وحدات النظام.',
            'الخصم الضريبي والتوريد يعرضان التسجيل الداخلي فقط، دون افتراض التزام أو إرسال إلى جهة خارجية.',
        ],
    }
