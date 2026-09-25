"""Pure validation for Hostex member-source orders. Does not debit or call APIs."""
from decimal import Decimal,InvalidOperation
import re,hashlib,json

def phone(value):
 if not isinstance(value,str):return None
 value=re.sub(r'[\s()-]','',value)
 if value.startswith('+86'):value=value[3:]
 elif value.startswith('0086'):value=value[4:]
 return value if re.fullmatch(r'1[3-9]\d{9}',value) else None

def amount_in_fen(value):
 if isinstance(value,bool) or value is None:return None
 try:
  n=Decimal(str(value));fen=n*100
  if not n.is_finite() or n<=0 or n>1000000 or fen!=fen.to_integral_value():return None
  return int(fen)
 except (InvalidOperation,ValueError,TypeError):return None

def assess_order(stays,source_id=None):
 # Caller must first retrieve every stay for one reservation_code.
 if not stays or any(not isinstance(r,dict) for r in stays):return {'eligible':False,'reason':'订单资料为空或格式无效'}
 codes={r.get('reservation_code') for r in stays}
 if len(codes)!=1 or not isinstance(next(iter(codes)),str) or not next(iter(codes)):return {'eligible':False,'reason':'订单编号缺失或混入其他订单'}
 code=next(iter(codes));result={'reservation_code':code,'eligible':False}
 if any((r.get('custom_channel') or {}).get('id')!=source_id for r in stays):return {**result,'reason':'不是指定会员来源，或订单各入住单来源不一致'}
 if any(r.get('status')!='accepted' for r in stays):return {**result,'reason':'订单未生效、已取消或状态不一致，不能扣款'}
 phones={phone(r.get('guest_phone')) for r in stays}
 if None in phones or len(phones)!=1:return {**result,'reason':'请在百居易填写一致且有效的会员手机号'}
 totals=[(r.get('rates') or {}).get('total_rate') or {} for r in stays]
 if any(t.get('currency')!='CNY' for t in totals):return {**result,'reason':'仅支持人民币订单，币种缺失或不一致'}
 amounts={amount_in_fen(t.get('amount')) for t in totals}
 if None in amounts or len(amounts)!=1:return {**result,'reason':'整单房费缺失、非正数、精度无效或各入住单金额不一致'}
 # total_rate is the whole reservation amount; never add it across stays.
 result.update(eligible=True,phone=next(iter(phones)),amount=next(iter(amounts)),currency='CNY',source_id=source_id)
 result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 return result

def match_member(db,assessment):
 if not assessment.get('eligible'):return assessment
 m=db.execute('SELECT id,deleted_at,practice FROM members WHERE phone=?',(assessment['phone'],)).fetchone()
 if not m:return {**assessment,'eligible':False,'reason':'未找到会员，请核对手机号或先建档'}
 if m['deleted_at'] or m['practice']:return {**assessment,'eligible':False,'reason':'已删除或练习会员不可用于正式订单扣款'}
 available=db.execute('SELECT COALESCE(SUM(amount),0) FROM ledger WHERE member_id=?',(m['id'],)).fetchone()[0]
 if available<assessment['amount']:return {**assessment,'eligible':False,'reason':'会员余额不足，不能扣款'}
 return {**assessment,'member_id':m['id'],'balance':available}
