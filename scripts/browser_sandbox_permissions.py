"""Source-tree adapter; standalone tools package the shared module here."""
from pathlib import Path
import importlib.util

# Do not import the app package before an external repair selects its project.
# Otherwise app.__path__ could bind all later imports to the tool's source tree.
_path = Path(__file__).resolve().parents[1] / 'backend/app/browser_permissions.py'
_spec = importlib.util.spec_from_file_location('juxin_sandbox_permissions', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
BrowserPermissionError = _module.BrowserPermissionError
ensure_browser_sandbox_access = _module.ensure_browser_sandbox_access

__all__ = ['BrowserPermissionError', 'ensure_browser_sandbox_access']
