"""Internal credential activation is private, bounded and never restarts work."""
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import unittest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from test_core import FakeBitBrowserClient

class InternalPexelsRouteR6Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{},clear=False);self.env.start();self.addCleanup(self.env.stop)
        os.environ.pop('IGAC_PEXELS_API_KEY',None)
        path=Path(self.temp.name)
        self.settings=Settings(startup_token='fixture-startup-token-not-a-real-key-12345',database_path=path/'db.sqlite3',data_dir=path)
        self.app=create_app(self.settings,bitbrowser=FakeBitBrowserClient())
        self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'X-Startup-Token':self.settings.startup_token}
        self.secret='fixture-pexels-key-for-offline-test-only'
        self.url='/api/internal/integrations/pexels'

    def test_startup_key_consumed_into_private_supplier_not_child_environment(self):
        path=Path(self.temp.name)/'startup-ingress'
        with patch.dict(os.environ,{'IGAC_STARTUP_TOKEN':self.settings.startup_token,
                'IGAC_DATA_DIR':str(path),'IGAC_DB_PATH':str(path/'db.sqlite3'),
                'IGAC_PEXELS_API_KEY':self.secret}):
            settings=Settings.from_env()
            self.assertEqual(self.secret,settings.pexels_api_key)
            self.assertNotIn(self.secret,repr(settings))
            app=create_app(settings,bitbrowser=FakeBitBrowserClient())
            self.assertEqual(self.secret,app.state.posting.provider.key_supplier())
            self.assertNotIn('IGAC_PEXELS_API_KEY',os.environ,
                             'browser/driver subprocesses must not inherit the startup credential')

    def test_startup_auth_required_and_errors_never_echo_input(self):
        for headers in ({},{'X-Startup-Token':'wrong'}):
            r=self.client.post(self.url,headers=headers,json={'pexels_api_key':self.secret})
            self.assertEqual(401,r.status_code);self.assertNotIn(self.secret,r.text)
        invalid=[{'pexels_api_key':self.secret,'extra':True},{'pexels_api_key':[self.secret]}, {'pexels_api_key':self.secret+'\nheader'}, {}]
        for body in invalid:
            r=self.client.post(self.url,headers=self.headers,json=body)
            self.assertGreaterEqual(r.status_code,400,r.text);self.assertNotIn(self.secret,r.text)
        for raw in (b'{invalid', b'{"pexels_api_key":"'+self.secret.encode()+b'"'+b'x'*9000):
            r=self.client.post(self.url,headers=self.headers,content=raw)
            self.assertGreaterEqual(r.status_code,400);self.assertNotIn(self.secret,r.text)
        self.assertNotIn('IGAC_PEXELS_API_KEY',os.environ)

    def test_update_clear_keeps_leases_tasks_database_and_process_unchanged(self):
        service=self.app.state.service
        owner=service.register_user('credential_test','fixture password only')['id']
        token=service._acquire_browser_lease_record(owner,'window-a',operation_type='studio',entity_id='active-studio')
        shutdown=Mock();self.app.state.request_shutdown=shutdown
        manager=self.app.state.execution_manager
        with service.database.read() as c:before=[tuple(r) for r in c.execute('SELECT * FROM browser_operation_leases')]
        r=self.client.post(self.url,headers=self.headers,json={'pexels_api_key':self.secret})
        self.assertEqual(200,r.status_code,r.text);self.assertEqual({'configured':True,'activated':True},r.json())
        self.assertNotIn(self.secret,r.text);self.assertNotIn('IGAC_PEXELS_API_KEY',os.environ);self.assertEqual(self.secret,self.app.state.posting.provider.key_supplier())
        self.assertIs(manager,self.app.state.execution_manager);shutdown.assert_not_called()
        with service.database.read() as c:
            self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM browser_operation_leases')])
            dump='\n'.join(c.iterdump())
        self.assertNotIn(self.secret,dump)
        r=self.client.post(self.url,headers=self.headers,json={'pexels_api_key':''})
        self.assertEqual({'configured':False,'activated':True},r.json());self.assertNotIn('IGAC_PEXELS_API_KEY',os.environ)
        self.assertIs(manager,self.app.state.execution_manager);shutdown.assert_not_called()
        service.release_browser_lease('window-a',token)
