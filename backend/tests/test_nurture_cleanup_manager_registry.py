"""The live application wires cleanup reconciliation to cross-manager registries."""
from pathlib import Path
from unittest.mock import patch
import unittest
from app.config import Settings
from app.main import create_app
import test_studio as fixtures

class CleanupManagerRegistryTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown

    def app(self):
        return create_app(Settings(startup_token='offline-registry-fixture-token-12345',
            database_path=self.db.path,data_dir=Path(self.tmp.name)),database=self.db,bitbrowser=self.browser)

    def test_terminal_collection_still_draining_is_protected_until_registry_retires(self):
        app=self.app();service=app.state.service
        task=service.create_task(self.owner,name='offline',modes=['followers'],targets=['fixture'],settings={},window_ids=['w1'])
        with self.db.write() as c:c.execute("UPDATE tasks SET status='completed' WHERE id=?",(task['id'],))
        check=app.state.studio.cleanup_profile_active
        with patch.object(app.state.execution_manager,'active_task_ids',return_value={task['id']}):
            self.assertTrue(check('w1'));self.assertFalse(check('other'))
        self.assertFalse(check('w1'))

    def test_removed_posting_manager_is_not_constructed(self):
        app=self.app()
        self.assertFalse(hasattr(app.state,'posting'))
        self.assertFalse(app.state.studio.cleanup_profile_active('unrelated'))
