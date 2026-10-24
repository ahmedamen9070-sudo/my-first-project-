import unittest, tempfile, subprocess, os, json, urllib.request, urllib.error, time, sqlite3
from pathlib import Path
SERVER=Path(__file__).resolve().parents[1]/'server.py'
class Workflow(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.db=Path(cls.tmp.name)/'test.sqlite';cls.start()
 @classmethod
 def start(cls):
  cls.proc=subprocess.Popen(['python3',str(SERVER),'--port','8092'],env={**os.environ,'MADAR_DB':str(cls.db)},stdout=subprocess.DEVNULL)
  for _ in range(100):
   try: cls.token=cls.req('bootstrap')['csrf'];return
   except Exception: time.sleep(.05)
  raise RuntimeError('Server startup failed')
 @classmethod
 def tearDownClass(cls):cls.proc.terminate();cls.proc.wait();cls.tmp.cleanup()
 @classmethod
 def req(cls,path,method='GET',data=None,token=True):
  headers={'Content-Type':'application/json'}
  if token: headers['X-Madar-CSRF']=getattr(cls,'token','')
  r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8092/api/'+path,data=json.dumps(data).encode() if data is not None else None,method=method,headers=headers),timeout=5)
  return json.load(r)
 def test_full_workflow(self):
  s=self.req('suppliers','POST',{'name':'مورد اختبار','phone':'0123'})
  s2=self.req('suppliers','POST',{'name':'مورد ثان'})
  p=self.req('products','POST',{'supplier_id':s['id'],'department_id':1,'name':'صيانة','unit':'زيارة','price':'100.05'})
  data={'request_key':'once','supplier_id':s['id'],'department_id':1,'reference':'TEST-1','business_date':'2026-10-24','kind':'service','tax_rate':'0','lines':[{'product_id':p['id'],'quantity':'1.5','price':'100.05'}]}
  i=self.req('invoices','POST',data);self.assertEqual(i['total_cents'],15008);self.assertEqual(i['kind'],'service');self.assertEqual(i['status'],'draft')
  self.assertEqual(self.req('invoices','POST',data)['id'],i['id'])
  with self.assertRaises(urllib.error.HTTPError) as e:self.req('invoices','POST',{**data,'request_key':'duplicate'})
  self.assertEqual(e.exception.code,409)
  with self.assertRaises(urllib.error.HTTPError):self.req('invoices','POST',{**data,'request_key':'foreign','supplier_id':s2['id'],'reference':'FOREIGN'})
  with self.assertRaises(urllib.error.HTTPError):self.req('invoices','POST',{**data,'request_key':'tax','reference':'TAX','tax_rate':'14'})
  with self.assertRaises(urllib.error.HTTPError):self.req('invoices','POST',{**data,'request_key':'bad','reference':'BAD','lines':[{'product_id':p['id'],'quantity':'-1','price':'1'}]})
  updated=self.req('invoices/'+str(i['id']),'PUT',{**data,'revision':i['revision'],'lines':[{'product_id':p['id'],'quantity':'2','price':'100.05'}]})
  self.assertEqual(updated['total_cents'],20010)
  with self.assertRaises(urllib.error.HTTPError) as e:self.req('invoices/'+str(i['id']),'PUT',{**data,'revision':1})
  self.assertEqual(e.exception.code,409)
  with self.assertRaises(urllib.error.HTTPError) as e:self.req('suppliers','POST',{'name':'forged'},token=False)
  self.assertEqual(e.exception.code,403)
  backup=self.req('backups','POST',{});con=sqlite3.connect(self.db.parent/'backups'/backup['filename']);self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(con.execute('SELECT total_cents FROM invoices').fetchone()[0],20010);con.close()
  self.proc.terminate();self.proc.wait();type(self).start()
  self.assertEqual(self.req('invoices/'+str(i['id']))['total_cents'],20010)
  self.assertEqual(self.req('dashboard')['count'],1)
  cancelled=self.req('invoices/'+str(i['id']),'PUT',{'revision':updated['revision'],'action':'cancel'})
  self.assertEqual(cancelled['status'],'cancelled');self.assertEqual(self.req('dashboard')['count'],0)
  self.assertGreaterEqual(len(self.req('audit')),6)
  with self.assertRaises(urllib.error.HTTPError):self.req('invoices/'+str(i['id']),'PUT',{**data,'revision':cancelled['revision']})
if __name__=='__main__':unittest.main(verbosity=2)
