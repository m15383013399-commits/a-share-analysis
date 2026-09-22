"""One isolated process per research task; no model can write the ledger directly."""
from __future__ import annotations
import json,os,sys,subprocess
from datetime import date,datetime
from pathlib import Path
from backend import state
from backend.data_sources import collect_supplement,collect_gold
from backend.contracts import validate_new_forecast
from market_diary.storage import atomic_write_json,atomic_write_text
from market_diary.calendar import load_calendar,is_trade_day,next_trade_day

SOURCE_URLS=['https://qt.gtimg.cn/','https://finance.sina.com.cn/','https://quote.eastmoney.com/']

def cli(task_id,args,config):
    run=state.RUNTIME/'runs'/task_id;run.mkdir(exist_ok=True)
    cfg=run/'config.json';atomic_write_json(cfg,config)
    env=dict(os.environ,ASHARE_HTTP_AUDIT=str(run/'http'),PYTHONPATH=str(state.ROOT))
    with (run/'cli.log').open('a') as log:
        p=subprocess.run([sys.executable,str(state.ROOT/'scripts/run_daily_report.py'),'--config',str(cfg),*args],cwd=state.ROOT,env=env,stdout=log,stderr=log,timeout=600)
    return p.returncode

def config_for(task_id):
    run=state.RUNTIME/'runs'/task_id
    config=state.read(state.ROOT/'config.example.json')
    config.update(report_name='A股分析平台',output_dir=str(run/'facts'),snapshot_dir=str(run/'snapshots'),forecast_dir=str(state.RUNTIME/'forecasts'),evaluation_detail_path=str(state.RUNTIME/'evaluations/evaluations.jsonl'),evaluation_summary_path=str(state.RUNTIME/'evaluations/summary.json'),calendar_path=str(state.ROOT/'config/trading_calendar_2026.json'),watch_codes=state.settings()['watch_codes'])
    return config

def collect(task_id,as_of):
    state.update(task_id,stage='校验交易日与采集收盘数据')
    cfg=config_for(task_id)
    with state.lock():
        existing=state.RUNTIME/'snapshots'/f'{as_of}.json'
        if existing.exists():
            snap=state.read(existing)
            if snap.get('market_status')=='ready' and snap.get('quality_status')=='passed':return snap
        if as_of!=state.today():raise ValueError('该历史日期缺少正式快照；实时行情接口不能补造过去收盘数据')
        code=cli(task_id,['--full-close-run','--date',as_of],cfg)
        if code:
            names={2:('failed','采集或配置错误，请查看本地任务日志'),3:('waiting_close','休市或收盘数据尚未稳定；正式收盘需上海时间15:15后'),4:('failed','行情质量校验未通过，未写入正式数据'),5:('failed','预测或评价校验失败，原台账已保留')}
            status,message=names.get(code,('failed',f'采集退出码 {code}'))
            cal=load_calendar(state.ROOT/'config/trading_calendar_2026.json')
            if code==3 and not is_trade_day(date.fromisoformat(as_of),cal):
                status='skipped_closed';message='休市；下一交易日 '+next_trade_day(date.fromisoformat(as_of),cal).isoformat()
            state.update(task_id,status=status,error=message,stage='采集未完成',result={'exit_code':code});return None
        snap=state.read(Path(cfg['snapshot_dir'])/f'{as_of}.json')
        if not snap or snap.get('market_status')!='ready' or snap.get('quality_status')!='passed':raise ValueError('正式快照没有通过质量门')
        if existing.exists() and state.read(existing)!=snap:raise ValueError('同日快照已存在不同版本，禁止覆盖')
        atomic_write_json(existing,snap)
        atomic_write_json(state.RUNTIME/'evidence'/f'{as_of}.json',{'snapshot_hash':state.digest(snap),'task_id':task_id,'fetched_at':state.now(),'audit_path':f'runs/{task_id}/http','provider_date_warning':'非腾讯数据未必含供应商日期，见原始载荷；不作为严格历史重放保证'})
        return snap

def previous_forecasts(as_of):
    values=[state.read(p) for p in sorted((state.RUNTIME/'forecasts').glob('*.json'))]
    earlier=[v for v in values if v.get('trade_date','')<as_of]
    previous=max(earlier,key=lambda v:v['trade_date']) if earlier else None
    targets=[v for v in earlier if v.get('next_trade_date')==as_of]
    if len(targets)>1:raise ValueError('多份预测指向同一交易日，需检查台账')
    return previous,targets[0] if targets else None

