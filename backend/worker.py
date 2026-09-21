import os,signal,subprocess,sys,time
from datetime import datetime
from backend import state
from market_diary.calendar import is_trade_day,load_calendar

def schedule():
    s=state.settings();now=datetime.now(state.TZ)
    if not s['schedule_enabled'] or now.strftime('%H:%M')<s['schedule_time']:return
    if not is_trade_day(now.date(),load_calendar(state.ROOT/'config/trading_calendar_2026.json')):return
    day=now.date().isoformat()
    with state.db() as c:
        if c.execute('SELECT 1 FROM schedule_runs WHERE day=?',(day,)).fetchone():return
    task_id=state.enqueue('research',{'date':day,'mode':'close','asset_type':'stock','code':None})
    with state.db() as c:c.execute('INSERT OR IGNORE INTO schedule_runs VALUES(?,?)',(day,task_id))

def main():
    state.init()
    with state.lock('worker',nonblocking=True):
        with state.db() as c:c.execute("UPDATE tasks SET status='interrupted',error='执行进程曾中断；可重试，正式台账保持不变' WHERE status='running'")
        while True:
            (state.RUNTIME/'worker-heartbeat').write_text(state.now())
            try:schedule()
            except ValueError:pass # Calendar coverage cannot be guessed.
            t=state.claim()
            if not t:time.sleep(2);continue
            run=state.RUNTIME/'runs'/t['id'];run.mkdir(exist_ok=True)
            with (run/'worker.log').open('a') as log:
                p=subprocess.Popen([sys.executable,'-m','backend.execute',t['id']],cwd=state.ROOT,stdout=log,stderr=log,start_new_session=True)
                start=time.monotonic()
                try:
                    while p.poll() is None:
                        (state.RUNTIME/'worker-heartbeat').write_text(state.now())
                        current=state.task(t['id'])
                        if current['cancel_requested'] or time.monotonic()-start>state.settings()['max_task_seconds']:
                            os.killpg(p.pid,signal.SIGTERM)
                            try:p.wait(timeout=5)
                            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
                            # Committed forecasts stay committed even when a later step was cancelled.
                            state.update(t['id'],status='cancelled' if current['cancel_requested'] else 'failed',stage='任务已停止',error='用户取消' if current['cancel_requested'] else '任务达到总超时；已保存证据，正式台账不回滚')
                            break
                        time.sleep(1)
                except BaseException:
                    if p.poll() is None:os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=5)
                    state.update(t['id'],status='interrupted',error='worker 退出，任务已中断');raise
                if state.task(t['id'])['status']=='running':state.update(t['id'],status='failed',error='任务进程异常退出，请重试')
if __name__=='__main__':main()
