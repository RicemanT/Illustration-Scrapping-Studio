import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from app.services import diagnostics as log
from app.services.request_diagnostics import install

class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.dict('os.environ', ARTIST_LOG_PATH=self.tmp.name)
        self.patch.start()
    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
    def test_redaction(self):
        payload = log.redact({'password': 'hunter-secret', 'message': 'Cookie: a=private; b=hidden\nhttps://user:pass@site/a?api_key=secret', 'nested': {'api_key': 'private'}})
        data = json.dumps(payload)
        for secret in ['hunter-secret', 'private', 'hidden', 'user:pass', 'api_key=secret']:
            self.assertNotIn(secret, data)
    def test_rotation_and_error_retention(self):
        with patch.object(log, 'MAX_BYTES', 800), patch.object(log, 'BACKUPS', 2):
            log.emit('failure', 'keep error', 'ERROR', error=OSError(errno.ENOSPC, 'full'))
            for i in range(40): log.emit('read', str(i), 'DEBUG')
            self.assertEqual(log.read_events(level='ERROR')['items'][0]['diagnostic']['code'], 'storage_full')
            self.assertLessEqual(len(list(Path(self.tmp.name).glob('events*'))), 3)
    def test_disk_failure_nonfatal(self):
        with patch.object(log, '_append', side_effect=PermissionError('denied')):
            self.assertEqual(log.emit('test', 'still returns')['event'], 'test')
        self.assertFalse(log.status()['writable'])
    def test_pagination_and_filter(self):
        for i in range(5): log.emit('test', str(i), job_id='job')
        page = log.read_events(job_id='job', limit=2)
        older = log.read_events(job_id='job', limit=2, before=page['next_cursor'])
        self.assertEqual([r['message'] for r in page['items'] + older['items']], ['4','3','2','1'])
    def test_classification(self):
        self.assertEqual(log.diagnose(TimeoutError('slow'))['code'], 'timeout')
        self.assertEqual(log.diagnose(ValueError('unknown'))['certainty'], 'unknown')
    def test_resume_lifecycle(self):
        job = {'job_id':'resume', 'status':'running'}
        for state in ['running','running','queued','running','completed']:
            job['status'] = state; log.job_event(job)
        self.assertEqual(len(log.read_events(job_id='resume')['items']),4)

class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_envelope_and_validation_do_not_leak_input(self):
        app = FastAPI()
        class Body(BaseModel):
            count: int
        @app.post('/api/test')
        async def action(body: Body):
            raise PermissionError('denied')
        install(app)
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', ARTIST_LOG_PATH=tmp):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                invalid = await client.post('/api/test', json={'count':'sensitive-invalid-value'})
                self.assertEqual(invalid.status_code,422)
                self.assertNotIn('sensitive-invalid-value', invalid.text)
                failed = await client.post('/api/test', json={'count':1})
                self.assertEqual(failed.status_code,500)
                self.assertEqual(failed.json()['diagnostic']['code'],'storage_permission')
                request_id = failed.headers['x-request-id']
                self.assertEqual(failed.json()['request_id'],request_id)
                self.assertEqual(len(log.read_events(request_id=request_id)['items']),3)
            self.assertNotIn('sensitive-invalid-value', (Path(tmp)/'events.jsonl').read_text())