def evaluation_rows(as_of):
    path=state.RUNTIME/'evaluations/evaluations.jsonl'
    if not path.exists():return []
    return [json.loads(line) for line in path.read_text().splitlines() if line and json.loads(line).get('actual_date')==as_of]

def context_for(as_of,snap=None,supplement=None):
    previous,target=previous_forecasts(as_of)
    evidence={};urls=set(SOURCE_URLS)
    if snap:
        if (snap.get('market_status'),snap.get('quality_status'))!=('ready','passed'):raise ValueError('只能分析 ready + passed 的正式 A 股快照')
        codes={r['code'] for r in previous['watch_pool']} if previous else set(state.settings()['watch_codes'])
        evidence['snapshot']={**snap,'stocks':[r for r in snap['stocks'] if r['code'] in codes],'sectors':{k:v[:12] for k,v in snap['sectors'].items() if isinstance(v,list)}}
        prior=[p for p in sorted((state.RUNTIME/'snapshots').glob('*.json')) if p.stem<as_of]
        if prior:
            old=state.read(prior[-1]);evidence['previous_snapshot']={k:old[k] for k in ['trade_date','indices','breadth']}
        evidence['evaluation']=evaluation_rows(as_of)
    if supplement:
        evidence['instrument']=supplement
        for part in ['history','financials','news']:
            s=supplement.get(part,{})
            if s.get('source_url'):urls.add(s['source_url'])
            for row in s.get('records',[]):
                if row.get('source_url'):urls.add(row['source_url'])
    gold=state.read(state.RUNTIME/'supplements/gold.json')
    if gold and gold.get('as_of')==as_of:
        evidence['gold']=gold
        for part in ['xauusd','au9999']:
            for row in gold.get(part,{}).get('records',[]):urls.add(row['source_url'])
    else:evidence['gold']={'status':'unavailable','reason':'该日期没有独立黄金报价，禁止使用黄金股票替代'}
    for path in sorted((state.RUNTIME/'supplements').glob(f'stock-*-{as_of}.json')):
        v=state.read(path)
        if snap and v['code'] in codes:
            evidence['stock_'+v['code']]=v
            for part in ['history','financials','news']:
                if v.get(part,{}).get('source_url'):urls.add(v[part]['source_url'])
                for row in v.get(part,{}).get('records',[]):
                    if row.get('source_url'):urls.add(row['source_url'])
    if snap:
        for path in sorted((state.RUNTIME/'supplements').glob(f'index-*-{as_of}.json')):
            v=state.read(path);evidence['index_'+v['code']]=v
        evidence['cumulative_evaluation']=state.read(state.RUNTIME/'evaluations/summary.json',{})
    return {'as_of':as_of,'generated_at':state.now(),'evidence':evidence,'previous_forecast':previous,'forecast_for_today':target,'ledger_note':'有当日目标预测，按实际结果复盘' if target else ('台账缺口：缺少面向该交易日的预测，禁止补造' if previous else '基线日：尚无前日预测'),'source_urls':sorted(urls),'snapshot_hash':state.digest(snap) if snap else None,'limitations':['技术指标基于未复权日线，除权跳变需核验','黄金政策解释仅可使用有时间的证据','概率为研究判断，不是验证后的准确率']}

def render_report(output,context,status):
    lines=[f'# {output.get("title","A股研究报告")}',f'\n研究日期：{context["as_of"]} · {status}', '\n'+output['summary']]
    for s in output['sections']:lines.extend(['\n## '+s['heading'],s['text'],'证据：'+', '.join(s['evidence_ids'])])
    lines+=['\n## 昨日预测与今日实际',context['ledger_note']]
    rows=context['evidence'].get('evaluation',[])
    if rows:
        lines+=['| 标的 | 预测区间 | 实际涨跌幅 | 偏差 | 评价 |','|---|---|---|---|---|']
        for r in rows:lines.append(f'| {r.get("asset_name",r.get("asset_code"))} | {r.get("lower","—")} ~ {r.get("upper","—")} | {r.get("actual_pct","—")} | {r.get("error","—")} | {r.get("reason",r["status"])} |')
    fc=output.get('forecast')
    if fc:
        lines+=['\n## 下一交易日预测：'+fc['next_trade_date'],'| 标的 | 区间% | 上/震/下概率 | 一周判断 | 失效条件 |','|---|---|---|---|---|']
        for group in ['indices','sectors','watch_pool']:
            for r in fc[group]:
                p=r['probabilities'];lines.append(f"| {r['name']} {r['code']} | {r['lower']} ~ {r['upper']} | {p['up']}/{p['flat']}/{p['down']} | {r.get('week_view',r.get('week_range',''))} | {r['invalidation']} |")
    lines+=['\n## 风险与失效条件',*['- '+str(r) for r in output['risks']],'\n## 黄金价格',str(output.get('gold',{}).get('text','缺少独立价格证据，暂无判断')),'\n不构成投资建议']
    return '\n\n'.join(lines)

