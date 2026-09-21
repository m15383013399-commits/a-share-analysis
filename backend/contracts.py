"""Strict new forecasts, with legacy read compatibility in market_diary."""
from datetime import date
from market_diary.forecast_ledger import validate_forecast, _validate_probabilities
from market_diary.calendar import load_calendar, next_trade_day
from backend.state import ROOT

def validate_new_forecast(document,previous=None):
    if document.get('schema_version') != '2.0':raise ValueError('新预测必须使用 schema_version=2.0')
    result=validate_forecast(document,previous=previous)
    expected=next_trade_day(date.fromisoformat(result['trade_date']),load_calendar(ROOT/'config/trading_calendar_2026.json')).isoformat()
    if result['next_trade_date']!=expected:raise ValueError('预测目标必须是下一交易日')
    for group in ['indices','sectors','watch_pool']:
        for item in result[group]:
            lo,hi=item.get('lower'),item.get('upper')
            if lo is None or hi is None or not ((lo>0 and hi>0) or (lo<0 and hi<0)):
                raise ValueError(f'{group}/{item["code"]}: 预测区间必须同号且非零')
            _validate_probabilities(item.get('probabilities'),item['code'])
            if not str(item.get('invalidation','')).strip():raise ValueError('每项预测必须填写失效条件')
            if not str(item.get('week_view',item.get('week_range',''))).strip():raise ValueError('每项预测必须填写一周观点')
            if group=='watch_pool' and item.get('status')!='active':raise ValueError('五只股票必须全部 active')
    return result
