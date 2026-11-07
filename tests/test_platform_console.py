from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
from app.max_settings import MaxSettings


class PlatformConsoleTests(unittest.TestCase):
    def test_full_platform_console_stays_available_when_max_is_disabled(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-console-off-')as folder:
            root=Path(folder);root.chmod(0o700)
            settings=MaxSettings(False,'test_bot',root/'never-read-token',None,root/'max.sqlite3',platform_enabled=True)
            with TestClient(create_app(max_settings=settings),base_url='http://127.0.0.1:5211')as web:
                self.assertEqual(web.get('/api/demo/health').status_code,200)
                self.assertEqual(len(web.get('/api/demo/bootstrap').json()['actors']),9)
                self.assertEqual(web.get('/api/demo/state?actor_id=710007').status_code,200)
                self.assertFalse(web.get('/api/max/status').json()['workerAlive'])
                headers={'Origin':'http://127.0.0.1:5211'}
                self.assertEqual(web.post('/api/max/invite',json={'actor_id':710007},headers=headers).status_code,409)
                self.assertFalse((root/'never-read-token').exists())
