import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import state
from backend.api import app
from backend.contracts import validate_new_forecast
from backend.execute import execute,collect
from backend.research import generate
from backend.evidence import clean_url

class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runtime=patch.object(state,'RUNTIME',Path(self.tmp.name));self.runtime.start()
        state.init()
        self.client=TestClient(app);self.client.__enter__()
        self.password=(state.RUNTIME/'admin-credentials.txt').read_text().split('密码：')[1].strip()
    def tearDown(self):
        self.client.__exit__(None,None,None);self.runtime.stop();self.tmp.cleanup()
    def login(self):
        r=self.client.post('/api/login',json={'username':'admin','password':self.password})
        self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie'])
    def test_auth_csrf_and_secret_redaction(self):
        self.assertEqual(self.client.get('/api/settings').status_code,401)
        self.login();state.update_settings({'api_key':'test-secret'})
        r=self.client.get('/api/settings');self.assertNotIn('test-secret',r.text)
        self.assertTrue(r.json()['api_key_configured'])
        self.assertEqual(self.client.post('/api/tasks',json={'kind':'demo'},headers={'origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/health',headers={'host':'evil.example'}).status_code,400)
    def test_no_key_blocks_analysis_but_allows_demo(self):
        self.login()
        self.assertEqual(self.client.post('/api/tasks',json={'kind':'research'}).status_code,409)
        task=self.client.post('/api/tasks',json={'kind':'demo'}).json()['id']
        execute(task)
        self.assertEqual(state.task(task)['status'],'succeeded')
        self.assertEqual(list((state.RUNTIME/'forecasts').glob('*')),[])
        self.assertEqual(len(self.client.get('/api/reports').json()),1)
    def test_codex_requires_ready_host_but_not_platform_api_key(self):
        self.login();state.update_settings({'analysis_engine':'codex_cli'})
        with patch('backend.codex_bridge.status',return_value={'online':False,'ready':False,'message':'本机服务离线'}):
            self.assertEqual(self.client.post('/api/tasks',json={'kind':'research'}).status_code,409)
        with patch('backend.codex_bridge.status',return_value={'online':True,'ready':True,'message':'ready'}):
            response=self.client.post('/api/tasks',json={'kind':'enginecheck'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(state.task(response.json()['id'])['kind'],'enginecheck')
        self.assertFalse(state.settings(public=True)['api_key_configured'])

    def test_queue_deduplicates_and_cancelled_is_not_claimed(self):
        self.login()
        one=self.client.post('/api/tasks',json={'kind':'demo'}).json()['id']
        two=self.client.post('/api/tasks',json={'kind':'demo'}).json()['id'];self.assertEqual(one,two)
        self.assertEqual(self.client.post(f'/api/tasks/{one}/cancel',json={}).status_code,200)
        self.assertIsNone(state.claim())
        new=self.client.post(f'/api/tasks/{one}/retry',json={}).json()['id'];self.assertNotEqual(new,one)
    def test_past_missing_snapshot_never_calls_live_provider(self):
        task=state.enqueue('collect',{'date':'2026-01-05'})
        (state.RUNTIME/'runs'/task).mkdir()
        with patch('backend.execute.cli') as cli,patch.object(state,'today',return_value='2026-09-21'):
            with self.assertRaisesRegex(ValueError,'历史日期'):collect(task,'2026-01-05')
            cli.assert_not_called()
    def test_invalid_forecast_contract(self):
        f=json.loads((state.ROOT/'tests/fixtures/forecast_valid.json').read_text())
        f['schema_version']='2.0'
        for g in ['indices','sectors','watch_pool']:
            for r in f[g]:r.update(lower=.1,upper=.8,midpoint=.45,probabilities={'up':50,'flat':30,'down':20},invalidation='证据发生变化',week_view='震荡偏强')
        self.assertEqual(len(validate_new_forecast(f)['watch_pool']),5)
        bad=copy.deepcopy(f);bad['indices'][0]['lower']=-.1
        with self.assertRaises(ValueError):validate_new_forecast(bad)
        bad=copy.deepcopy(f);bad['next_trade_date']='2026-08-13'
        with self.assertRaises(ValueError):validate_new_forecast(bad)
        bad=copy.deepcopy(f);bad['watch_pool'][0]['status']='inactive'
        with self.assertRaises(ValueError):validate_new_forecast(bad)
    def test_model_repair_is_never_silently_published(self):
        task=state.enqueue('research',{});(state.RUNTIME/'runs'/task).mkdir()
        context={'evidence':{'instrument':{}},'as_of':'2026-09-21'}
        draft={'title':'test','summary':'test','sections':[{'heading':'事实','text':'缺资料','evidence_ids':['instrument']}],'risks':['缺少资料'],'forecast':None}
        with patch('backend.research.call_model',side_effect=[copy.deepcopy(draft),{'passed':False,'issues':['需解释缺失']},copy.deepcopy(draft)]):
            out=generate(task,context,False);self.assertEqual(out['review_status'],'revised_needs_review')
        with patch('backend.research.call_model',side_effect=[copy.deepcopy(draft),{'passed':True,'issues':[]}]):
            out=generate(task,context,False);self.assertEqual(out['review_status'],'passed')
    def test_formal_pipeline_registers_once_and_preserves_first_forecast(self):
        from backend.execute import research
        from test_cli import valid_snapshot
        f=json.loads((state.ROOT/'tests/fixtures/forecast_valid.json').read_text())
        f['schema_version']='2.0'
        for g in ['indices','sectors','watch_pool']:
            for r in f[g]:r.update(lower=.1,upper=.8,midpoint=.45,probabilities={'up':50,'flat':30,'down':20},invalidation='证据改变',week_view='震荡偏强')
        snap=valid_snapshot(f['trade_date']);snap['sectors']['industries']=[{'code':r['code'],'name':r['name']} for r in f['sectors']]
        context={'as_of':f['trade_date'],'snapshot_hash':state.digest(snap),'source_urls':f['sources'],'previous_forecast':None,'ledger_note':'合成测试基线','limitations':[], 'evidence':{'snapshot':snap}}
        draft={'title':'合成测试','summary':'合成证据','sections':[{'heading':'事实','text':'合成测试','evidence_ids':['snapshot']}],'risks':['仅测试'],'forecast':f}
        state.update_settings({'watch_codes':[r['code'] for r in f['watch_pool']]})
        source=state.enqueue('research',{});folder=state.RUNTIME/'runs'/source;folder.mkdir();(folder/'context.json').write_text(json.dumps(context))
        task=state.enqueue('research',{'retry_of':source});(state.RUNTIME/'runs'/task).mkdir()
        with patch.object(state,'today',return_value=f['trade_date']),patch('backend.research.call_model',side_effect=[copy.deepcopy(draft),{'passed':True,'issues':[]}]):
            research(task,{'mode':'close','date':f['trade_date'],'retry_of':source})
        path=state.RUNTIME/'forecasts'/f"{f['trade_date']}.json"
        self.assertTrue(path.exists());original=path.read_bytes()
        self.assertEqual(state.task(task)['result']['publication'],'正式预测已入账')
        f['indices'][0].update(lower=.2,upper=.9,midpoint=.55)
        second=state.enqueue('research',{'retry_of':source,'version':2});(state.RUNTIME/'runs'/second).mkdir()
        with patch.object(state,'today',return_value=f['trade_date']),patch('backend.research.call_model',side_effect=[copy.deepcopy(draft),{'passed':True,'issues':[]}]):
            research(second,{'mode':'close','date':f['trade_date'],'retry_of':source})
        self.assertEqual(original,path.read_bytes())
        self.assertIn('草稿',state.task(second)['result']['publication'])

    def test_source_urls_redact_credentials(self):
        url=clean_url('https://user:pass@host.test/path?token=secret&symbol=600000')
        self.assertNotIn('secret',url);self.assertNotIn('user',url);self.assertIn('symbol=600000',url)

if __name__=='__main__':unittest.main()
