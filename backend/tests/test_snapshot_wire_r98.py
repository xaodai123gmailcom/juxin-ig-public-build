"""Opt-in workbench wire projection removes repeated data, never business rows."""
import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import CoreService
from app.snapshot_runtime import compact_workbench_snapshot, WORKBENCH_ROW_ALIASES


class Request:
    async def is_disconnected(self):
        return False


class Inventory:
    def list_all_windows(self):
        return {'windows': [], 'connection': {'connected': True, 'state': 'ready'}}


class SnapshotWireR98Tests(unittest.IsolatedAsyncioTestCase):
    def test_projection_is_shallow_and_only_omits_exact_row_aliases(self):
        rows = [{'id': 'kept', 'profile': {'bio': 'unaltered'}}]
        full = {key: rows for key in WORKBENCH_ROW_ALIASES}
        full.update(pending={'public': rows, 'private': rows},
                    approved={'public': rows, 'private': rows},
                    history={'approvals': rows, 'manual_rejections': rows,
                             'collection_exclusions': rows},
                    action_success_history=rows, has_more={'approval_history': True},
                    counts={'approved': 4001}, windows=[{'locked': True}], revision=19)
        compact = compact_workbench_snapshot(full)
        self.assertEqual(set(full) - WORKBENCH_ROW_ALIASES, set(compact))
        for key in compact:
            self.assertIs(full[key], compact[key])
        self.assertEqual(7, len(WORKBENCH_ROW_ALIASES & set(full)))
        self.assertNotIn('approval_history', compact)
        self.assertTrue(compact['has_more']['approval_history'])

    async def test_endpoint_default_compatible_and_opt_in_reads_latest_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'core.sqlite3'); db.initialize()
            service = CoreService(db)
            owner = service.register_user('wire-fixture', 'synthetic-wire-password')
            settings = Settings(startup_token='synthetic-wire-startup-token',
                                database_path=db.path, data_dir=Path(tmp))
            app = create_app(settings, database=db, bitbrowser=Inventory())
            endpoint = next(r.endpoint for r in app.routes
                            if getattr(r, 'path', '') == '/api/workbench/snapshot')
            async def read(compact=None):
                return json.loads((await endpoint(Request(), (owner, 'session'),
                    limit=10, history_limit=10, platform='instagram', compact=compact)).body)
            legacy = await read()
            self.assertTrue(WORKBENCH_ROW_ALIASES <= set(legacy))
            current = await read('1')
            self.assertFalse(WORKBENCH_ROW_ALIASES & set(current))
            for field in ('pending','approved','history','counts','has_more','windows'):
                self.assertEqual(legacy[field], current[field])
            task = service.create_task(owner['id'], name='Newest', modes=['followers'],
                targets=['latest_source'], settings={})
            updated = await read('1')
            self.assertIn(task['id'], [row['id'] for row in updated['tasks']])
            self.assertGreater(updated['revision'], current['revision'])
            await app.state.snapshot_inventory.close()
