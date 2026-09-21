from __future__ import annotations
import hashlib,hmac,json,re,secrets,time
from contextlib import asynccontextmanager
from datetime import date,datetime
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,HTTPException,Request,Response,Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,field_validator
from backend import state
from backend.data_sources import DATASETS
from market_diary.calendar import load_calendar,is_trade_day,next_trade_day

@asynccontextmanager
async def lifespan(app):state.init();yield
app=FastAPI(title='A股分析平台',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def local_security(request,call_next):
    host=request.headers.get('host','').split(':')[0]
    if host not in {'localhost','127.0.0.1','testserver'}:
        return Response('不允许的主机',400)
    origin=request.headers.get('origin')
    if request.method not in {'GET','HEAD','OPTIONS'}:
        if origin and urlsplit(origin).netloc!=request.headers.get('host'):
            return Response('不允许的跨域请求',403)
        if not request.headers.get('content-type','').startswith('application/json'):
            return Response('需要 JSON 请求',415)
    response=await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
    if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
    return response

def require_session(request:Request):
    token=request.cookies.get('ashare_session','')
    with state.db() as c:r=c.execute('SELECT expires FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not r or r['expires']<time.time():raise HTTPException(401,'请先登录')
    return True

class Login(BaseModel):username:str;password:str=Field(min_length=6,max_length=100)
@app.post('/api/login')
def login(body:Login,request:Request,response:Response):
    ip=request.client.host if request.client else 'local'
    with state.db() as c:
        row=c.execute('SELECT count,until FROM login_attempts WHERE ip=?',(ip,)).fetchone()
        if row and row['count']>=8 and row['until']>time.time():raise HTTPException(429,'尝试过多，请15分钟后再试')
    if body.username!='admin' or not state.authenticate(body.password):
        with state.db() as c:
            count=row['count']+1 if row and row['until']>time.time() else 1
            c.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(ip,count,time.time()+900))
        raise HTTPException(401,'账号或密码不正确')
    token=secrets.token_urlsafe(32)
    with state.db() as c:
        c.execute('DELETE FROM login_attempts WHERE ip=?',(ip,));c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),time.time()+86400))
    response.set_cookie('ashare_session',token,httponly=True,samesite='strict',max_age=86400)
    return {'username':'admin'}
@app.post('/api/logout',dependencies=[Depends(require_session)])
def logout(request:Request,response:Response):
    with state.db() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(request.cookies.get('ashare_session','').encode()).hexdigest(),))
    response.delete_cookie('ashare_session');return {'ok':True}
@app.get('/api/session',dependencies=[Depends(require_session)])
def session():return {'username':'admin'}
@app.get('/api/health')
def health():return {'status':'ok','service':'a-share-analysis'}

class Settings(BaseModel):
    model_base_url:str=Field(max_length=300)
    model_name:str=Field(min_length=1,max_length=100)
    api_key:str=Field(default='',max_length=500)
    gold_api_key:str=Field(default='',max_length=500)
    max_output_tokens:int=Field(default=6500,ge=1000,le=16000)
    max_calls:int=Field(default=3,ge=2,le=3)
    temperature:float=Field(default=0.2,ge=0,le=1)
    schedule_enabled:bool=False
    schedule_time:str=Field(default='15:20',pattern=r'^(1[5-9]|2[0-3]):[0-5][0-9]$')
    watch_codes:list[str]=Field(min_length=5,max_length=5)
    max_task_seconds:int=Field(default=1200,ge=300,le=3600)
    @field_validator('model_base_url')
    @classmethod
    def endpoint(cls,v):
        p=urlsplit(v)
        if p.username or p.password or p.query or p.fragment:raise ValueError('服务地址不能包含账号、密钥或查询参数')
        if p.scheme!='https' and not (p.scheme=='http' and p.hostname in {'localhost','127.0.0.1','host.docker.internal'}):raise ValueError('使用 HTTPS，或本机模型地址')
        if not p.hostname:raise ValueError('模型地址无效')
        return v.rstrip('/')
    @field_validator('watch_codes')
    @classmethod
    def codes(cls,v):
        if len(set(v))!=5 or any(not re.fullmatch(r'\d{6}',x) for x in v):raise ValueError('需要五个不重复的六位股票代码')
        return v
@app.get('/api/settings',dependencies=[Depends(require_session)])
def settings():return state.settings(public=True)
@app.put('/api/settings',dependencies=[Depends(require_session)])
def save_settings(body:Settings):state.update_settings(body.model_dump());return state.settings(public=True)

