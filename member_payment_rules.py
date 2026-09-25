"""Pure classification for a future payment-led sync. Never mutates balances.
Mappings must be explicit IDs from the user's own Hostex dictionaries.
Order source, total room price and generic 'wallet' labels are not evidence.
"""
from decimal import Decimal, InvalidOperation

def classify(record, *, balance_method_id=None, recharge_item_id=None, external_method_ids=(), room_item_id=None):
 def result(kind,reason='',delta=0):return {'kind':kind,'reason':reason,'balance_delta':delta}
 if balance_method_id is None or recharge_item_id is None:return result('blocked','尚未确认会员余额付款方式和会员充值项目编号')
 if not isinstance(record,dict):return result('blocked','收款记录格式异常')
 if record.get('status')!='paid':return result('pending','尚未确认已收款')
 if record.get('direction')!='income':return result('review','退款需核对对应的原会员交易，不能直接按支出金额调整余额')
 if record.get('currency')!='CNY':return result('blocked','仅支持人民币')
 try:
  value=record.get('amount')
  if value is None or isinstance(value,bool):raise ValueError()
  n=Decimal(str(value))*100
  if not n.is_finite() or n<=0 or n>100000000 or n!=n.to_integral_value():raise ValueError()
  n=int(n)
 except (ValueError,TypeError,InvalidOperation):return result('blocked','金额无效')
 method=record.get('payment_method_id');item=record.get('item_id')
 if item==recharge_item_id:
  if method==balance_method_id:return result('blocked','不能用会员余额给同一会员卡充值')
  if method not in external_method_ids:return result('blocked','充值收款方式尚未确认')
  return result('recharge','仍需核对会员、实际收款门店和业绩归属',n)
 if method==balance_method_id:
  if room_item_id is None or item!=room_item_id:return result('review','会员余额支付的费用项目尚未确认')
  if not record.get('reservation_code'):return result('blocked','会员房费支付未关联订单')
  return result('member_payment','仍需核对会员和实际消费门店',-n)
 if method in external_method_ids:return result('external_payment','线下收款，不改变会员卡余额')
 return result('review','付款方式尚未明确，不调整会员余额')