def research(task_id,payload):
    as_of=payload['date'];mode=payload.get('mode','close')
    codes=state.settings()['watch_codes']
    retry_of=payload.get('retry_of')
    if retry_of:
        import shutil
        source=state.RUNTIME/'runs'/retry_of;target=state.RUNTIME/'runs'/task_id
        context=state.read(source/'context.json')
        if not context:raise ValueError('重试的冻结证据已不存在')
        for name in ['analysis','review','repair']:
            if (source/f'{name}.json').exists():shutil.copy2(source/f'{name}.json',target/f'{name}.json')
        codes=[r['code'] for r in (context.get('previous_forecast') or {}).get('watch_pool',[])] or codes
    else:
        if mode=='close':
            snap=state.read(state.RUNTIME/'snapshots'/f'{as_of}.json')
            if not snap:snap=collect(task_id,as_of)
            if not snap:return
            # Evaluate even when reusing a frozen snapshot; this remains idempotent.
            with state.lock():
                cfg=config_for(task_id);cfg['snapshot_dir']=str(state.RUNTIME/'snapshots')
                _,target=previous_forecasts(as_of)
                if target and cli(task_id,['--evaluate','--date',as_of],cfg)!=0:raise ValueError('昨日预测评价失败；停止新预测，检查台账')
            previous,_=previous_forecasts(as_of)
            codes=[r['code'] for r in previous['watch_pool']] if previous else state.settings()['watch_codes']
            for code in codes:
                cached=state.read(state.RUNTIME/'supplements'/f'stock-{code}-{as_of}.json')
                if not cached:collect_supplement(task_id,code,'stock',as_of)
            for code in ['399006','000688','000905','000300']:
                if not (state.RUNTIME/'supplements'/f'index-{code}-{as_of}.json').exists():collect_supplement(task_id,code,'index',as_of)
            if as_of==state.today():
                gold=state.read(state.RUNTIME/'supplements/gold.json')
                if not gold or gold.get('as_of')!=as_of:collect_gold(task_id)
            context=context_for(as_of,snap)
        else:
            key=f'{payload["asset_type"]}-{payload["code"]}-{as_of}.json'
            supplement=state.read(state.RUNTIME/'supplements'/key) or collect_supplement(task_id,payload['code'],payload['asset_type'],as_of)
            if supplement['history']['status']!='available':raise ValueError('没有可用标的历史行情，暂不能分析')
            context=context_for(as_of,supplement=supplement)
    formal=mode=='close' and as_of==state.today()
    from backend.research import generate
    output=generate(task_id,context,formal)
    status='研究报告'
    if formal and output['review_status']=='passed':
        fc=output['forecast'];run=state.RUNTIME/'runs'/task_id
        atomic_write_json(run/'forecast-draft.json',fc)
        with state.lock():
            cfg=config_for(task_id)
            if cli(task_id,['--forecast-file',str(run/'forecast-draft.json')],cfg)!=0:
                status='草稿：正式预测登记失败（可能同日已存在不同预测）'
            else:status='正式预测已入账'
    elif output['review_status']!='passed':status='草稿：修订后尚需再次审阅，未入账'
    evidence=context['evidence'];g=evidence.get('gold',{})
    incomplete=mode=='close' and (not g.get('xauusd',{}).get('records') or any(evidence.get('stock_'+code,{}).get(part,{}).get('status')!='available' for code in codes for part in ['history','financials','news']))
    report={'engine':state.read(state.RUNTIME/'runs'/task_id/'engine.json',{}).get('analysis_engine','api'),'id':task_id,'date':as_of,'mode':mode,'code':payload.get('code'),'status':status,'created_at':state.now(),'data_hash':context['snapshot_hash'],'output':output,'context':context,'incomplete':incomplete,'limitations':context['limitations'],'markdown':render_report(output,context,status)}
    atomic_write_json(state.RUNTIME/'reports'/f'{task_id}.json',report)
    atomic_write_text(state.RUNTIME/'reports'/f'{task_id}.md',report['markdown'])
    state.update(task_id,status='degraded' if '草稿' in status or incomplete else 'succeeded',stage='分析完成',result={'report_id':task_id,'publication':status,'incomplete':incomplete})

