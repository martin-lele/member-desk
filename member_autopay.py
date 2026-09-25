"""Membership-only Hostex consumer. Immutable deltas, atomic wallet updates.
All callers share a file lock from fetch through commit. No PMS writes.
"""
import contextlib,datetime as dt,hashlib,json,time,threading
from decimal import Decimal,InvalidOperation
import hostex
from member_order_rules import phone

SCHEMA='''
CREATE TABLE IF NOT EXISTS member_autopay_config(
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, started TEXT NOT NULL,
 source_id INTEGER NOT NULL, source_name TEXT NOT NULL, store_id TEXT NOT NULL REFERENCES stores(id),
 token_hash TEXT NOT NULL, last_scan TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS member_autopay_orders(
 code TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id), member_id TEXT REFERENCES members(id),
 member_phone TEXT NOT NULL DEFAULT '', charged INTEGER NOT NULL DEFAULT 0 CHECK(charged>=0),
 target INTEGER, revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT NOT NULL DEFAULT '', checked TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
 created TEXT NOT NULL, first_ledger TEXT);
CREATE INDEX IF NOT EXISTS member_autopay_due ON member_autopay_orders(checked);
CREATE TABLE IF NOT EXISTS member_autopay_entries(
 code TEXT NOT NULL REFERENCES member_autopay_orders(code), revision INTEGER NOT NULL,
 ledger_id TEXT NOT NULL UNIQUE REFERENCES ledger(id), PRIMARY KEY(code,revision));
'''

def timestamp(s):
 try:
  d=dt.datetime.fromisoformat(s.replace('Z','+00:00'))
  if d.tzinfo is None:raise ValueError()
  return d.timestamp()
 except (ValueError,AttributeError,TypeError):raise hostex.Error('订单创建时间缺失或无时区，暂未处理')

def assess(stays,code,source_id,started):
 if not stays:raise hostex.Error('订单暂未找到；不会将查询失败当作取消退款')
 if any(r.get('reservation_code')!=code for r in stays):raise hostex.Error('订单编号不一致')
 dates=[timestamp(r.get('created_at')) for r in stays]
 if min(dates)<=timestamp(started):return {'historical':True}
 statuses={r.get('status') for r in stays}
 # An explicit full cancellation returns money to the pinned member even if
 # the PMS guest/phone/source has since been cleared. Missing != cancellation.
 if statuses=={'cancelled'}:return {'target':0,'cancelled':True}
 if any((r.get('custom_channel') or {}).get('id')!=source_id for r in stays):raise hostex.Error('会员订单来源已改变或不一致，请恢复原会员来源后重试')
 if statuses!={'accepted'}:raise hostex.Error('订单尚未生效或包含部分取消，请核对整单状态')
 phones={phone(r.get('guest_phone')) for r in stays}
 if None in phones or len(phones)!=1:raise hostex.Error('请在百居易填写与会员档案一致的手机号（所有入住单一致）')
 amounts=set()
 for r in stays:
  total=(r.get('rates') or {}).get('total_rate') or {}
  if total.get('currency')!='CNY':raise hostex.Error('只支持人民币房费，币种缺失或不一致')
  value=total.get('amount')
  try:
   if value is None or isinstance(value,bool):raise ValueError()
   a=Decimal(str(value))*100
   if not a.is_finite() or a<0 or a>100000000 or a!=a.to_integral_value():raise ValueError()
   amounts.add(int(a))
  except (InvalidOperation,ValueError,TypeError):raise hostex.Error('订单房费缺失或金额格式无效')
 if len(amounts)!=1:raise hostex.Error('多房间订单整单房费不一致，本次未调整余额')
 return {'target':amounts.pop(),'phone':phones.pop(),'cancelled':False}

