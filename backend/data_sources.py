"""On-demand supplementary evidence. Missing data is never replaced with zero."""
from __future__ import annotations
import json, math, time
from datetime import date,datetime,timedelta
from pathlib import Path
from urllib.parse import urlparse
from backend import state
from backend.evidence import install_http_audit
from market_diary.providers import provider_timeout
from market_diary.storage import atomic_write_json

DATASETS=[('indices','六指数行情','腾讯 / AkShare'),('breadth','全市场广度与成交额','新浪 / AkShare'),('sectors','行业与概念代理','东方财富 / 新浪'),('history','历史日线','东方财富，经 AkShare'),('financials','财务摘要','新浪，经 AkShare'),('news','公告与新闻','东方财富，经 AkShare'),('gold','黄金本体','GoldAPI / 上海黄金交易所')]

def records(frame):
    return json.loads(frame.to_json(orient='records',date_format='iso',force_ascii=False))

def safe_call(label,fn):
    try:
        with provider_timeout(35):value=fn()
        if not value:raise ValueError('数据源返回空数据')
        return {'status':'available','records':value,'fetched_at':state.now()}
    except Exception as exc:
        return {'status':'unavailable','reason':f'{label}采集失败（{type(exc).__name__}）；请重试或检查网络和权限','records':[],'fetched_at':state.now()}

def history_metrics(rows):
    prices=[r['close'] for r in rows if isinstance(r.get('close'),(int,float)) and r['close']>0]
    result={'observations':len(prices),'adjustment':'none','warning':'未复权；除权除息可能造成价格跳变，不据此自动认定趋势反转'}
    for n in [5,20,60]:result[f'ma{n}']=round(sum(prices[-n:])/n,4) if len(prices)>=n else None
    if len(prices)>=2:result['latest_change_pct']=round((prices[-1]/prices[-2]-1)*100,4)
    return result

def fetch_history(ak,code,kind,as_of):
    end=date.fromisoformat(as_of);start=end-timedelta(days=500)
    kwargs={'symbol':code,'start_date':start.strftime('%Y%m%d'),'end_date':end.strftime('%Y%m%d')}
    frame=ak.index_zh_a_hist(**kwargs) if kind=='index' else ak.stock_zh_a_hist(**kwargs,period='daily',adjust='')
    out=[]
    for r in records(frame):
        day=str(r.get('日期',r.get('date','')))[:10]
        if day>end.isoformat():continue
        if day==state.today() and datetime.now(state.TZ).hour<15:continue
        row={'date':day,'open':r.get('开盘'),'close':r.get('收盘'),'high':r.get('最高'),'low':r.get('最低'),'volume':r.get('成交量'),'amount':r.get('成交额')}
        if not day or not all(isinstance(row[k],(float,int)) and math.isfinite(row[k]) and row[k]>0 for k in ['open','close','high','low']):continue
        if row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):continue
        out.append(row)
    if len({r['date'] for r in out})!=len(out):raise ValueError('历史行情日期重复')
    return sorted(out,key=lambda x:x['date'])[-250:]

def collect_supplement(task_id,code,kind,as_of):
    import akshare as ak
    run=state.RUNTIME/'runs'/task_id;run.mkdir(exist_ok=True)
    install_http_audit(run/'http')
    result={'instrument_id':f'{kind}:{"SH" if code.startswith("6") or (kind=="index" and code.startswith("0")) else "SZ"}:{code}','code':code,'kind':kind,'as_of':as_of,'fetched_at':state.now(),'available_at':state.now(),'historical_replay_safe':False,'sources':[]}
    state.update(task_id,stage=f'采集 {code} 历史日线')
    result['history']=safe_call('日线',lambda:fetch_history(ak,code,kind,as_of))
    result['history'].update(unit='CNY' if kind=='stock' else 'index_points',adjustment='none',source_url='https://quote.eastmoney.com/',date_verified=True)
    result['history']['metrics']=history_metrics(result['history']['records'])
    if kind=='stock' and as_of==state.today():
        state.update(task_id,stage=f'采集 {code} 财务与事件资料')
        result['financials']=safe_call('财务摘要',lambda:records(ak.stock_financial_abstract(symbol=code))[:45])
        result['financials'].update(source_url=f'https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/{code}.phtml',date_verified=False,warning='仅当前研究可参考；接口缺披露时间，不可作为历史时点财务证据')
        def news():
            out=[]
            for r in records(ak.stock_news_em(symbol=code)):
                url=str(r.get('新闻链接',''));published=str(r.get('发布时间',''))
                if urlparse(url).scheme not in ('http','https'):continue
                if not published or published[:10]>as_of:continue
                out.append({'title':r.get('新闻标题'),'summary':str(r.get('新闻内容',''))[:1800],'published_at':published,'source_url':url,'source':r.get('文章来源')})
            return out[:12]
        result['news']=safe_call('新闻',news)
        result['news'].update(source_url='https://so.eastmoney.com/',date_verified=False,warning='新闻须核对来源与发布时间，不能据单篇报道确认行业趋势')
    else:
        for k in ['financials','news']:result[k]={'status':'unavailable','records':[],'reason':'指数不适用，或历史日期缺少当时可见的归档资料'}
    atomic_write_json(run/'supplement.json',result)
    atomic_write_json(state.RUNTIME/'supplements'/f'{kind}-{code}-{as_of}.json',result)
    return result

def collect_gold(task_id):
    import requests,akshare as ak
    run=state.RUNTIME/'runs'/task_id;run.mkdir(exist_ok=True)
    install_http_audit(run/'http')
    s=state.settings(); result={'fetched_at':state.now(),'as_of':state.today(),'asset_type':'gold_price','research_only':True,'scoring':'尚未纳入正式预测评分'}
    def international():
        if not s.get('gold_api_key'):raise ValueError('未配置 GoldAPI 凭证')
        r=requests.get('https://www.goldapi.io/api/XAU/USD',headers={'x-access-token':s['gold_api_key']},timeout=15);r.raise_for_status();v=r.json()
        price=float(v['price']);ts=int(v['timestamp'])
        if not math.isfinite(price) or price<=0 or abs(time.time()-ts)>86400:raise ValueError('报价不新鲜或无效')
        return [{'symbol':'XAU/USD','price':price,'quoted_at':datetime.fromtimestamp(ts,state.TZ).isoformat(),'unit':'USD/troy_oz','state':'intraday_or_delayed','source_url':'https://www.goldapi.io/','provider':'GoldAPI'}]
    result['xauusd']=safe_call('XAU/USD',international)
    if not s.get('gold_api_key'):result['xauusd']['reason']='未配置 GoldAPI 密钥；国际现货黄金暂不可用'
    def domestic():
        rows=records(ak.spot_hist_sge(symbol='Au99.99'))
        rows=[r for r in rows if str(r.get('date',''))[:10]<=state.today()]
        if not rows:return []
        r=sorted(rows,key=lambda r:str(r['date']))[-1]
        return [{'symbol':'Au99.99','price':r['close'],'trade_date':str(r['date'])[:10],'unit':'CNY/g','timezone':'Asia/Shanghai','state':'daily_historical','source_url':'https://www.sge.com.cn/sjzx/mrhq'}]
    result['au9999']=safe_call('Au99.99',domestic)
    atomic_write_json(run/'gold.json',result);atomic_write_json(state.RUNTIME/'supplements'/'gold.json',result)
    return result
