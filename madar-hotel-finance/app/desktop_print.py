"""Native Qt printing for the supported Linux desktop backend."""
def configure(window):
    from PyQt6.QtPrintSupport import QPrinter, QPrintDialog
    from PyQt6.QtWidgets import QDialog
    from PyQt6.QtGui import QPageSize
    view=window.native.webview
    active=[]
    def finished(success):
        active.clear()
    def requested():
        if active:return
        printer=QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        dialog=QPrintDialog(printer,window.native)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            active.append(printer)
            view.print(printer)
    view.printRequested.connect(requested)
    view.printFinished.connect(finished)
    window._am_print_state=(active,requested,finished)