def demo(task_id):
    from market_diary.sample import build_sample_payload
    data=build_sample_payload('2026-01-05')
    output={'title':'先看数据，再作判断','summary':'这是离线演示，用于了解采集、证据和报告的阅读方式。示例不是实时行情，也不会进入正式预测与评分。','sections':[{'heading':'数据决定分析的边界','text':'每条行情都保留来源和时间。只有通过质量校验的正式快照，才能进入收盘研究。缺失项会明确列出，不会补成零。','evidence_ids':['demo']},{'heading':'让每次判断都能回看','text':'报告将昨日预测与实际结果放在一起，计算方向命中、区间命中和误差。一次表现不能代表长期有效。','evidence_ids':['demo']}],'risks':['演示数据不能用于投资判断'],'gold':{'text':'演示不提供黄金价格或预测'},'forecast':None}
    context={'as_of':'2026-01-05','evidence':{'demo':{'kind':'synthetic','label':'内置合成演示数据'}},'ledger_note':'演示模式，不计分','snapshot_hash':None,'source_urls':[]}
    report={'id':task_id,'date':'2026-01-05','mode':'demo','status':'离线演示 · 不入账','created_at':state.now(),'output':output,'context':context,'markdown':render_report(output,context,'离线演示'),'incomplete':False}
    atomic_write_json(state.RUNTIME/'demo'/f'{task_id}.json',report)
    state.update(task_id,status='succeeded',stage='演示已生成',result={'report_id':task_id,'demo':True})

def execute(task_id):
    state.init();t=state.task(task_id);p=t['payload'];(state.RUNTIME/'runs'/task_id).mkdir(exist_ok=True)
    try:
        if t['kind']=='collect':
            snap=collect(task_id,p['date'])
            if snap:state.update(task_id,status='succeeded',stage='正式快照已就绪',result={'date':p['date'],'snapshot_hash':state.digest(snap)})
        elif t['kind']=='supplement':
            result=collect_supplement(task_id,p['code'],p['asset_type'],p['date'])
            state.update(task_id,status='succeeded' if all(result[k]['status']=='available' for k in ['history','financials','news']) else 'degraded',stage='资料采集完成',result={'dataset':result['instrument_id'],'warnings':[result[k].get('reason') for k in ['history','financials','news'] if result[k]['status']!='available']})
        elif t['kind']=='gold':
            r=collect_gold(task_id);state.update(task_id,status='succeeded' if all(r[k]['status']=='available' for k in ['xauusd','au9999']) else 'degraded',stage='黄金资料采集完成',result=r)
        elif t['kind']=='research':research(task_id,p)
        elif t['kind']=='enginecheck':
            from backend.research import call_model
            result=call_model(task_id,'connection',[{'role':'user','content':'这是分析引擎连通测试。不读取文件、不执行命令、不调用工具。只返回 JSON 对象 {"ok":true}。'}])
            if result.get('ok') is not True:raise ValueError('引擎返回不符合预期')
            state.update(task_id,status='succeeded',stage='分析引擎测试通过',result={'engine':state.settings()['analysis_engine']})
        elif t['kind']=='demo':demo(task_id)
        else:raise ValueError('未知任务类型')
    except Exception as exc:
        # Raw exception messages can contain provider credentials; only controlled errors are shown.
        message=str(exc) if isinstance(exc,ValueError) else f'{type(exc).__name__}：任务失败，请检查资料、设置后重试'
        for key in ['api_key','gold_api_key']:
            secret=state.settings().get(key)
            if secret:message=message.replace(secret,'[已隐藏]')
        state.update(task_id,status='failed',stage='任务中止',error=message[:600])

if __name__=='__main__':execute(sys.argv[1])
