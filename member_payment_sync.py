"""Hostex payment-method membership sync. Amount comes from paid receipts.
No money is inferred from reservation source or total room price.
"""
import os,contextlib,datetime as dt,hashlib,json,time
from decimal import Decimal,InvalidOperation
import hostex
from member_autopay import Engine as Base, timestamp, signature
from member_order_rules import phone

METHOD=int(os.environ.get('HOSTEX_BALANCE_METHOD_ID','0'))
ROOM_ITEM=int(os.environ.get('HOSTEX_ROOM_ITEM_ID','0'))
POLICY=f'explicit_payment_method_{METHOD}'
SCHEMA='''
CREATE TABLE IF NOT EXISTS member_payment_evidence(
 code TEXT NOT NULL REFERENCES member_autopay_orders(code), revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(code,revision));
CREATE TABLE IF NOT EXISTS member_payment_receipts(
 receipt_id INTEGER PRIMARY KEY, code TEXT NOT NULL REFERENCES member_autopay_orders(code));
'''

def money(value):
 try:
  if value is None or isinstance(value,bool):raise ValueError()
  n=Decimal(str(value))*100
  if not n.is_finite() or n<0 or n>100000000 or n!=n.to_integral_value():raise ValueError()
  return int(n)
 except (InvalidOperation,ValueError,TypeError):raise hostex.Error('储值收款金额无效，未调整余额')

def receipt_signature(rows):
 return json.dumps(sorted([{k:r.get(k) for k in ('id','reservation_code','direction','item_id','payment_method_id','status','amount','currency')} for r in rows],key=lambda r:r['id']),sort_keys=True,ensure_ascii=False)

def assess_payment(stays,receipts,code,started):
 if METHOD<=0 or ROOM_ITEM<=0:raise hostex.Error('请先配置本酒店的储值付款方式与房费项目编号')
 if not stays:raise hostex.Error('订单暂未找到，不会当作取消退款')
 if any(r.get('reservation_code')!=code for r in stays):raise hostex.Error('订单编号不一致')
 if min(timestamp(r.get('created_at')) for r in stays)<=timestamp(started):return {'historical':True}
 if len({r.get('id') for r in receipts})!=len(receipts):raise hostex.Error('收支编号重复，不能重复计入')
 if any(r.get('reservation_code')!=code for r in receipts):raise hostex.Error('收支记录不属于本订单，未调整余额')
 snapshot=json.loads(receipt_signature(receipts));target=0;ids=[]
 for r in receipts:
  if r.get('payment_method_id')!=METHOD:continue
  if r.get('status')=='outstanding':continue
  if r.get('status')!='paid':raise hostex.Error('储值收款状态未知，未调整余额')
  if r.get('direction')!='income':raise hostex.Error('储值支出需关联原退款记录，暂不自动处理')
  if r.get('item_id')!=ROOM_ITEM:raise hostex.Error('当前仅接入房费的储值消费，其他项目需确认')
  if r.get('currency')!='CNY':raise hostex.Error('仅支持人民币储值消费')
  target+=money(r.get('amount'));ids.append(r['id'])
 if target>100000000:raise hostex.Error('储值收款合计超过单笔上限')
 statuses={r.get('status') for r in stays}
 result={'target':target,'cancelled':False,'released':False,'evidence':snapshot,'receipt_ids':ids}
 if statuses=={'cancelled'}:return {**result,'target':0,'cancelled':True}
 if statuses!={'accepted'}:raise hostex.Error('订单尚未生效或部分取消，请核对整单状态')
 if target==0:return {**result,'released':True}
 phones={phone(r.get('guest_phone')) for r in stays}
 if None in phones or len(phones)!=1:raise hostex.Error('请在百居易订单填写与会员档案一致的手机号后保存')
 return {**result,'phone':phones.pop()}