def signature(rows):
 # Compare financial inputs across two full reads, not volatile ancillary data.
 return json.dumps(sorted([{'stay':r.get('stay_code'),'code':r.get('reservation_code'),'created':r.get('created_at'),'status':r.get('status'),'phone':r.get('guest_phone'),'source':(r.get('custom_channel') or {}).get('id'),'total':(r.get('rates') or {}).get('total_rate')} for r in rows],key=lambda r:str(r['stay'])),sort_keys=True,ensure_ascii=False)

class Engine:
 def __init__(self,app,client_factory=hostex.Client):self.app=app;self.client_factory=client_factory;self.stop=threading.Event()
 def config(self):
  with contextlib.closing(self.app.connect()) as c:
   r=c.execute('SELECT * FROM member_autopay_config WHERE id=1').fetchone()
   return dict(r) if r else None
 def status(self,user):
  a=self.app;config=self.config()
  with contextlib.closing(a.connect()) as c:
   where='' if user['role']=='admin' else ' WHERE o.store_id=?'
   args=() if user['role']=='admin' else (user['store_id'],)
   orders=a.rows(c,'SELECT o.code,o.store_id,o.charged,o.target,o.status,o.reason,o.checked,o.updated,m.name AS member_name FROM member_autopay_orders o LEFT JOIN members m ON m.id=o.member_id'+where+' ORDER BY (o.status="blocked") DESC,o.updated DESC LIMIT 100',args)
   counts=dict(c.execute('SELECT status,COUNT(*) FROM member_autopay_orders o'+where+' GROUP BY status',args).fetchall())
   store=c.execute('SELECT name FROM stores WHERE id=?',(config['store_id'],)).fetchone() if config else None
  return {'enabled':bool(config and config['enabled']),'started':config['started'] if config else '', 'source_name':config['source_name'] if config else '会员','store_name':store[0] if store else '', 'last_scan':config['last_scan'] if config else '', 'error':config['error'] if config else '', 'orders':orders,'counts':counts,'interval':30,'discovery_end':self.windows(config)[-1][1] if config else ''}
 def control(self,action,user):
  a=self.app
  with a.PMS.locked():
   config=self.config()
   if action=='pause':
    with a.transaction() as c:c.execute('UPDATE member_autopay_config SET enabled=0 WHERE id=1');a.audit(c,user,'member_autopay.pause')
   elif action=='enable':
    raise hostex.Error('来源驱动扣款已禁用；请使用经过配置和验证的付款方式适配器')
   else:raise hostex.Error('不支持的自动扣款操作')
  return self.status(user)
 def windows(self,config):
  # API permits at most 180 days per discovery request. Keep the entire
  # activation-to-now interval after downtime, plus a visible 2-year horizon.
  begin=dt.datetime.fromtimestamp(timestamp(config['started']),self.app.CST).date()-dt.timedelta(days=1)
  end=dt.datetime.now(self.app.CST).date()+dt.timedelta(days=730)
  windows=[]
  while begin<=end:
   last=min(end,begin+dt.timedelta(days=179));windows.append((begin.isoformat(),last.isoformat()));begin=last+dt.timedelta(days=1)
  return windows
 def discover(self,client,config):
  a=self.app;cutoff=timestamp(config['started']);codes={}
  for window in self.windows(config):
   seen=set();previous=None
   for offset in range(0,10000,100):
    page=client.page(offset,window=window)
    if not page:break
    dates=[timestamp(r.get('created_at')) for r in page]
    if previous is not None and dates[0]>previous:raise hostex.Error('订单分页排序发生变化，将自动重新扫描')
    for r,created in zip(page,dates):
     stay=r.get('stay_code');code=r.get('reservation_code')
     if not isinstance(stay,str) or not isinstance(code,str) or not code or stay in seen:raise hostex.Error('订单分页重复或编号缺失，本轮扫描未完成，将自动重试')
     seen.add(stay)
     if created>cutoff and (r.get('custom_channel') or {}).get('id')==config['source_id']:codes[code]=r['created_at']
    if any(dates[i]<dates[i+1] for i in range(len(dates)-1)):raise hostex.Error('订单排序异常，未完成扫描')
    previous=dates[-1]
    if min(dates)<=cutoff:break
   else:raise hostex.Error('新订单扫描超过 100 页，请联系维护人员；已跟踪订单仍继续核对')
  with a.transaction() as c:
   for code,created in codes.items():c.execute('INSERT OR IGNORE INTO member_autopay_orders(code,store_id,created,updated) VALUES(?,?,?,?)',(code,config['store_id'],created,a.now()))
   c.execute('UPDATE member_autopay_config SET last_scan=?,error="" WHERE id=1',(a.now(),))
 def block(self,code,reason,target=None):
  a=self.app
  with a.transaction() as c:
   old=c.execute('SELECT status,reason,target FROM member_autopay_orders WHERE code=?',(code,)).fetchone()
   changed=old and (old['status']!='blocked' or old['reason']!=reason or old['target']!=target)
   c.execute('UPDATE member_autopay_orders SET status="blocked",reason=?,target=?,checked=?,updated=CASE WHEN ? THEN ? ELSE updated END WHERE code=?',(reason,target,a.now(),changed,a.now(),code))
 def apply(self,code,result,config):
  a=self.app
  with a.transaction() as c:
   state=c.execute('SELECT * FROM member_autopay_orders WHERE code=?',(code,)).fetchone()
   if not state:raise hostex.Error('本机订单记录缺失')
   state=dict(state)
   if result.get('store_id') and result['store_id']!=state['store_id']:
    if state['charged']:raise hostex.Error('已扣款订单的消费门店改变，请核对原订单后处理')
    state['store_id']=result['store_id'];c.execute('UPDATE member_autopay_orders SET store_id=? WHERE code=?',(result['store_id'],code))
   if result.get('historical'):
    if state['charged']:raise hostex.Error('订单创建日期变更，需人工核对')
    c.execute('UPDATE member_autopay_orders SET status="historical",reason="启用前的历史订单，不自动扣款",checked=? WHERE code=?',(a.now(),code));return
   target=result['target'];delta=target-state['charged'];mid=state['member_id']
   actual=-c.execute('SELECT COALESCE(SUM(l.amount),0) FROM member_autopay_entries e JOIN ledger l ON l.id=e.ledger_id WHERE e.code=?',(code,)).fetchone()[0]
   if actual!=state['charged']:raise hostex.Error('自动扣款流水与订单余额不一致，已阻止自动调整')
   if not result['cancelled'] and not result.get('released'):
    if mid and result['phone']!=state['member_phone']:raise hostex.Error('订单手机号在关联后被修改，已暂停调整；请核对并恢复原会员手机号')
    member=c.execute('SELECT * FROM members WHERE id=?' if mid else 'SELECT * FROM members WHERE phone=?',(mid or result['phone'],)).fetchone()
    if not member:raise hostex.Error('未找到会员，请先按订单手机号建立会员档案')
    if member['deleted_at'] or member['practice']:raise hostex.Error('已删除或练习会员不能自动扣款')
    if member['phone']!=result['phone']:raise hostex.Error('会员档案手机号已更改，请核对原会员与订单手机号')
    mid=member['id']
   if delta:
    if not mid:raise hostex.Error('未找到原扣款会员，无法退款')
    available=a.balance(c,mid)
    if delta>available:raise hostex.Error('余额不足，请先充值；补足后自动重试差额，不会部分扣款')
    if delta>0 and not c.execute('SELECT 1 FROM stores WHERE id=? AND active=1',(state['store_id'],)).fetchone():raise hostex.Error('扣款门店已停用，请联系管理员')
    store=c.execute('SELECT name FROM stores WHERE id=?',(state['store_id'],)).fetchone()[0]
    entry_type='pms_spend' if delta>0 else 'pms_refund';revision=state['revision']+1;lid=a.uid()
    label='取消退回' if result['cancelled'] else '储值收款调整'
    if result.get('evidence') is not None:label+='（按实际储值支付）'
    note=f'百居易订单 {code} · {label} · 原已扣 {state["charged"]/100:.2f} 元 → 应扣 {target/100:.2f} 元'
    c.execute('INSERT INTO ledger(id,member_id,amount,note,created,store_id,store_name,attribution,employee_id,employee_name,operator_id,operator_name,balance_after,entry_type,original_id,payment_method,payment_reference) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(lid,mid,-delta,note,a.now(),state['store_id'],store,'pms',None,'',None,'百居易会员订单自动记账',available-delta,entry_type,state['first_ledger'],'member_balance',code))
    c.execute('INSERT INTO member_autopay_entries VALUES(?,?,?)',(code,revision,lid))
    if delta>0:
     member_phone=c.execute('SELECT phone FROM members WHERE id=?',(mid,)).fetchone()[0]
     c.execute('INSERT INTO sms_notifications VALUES(?,?,?,?,?,?)',(lid,member_phone,delta,available-delta,'not_configured',a.now()))
    a.audit(c,{'id':'system:hostex'},'member_autopay.delta',json.dumps({'code':code,'ledger_id':lid,'delta':-delta}))
    c.execute('UPDATE member_autopay_orders SET charged=?,revision=?,first_ledger=COALESCE(first_ledger,?) WHERE code=?',(target,revision,lid,code))
   if 'evidence' in result:
    revision=state['revision']+(1 if delta else 0)
    c.execute('INSERT OR REPLACE INTO member_payment_evidence VALUES(?,?,?,?)',(code,revision,json.dumps(result['evidence'],ensure_ascii=False),a.now()))
    for rid in result.get('receipt_ids',[]):
     old=c.execute('SELECT code FROM member_payment_receipts WHERE receipt_id=?',(rid,)).fetchone()
     if old and old[0]!=code:raise hostex.Error('此收款已关联另一订单，暂停处理以避免重复扣款')
     c.execute('INSERT OR IGNORE INTO member_payment_receipts VALUES(?,?)',(rid,code))
   status='cancelled' if result['cancelled'] else 'external' if result.get('released') else 'settled'
   changed=delta!=0 or state['status']!=status or state['target']!=target
   c.execute('UPDATE member_autopay_orders SET member_id=?,member_phone=CASE WHEN member_phone="" THEN ? ELSE member_phone END,target=?,status=?,reason="",checked=?,updated=CASE WHEN ? THEN ? ELSE updated END WHERE code=?',(mid,result.get('phone',''),target,status,a.now(),changed,a.now(),code))
 def cycle(self):
  a=self.app
  try:
   with a.PMS.locked():
    config=self.config()
    if not config or not config['enabled']:return
    token=a.PMS.read().get('token','')
    if not token or hashlib.sha256(token.encode()).hexdigest()!=config['token_hash']:raise hostex.Error('只读授权缺失或改变，自动调整已停止')
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
      first=client.order(code);second=client.order(code)
      if signature(first)!=signature(second):raise hostex.Error('订单正在变更，待数据稳定后自动重试')
      result=assess(second,code,config['source_id'],config['started']);target=result.get('target');self.apply(code,result,config)
     except hostex.Error as e:self.block(code,str(e),target)
     if time.monotonic()>deadline:break
  except hostex.Error as e:
   if '正在进行' not in str(e):
    with a.transaction() as c:c.execute('UPDATE member_autopay_config SET error=? WHERE id=1',(str(e),))
 def run(self):
  while not self.stop.is_set():
   try:self.cycle()
   except Exception:
    # Never log API bodies or member data. Surface unexpected failure to staff.
    try:
     with self.app.transaction() as c:c.execute('UPDATE member_autopay_config SET error="自动核对异常，余额事务已回滚，请联系维护人员" WHERE id=1')
    except Exception:pass
   self.stop.wait(30)
