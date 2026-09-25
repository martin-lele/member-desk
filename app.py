#!/usr/bin/env python3
"""Local-only member desk. Python stdlib, SQLite, no cloud or CDN dependencies."""
import argparse, contextlib, datetime as dt, hashlib, hmac, http.cookies, ipaddress, json, os, re, secrets, sqlite3, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('MEMBER_DATA_DIR',str(ROOT/'data'))).resolve()
DB_PATH=DATA/'members.sqlite3'
sys.path.insert(0,str(ROOT))
import hostex
import member_autopay
import member_payment_sync
PMS=hostex.Service(DATA)
UTC=dt.timezone.utc
CST=dt.timezone(dt.timedelta(hours=8))
SCHEMA='''
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,status TEXT NOT NULL,store_id TEXT,employee_id TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY,name TEXT NOT NULL,store_id TEXT NOT NULL REFERENCES stores(id),active INTEGER NOT NULL DEFAULT 1,UNIQUE(name,store_id));
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL UNIQUE,birthday TEXT NOT NULL,identity TEXT NOT NULL,deleted_at TEXT NOT NULL DEFAULT '',created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,member_id TEXT NOT NULL REFERENCES members(id),amount INTEGER NOT NULL CHECK(amount!=0),note TEXT NOT NULL,created TEXT NOT NULL,store_id TEXT REFERENCES stores(id),store_name TEXT NOT NULL,attribution TEXT NOT NULL,employee_id TEXT REFERENCES employees(id),employee_name TEXT NOT NULL,operator_id TEXT,operator_name TEXT NOT NULL,balance_after INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_member ON ledger(member_id,created);
CREATE INDEX IF NOT EXISTS ledger_report ON ledger(created,store_id);
CREATE TABLE IF NOT EXISTS sms_notifications(ledger_id TEXT PRIMARY KEY REFERENCES ledger(id),phone TEXT NOT NULL,amount INTEGER NOT NULL,balance INTEGER NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pms_links(id TEXT PRIMARY KEY,member_id TEXT NOT NULL REFERENCES members(id),provider TEXT NOT NULL,property_id TEXT NOT NULL,external_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT NOT NULL,created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempt_time ON login_attempts(bucket,created);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''
def now():return dt.datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')
def uid():return str(uuid.uuid4())
def connect():
 c=sqlite3.connect(DB_PATH,timeout=20,isolation_level=None);c.row_factory=sqlite3.Row
 c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA secure_delete=ON');c.execute('PRAGMA busy_timeout=20000')
 return c
@contextlib.contextmanager
def transaction():
 c=connect()
 try:
  c.execute('BEGIN IMMEDIATE');yield c;c.commit()
 except BaseException:c.rollback();raise
 finally:c.close()
def migrate(c):
 # One locked migration shared by the local and employee-entry processes.
 c.execute('BEGIN IMMEDIATE')
 try:
  additions={'members': [('practice', 'INTEGER NOT NULL DEFAULT 0')], 'ledger': [('entry_type', "TEXT NOT NULL DEFAULT ''"), ('original_id', 'TEXT'), ('payment_method', "TEXT NOT NULL DEFAULT 'legacy'"), ('payment_reference', "TEXT NOT NULL DEFAULT ''")]}
  for table,cols in additions.items():
   existing={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
   for name,definition in cols:
    if name not in existing:c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
  c.execute("UPDATE ledger SET entry_type=CASE WHEN amount>0 THEN 'recharge' ELSE 'spend' END WHERE entry_type=''")
  c.execute("CREATE TABLE IF NOT EXISTS refund_requests(id TEXT PRIMARY KEY,original_id TEXT NOT NULL,amount INTEGER NOT NULL,reason TEXT NOT NULL,requested_by TEXT NOT NULL,requester_name TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,reviewer TEXT,reviewed TEXT)")
  c.execute("CREATE TABLE IF NOT EXISTS password_resets(user_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,expires INTEGER NOT NULL)")
  c.execute("CREATE TABLE IF NOT EXISTS pms_outbox(id TEXT PRIMARY KEY,event_type TEXT NOT NULL,entity_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'awaiting_configuration',created TEXT NOT NULL,UNIQUE(event_type,entity_id))")
  c.execute("DELETE FROM sms_notifications WHERE ledger_id IN(SELECT l.id FROM ledger l JOIN members m ON m.id=l.member_id WHERE m.practice=1)")
  c.execute('DELETE FROM password_resets')
  c.execute("CREATE TRIGGER IF NOT EXISTS disable_legacy_password_resets BEFORE INSERT ON password_resets BEGIN SELECT RAISE(ABORT, 'legacy password reset disabled'); END")
  c.commit()
 except BaseException:c.rollback();raise

def enqueue(c,event,entity):
 c.execute('INSERT OR IGNORE INTO pms_outbox(id,event_type,entity_id,created) VALUES(?,?,?,?)',(uid(),event,entity,now()))

def init():
 os.umask(0o077);DATA.mkdir(parents=True,exist_ok=True);DATA.chmod(0o700)
 with contextlib.closing(connect()) as c:
  c.execute('PRAGMA journal_mode=WAL');c.executescript(SCHEMA);migrate(c);c.executescript(member_autopay.SCHEMA);c.executescript(member_payment_sync.SCHEMA)
  initialized=bool(c.execute('SELECT 1 FROM users WHERE role="admin"').fetchone())
 token_path=DATA/'setup-token.txt'
 if not initialized and not token_path.exists():token_path.write_text(secrets.token_urlsafe(32))
 DB_PATH.chmod(0o600)
class Problem(Exception):
 def __init__(self,message,status=400):self.message=message;self.status=status

def text(v,max_len=200):
 if not isinstance(v,str) or len(v.strip())>max_len:raise Problem('输入格式或长度不正确')
 return v.strip()
def password_hash(p):
 if not isinstance(p,str) or not 10<=len(p)<=128:raise Problem('密码需要 10–128 个字符')
 salt=secrets.token_hex(16);digest=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),600000).hex()
 return 'pbkdf2_sha256$600000$'+salt+'$'+digest
def verify_password(p,stored):
 try:
  _,iterations,salt,digest=stored.split('$');computed=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),int(iterations)).hex();return hmac.compare_digest(computed,digest)
 except (ValueError,AttributeError):return False
def audit(c,user,action,detail=''):
 c.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(uid(),user['id'],action,detail,now()))
def admin(user):
 if user['role']!='admin':raise Problem('此操作仅限管理员',403)
def valid_date(s):
 try:return dt.date(1900,1,1)<=dt.date.fromisoformat(s)<=dt.datetime.now(CST).date()
 except ValueError:return False
def member_input(b):
 name=text(b.get('name',''),50);phone=text(b.get('phone',''),11);identity=text(b.get('identity',''),18).upper();birthday=text(b.get('birthday',''),20)
 if not re.fullmatch(r'1[3-9]\d{9}',phone):raise Problem('请输入有效的 11 位手机号')
 m=re.fullmatch(r'(\d{4})(\d{2})(\d{2})',birthday) or re.fullmatch(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?',birthday)
 if m:birthday=f'{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}'
 if birthday and not valid_date(birthday):raise Problem('生日日期无效，例如请输入 19910605')
 if identity:
  if not re.fullmatch(r'\d{17}[\dX]',identity):raise Problem('身份证需为 18 位')
  w=[7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]
  if '10X98765432'[sum(int(x)*y for x,y in zip(identity[:17],w))%11]!=identity[-1]:raise Problem('身份证校验失败')
  dob=identity[6:10]+'-'+identity[10:12]+'-'+identity[12:14]
  if not valid_date(dob):raise Problem('身份证出生日期无效')
  if birthday and birthday!=dob:raise Problem('生日与身份证出生日期不一致，请核对')
  if not birthday:birthday=dob
 return name,phone,birthday,identity

def cents(value):
 if not isinstance(value,str) or not re.fullmatch(r'\d{1,7}(\.\d{1,2})?',value):raise Problem('金额应为正数，最多两位小数')
 a,_,b=value.partition('.');n=int(a)*100+int((b+'00')[:2])
 if not 0<n<=100000000:raise Problem('单笔金额需大于 0 且不超过 100 万元')
 return n

def public_user(u):return {k:u[k] for k in ('id','username','name','role','status','store_id','employee_id')}
def rows(c,sql,args=()):return [dict(r) for r in c.execute(sql,args).fetchall()]
def balance(c,mid):return c.execute('SELECT COALESCE(SUM(amount),0) FROM ledger WHERE member_id=?',(mid,)).fetchone()[0]
def active_member(c,mid):
 m=c.execute('SELECT * FROM members WHERE id=? AND deleted_at=""',(mid,)).fetchone()
 if not m:raise Problem('会员不存在或已删除',404)
 return m

def perform(c,u,b):
 action=b.get('action');mid=b.get('id','')
 if action=='create_member':
  m=member_input(b)
  if c.execute('SELECT 1 FROM members WHERE phone=?',(m[1],)).fetchone():raise Problem('该手机号已有会员档案，请搜索手机号；已删除会员请联系管理员恢复',409)
  mid=uid();c.execute('INSERT INTO members(id,name,phone,birthday,identity,created) VALUES(?,?,?,?,?,?)',(mid,*m,now()));c.execute('UPDATE members SET practice=? WHERE id=?',(int(b.get('practice') in (True,'1','on')),mid));audit(c,u,'create_member',mid)
  if not b.get('practice'):enqueue(c,'member.created',mid)
  return {'id':mid}
 if action=='edit_member':
  active_member(c,mid);name,phone,birthday,identity=member_input(b)
  c.execute('UPDATE members SET name=?,phone=?,birthday=?,identity=CASE WHEN ?="" THEN identity ELSE ? END WHERE id=?',(name,phone,birthday,identity,identity,mid));audit(c,u,'edit_member',mid)
  if not active_member(c,mid)['practice']:enqueue(c,'member.updated',mid)
  return {'ok':True}
 if action in ('archive_member','restore_member','purge_member'):
  admin(u);m=c.execute('SELECT * FROM members WHERE id=?',(mid,)).fetchone()
  if not m:raise Problem('会员不存在',404)
  if action!='restore_member' and balance(c,mid)!=0:raise Problem('会员仍有余额，请先真实结清后再删除')
  if action!='restore_member' and c.execute('SELECT 1 FROM member_autopay_orders WHERE member_id=?',(mid,)).fetchone():raise Problem('该会员有关联的自动扣款订单，需保留档案和流水以处理后续改价或取消，暂不能删除')
  if action=='archive_member':c.execute('UPDATE members SET deleted_at=? WHERE id=?',(now(),mid))
  elif action=='restore_member':c.execute('UPDATE members SET deleted_at="" WHERE id=?',(mid,))
  else:
   if not m['deleted_at']:raise Problem('请先将会员移入已删除列表')
   if b.get('confirmation')!='完全删除' or b.get('phone_tail')!=m['phone'][-4:]:raise Problem('请输入“完全删除”和会员手机号后四位')
   if c.execute("SELECT 1 FROM refund_requests WHERE original_id IN(SELECT id FROM ledger WHERE member_id=?) AND status='pending'",(mid,)).fetchone():raise Problem('存在待处理退款申请，请先处理')
   c.execute('DELETE FROM refund_requests WHERE original_id IN(SELECT id FROM ledger WHERE member_id=?)',(mid,))
   c.execute('DELETE FROM pms_outbox WHERE entity_id=? OR entity_id IN(SELECT id FROM ledger WHERE member_id=?)',(mid,mid))
   count=c.execute('SELECT COUNT(*) FROM ledger WHERE member_id=?',(mid,)).fetchone()[0]
   c.execute('DELETE FROM sms_notifications WHERE ledger_id IN(SELECT id FROM ledger WHERE member_id=?)',(mid,));c.execute('DELETE FROM pms_links WHERE member_id=?',(mid,));c.execute('DELETE FROM ledger WHERE member_id=?',(mid,));c.execute('DELETE FROM members WHERE id=?',(mid,))
   c.execute('DELETE FROM audit WHERE detail=?',(mid,));audit(c,u,'purge_member',f'已移除会员及 {count} 条流水，不保留客户标识');return {'ok':True,'purged':True}
  audit(c,u,action,mid);return {'ok':True}
 if action=='transaction':
  key=text(b.get('key',''),80)
  if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',key):raise Problem('交易请求编号无效')
  kind=b.get('kind');amount=cents(b.get('amount'))
  if kind not in ('recharge','spend'):raise Problem('交易类型无效')
  if kind=='spend':amount=-amount
  note=text(b.get('note','')) or ('会员充值' if amount>0 else '会员消费')
  store_id=text(b.get('store_id',''),80);attr=b.get('attribution') if amount>0 else 'none';eid=b.get('employee_id') if attr=='staff' else None
  if amount>0 and attr not in ('staff','walkin'):raise Problem('请选择业绩归属员工或上门客户')
  method=text(b.get('payment_method',''),20) if kind=='recharge' else ''
  reference=text(b.get('payment_reference',''),100) if kind=='recharge' else ''
  if kind=='recharge':
   if method not in ('cash','wechat','alipay','bank'):raise Problem('请选择收款方式')
   if b.get('receipt_confirmed') not in (True,'on'):raise Problem('请核对实际到账后勾选收款确认')
   if method!='cash' and not reference:raise Problem('非现金充值需要填写收款流水号')
  original=c.execute('SELECT * FROM ledger WHERE id=?',(key,)).fetchone()
  if original:
   if any(original[k]!=v for k,v in [('member_id',mid),('amount',amount),('note',note),('store_id',store_id),('attribution',attr),('employee_id',eid),('operator_id',u['id']),('payment_method',method),('payment_reference',reference)]):raise Problem('重复请求与原交易不一致',409)
   return {'ok':True,'replayed':True,'notification':'not_configured' if amount<0 else None}
  m=active_member(c,mid)
  if reference and c.execute('SELECT 1 FROM ledger l JOIN members m ON m.id=l.member_id WHERE l.store_id=? AND l.payment_method=? AND l.payment_reference=? AND m.practice=?',(store_id,method,reference,m['practice'])).fetchone():raise Problem('该门店的收款流水号已记账，请查看原流水，勿重复充值',409)
  store=c.execute('SELECT * FROM stores WHERE id=? AND active=1',(store_id,)).fetchone()
  if not store:raise Problem('请选择有效门店')
  if u['role']!='admin' and u['store_id']!=store_id:raise Problem('只能操作分配给你的门店',403)
  employee_name='上门客户' if attr=='walkin' else ''
  if attr=='staff':
   employee=c.execute('SELECT * FROM employees WHERE id=? AND store_id=? AND active=1',(eid,store_id)).fetchone()
   if not employee:raise Problem('请选择该门店名单中的在职员工')
   employee_name=employee['name']
  after=balance(c,mid)+amount
  if after<0:raise Problem('余额不足')
  stamp=now()
  c.execute('INSERT INTO ledger(id,member_id,amount,note,created,store_id,store_name,attribution,employee_id,employee_name,operator_id,operator_name,balance_after) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,mid,amount,note,stamp,store_id,store['name'],attr,eid,employee_name,u['id'],u['name'],after))
  c.execute('UPDATE ledger SET entry_type=?,payment_method=?,payment_reference=? WHERE id=?',(kind,method,reference,key))
  if not m['practice']:enqueue(c,'ledger.'+kind,key)
  if amount<0 and not m['practice']:c.execute('INSERT INTO sms_notifications VALUES(?,?,?,?,?,?)',(key,m['phone'],-amount,after,'not_configured',stamp))
  return {'ok':True,'notification':'not_configured' if amount<0 else None}
 if action=='request_refund':
  original=c.execute('SELECT l.*,m.practice FROM ledger l JOIN members m ON m.id=l.member_id WHERE l.id=?',(b.get('original_id'),)).fetchone()
  if not original or original['entry_type'] not in ('recharge','spend'):raise Problem('只能对原充值或消费流水申请退款')
  if u['role']!='admin' and u['store_id']!=original['store_id']:raise Problem('只能申请本门店的退款',403)
  active_member(c,original['member_id']);amount=cents(b.get('amount'));reason=text(b.get('reason',''))
  if not reason:raise Problem('请填写退款或纠错原因')
  key=text(b.get('key',''),80)
  if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',key):raise Problem('申请编号无效')
  existing=c.execute('SELECT * FROM refund_requests WHERE id=?',(key,)).fetchone()
  if existing:
   if (existing['original_id'],existing['amount'],existing['reason'],existing['requested_by'])!=(original['id'],amount,reason,u['id']):raise Problem('重复请求与原申请不一致',409)
   return {'ok':True,'replayed':True}
  reserved=c.execute("SELECT COALESCE(SUM(amount),0) FROM refund_requests WHERE original_id=? AND status IN ('pending','approved')",(original['id'],)).fetchone()[0]
  if amount+reserved>abs(original['amount']):raise Problem('超过原流水可退金额（含待审批申请）')
  c.execute('INSERT INTO refund_requests(id,original_id,amount,reason,requested_by,requester_name,status,created) VALUES(?,?,?,?,?,?,?,?)',(key,original['id'],amount,reason,u['id'],u['name'],'pending',now()));audit(c,u,action,key);return {'ok':True}
 if action=='review_refund':
  admin(u);request=c.execute('SELECT * FROM refund_requests WHERE id=?',(mid,)).fetchone()
  if not request:raise Problem('退款申请不存在',404)
  decision=b.get('decision')
  if decision not in ('approved','rejected'):raise Problem('审批结果无效')
  if request['status']!='pending':
   if request['status']==decision:return {'ok':True,'replayed':True}
   raise Problem('申请已处理，请刷新',409)
  original=c.execute('SELECT * FROM ledger WHERE id=?',(request['original_id'],)).fetchone()
  if decision=='approved':
   m=active_member(c,original['member_id']);signed=-request['amount'] if original['amount']>0 else request['amount'];after=balance(c,m['id'])+signed
   if after<0:raise Problem('会员余额不足以退还该笔充值；请核实已消费金额')
   if original['amount']>0 and b.get('refund_confirmed') not in (True,'on'):raise Problem('请确认已向客户退还款项；本系统仅记账，不会自动转账')
   key='refund-'+mid;kind='recharge_refund' if signed<0 else 'spend_refund'
   c.execute('INSERT INTO ledger(id,member_id,amount,note,created,store_id,store_name,attribution,employee_id,employee_name,operator_id,operator_name,balance_after,entry_type,original_id,payment_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,m['id'],signed,request['reason'],now(),original['store_id'],original['store_name'],original['attribution'],original['employee_id'],original['employee_name'],u['id'],u['name'],after,kind,original['id'],original['payment_method']))
   if not m['practice']:enqueue(c,'ledger.'+kind,key)
  c.execute('UPDATE refund_requests SET status=?,reviewer=?,reviewed=? WHERE id=?',(decision,u['name'],now(),mid));audit(c,u,action,mid);return {'ok':True}
 if action=='reset_password':raise Problem('管理员重置码功能已移除，请使用短信验证码找回密码',410)
 if action=='add_store':
  admin(u);name=text(b.get('name',''),50)
  if not name:raise Problem('请填写门店名称')
  c.execute('INSERT INTO stores VALUES(?,?,1)',(uid(),name));audit(c,u,action);return {'ok':True}
 if action=='add_employee':
  admin(u);name=text(b.get('name',''),50);sid=b.get('store_id')
  if not name or not c.execute('SELECT 1 FROM stores WHERE id=? AND active=1',(sid,)).fetchone():raise Problem('请填写姓名并选择有效门店')
  c.execute('INSERT INTO employees VALUES(?,?,?,1)',(uid(),name,sid));audit(c,u,action);return {'ok':True}
 if action in ('toggle_store','toggle_employee'):
  admin(u);table='stores' if action=='toggle_store' else 'employees';active=b.get('active')
  if type(active)!=bool:raise Problem('状态无效')
  c.execute('UPDATE '+table+' SET active=? WHERE id=?',(int(active),mid));audit(c,u,action);return {'ok':True}
 if action=='approve_user':
  admin(u)
  if b.get('frontdesk_verified') is not True:raise Problem('请先核实申请人本人、百居易账号及前厅角色，并勾选核验确认')
  target=c.execute('SELECT * FROM users WHERE id=? AND role="staff"',(mid,)).fetchone()
  if target and b.get('store_id'):
   sid=b['store_id']
   if not c.execute('SELECT 1 FROM stores WHERE id=? AND active=1',(sid,)).fetchone():raise Problem('请选择启用的门店')
   c.execute('INSERT OR IGNORE INTO employees VALUES(?,?,?,1)',(uid(),target['name'],sid))
   roster=c.execute('SELECT id FROM employees WHERE name=? AND store_id=?',(target['name'],sid)).fetchone();b={**b,'employee_id':roster['id']}
  employee=c.execute('SELECT e.* FROM employees e JOIN stores s ON s.id=e.store_id WHERE e.id=? AND e.active=1 AND s.active=1',(b.get('employee_id'),)).fetchone()
  if not target or not employee:raise Problem('请选择申请账号和有效员工')
  if c.execute('SELECT 1 FROM users WHERE employee_id=? AND status="active" AND id!=?',(employee['id'],mid)).fetchone():raise Problem('该员工已绑定其他账号')
  c.execute('UPDATE users SET status="active",store_id=?,employee_id=?,name=? WHERE id=?',(employee['store_id'],employee['id'],employee['name'],mid));audit(c,u,action,mid);audit(c,u,'frontdesk_identity_verified',json.dumps({'user_id':mid,'username':target['username'],'store_id':employee['store_id'],'method':'administrator_manual_review'},ensure_ascii=False));return {'ok':True}
 if action=='disable_user':
  admin(u)
  if not c.execute('SELECT 1 FROM users WHERE id=? AND role="staff"',(mid,)).fetchone():raise Problem('此处只能停用员工账号')
  c.execute('UPDATE users SET status="disabled" WHERE id=?',(mid,));c.execute('DELETE FROM sessions WHERE user_id=?',(mid,));audit(c,u,action,mid);return {'ok':True}
 if action=='change_password':
  if not verify_password(b.get('old_password',''),u['password']):raise Problem('原密码不正确')
  c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(b.get('new_password')),u['id']));c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],));return {'ok':True,'logout':True}
 raise Problem('未知操作')

class Handler(BaseHTTPRequestHandler):
 server_version='MemberDesk';sys_version=''
 def log_message(self,*args):pass
 def respond(self,payload,status=200,cookie=None):
  raw=json.dumps(payload,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.security_headers();self.send_header('Content-Length',str(len(raw)))
  if cookie:self.send_header('Set-Cookie',cookie)
  self.end_headers();self.wfile.write(raw)
 def security_headers(self):
  self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
 def check_host(self):
  if self.headers.get('Host','') not in self.server.allowed_hosts:raise Problem('访问地址不受信任',403)
 def check_origin(self):
  if self.headers.get('Origin','') not in self.server.allowed_origins:raise Problem('请求来源无效，请刷新页面重试',403)
  if self.headers.get_content_type()!='application/json':raise Problem('仅接受 JSON 请求',415)
 def get_user(self,c):
  cookie=http.cookies.SimpleCookie()
  try:cookie.load(self.headers.get('Cookie',''))
  except http.cookies.CookieError:return None
  token=cookie.get('member_session')
  if not token:return None
  digest=hashlib.sha256(token.value.encode()).hexdigest()
  row=c.execute('SELECT u.*,s.csrf,s.token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.status="active"',(digest,int(time.time()))).fetchone()
  if row and row['role']=='staff':
   if not c.execute('SELECT 1 FROM employees e JOIN stores s ON s.id=e.store_id WHERE e.id=? AND e.active=1 AND s.active=1',(row['employee_id'],)).fetchone():return None
  return row
 def require_user(self,c):
  u=self.get_user(c)
  if not u:raise Problem('请登录后使用',401)
  return u
 def do_GET(self):
  try:
   self.check_host();url=urlsplit(self.path);path=url.path;q=parse_qs(url.query)
   if path.startswith('/api/'):
    with contextlib.closing(connect()) as c:
     u=self.get_user(c)
     if path=='/api/bootstrap':
      setup=not bool(c.execute('SELECT 1 FROM users WHERE role="admin"').fetchone())
      return self.respond({'version':'2026.09.24.2','password_recovery':{'method':'sms','available':False},'application':'local-member-desk','setup':setup,'user':public_user(u) if u else None,'csrf':u['csrf'] if u else None,'storage':'本机 SQLite 数据库','remote_configured':bool(self.server.public_url)})
     u=self.require_user(c)
     if path=='/api/members':
      data=rows(c,"SELECT m.id,m.name,m.phone,m.birthday,m.created,m.deleted_at,m.practice,CASE WHEN m.identity='' THEN '' ELSE substr(m.identity,1,3)||'***********'||substr(m.identity,-4) END AS identity,COALESCE(SUM(l.amount),0) AS balance FROM members m LEFT JOIN ledger l ON l.member_id=m.id GROUP BY m.id ORDER BY m.created DESC")
      return self.respond({'members':data})
     if path=='/api/catalog':
      stores=rows(c,'SELECT * FROM stores ORDER BY name');employees=rows(c,'SELECT * FROM employees ORDER BY name')
      if u['role']!='admin':stores=[s for s in stores if s['id']==u['store_id']];employees=[e for e in employees if e['store_id']==u['store_id']]
      return self.respond({'stores':stores,'employees':employees})
     if path=='/api/ledger':
      mid=q.get('member',[''])[0]
      return self.respond({'ledger':rows(c,'SELECT l.*,n.status AS notification_status FROM ledger l LEFT JOIN sms_notifications n ON n.ledger_id=l.id WHERE l.member_id=? ORDER BY l.created DESC',(mid,))})
     if path=='/api/recharges':
      start=q.get('start',[''])[0];end=q.get('end',[''])[0];where=["l.entry_type IN ('recharge','recharge_refund')",'m.practice=0'];args=[]
      if start and end and start>end:raise Problem('开始日期不能晚于结束日期')
      for v,operator,add in [(start,'>=',0),(end,'<',1)]:
       if v:
        try:d=dt.datetime.combine(dt.date.fromisoformat(v)+dt.timedelta(days=add),dt.time(),CST).astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')
        except ValueError:raise Problem('筛选日期无效')
        where.append('l.created'+operator+'?');args.append(d)
      store=q.get('store',[''])[0];employee=q.get('employee',[''])[0]
      if u['role']!='admin':store=u['store_id']
      if store:where.append('l.store_id=?');args.append(store)
      if employee=='walkin':where.append('l.attribution="walkin"')
      elif employee:where.append('l.employee_id=?');args.append(employee)
      data=rows(c,'SELECT l.*,m.name AS member_name,m.phone FROM ledger l JOIN members m ON m.id=l.member_id WHERE '+' AND '.join(where)+' ORDER BY l.created DESC',args)
      groups={}
      for r in data:
       key=(r['store_id'],r['employee_id'],r['attribution'])
       if key not in groups:groups[key]={'store':r['store_name'],'employee':r['employee_name'] or '历史未归属','amount':0,'count':0}
       groups[key]['amount']+=r['amount'];groups[key]['count']+=1
      return self.respond({'records':data,'groups':list(groups.values()),'total':sum(r['amount'] for r in data)})
     if path=='/api/refunds':
      where='' if u['role']=='admin' else ' WHERE l.store_id=?';args=() if u['role']=='admin' else (u['store_id'],)
      return self.respond({'requests':rows(c,'SELECT r.*,l.member_id,l.entry_type,l.store_name,m.name AS member_name,m.practice FROM refund_requests r JOIN ledger l ON l.id=r.original_id JOIN members m ON m.id=l.member_id'+where+' ORDER BY r.created DESC',args)})
     if path=='/api/pms/status':
      admin(u);return self.respond({**PMS.status(),'autopay':AUTO.status(u)})
     if path=='/api/member-orders':return self.respond(AUTO.status(u))
     if path=='/api/users':
      admin(u);return self.respond({'users':rows(c,'SELECT id,username,name,role,status,store_id,employee_id,created FROM users ORDER BY created DESC')})
     raise Problem('页面不存在',404)
   name={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}.get(path)
   if not name:raise Problem('页面不存在',404)
   raw=(ROOT/'web'/name).read_bytes();self.send_response(200);self.security_headers();self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8','svg':'image/svg+xml'}[name.split('.')[-1]]);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  except hostex.Error as e:self.respond({'error':str(e)},400)
  except Problem as e:self.respond({'error':e.message},e.status)
  except (sqlite3.Error,OSError):self.respond({'error':'本机数据库或文件暂不可用'},503)
 def do_POST(self):
  try:
   self.check_host();self.check_origin();length=int(self.headers.get('Content-Length','0'))
   if not 0<length<=65536:raise Problem('请求过大或为空',413)
   b=json.loads(self.rfile.read(length))
   if not isinstance(b,dict):raise Problem('请求格式无效')
   path=urlsplit(self.path).path
   if path in ('/api/setup','/api/register','/api/login','/api/reset-password','/api/password/send-code','/api/password/verify-code'):return self.auth_action(path,b)
   if path.startswith('/api/pms/'):
    with contextlib.closing(connect()) as c:
     u=self.require_user(c);admin(u)
     if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),u['csrf']):raise Problem('页面已失效，请刷新后重试',403)
     stores={r['id'] for r in c.execute('SELECT id FROM stores WHERE active=1')}
    action=path.rsplit('/',1)[-1]
    if action in ('enable','pause'):return self.respond(AUTO.control(action,u))
    if AUTO.config():raise Problem('已建立自动扣款订单关联，不能直接替换或移除授权；更换账号需由维护人员核对历史订单')
    result=PMS.execute(action,b,not self.server.public_url,stores)
    with transaction() as c:audit(c,u,'pms_'+path.rsplit('/',1)[-1],'百居易只读集成操作（不记录密钥或返回的客户信息）')
    return self.respond(result)
   with transaction() as c:
    u=self.require_user(c)
    if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),u['csrf']):raise Problem('页面已失效，请刷新后重试',403)
    if path=='/api/logout':c.execute('DELETE FROM sessions WHERE token=?',(u['token'],));result={'ok':True}
    elif path=='/api/action':result=perform(c,u,b)
    else:raise Problem('接口不存在',404)
   if result.get('purged'):
    try:
     with contextlib.closing(connect()) as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except sqlite3.Error:pass
   self.respond(result)
  except hostex.Error as e:self.respond({'error':str(e)},400)
  except Problem as e:self.respond({'error':e.message},e.status)
  except sqlite3.IntegrityError:self.respond({'error':'记录已存在，请检查手机号、账号或名称（包含已删除记录）'},409)
  except (ValueError,TypeError,json.JSONDecodeError):self.respond({'error':'请求格式无效'},400)
  except OSError:self.respond({'error':'本机配置文件暂不可用，请稍后重试'},503)
  except sqlite3.Error:self.respond({'error':'数据库忙碌或操作失败，请稍后重试'},503)
 def auth_action(self,path,b):
  if path=='/api/setup' and self.server.public_url:raise Problem('首次设置仅可通过本机入口完成',403)
  username=text(b.get('username',''),40).lower()
  if not re.fullmatch(r'[a-z0-9_.@-]{3,40}',username):raise Problem('账号需为 3–40 位英文字母、数字或 _.@-')
  password=b.get('password','')
  if not isinstance(password,str) or len(password)>128:raise Problem('密码格式无效')
  if path=='/api/setup':
   if self.server.public_url or not ipaddress.ip_address(self.client_address[0]).is_loopback:raise Problem('首次设置只能在本机进行',403)
   p=DATA/'setup-token.txt'
   if not p.exists() or not hmac.compare_digest(text(b.get('setup_token',''),100),p.read_text().strip()):raise Problem('请通过本机“启动会员台.command”打开首次设置页面',403)
   name=text(b.get('name',''),50);store=text(b.get('store',''),50)
   if not name or not store:raise Problem('请填写管理员姓名和第一家门店')
   hashed=password_hash(password)
   with transaction() as c:
    if c.execute('SELECT 1 FROM users WHERE role="admin"').fetchone():raise Problem('管理员已设置',409)
    sid,eid,user_id=uid(),uid(),uid();c.execute('INSERT INTO stores VALUES(?,?,1)',(sid,store));c.execute('INSERT INTO employees VALUES(?,?,?,1)',(eid,name,sid));c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?)',(user_id,username,name,hashed,'admin','active',sid,eid,now()))
   p.unlink(missing_ok=True);return self.respond({'ok':True,'message':'管理员已建立，请登录'})
  stamp=int(time.time());bucket=hashlib.sha256((username+'|'+self.client_address[0]).encode()).hexdigest();ipbucket=hashlib.sha256(self.client_address[0].encode()).hexdigest()
  with transaction() as c:
   c.execute('DELETE FROM login_attempts WHERE created<?',(stamp-900,))
   if c.execute('SELECT COUNT(*) FROM login_attempts WHERE bucket IN (?,?)',(bucket,ipbucket)).fetchone()[0]>=40:raise Problem('尝试过于频繁，请 15 分钟后重试',429)
   c.execute('INSERT INTO login_attempts VALUES(?,?)',(bucket,stamp));c.execute('INSERT INTO login_attempts VALUES(?,?)',(ipbucket,stamp))
  if path in ('/api/reset-password','/api/password/send-code','/api/password/verify-code'):
   if path=='/api/reset-password':raise Problem('管理员重置码功能已移除，请使用短信验证码找回密码',410)
   if not re.fullmatch(r'1[3-9]\d{9}',username):raise Problem('请输入注册账号使用的 11 位手机号')
   raise Problem('短信验证码服务尚未开通，暂时无法找回密码；请使用原密码登录',503)
  if path=='/api/register':
   name=text(b.get('name',''),50)
   if not name:raise Problem('请填写真实姓名')
   hashed=password_hash(password)
   with transaction() as c:
    if not c.execute('SELECT 1 FROM users WHERE role="admin"').fetchone():raise Problem('请先由管理员完成本机设置')
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?)',(uid(),username,name,hashed,'staff','pending',None,None,now()))
   return self.respond({'ok':True,'message':'申请已提交，请联系管理员核实百居易前厅身份并分配门店；通过后使用本次设置的会员台密码登录'})
  with transaction() as c:
   u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
   if not u or not verify_password(password,u['password']):raise Problem('账号或密码不正确',401)
   if u['status']!='active':raise Problem('注册申请待审批，请联系管理员在“门店与员工”中分配门店' if u['status']=='pending' else '账号已停用，请联系管理员恢复',403)
   if u['role']=='staff' and not c.execute('SELECT 1 FROM employees e JOIN stores s ON s.id=e.store_id WHERE e.id=? AND e.active=1 AND s.active=1',(u['employee_id'],)).fetchone():raise Problem('所属门店或员工已停用',403)
   token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);digest=hashlib.sha256(token.encode()).hexdigest();c.execute('DELETE FROM sessions WHERE expires<?',(stamp,));c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest,u['id'],csrf,stamp+28800))
  secure='; Secure' if self.server.public_url else ''
  return self.respond({'ok':True},cookie=f'member_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'+secure)

AUTO=member_payment_sync.Engine(sys.modules[__name__])

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--public-url',default='');args=parser.parse_args()
 if args.public_url and (urlsplit(args.public_url).scheme!='https' or urlsplit(args.public_url).path not in ('','/')):parser.error('--public-url must be an HTTPS origin behind a configured reverse proxy')
 init();server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.daemon_threads=True
 server.public_url=args.public_url.rstrip('/');server.allowed_hosts={f'localhost:{args.port}',f'127.0.0.1:{args.port}'};server.allowed_origins={f'http://localhost:{args.port}',f'http://127.0.0.1:{args.port}'}
 if server.public_url:server.allowed_hosts.add(urlsplit(server.public_url).netloc);server.allowed_origins={server.public_url}
 if not server.public_url:
  worker=threading.Thread(target=AUTO.run,name='member-autopay',daemon=True);worker.start()
 print(f'MemberDesk ready: http://127.0.0.1:{args.port}/',flush=True)
 server.timeout=1;stop_file=DATA/('stop-'+str(args.port));stop_file.unlink(missing_ok=True)
 try:
  while not stop_file.exists():server.handle_request()
  stop_file.unlink(missing_ok=True)
 except KeyboardInterrupt:pass
 finally:
  AUTO.stop.set();server.server_close()
  if not server.public_url:worker.join(timeout=35)
if __name__=='__main__':main()