class Engine(Base):
 def status(self,user):
  result=super().status(user)
  result.update(payment_mode=True,payment_method_id=METHOD,payment_method_name='储值消费')
  return result
 def control(self,action,user):
  a=self.app
  if action=='pause':return super().control(action,user)
  if action!='enable':raise hostex.Error('不支持的操作')
  with a.PMS.locked():
   config=self.config()
   if not config:raise hostex.Error('请先完成本机首次支付映射配置')
   with contextlib.closing(a.connect()) as c:policy=c.execute("SELECT value FROM metadata WHERE key='member_payment_policy'").fetchone()
   if not policy or policy[0]!=POLICY:raise hostex.Error('储值付款规则尚未配置完成')
   token=a.PMS.read().get('token','')
   if not token or hashlib.sha256(token.encode()).hexdigest()!=config['token_hash']:raise hostex.Error('授权发生变化，请先核对原账号')
   methods=self.client_factory(token).get('income_methods').get('income_methods',[])
   if not any(m.get('id')==METHOD and m.get('name')=='储值消费' for m in methods):raise hostex.Error('百居易储值消费方式未找到或已改名，请核对')
   with a.transaction() as c:
    c.execute('UPDATE member_autopay_config SET enabled=1,error="" WHERE id=1');a.audit(c,user,'member_payment_sync.enable','按已收房费、已配置的储值消费方式 及会员手机号同步')
  return self.status(user)
 def date_windows(self,started):
  begin=dt.datetime.fromtimestamp(timestamp(started),self.app.CST).date()-dt.timedelta(days=1)
  end=dt.datetime.now(self.app.CST).date()+dt.timedelta(days=1)
  while begin<=end:
   last=min(end,begin+dt.timedelta(days=364));yield begin.isoformat(),last.isoformat();begin=last+dt.timedelta(days=1)
 def discover(self,client,config):
  codes=set()
  for start,end in self.date_windows(config['started']):
   rows=client.transactions({'start_date':start,'end_date':end,'direction':'income','payment_method_id':METHOD})
   for r in rows:
    if r.get('payment_method_id')!=METHOD or r.get('direction')!='income':raise hostex.Error('储值收支筛选结果不一致')
    code=r.get('reservation_code')
    if not code:raise hostex.Error('发现未关联订单的储值消费，需在百居易关联订单及会员手机号')
    codes.add(code)
  a=self.app
  with a.transaction() as c:
   for code in codes:c.execute('INSERT OR IGNORE INTO member_autopay_orders(code,store_id,created,updated) VALUES(?,?,?,?)',(code,config['store_id'],a.now(),a.now()))
   c.execute('UPDATE member_autopay_config SET last_scan=?,error="" WHERE id=1',(a.now(),))
 def receipts(self,client,code,stays,config):
  found={}
  # Query complete receipts from order creation; known IDs are also read directly
  # so moving their action date out of this range cannot silently refund money.
  started=min((s.get('created_at') for s in stays),key=timestamp) if stays else config['started']
  for start,end in self.date_windows(started):
   for r in client.transactions({'reservation_code':code,'start_date':start,'end_date':end}):
    if r.get('reservation_code')!=code:raise hostex.Error('收支订单过滤异常')
    if r['id'] in found:raise hostex.Error('跨日期收支编号重复，请稍后重试')
    found[r['id']]=r
  with contextlib.closing(self.app.connect()) as c:known=[r[0] for r in c.execute('SELECT receipt_id FROM member_payment_receipts WHERE code=?',(code,))]
  for rid in known:
   if rid in found:continue
   result=client.transactions({'id':rid})
   if not result:raise hostex.Error('已同步收款记录被删除或暂不可查，请人工核对，暂不自动退款')
   if len(result)!=1 or result[0].get('id')!=rid or result[0].get('reservation_code')!=code:raise hostex.Error('原储值收款已移至其他订单，暂停自动调整')
   found[rid]=result[0]
  return list(found.values())
 def store_for(self,client,stays):
  a=self.app
  with contextlib.closing(a.connect()) as c:
   row=c.execute("SELECT value FROM metadata WHERE key='member_payment_group_stores'").fetchone()
  mapping=json.loads(row[0]) if row else {};stores=set()
  for pid in {s.get('property_id') for s in stays}:
   if type(pid) is not int or pid<=0:raise hostex.Error('订单未分配房间，消费门店无法确认')
   rows=client.get('properties',{'id':pid,'limit':100}).get('properties',[])
   if len(rows)!=1 or rows[0].get('id')!=pid:raise hostex.Error('房间分组无法完整确认，未调整余额')
   matches={mapping[str(g['id'])] for g in rows[0].get('groups',[]) if str(g.get('id')) in mapping}
   if len(matches)!=1:raise hostex.Error('房间的门店分组未配置或冲突，暂未扣款')
   stores.update(matches)
  if len(stores)!=1:raise hostex.Error('跨门店整单储值消费需先拆分核对')
  return stores.pop()
 def cycle(self):
  a=self.app
  try:
   with a.PMS.locked():
    config=self.config()
    if not config or not config['enabled']:return
    with contextlib.closing(a.connect()) as c:policy=c.execute("SELECT value FROM metadata WHERE key='member_payment_policy'").fetchone()
    if not policy or policy[0]!=POLICY:raise hostex.Error('支付规则尚未确认，自动记账暂停')
    token=a.PMS.read().get('token','')
    if not token or hashlib.sha256(token.encode()).hexdigest()!=config['token_hash']:raise hostex.Error('授权缺失或变更，自动记账暂停')
    client=self.client_factory(token)
    try:self.discover(client,config)
    except hostex.Error as e:
     with a.transaction() as c:c.execute('UPDATE member_autopay_config SET error=? WHERE id=1',(str(e),))
    with contextlib.closing(a.connect()) as c:codes=[r[0] for r in c.execute('SELECT code FROM member_autopay_orders WHERE status!="historical" ORDER BY checked,code LIMIT 100')]
    deadline=time.monotonic()+50
    for code in codes:
     if self.stop.is_set():break
     target=None
     try:
      first=client.order(code)
      if first and min(timestamp(r.get('created_at')) for r in first)<=timestamp(config['started']):
       self.apply(code,{'historical':True},config);continue
      r1=[] if first and all(r.get('status')=='cancelled' for r in first) else self.receipts(client,code,first,config)
      second=client.order(code)
      r2=[] if second and all(r.get('status')=='cancelled' for r in second) else self.receipts(client,code,second,config)
      if signature(first)!=signature(second) or receipt_signature(r1)!=receipt_signature(r2):raise hostex.Error('订单或收款正在变化，待保存稳定后重试')
      result=assess_payment(second,r2,code,config['started']);target=result.get('target')
      if not result.get('historical') and target:result['store_id']=self.store_for(client,second)
      self.apply(code,result,config)
     except hostex.Error as e:self.block(code,str(e),target)
     if time.monotonic()>deadline:break
  except hostex.Error as e:
   if '正在进行' not in str(e):
    with a.transaction() as c:c.execute('UPDATE member_autopay_config SET error=? WHERE id=1',(str(e),))
