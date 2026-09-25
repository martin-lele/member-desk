"""Hostex read-only credentials and member-order API client."""
import fcntl,json,os,re,tempfile
from contextlib import contextmanager
class Error(Exception):pass

class Service:
 def __init__(self,data):self.path=data/'hostex-private.json';self.lock=data/'hostex-config.lock'
 @contextmanager
 def locked(self):
  with self.lock.open('a') as f:
   os.chmod(self.lock,0o600)
   try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise Error('百居易操作正在进行，请稍后重试')
   try:yield
   finally:fcntl.flock(f,fcntl.LOCK_UN)
 def read(self):
  try:return json.loads(self.path.read_text())
  except FileNotFoundError:return {}
  except (ValueError,OSError):raise Error('本机百居易配置无法读取')
 def save(self,v):
  fd,name=tempfile.mkstemp(dir=self.path.parent,prefix='.hostex-')
  try:
   with os.fdopen(fd,'w') as f:json.dump(v,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
   os.chmod(name,0o600);os.replace(name,self.path)
  finally:
   if os.path.exists(name):os.unlink(name)
 def status(self):
  c=self.read();return {'configured':bool(c.get('token')),'member_connected':False,'mode':'members_only','message':'只读授权已保存；下方可启用会员来源订单自动扣款' if c.get('token') else '尚未保存只读授权'}
 def execute(self,action,b,local,stores):
  if action not in ('configure','disconnect'):raise Error('该功能已移除，目前仅保留会员对接授权配置')
  if not local:raise Error('授权配置只能在这台主机的本机入口操作')
  with self.locked():
   if action=='disconnect':self.path.unlink(missing_ok=True);return self.status()
   token=b.get('token','')
   if not isinstance(token,str) or not 8<=len(token)<=4096 or re.search(r'[^\x21-\x7e]',token):raise Error('请填写有效格式的百居易 Access Token')
   self.save({'token':token,'scope':'members_only','member_connected':False})
   return self.status()

class NoRedirect(__import__('urllib.request',fromlist=['HTTPRedirectHandler']).HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise Error('百居易接口发生重定向，已停止请求')

class Client:
 """Read-only, fixed-host API. Never include response bodies/secrets in errors."""
 def __init__(self,token):self.token=token
 def get(self,path,params=None):
  import urllib.request,urllib.parse,urllib.error
  if path not in ('reservations','custom_channels','transactions','income_methods','properties','groups'):raise Error('仅允许读取会员订单及来源')
  req=urllib.request.Request('https://api.myhostex.com/v3/'+path+'?'+urllib.parse.urlencode(params or {}),headers={'Hostex-Access-Token':self.token,'Accept':'application/json'},method='GET')
  try:
   with urllib.request.build_opener(NoRedirect()).open(req,timeout=15) as response:
    raw=response.read(8000001)
   if len(raw)>8000000:raise Error('百居易响应过大，已停止处理')
   v=json.loads(raw,parse_float=str)
  except (OSError,ValueError):raise Error('百居易连接失败，暂未调整余额，稍后自动重试')
  if isinstance(v,dict) and v.get('error_code')==400:raise Error('百居易拒绝订单查询参数，本次未调整余额，请联系维护人员')
  if not isinstance(v,dict) or v.get('error_code') not in (0,200) or not isinstance(v.get('data'),dict):raise Error('百居易授权或接口返回异常，请检查授权及 OpenAPI 权限')
  return v['data']
 def page(self,offset=0,code=None,window=None):
  p={'offset':offset,'limit':100,'order_by':'created_at'}
  if window:p.update(start_check_out_date=window[0],end_check_out_date=window[1])
  if code:p['reservation_code']=code
  rows=self.get('reservations',p).get('reservations')
  if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise Error('百居易订单列表格式异常')
  return rows
 def order(self,code):
  rows=[];seen=set()
  for offset in range(0,10000,100):
   page=self.page(offset,code)
   if not page:return rows
   for r in page:
    stay=r.get('stay_code')
    if r.get('reservation_code')!=code or not isinstance(stay,str) or not stay or stay in seen:raise Error('订单分页重复或不完整，本次未调整余额')
    seen.add(stay);rows.append(r)
  raise Error('订单超过分页上限，本次未调整余额')
 def transactions(self,params):
  rows=[];seen=set();expected=None
  for offset in range(0,10000,100):
   data=self.get('transactions',{**params,'offset':offset,'limit':100});page=data.get('transactions');total=data.get('total')
   if not isinstance(page,list) or type(total) is not int or total<0:raise Error('收支分页格式异常，未调整余额')
   if expected is not None and total!=expected:raise Error('收支记录正在变化，稍后重试')
   expected=total
   for r in page:
    if not isinstance(r,dict) or type(r.get('id')) is not int or r['id'] in seen:raise Error('收支编号重复或缺失，未调整余额')
    seen.add(r['id']);rows.append(r)
   if len(rows)==total:return rows
   if not page or len(rows)>total:raise Error('收支列表不完整，未调整余额')
  raise Error('收支分页超过上限，未调整余额')
