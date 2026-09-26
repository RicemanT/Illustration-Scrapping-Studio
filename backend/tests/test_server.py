import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import app.db as db
from app.services import server
from app.routes.settings import StorageSettings, update_storage
from app.services.diagnostics import diagnose

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = db.DB_PATH
        db.DB_PATH = self.root / 'index.db'
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old
        self.temp.cleanup()

    def test_reserve_blocks_and_can_be_changed(self):
        with patch.object(server.shutil, 'disk_usage', return_value=SimpleNamespace(free=4*1024**3)):
            with self.assertRaises(server.StorageReserveError) as error:
                server.require_space(self.root)
            self.assertEqual(diagnose(error.exception)['code'], 'storage_reserve')
            update_storage(StorageSettings(reserve_gib=3))
            server.require_space(self.root)
            self.assertEqual(server.reserve_gib(),3)

    def test_capabilities_use_measured_capacity(self):
        with patch.object(server, 'LIBRARY_PATH', self.root):
            data=server.capabilities()
        self.assertGreater(data['disk_total_bytes'],0)
        self.assertGreaterEqual(data['disk_free_bytes'],0)
        self.assertEqual(data['library_path'],str(self.root))
        self.assertNotIn('training',data)

    def test_invalid_reserves_rejected(self):
        for value in (-1, float('inf'), float('nan')):
            with self.assertRaises(ValueError): StorageSettings(reserve_gib=value)