class TaskRequest(BaseModel):
    kind:str=Field(pattern='^(collect|research|supplement|gold|demo)$')
    date:str=Field(default_factory=state.today)
    mode:str=Field(default='close',pattern='^(close|instrument)$')
    asset_type:str=Field(default='stock',pattern='^(stock|index)$')
    code:str|None=Field(default=None,pattern=r'^\d{6}$')
    @field_validator('date')
    @classmethod
    def valid_date(cls,v):
        if date.fromisoformat(v).isoformat()!=v or v>state.today():raise ValueError('日期必须有效且不能是未来')
        return v
@app.post('/api/tasks',dependencies=[Depends(require_session)])
def start_task(body:TaskRequest):
    if (body.kind=='supplement' or (body.kind=='research' and body.mode=='instrument')) and not body.code:raise HTTPException(422,'请填写六位标的代码')
    if body.kind=='research' and not state.settings().get('api_key'):raise HTTPException(409,'尚未配置模型密钥；可先采集数据或运行离线演示')
    payload=body.model_dump(exclude={'kind'})
    return {'id':state.enqueue(body.kind,payload)}
@app.get('/api/tasks',dependencies=[Depends(require_session)])
def tasks():return state.tasks()
@app.post('/api/tasks/{task_id}/cancel',dependencies=[Depends(require_session)])
def cancel(task_id:str):
    t=state.task(task_id)
    if not t:raise HTTPException(404,'任务不存在')
    if t['status']=='queued':state.update(task_id,status='cancelled',stage='已取消')
    elif t['status']=='running':state.update(task_id,cancel_requested=1)
    else:raise HTTPException(409,'该任务已结束')
    return {'ok':True}
@app.post('/api/tasks/{task_id}/retry',dependencies=[Depends(require_session)])
def retry(task_id:str):
    t=state.task(task_id)
    if not t:raise HTTPException(404,'任务不存在')
    if t['status'] not in state.TERMINAL:raise HTTPException(409,'任务尚未结束')
    if t['kind']=='research' and (state.RUNTIME/'runs'/task_id/'context.json').exists():
        import shutil
        payload={**t['payload'],'retry_of':task_id}
        new_id=state.enqueue(t['kind'],payload)
        target=state.RUNTIME/'runs'/new_id;target.mkdir(exist_ok=True)
        # Queue workers only read retry_of; source evidence is immutable after completion.
        return {'id':new_id}
    return {'id':state.enqueue(t['kind'],t['payload'])}

@app.get('/api/overview',dependencies=[Depends(require_session)])
def overview():
    paths=sorted((state.RUNTIME/'snapshots').glob('*.json'));snap=state.read(paths[-1]) if paths else None
    heartbeat=state.RUNTIME/'worker-heartbeat';worker=heartbeat.exists() and time.time()-heartbeat.stat().st_mtime<15
    calendar=load_calendar(state.ROOT/'config/trading_calendar_2026.json')
    today=state.today();missing=[]
    if paths:
        from datetime import timedelta
        day=date.fromisoformat(paths[0].stem)
        while day<=date.fromisoformat(today) and day.year==calendar['year']:
            if is_trade_day(day,calendar) and not (state.RUNTIME/'snapshots'/f'{day}.json').exists():missing.append(str(day))
            day+=timedelta(days=1)
    return {'today':today,'snapshot_date':snap.get('trade_date') if snap else None,'snapshot_count':len(paths),'forecast_count':len(list((state.RUNTIME/'forecasts').glob('*.json'))),'indices':snap.get('indices',[]) if snap else [],'breadth':snap.get('breadth',{}) if snap else {},'quality_status':snap.get('quality_status') if snap else None,'worker_online':worker,'calendar_year':calendar['year'],'missing_dates':missing,'summary':state.read(state.RUNTIME/'evaluations/summary.json',{}),'model_configured':state.settings(public=True).get('api_key_configured',False),'dates':[p.stem for p in reversed(paths)]}
