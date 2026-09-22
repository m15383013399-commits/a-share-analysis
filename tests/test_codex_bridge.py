import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend import state, codex_bridge as bridge, codex_host as host
from backend.research import call_model
from market_diary.storage import atomic_write_json


class CodexBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(state, 'RUNTIME', Path(self.temp.name));self.patch.start()
        state.init();self.root = bridge.directory()
        self.task = state.enqueue('enginecheck', {})
        (state.RUNTIME/'runs'/self.task).mkdir()
        self.request_id = 'a'*32
        self.binary = Path(self.temp.name)/'fake-codex'
        self.binary.write_text(f'#!{sys.executable}\n'+'''import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
if 'SLOW_TEST' in prompt:time.sleep(60)
output=Path(sys.argv[sys.argv.index('--output-last-message')+1])
output.write_text(json.dumps({'result_json':json.dumps({'ok':True})}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
''')
        self.binary.chmod(0o700)
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def heartbeat(self):atomic_write_json(self.root/'heartbeat.json', {'ready':True,'message':'ready','version':'test'})
    def request(self, content='test'):
        value={'protocol':1,'task_id':self.task,'step':'connection','model':'','timeout':30,
               'messages':[{'role':'user','content':content}]}
        atomic_write_json(self.root/f'{self.request_id}.request.json',value)
        (self.root/f'{self.request_id}.lease').touch()
        return value
    def result(self):return state.read(self.root/f'{self.request_id}.result.json')
    def test_host_executes_fixed_command_and_returns_json_usage(self):
        self.request();host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat)
        self.assertEqual(self.result()['parsed'],{'ok':True})
        self.assertEqual(self.result()['usage']['output_tokens'],5)
        cmd=host.command('codex',Path('/tmp/work'),'model-name')
        self.assertIn('read-only',cmd);self.assertIn('--ignore-user-config',cmd)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',cmd)
        self.assertIn('project_doc_max_bytes=0',cmd);self.assertIn('shell_tool',cmd);self.assertIn('web_search="disabled"',cmd)
    def test_request_rejects_command_injection_model(self):
        v=self.request();v['model']='--dangerously-bypass-approvals-and-sandbox'
        with self.assertRaises(ValueError):bridge.validate_request(v)
        v=self.request();v['messages']=[{'role':'user','content':3}]
        with self.assertRaises(ValueError):bridge.validate_request(v)
    def test_stale_lease_prevents_start(self):
        self.request();lease=self.root/f'{self.request_id}.lease';os.utime(lease,(0,0))
        with patch('backend.codex_host.subprocess.Popen') as popen:
            host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat)
            popen.assert_not_called()
        self.assertEqual(self.result()['status'],'failed')
    def test_cancel_terminates_active_process(self):
        self.request('SLOW_TEST')
        timer=threading.Timer(.6,lambda:(self.root/f'{self.request_id}.cancel').touch());timer.start()
        start=time.monotonic()
        host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat)
        timer.join();self.assertLess(time.monotonic()-start,5)
        self.assertEqual(self.result()['status'],'failed')
        self.assertIn('取消',self.result()['error'])
    def test_lost_container_lease_terminates_active_process(self):
        self.request('SLOW_TEST')
        lease=self.root/f'{self.request_id}.lease'
        timer=threading.Timer(.6,lambda:os.utime(lease,(0,0)));timer.start()
        start=time.monotonic()
        host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat)
        timer.join();self.assertLess(time.monotonic()-start,5)
        self.assertIn('断开',self.result()['error'])
    def test_timeout_terminates_process_and_returns_failure(self):
        self.request('SLOW_TEST')
        with patch('backend.codex_host.time.monotonic',side_effect=[0,31]):
            host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat)
        self.assertEqual(self.result()['status'],'failed')
        self.assertIn('超时',self.result()['error'])
    def test_host_restart_never_replays_started_request(self):
        self.request();(self.root/f'{self.request_id}.started').touch()
        with patch('backend.codex_host.subprocess.Popen') as popen:
            host.run_request(self.root,self.request_id,str(self.binary),self.heartbeat);popen.assert_not_called()
        self.assertIn('中断',self.result()['error'])
    def test_client_to_host_roundtrip_and_cached_result(self):
        self.heartbeat();state.update_settings({'analysis_engine':'codex_cli'})
        failures=[]
        def runner():
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    requests=list(self.root.glob('*.request.json'))
                    if requests:
                        host.run_request(self.root,requests[0].name.split('.')[0],str(self.binary),self.heartbeat);return
                    time.sleep(.02)
            except Exception as exc:failures.append(exc)
        thread=threading.Thread(target=runner);thread.start()
        messages=[{'role':'user','content':'test'}]
        try:result=call_model(self.task,'connection',messages)
        finally:thread.join(timeout=5)
        self.assertFalse(failures);self.assertEqual(result,{'ok':True})
        with patch('backend.codex_bridge.call') as call:
            self.assertEqual(call_model(self.task,'connection',messages),result);call.assert_not_called()
        record=state.read(state.RUNTIME/'runs'/self.task/'connection.json')
        self.assertEqual(record['engine'],'codex_cli')
        self.assertNotIn('api_key',record)
    def test_offline_service_and_cancelled_client_do_not_hang(self):
        with self.assertRaisesRegex(ValueError,'未启动'):
            bridge.call(self.task,'connection',[{'role':'user','content':'test'}],state.settings())
        self.heartbeat();state.update(self.task,cancel_requested=1)
        with self.assertRaisesRegex(ValueError,'取消'):
            bridge.call(self.task,'connection',[{'role':'user','content':'test'}],state.settings())
        self.assertTrue(list(self.root.glob('*.cancel')))
    def test_child_environment_excludes_provider_credentials(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','MY_PRIVATE_TOKEN':'private'}):
            env=host.environment()
            self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('MY_PRIVATE_TOKEN',env)


if __name__=='__main__':unittest.main()
