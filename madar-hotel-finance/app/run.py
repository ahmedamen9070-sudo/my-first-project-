"""A&M desktop launcher: native application window, no external browser."""
import argparse
import sys
import threading
from http.server import ThreadingHTTPServer
import server

TITLE = 'A&M — Accounting Hotel for Audit &Control | نيو بانوراما رمسيس'

def main():
    parser=argparse.ArgumentParser(description='A&M desktop application')
    parser.add_argument('--port',type=int,default=0,help='Internal local service port; automatic by default')
    parser.add_argument('--headless-server',action='store_true',help='Development/API testing only, no desktop window')
    parser.add_argument('--no-browser',dest='headless_server',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args.headless_server:
        try:
            import webview
        except ImportError:
            print('Desktop components are missing. Run START_WINDOWS.bat or install requirements-desktop.txt. No browser will be opened.',file=sys.stderr)
            return 1
    server.init()
    try:
        http=ThreadingHTTPServer(('127.0.0.1',args.port),server.Handler)
    except OSError:
        print('Unable to start the internal service. Close the previous instance or use a different --port.',file=sys.stderr)
        return 1
    if args.headless_server:
        print('A&M development service started on port '+str(http.server_port),flush=True)
        try: http.serve_forever()
        except KeyboardInterrupt: pass
        finally: http.server_close()
        return 0
    worker=threading.Thread(target=http.serve_forever,daemon=True,name='am-local-data')
    worker.start()
    try:
        window=webview.create_window(TITLE,f'http://127.0.0.1:{http.server_port}',width=1440,height=1000,min_size=(980,720),background_color='#03111b',text_select=True,confirm_close=True)
        if sys.platform.startswith('linux'):
            from desktop_print import configure
            window.events.before_show+=lambda:configure(window)
        webview.start(gui='qt' if sys.platform.startswith('linux') else None,debug=False,private_mode=True,localization={'global.quitConfirmation':'هل تريد إغلاق A&M؟ تأكد من حفظ أي تعديلات قبل الإغلاق.'})
        return 0
    except Exception as exc:
        print('Unable to initialize the desktop window: '+str(exc),file=sys.stderr)
        return 1
    finally:
        http.shutdown();http.server_close();worker.join(timeout=3)

if __name__=='__main__': raise SystemExit(main())
