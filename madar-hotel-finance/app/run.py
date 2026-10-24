"""Local launcher. Python 3.10+; no third-party dependencies."""
from pathlib import Path
import argparse, threading, webbrowser, socket
from http.server import ThreadingHTTPServer
import server
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8090);p.add_argument('--no-browser',action='store_true');args=p.parse_args()
    server.init()
    try: http=ThreadingHTTPServer(('127.0.0.1',args.port),server.Handler)
    except OSError:
        print('Port is occupied. Close the previous app instance or use --port 8093.');raise SystemExit(1)
    if not args.no_browser:threading.Timer(.7,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    print('A&M Accounting Hotel for Audit &Control is running. Keep this terminal open. Press Ctrl+C to stop.',flush=True)
    try:http.serve_forever()
    except KeyboardInterrupt:pass
    finally:http.server_close()
