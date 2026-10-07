"""Authenticated server telemetry projection; no AWS, brokers, or actual commands."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from portal.access import RemoteSettings, password_record
from portal.controller import create_controller
from portal.runtime import Reply
from portal.server_status import HostMetrics, ServerStatus


class FakeRuntime:
    def __init__(self):
        self.running=False
        self.closing=False
        self.health_calls=0
        self.requests=[]
        self.state={'active':None,'automation':{'enabled':True,'status':'waiting','api_key':'PRIVATE-KEY'},
                    'broker':{'name':'Replaceable paper broker','account_id':'PRIVATE-ACCOUNT'},
                    'account':{'status':'connected','checked_at':'2026-10-03T04:00:00+00:00',
                               'details':{'cash':'PRIVATE-CASH','secret':'PRIVATE-KEY'}},
                    'jobs':[{'command':'PRIVATE-COMMAND'}]}
        self.reply_status=200
    def status(self):
        self.health_calls+=1
        return {'status':'running' if self.running else 'stopped','alive':self.running,'pid':42 if self.running else None,
                'started_at':None,'checked_at':'2026-10-03T04:00:00+00:00','uptime_seconds':None,
                'token':'PRIVATE-TOKEN','command':'PRIVATE-COMMAND'}
    def _request(self,method,path,**kwargs):
        self.requests.append((method,path,kwargs))
        return Reply(self.reply_status,json.dumps(self.state).encode(),{})
    def start(self):self.running=True;return {'status':'starting'}
    def stop(self):self.running=False;return {'status':'stopping'}
    def forward(self,*args,**kwargs):raise AssertionError('Server status must not use the long-timeout proxy')


class MetricsTests(unittest.TestCase):
    def test_linux_cpu_delta_memory_disk_and_uptime_are_real_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'stat').write_text('cpu  100 0 100 800 0 0 0 0 0 0\n',encoding='ascii')
            metrics=HostMetrics(root,proc=root)
            (root/'stat').write_text('cpu  150 0 150 900 0 0 0 0 0 0\n',encoding='ascii')
            (root/'meminfo').write_text('MemTotal: 20000 kB\nMemAvailable: 5000 kB\n',encoding='ascii')
            (root/'uptime').write_text('12345.9 45678.0\n',encoding='ascii')
            with patch('portal.server_status.shutil.disk_usage',return_value=SimpleNamespace(total=100,used=40)),\
                    patch('portal.server_status.os.cpu_count',return_value=2):
                value=metrics.sample()
            self.assertEqual(value['cpu_percent'],50.0)
            self.assertEqual(value['cpu_count'],2)
            self.assertEqual(value['memory_total_bytes'],20000*1024)
            self.assertEqual(value['memory_used_bytes'],15000*1024)
            self.assertEqual(value['disk_total_bytes'],100)
            self.assertEqual(value['disk_used_bytes'],40)
            self.assertEqual(value['uptime_seconds'],12345)
            self.assertIsNotNone(datetime.fromisoformat(value['sampled_at']).tzinfo)

    def test_missing_metrics_and_first_cpu_sample_are_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as folder,patch('portal.server_status.os.cpu_count',return_value=None),\
                patch('portal.server_status.shutil.disk_usage',side_effect=OSError('unavailable')):
            metrics=HostMetrics(folder,proc=Path(folder))
            value=metrics.sample()
            for key in ('cpu_count','cpu_percent','memory_total_bytes','memory_used_bytes',
                        'disk_total_bytes','disk_used_bytes','uptime_seconds'):
                self.assertIsNone(value[key],key)
            (Path(folder)/'stat').write_text('cpu 1 2 3 4 5 6 7 8\n',encoding='ascii')
            self.assertIsNone(metrics.sample()['cpu_percent'])


class ServerStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password='Server panel test password!'
        cls.access=RemoteSettings('https://bot.example',password_record(cls.password))
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.runtime=FakeRuntime()
        self.app=create_controller(self.root,port=5329,instance='test',runtime=self.runtime,access=self.access)
        self.client=TestClient(self.app,base_url=self.access.origin)
        self.addCleanup(self.client.close)
        self.token=self.client.get('/api/auth/status').json()['login_token']
        self.client.headers.update({'Origin':self.access.origin,'X-Portal-Token':self.token})
    def login(self):
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'owner','password':self.password}).status_code,200)
    def metadata(self,raw):
        path=self.root/'secrets/portal/deployment.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(raw),encoding='utf-8');path.chmod(0o600)
    def status(self):return self.client.get('/api/server/status')

    def test_unauthenticated_and_missing_token_cannot_read_server_status(self):
        self.assertEqual(self.status().status_code,401)
        self.assertEqual(self.runtime.health_calls,0)
        self.login()
        self.assertEqual(self.client.get('/api/server/status',headers={'X-Portal-Token':''}).status_code,403)
        self.assertEqual(self.runtime.health_calls,0)

    def test_server_page_remains_available_with_backend_stopped(self):
        self.login()
        result=self.status()
        self.assertEqual(result.status_code,200)
        value=result.json()
        self.assertEqual(value['portal']['status'],'running')
        self.assertEqual(value['runtime']['status'],'stopped')
        self.assertFalse(value['backend']['reachable'])
        self.assertIsNone(value['backend']['automation_enabled'])
        self.assertTrue(next(x for x in value['operations'] if x['id']=='start')['enabled'])
        self.assertFalse(next(x for x in value['operations'] if x['id']=='stop')['enabled'])
        self.assertEqual(self.runtime.requests,[])
        self.assertEqual(value['links'],{'console':None,'terminal':None})

    def test_diagnostics_require_login_and_token_and_do_not_start_backend(self):
        self.assertEqual(self.client.get('/api/server/diagnostics').status_code,401)
        self.login()
        self.assertEqual(self.client.get('/api/server/diagnostics',headers={'X-Portal-Token':''}).status_code,403)
        reply=self.client.get('/api/server/diagnostics')
        self.assertEqual(reply.status_code,200)
        self.assertTrue(reply.json()['read_only'])
        self.assertFalse(reply.json()['broker_queries'])
        self.assertIn('attachment',reply.headers['content-disposition'])
        self.assertFalse(self.runtime.running)
        self.assertEqual(self.runtime.requests,[])

    def test_running_backend_is_projected_bounded_cached_and_never_leaks_secrets(self):
        self.login();self.runtime.running=True
        result=self.status();value=result.json()
        self.assertTrue(value['backend']['reachable'])
        self.assertEqual(value['backend']['broker_name'],'Replaceable paper broker')
        self.assertTrue(value['backend']['automation_enabled'])
        self.assertEqual(value['backend']['automation_status'],'waiting')
        self.assertTrue(next(x for x in value['operations'] if x['id']=='stop')['enabled'])
        self.assertEqual(self.runtime.requests,[('GET','/api/state',{'timeout':2})])
        self.assertEqual(self.status().json()['checked_at'],value['checked_at'])
        self.assertEqual(self.runtime.health_calls,1)
        self.assertEqual(len(self.runtime.requests),1)
        for secret in ('PRIVATE-KEY','PRIVATE-ACCOUNT','PRIVATE-CASH','PRIVATE-COMMAND','PRIVATE-TOKEN',self.password):
            self.assertNotIn(secret,result.text)

    def test_unknown_or_busy_backend_cannot_advertise_stop_as_safe(self):
        self.runtime.running=True
        for state in ({}, {'active':'unknown'}, {'active':{}}, {'active':{'id':'job'}}):
            self.runtime.state=state
            report=ServerStatus(self.root,self.runtime).status()
            self.assertFalse(next(x for x in report['operations'] if x['id']=='stop')['enabled'])
        self.runtime.reply_status=500
        report=ServerStatus(self.root,self.runtime).status()
        self.assertFalse(report['backend']['reachable'])

    def test_private_metadata_links_are_validated_and_extra_secret_fields_not_projected(self):
        self.login()
        terminal='https://123-example.ap-southeast-2.console.aws.amazon.com/lightsail/remote/ap-southeast-2/instances/trading-bot/terminal?protocol=ssh'
        self.metadata({'provider':'aws_lightsail','region':'ap-southeast-2','instance_name':'trading-bot','terminal_url':terminal})
        value=self.status().json()
        self.assertEqual(value['links'],{'console':'https://lightsail.aws.amazon.com/','terminal':terminal})
        self.assertNotIn('terminal_url',value['deployment'])
        self.metadata({'provider':'aws_lightsail','region':'ap-southeast-2','instance_name':'trading-bot','api_key':'PRIVATE-KEY'})
        self.app.state.server_status.invalidate()
        result=self.status()
        self.assertIsNone(result.json()['deployment']['provider'])
        self.assertIsNone(result.json()['links']['terminal'])
        self.assertNotIn('PRIVATE-KEY',result.text)

    def test_existing_start_stop_routes_keep_csrf_and_invalidate_status_cache(self):
        self.login();self.status()
        for path in ('start','stop'):
            self.assertEqual(self.client.post('/api/runtime/'+path,json={},headers={'X-Portal-Token':''}).status_code,403)
        self.assertEqual(self.client.post('/api/runtime/start',json={}).status_code,200)
        self.assertEqual(self.status().json()['runtime']['status'],'running')
        self.assertEqual(self.client.post('/api/runtime/stop',json={}).status_code,200)
        self.assertEqual(self.status().json()['runtime']['status'],'stopped')
        for path in ('/api/server/shell','/api/server/reboot','/api/server/status/extra'):
            self.assertEqual(self.client.post(path,json={'command':'whoami'}).status_code,404)

    def test_desktop_server_endpoint_is_404_and_never_proxied(self):
        runtime=FakeRuntime()
        client=TestClient(create_controller(self.root,port=8765,instance='desktop',runtime=runtime),
                          base_url='http://127.0.0.1:8765')
        with client:
            token=client.get('/api/session').json()['token']
            client.headers['X-Portal-Token']=token
            self.assertEqual(client.get('/api/server/status').status_code,404)
        self.assertEqual(runtime.health_calls,0)
        self.assertEqual(runtime.requests,[])

    def test_manifest_version_is_a_hash_not_untrusted_manifest_text(self):
        (self.root/'release-manifest.json').write_text('{"created_at":"PRIVATE-KEY"}',encoding='utf-8')
        report=ServerStatus(self.root,self.runtime).status()
        self.assertRegex(report['portal']['version'],r'^build-[a-f0-9]{12}$')
        self.assertNotIn('PRIVATE-KEY',json.dumps(report))
