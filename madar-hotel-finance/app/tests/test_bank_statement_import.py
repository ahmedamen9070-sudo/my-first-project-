"""CSV import validates complete external statements without guessing amounts."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bank_statement_import as importer


class BankStatementImport(unittest.TestCase):
    def data(self, text=None, **changes):
        return {'csv_text': text or 'Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,إيداع موثق,0000123,100.25,\n2026-10-02,عمولة موثقة,0000124,,2.50\n',
                'delimiter': 'auto', 'date_format': 'YYYY-MM-DD', 'decimal_separator': 'point',
                'amount_mode': 'columns', 'columns': {'date': 0, 'description': 1, 'reference': 2,
                                                     'deposit': 3, 'withdrawal': 4}, **changes}

    def test_english_header_inspection_keeps_indices_and_direction_proposals(self):
        result = importer.inspect(self.data())
        self.assertEqual(result['delimiter'], 'comma')
        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['headers'][2], {'index': 2, 'name': 'Reference'})
        self.assertEqual(result['proposed_mapping'], {'date': 0, 'description': 1, 'reference': 2, 'deposit': 3, 'withdrawal': 4})
        self.assertEqual(result['proposed_amount_mode'], 'columns')
        self.assertEqual(result['sample_rows'][0][2], '0000123')

    def test_arabic_bom_and_already_decoded_windows_text_are_preserved(self):
        text = '\ufeffالتاريخ;البيان;المرجع;دائن;مدين\r\n١/١٠/٢٠٢٦;إيداع نقدية؛ فرع القاهرة;٠٠٠٠١;١٬٢٣٤٫٥٦;\r\n٢/١٠/٢٠٢٦;عمولة تحويل;٠٠٠٠٢;;٢٫٥٠\r\n'
        result = importer.inspect(self.data(text))
        self.assertEqual(result['delimiter'], 'semicolon')
        self.assertEqual(result['proposed_mapping']['deposit'], 3)
        parsed = importer.parse(self.data(text, date_format='DD/MM/YYYY'))
        self.assertEqual(parsed['lines'][0]['amount_cents'], 123456)
        self.assertEqual(parsed['lines'][1]['amount_cents'], -250)
        self.assertEqual(parsed['lines'][0]['reference'], '٠٠٠٠١')
        self.assertEqual(parsed['lines'][0]['date'], '2026-10-01')
        self.assertEqual(parsed['lines'][0]['description'], 'إيداع نقدية؛ فرع القاهرة')
        self.assertEqual(parsed['totals'], {'deposit_cents': 123456, 'withdrawal_cents': 250, 'net_cents': 123206})

    def test_signed_amount_column_exact_cents_value_date_and_inert_formula_text(self):
        text = 'Date\tValue Date\tReference\tDescription\tAmount\n2026-10-01\t2026-10-02\t00001\t=HYPERLINK("https://example.invalid")\t+10.50\n2026-10-03\t\t00002\t@SUM(A1:A2)\t(0.25)\n'
        data = self.data(text, delimiter='tab', amount_mode='signed', columns={
            'date': 0, 'value_date': 1, 'reference': 2, 'description': 3, 'amount': 4})
        result = importer.parse(data)
        self.assertEqual([row['amount_cents'] for row in result['lines']], [1050, -25])
        self.assertEqual(result['lines'][0]['value_date'], '2026-10-02')
        self.assertEqual(result['lines'][1]['value_date'], '2026-10-03')
        self.assertEqual(result['lines'][0]['description'], '=HYPERLINK("https://example.invalid")')
        self.assertEqual(result['lines'][1]['reference'], '00002')

    def test_comma_decimal_uses_explicit_grouping_and_never_guesses(self):
        text = 'Date;Description;Reference;Deposit;Withdrawal\n31/10/2026;إيداع;001;1.234,56;\n'
        result = importer.parse(self.data(text, date_format='DD/MM/YYYY', decimal_separator='comma'))
        self.assertEqual(result['totals']['net_cents'], 123456)
        with self.assertRaisesRegex(ValueError, 'لم يستورد أي سطر'):
            importer.parse(self.data(text, date_format='DD/MM/YYYY', decimal_separator='point'))

    def test_quoted_comma_and_multiline_description_are_one_movement(self):
        text = 'Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,"إيداع, فرع القاهرة\nإشعار رقم 7",0000007,"1,234.50",\n'
        result = importer.parse(self.data(text))
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['lines'][0]['description'], 'إيداع, فرع القاهرة\nإشعار رقم 7')
        self.assertEqual(result['lines'][0]['reference'], '0000007')
        self.assertEqual(result['lines'][0]['amount_cents'], 123450)

    def test_empty_rows_skipped_but_embedded_nonmovement_headers_rejected(self):
        text = '\n\nDate,Description,Reference,Deposit,Withdrawal\n,,,,\n2026-10-01,إيداع,001,10,\n\n'
        self.assertEqual(importer.parse(self.data(text))['count'], 1)
        invalid = text + 'رصيد افتتاحي,,,0,0\n'
        with self.assertRaisesRegex(ValueError, 'لم يستورد أي سطر'):
            importer.parse(self.data(invalid))

    def test_multiple_row_errors_reject_complete_preview_with_actual_row_numbers(self):
        text = 'Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,صحيح,001,10,\n2026-02-30,تاريخ خاطئ,002,10,\n2026-10-03,كسور زائدة,003,,1.005\n'
        with self.assertRaises(ValueError) as raised:
            importer.parse(self.data(text))
        self.assertIn('السطر 3', str(raised.exception))
        self.assertIn('السطر 4', str(raised.exception))
        self.assertIn('لم يستورد أي سطر', str(raised.exception))

    def test_withdrawal_deposit_do_not_net_two_flows_or_turn_negative_amounts(self):
        for deposit, withdrawal in [('10', '3'), ('0', '0'), ('', ''), ('-10', ''), ('', '(10)')]:
            with self.subTest(deposit=deposit, withdrawal=withdrawal):
                text = f'Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,حركة,001,{deposit},{withdrawal}\n'
                with self.assertRaises(ValueError):
                    importer.parse(self.data(text))

    def test_only_one_direction_column_is_explicitly_allowed(self):
        text = 'Date,Amount\n2026-10-01,25.20\n'
        result = importer.parse(self.data(text, columns={'date': 0, 'withdrawal': 1}))
        self.assertEqual(result['lines'], [{'date': '2026-10-01', 'value_date': '2026-10-01',
                                          'description': '', 'reference': '', 'amount_cents': -2520}])

    def test_running_balances_validate_real_sequence_and_preserve_overdraft(self):
        text = 'Date,Amount,Balance\n2026-10-01,-20.25,-10.25\n2026-10-02,5.00,-5.25\n'
        data = self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1, 'balance': 2})
        result = importer.parse(data)
        self.assertEqual(result['balance_check'], {'implied_opening_cents': 1000, 'closing_cents': -525, 'consistent': True})
        self.assertEqual(result['lines'][0]['balance_cents'], -1025)
        with self.assertRaisesRegex(ValueError, 'الرصيد الجاري'):
            importer.parse({**data, 'csv_text': text.replace('-5.25', '-5.24')})

    def test_missing_mapped_running_balance_is_not_silently_ignored(self):
        text = 'Date,Amount,Balance\n2026-10-01,10,\n'
        with self.assertRaises(ValueError):
            importer.parse(self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1, 'balance': 2}))

    def test_exact_date_format_resolves_ambiguous_dates_and_validates_leap_day(self):
        text = 'Date,Amount\n02/03/2026,10\n'
        data = self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1})
        self.assertEqual(importer.parse({**data, 'date_format': 'DD/MM/YYYY'})['lines'][0]['date'], '2026-03-02')
        self.assertEqual(importer.parse({**data, 'date_format': 'MM/DD/YYYY'})['lines'][0]['date'], '2026-02-03')
        with self.assertRaises(ValueError):
            importer.parse(data)
        for invalid in ('29-02-2025', '31-04-2026', '30-10-26'):
            with self.subTest(date=invalid), self.assertRaises(ValueError):
                importer.parse({**data, 'csv_text': 'Date,Amount\n'+invalid+',10\n', 'date_format': 'DD-MM-YYYY'})

    def test_header_only_no_activity_statement_is_a_valid_empty_preview(self):
        result = importer.parse(self.data('Date,Description,Reference,Deposit,Withdrawal\n'))
        self.assertEqual(result['lines'], [])
        self.assertEqual(result['totals']['net_cents'], 0)
        self.assertEqual(result['count'], 0)

    def test_duplicate_headers_are_kept_by_index_and_not_guessed(self):
        result = importer.inspect(self.data('Date,Reference,Reference,Deposit,Withdrawal\n2026-10-01,001,002,10,\n'))
        self.assertNotIn('reference', result['proposed_mapping'])
        self.assertEqual([h['index'] for h in result['headers']], [0, 1, 2, 3, 4])
        self.assertEqual(result['sample_rows'][0][1:3], ['001', '002'])

    def test_malformed_csv_width_quotes_and_wrong_delimiter_reject_without_shift(self):
        for text in ('Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,desc,001,1,234.50,\n',
                     'Date,Description,Reference,Deposit,Withdrawal\n2026-10-01,"unterminated,001,10,\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                importer.parse(self.data(text))
        with self.assertRaises(ValueError):
            importer.parse(self.data(delimiter='semicolon'))

    def test_bad_money_shapes_precision_and_oversize_are_not_coerced(self):
        for value in ('1.005', 'NaN', 'Infinity', '1e3', 'EGP 10', '10-', '--10', '1,23', '1,,234', '1,234 567', '(+10)', '(10', '100000000000.01', '1.2.3'):
            with self.subTest(amount=value), self.assertRaises(ValueError):
                text = 'Date;Amount\n2026-10-01;'+value+'\n'
                importer.parse(self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1}))

    def test_whitespace_grouping_and_arabic_digits_remain_exact(self):
        text = 'Date;Amount\n۲۰۲۶-۱۰-۰۱;−١\u202f٢٣٤٫٥٠\n'
        result = importer.parse(self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1}))
        self.assertEqual(result['lines'][0]['amount_cents'], -123450)

    def test_invalid_mapping_and_options_are_controlled_errors(self):
        for changes in ({'columns': {'date': True, 'deposit': 3}},
                        {'columns': {'date': '0', 'deposit': 3}},
                        {'columns': {'date': 0.0, 'deposit': 3}},
                        {'columns': {'date': 0, 'deposit': 99}},
                        {'columns': {'date': 0, 'deposit': 3, 'withdrawal': 3}},
                        {'columns': {'deposit': 3}}, {'columns': {'date': 0}},
                        {'columns': {'date': 0, 'deposit': 3, 'typo': 4}},
                        {'amount_mode': 'guess'}, {'decimal_separator': 'guess'},
                        {'date_format': 'guess'}, {'delimiter': []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                importer.parse(self.data(**changes))

    def test_file_rows_columns_text_and_totals_have_bounded_limits(self):
        for data in ({'csv_text': ''}, {'csv_text': 'Date,Amount\n'+('x'*importer.MAX_BYTES)},
                     {'csv_text': '\x00binary'}, {'csv_text': '\ud800'},
                     {'csv_text': ','.join(['col']*65)+'\n'},
                     {'csv_text': 'Date,Amount\n'+('2026-10-01,1\n'*2001)}):
            with self.subTest(kind=str(data)[:50]), self.assertRaises(ValueError):
                importer.inspect(data)
        text = 'Date,Amount\n2026-10-01,100000000000\n2026-10-02,1\n'
        with self.assertRaisesRegex(ValueError, 'إجمالي الكشف'):
            importer.parse(self.data(text, amount_mode='signed', columns={'date': 0, 'amount': 1}))


if __name__ == '__main__':
    unittest.main()
