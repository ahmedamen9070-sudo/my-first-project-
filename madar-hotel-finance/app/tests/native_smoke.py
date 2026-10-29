"""Linux/Qt native-window and PDF smoke check; requires a display and desktop dependencies.
Run in an isolated environment with MADAR_DB pointing to a temporary database.
"""
import sys,os,json,time,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run,webview
from PyQt6 import QtPrintSupport,QtWidgets
from PIL import ImageGrab
output=Path(tempfile.mkdtemp(prefix='am-desktop-smoke-'))/'native-print.pdf'
real_create=webview.create_window
result={}
class PrintToTestPdf:
    def __init__(self,printer,parent):
        printer.setOutputFormat(QtPrintSupport.QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(output))
    def exec(self):return QtWidgets.QDialog.DialogCode.Accepted
QtPrintSupport.QPrintDialog=PrintToTestPdf

def create(*a,**kw):
    kw['confirm_close']=False
    w=real_create(*a,**kw)
    def evaluate(script):return json.loads(w.run_js('JSON.stringify('+script+')'))
    def loaded():
        try:
            for _ in range(100):
                if evaluate('document.querySelector("#loading").hidden'):break
                time.sleep(.1)
            else:raise AssertionError('Data bootstrap did not finish')
            result['hotel']=evaluate('document.querySelector(".hotel-copy h1").innerText')
            assert 'نيو بانوراما' in result['hotel'] and 'رمسيس' in result['hotel']
            assert evaluate('document.querySelector(".hotel-photo").naturalWidth')>100
            assert evaluate('document.querySelectorAll("#modules [data-page=invoices]").length')==0
            w.run_js('page("accounting")')
            for _ in range(100):
                if evaluate('accountingData !== null'):break
                time.sleep(.1)
            else:raise AssertionError('Native accounting page did not load')
            assert evaluate('accountingData.settings.base_currency')=='EGP'
            assert evaluate('accountingData.accounts.length')>=250
            assert evaluate('accountingData.accounts.filter(a=>a.parent_id===null).length')==8
            result['chart_accounts']=evaluate('accountingData.accounts.length')
            w.run_js('document.querySelector("#account-search").value="111000";renderAccountTree()')
            time.sleep(.2)
            ImageGrab.grab().save(str(Path(__file__).with_name('accounting-native.png')))
            w.run_js('page("supplier"); supplierTab("invoices")')
            assert evaluate('!document.querySelector("#invoices").hidden')
            w.run_js('page("dashboard")')
            time.sleep(.3)
            ImageGrab.grab().save(str(Path(__file__).with_name('desktop-test.png')))
            w.run_js('FiscalSetup.periods(2026)')
            for _ in range(100):
                if evaluate('document.querySelectorAll("#fiscal-setup-window[open] .fiscal-period-card").length===12'):break
                time.sleep(.1)
            else:raise AssertionError('Native fiscal period screen did not load')
            result['fiscal_periods']=evaluate('document.querySelectorAll(".fiscal-period-card").length')
            ImageGrab.grab().save(str(Path(__file__).with_name('fiscal-periods-native.png')))
            w.run_js('document.querySelector("[data-fiscal-action=close]").click()')
            w.run_js('FiscalSetup.taxes()')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#fiscal-setup-window[open] [data-fiscal-action=save-tax-settings]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native tax setup did not load')
            result['tax_setup']=evaluate('document.querySelector("#fiscal-title").innerText')
            ImageGrab.grab().save(str(Path(__file__).with_name('tax-setup-native.png')))
            w.run_js('document.querySelector("[data-fiscal-action=close]").click()')
            w.run_js('SupplierSettlement.reconciliation()')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#supplier-settlement-window[open] .ss-result"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native supplier reconciliation did not load')
            result['reconciliation']=evaluate('document.querySelector("#ss-title").innerText')
            assert 'مطابقة' in result['reconciliation']
            ImageGrab.grab().save(str(Path(__file__).with_name('settlement-native.png')))
            w.run_js('document.querySelector("[data-ss-action=print-reconciliation]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("[data-ss-action=close]").click()')
            w.run_js('LedgerReports.trialBalance({from_date:"2026-01-01",to_date:"2026-12-31",include_zero:true})')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#ledger-reports-window[open] .lr-trial-table"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native trial balance did not load')
            result['trial_balance']=evaluate('document.querySelector("#ledger-reports-title").innerText')
            assert 'ميزان المراجعة' in result['trial_balance']
            ImageGrab.grab().save(str(Path(__file__).with_name('trial-balance-native.png')))
            cash=evaluate('accountingData.accounts.find(a=>a.code==="111002").id')
            w.run_js('LedgerReports.ledger('+str(cash)+',{from_date:"2026-01-01",to_date:"2026-12-31"})')
            for _ in range(100):
                if evaluate('document.querySelector("#ledger-reports-title").innerText.includes("حركة الحساب")'):break
                time.sleep(.1)
            else:raise AssertionError('Native account ledger did not load')
            output.unlink()
            w.run_js('document.querySelector("#ledger-reports-window [data-ledger-action=print]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native ledger PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['ledger_pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("#ledger-reports-window [data-ledger-action=close]").click()')
            w.run_js('WithholdingSetup.codes()')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#withholding-setup-window[open] [data-withholding-action=new-code]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native withholding setup did not load')
            result['withholding_setup']=evaluate('document.querySelector("#withholding-title").innerText')
            w.run_js('document.querySelector("#withholding-setup-window [data-withholding-action=close]").click()')
            w.run_js('WithholdingSetup.report("2026-01-01","2026-12-31")')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#withholding-setup-window[open] [data-withholding-action=print-report]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native withholding report did not load')
            ImageGrab.grab().save(str(Path(__file__).with_name('withholding-native.png')))
            output.unlink()
            w.run_js('document.querySelector("#withholding-setup-window [data-withholding-action=print-report]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native withholding PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['withholding_pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("#withholding-setup-window [data-withholding-action=close]").click()')
            w.run_js('WithholdingRemittances.balances("2026-12-31")')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#withholding-remittance-window[open] [data-remittance-action=print-balances]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native withholding remittance balances did not load')
            result['remittance_balances']=evaluate('document.querySelector("#remittance-title").innerText')
            ImageGrab.grab().save(str(Path(__file__).with_name('remittance-native.png')))
            output.unlink()
            w.run_js('document.querySelector("#withholding-remittance-window [data-remittance-action=print-balances]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native remittance balances PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['remittance_pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("#withholding-remittance-window [data-remittance-action=close]").click()')
            w.run_js('SupplierReview.open("2026-12-31")')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#supplier-review-window[open] [data-review-tab=overdue]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native supplier review center did not load')
            result['supplier_review']=evaluate('document.querySelector("#supplier-review-title").innerText')
            assert 'مركز مراجعة الموردين' in result['supplier_review']
            ImageGrab.grab().save(str(Path(__file__).with_name('supplier-review-native.png')))
            output.unlink()
            w.run_js('document.querySelector("#supplier-review-window [data-review-action=print]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native supplier review PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['supplier_review_pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("#supplier-review-window [data-review-action=close]").click()')
            w.run_js('Treasury.open()')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#treasury-window[open] [data-treasury-action=new-receipt]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native treasury workspace did not load')
            result['treasury']=evaluate('document.querySelector("#treasury-title").innerText')
            ImageGrab.grab().save(str(Path(__file__).with_name('treasury-native.png')))
            w.run_js('Treasury.cashBook(null,{from_date:"2026-01-01",to_date:"2026-12-31"})')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#treasury-window[open] [data-treasury-action=print-report]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native cash book did not load')
            output.unlink()
            w.run_js('document.querySelector("#treasury-window [data-treasury-action=print-report]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native cash book PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['cash_book_pdf_bytes']=output.stat().st_size
            w.run_js('Treasury.balances("2026-12-31")')
            for _ in range(100):
                if evaluate('document.querySelector("#treasury-title").innerText.includes("مطابقة العُهد")'):break
                time.sleep(.1)
            else:raise AssertionError('Native custody balance report did not load')
            output.unlink()
            w.run_js('document.querySelector("#treasury-window [data-treasury-action=print-report]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native custody balances PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['custody_pdf_bytes']=output.stat().st_size
            w.run_js('document.querySelector("#treasury-window [data-treasury-action=close]").click()')
            w.run_js('CashCounts.open()')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#cash-count-window[open] [data-cash-count-action=new]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native cash count list did not load')
            w.run_js('api("cash-counts","POST",{request_key:crypto.randomUUID(),business_date:"2026-10-30",reference:crypto.randomUUID(),cash_account_id:accountingData.accounts.find(a=>a.code==="111002").id,counted_by:"قائم بالجرد في اختبار معزول",reviewed_by:"مراجع الجرد في الاختبار",notes:"محضر اختبار غير مالي",lines:[{denomination_cents:20000,quantity:0}]}).then(d=>api("cash-counts/"+d.id+"/confirm","POST",{revision:d.revision})).then(d=>CashCounts.openDocument(d.id)).catch(e=>{window.__nativeCountError=e.message})')
            for _ in range(100):
                if evaluate('Boolean(document.querySelector("#cash-count-window[open] [data-cash-count-action=print]"))'):break
                time.sleep(.1)
            else:raise AssertionError('Native saved cash count did not load: '+str(evaluate('window.__nativeCountError||""')))
            result['cash_count']=evaluate('document.querySelector("#cash-count-title").innerText')
            ImageGrab.grab().save(str(Path(__file__).with_name('cash-count-native.png')))
            output.unlink()
            w.run_js('document.querySelector("#cash-count-window [data-cash-count-action=print]").click()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100 and not getattr(w,'_am_print_state',[[]])[0]:break
                time.sleep(.1)
            else:raise AssertionError('Native cash count PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['cash_count_pdf_bytes']=output.stat().st_size
            result['ok']=True
        except BaseException as e:
            result['error']=str(e)
            try:result['treasury_error']=evaluate('document.querySelector("#treasury-error")?.innerText||""')
            except BaseException:pass
        finally:w.destroy()
    w.events.loaded+=loaded
    return w
webview.create_window=create
code=run.main()
print('NATIVE_RESULT',json.dumps(result,ensure_ascii=False),flush=True)
if code or not result.get('ok'):raise SystemExit(1)
