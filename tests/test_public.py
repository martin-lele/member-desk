import os,sys,tempfile,unittest,contextlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import member_payment_sync as sync
import hostex
class Payments(unittest.TestCase):
 def setUp(self):
  self.old=(sync.METHOD,sync.ROOM_ITEM);sync.METHOD=101;sync.ROOM_ITEM=202
  self.stays=[dict(reservation_code='demo',created_at='2026-02-01T00:00:00Z',status='accepted',guest_phone='13900000000')]
  self.receipt=dict(id=1,reservation_code='demo',payment_method_id=101,item_id=202,status='paid',direction='income',currency='CNY',amount='500.00')
 def tearDown(self):sync.METHOD,sync.ROOM_ITEM=self.old
 def assess(self,rows):return sync.assess_payment(self.stays,rows,'demo','2026-01-01T00:00:00Z')
 def test_exact_amount(self):self.assertEqual(self.assess([self.receipt])['target'],50000)
 def test_cash_not_debited(self):self.assertEqual(self.assess([{**self.receipt,'payment_method_id':999}])['target'],0)
 def test_cancel(self):
  self.stays[0]['status']='cancelled';self.assertEqual(self.assess([self.receipt])['target'],0)
 def test_phone_required(self):
  self.stays[0]['guest_phone']=''
  with self.assertRaises(hostex.Error):self.assess([self.receipt])
 def test_unconfigured_rejected(self):
  sync.METHOD=0
  with self.assertRaises(hostex.Error):self.assess([self.receipt])
 def test_duplicate_receipt(self):
  with self.assertRaises(hostex.Error):self.assess([self.receipt,self.receipt])
 def test_fresh_install(self):
  with tempfile.TemporaryDirectory() as tmp:
   os.environ['MEMBER_DATA_DIR']=tmp
   import app
   app.init()
   with contextlib.closing(app.connect()) as c:
    self.assertEqual(c.execute('SELECT count(*) FROM users').fetchone()[0],0)
    self.assertEqual(c.execute('SELECT count(*) FROM members').fetchone()[0],0)
    self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
   self.assertTrue((Path(tmp)/'setup-token.txt').exists())
if __name__=='__main__':unittest.main()