@app.get('/api/datasets',dependencies=[Depends(require_session)])
def datasets():
    paths=sorted((state.RUNTIME/'snapshots').glob('*.json'));snap=state.read(paths[-1]) if paths else None
    result=[]
    for key,label,provider in DATASETS:
        row={'key':key,'name':label,'provider':provider,'status':'missing','as_of':None,'count':0,'note':'尚未采集'}
        if key in {'indices','breadth','sectors'} and snap:
            value=snap.get(key,[])
            row.update(status='passed' if snap.get('quality_status')=='passed' else 'failed',as_of=snap['trade_date'],fetched_at=snap['generated_at'],count=len(value) if isinstance(value,list) else (sum(len(v) for v in value.values() if isinstance(v,list)) if key=='sectors' else value.get('counts',{}).get('total',0)),note='历史归档，不代表今日行情' if snap['trade_date']<state.today() else '正式快照',sources=snap.get('sources',{}))
        elif key in {'history','financials','news'}:
            parts=[state.read(p) for p in (state.RUNTIME/'supplements').glob('*-*.json')]
            available=[p for p in parts if p.get(key,{}).get('status')=='available']
            if parts:
                newest=max(parts,key=lambda p:p['fetched_at']);part=newest.get(key,{})
                row.update(status=part.get('status','missing'),as_of=newest['as_of'],count=len(available),note=part.get('reason') or f'{len(available)} 份已采集资料；部分数据不支持历史时点重放')
        elif key=='gold':
            gold=state.read(state.RUNTIME/'supplements/gold.json')
            if gold:
                count=sum(bool(gold.get(k,{}).get('records')) for k in ['xauusd','au9999']);row.update(status='available' if count==2 else 'partial',as_of=gold['as_of'],count=count,note='黄金本体独立研究，不纳入股票评分')
            else:row['note']='XAU/USD 需 GoldAPI 密钥；Au99.99 使用上金所数据'
        result.append(row)
    return result
@app.get('/api/datasets/{key}',dependencies=[Depends(require_session)])
def dataset_evidence(key:str):
    if key not in {r[0] for r in DATASETS}:raise HTTPException(404,'未知数据集')
    if key in {'indices','breadth','sectors'}:
        paths=sorted((state.RUNTIME/'snapshots').glob('*.json'))
        if not paths:raise HTTPException(404,'尚无正式快照')
        snap=state.read(paths[-1])
        return {'date':snap['trade_date'],'data':snap.get(key),'sources':snap.get('sources'),'quality_status':snap.get('quality_status'),'manifest':state.read(state.RUNTIME/'evidence'/f"{snap['trade_date']}.json",{'note':'历史导入，未补造原始响应'})}
    if key=='gold':return state.read(state.RUNTIME/'supplements/gold.json',{'status':'missing'})
    records=[state.read(p) for p in (state.RUNTIME/'supplements').glob('*-*.json')]
    return {'dataset':key,'records':[{'instrument_id':r['instrument_id'],'as_of':r['as_of'],'data':r.get(key)} for r in sorted(records,key=lambda r:r['fetched_at'],reverse=True)[:30]]}

@app.get('/api/snapshot/{day}',dependencies=[Depends(require_session)])
def snapshot(day:str):
    try:date.fromisoformat(day)
    except ValueError:raise HTTPException(422,'日期无效')
    path=state.RUNTIME/'snapshots'/f'{day}.json'
    if not path.exists():raise HTTPException(404,'该日期没有正式快照')
    snap=state.read(path);return {'snapshot':snap,'manifest':state.read(state.RUNTIME/'evidence'/f'{day}.json',{'note':'历史导入，未补造原始采集记录'})}

@app.get('/api/reports',dependencies=[Depends(require_session)])
def reports():
    result=[]
    for folder in ['reports','demo']:
        for p in (state.RUNTIME/folder).glob('*.json'):
            r=state.read(p);result.append({k:r.get(k) for k in ['id','date','mode','status','created_at','incomplete']}|{'title':r.get('output',{}).get('title','研究报告'),'summary':r.get('output',{}).get('summary','')[:180]})
    return sorted(result,key=lambda r:r.get('created_at') or '',reverse=True)
@app.get('/api/reports/{report_id}',dependencies=[Depends(require_session)])
def report(report_id:str):
    if not re.fullmatch(r'[a-f0-9]{24}',report_id):raise HTTPException(404,'报告不存在')
    for folder in ['reports','demo']:
        p=state.RUNTIME/folder/f'{report_id}.json'
        if p.exists():return state.read(p)
    raise HTTPException(404,'报告不存在')
@app.get('/api/reviews/{day}',dependencies=[Depends(require_session)])
def reviews(day:str):
    try:date.fromisoformat(day)
    except ValueError:raise HTTPException(422,'日期无效')
    from backend.execute import evaluation_rows,previous_forecasts
    prior,target=previous_forecasts(day)
    return {'records':evaluation_rows(day),'forecast':target,'gap':not bool(target),'note':'缺少面向该日的正式预测' if not target else '按已归档实际行情评价'}

frontend=state.ROOT/'frontend/dist'
if frontend.exists():
    app.mount('/assets',StaticFiles(directory=frontend/'assets'),name='assets')
    @app.get('/favicon.svg')
    def favicon():return FileResponse(frontend/'favicon.svg')
    @app.get('/{path:path}')
    def spa(path:str):
        if path.startswith('api/'):raise HTTPException(404,'接口不存在')
        return FileResponse(frontend/'index.html',headers={'Cache-Control':'no-cache'})
