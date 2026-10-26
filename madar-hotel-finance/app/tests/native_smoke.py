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
            w.run_js('document.querySelector("#print-document").innerHTML="<h1>A&amp;M native print test</h1><p>Saved document printing</p>"; window.print()')
            for _ in range(100):
                if output.exists() and output.stat().st_size>100:break
                time.sleep(.1)
            else:raise AssertionError('Native PDF was not created')
            assert output.read_bytes().startswith(b'%PDF')
            result['pdf_bytes']=output.stat().st_size
            result['ok']=True
        except BaseException as e:result['error']=str(e)
        finally:w.destroy()
    w.events.loaded+=loaded
    return w
webview.create_window=create
code=run.main()
print('NATIVE_RESULT',json.dumps(result,ensure_ascii=False),flush=True)
if code or not result.get('ok'):raise SystemExit(1)
