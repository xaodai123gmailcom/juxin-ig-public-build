"""Visible desktop copies, separate from the app's durable media originals."""
from __future__ import annotations

import filecmp
import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from .errors import ValidationError


def safe_name(value, limit=36):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:limit].rstrip(' .')
    if not value or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def desktop_root():
    # Electron resolves redirected desktops (including OneDrive) for this user.
    desktop = os.environ.get('IGAC_DESKTOP_DIR')
    if not desktop and os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            desktop = os.path.expandvars(winreg.QueryValueEx(key, 'Desktop')[0])
    return (Path(desktop) if desktop else Path.home() / 'Desktop') / '聚鑫国际素材'


class StudioFiles:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else desktop_root()

    def owner_folder(self, owner):
        return self.root / hashlib.sha256(str(owner).encode()).hexdigest()[:10]

    def selection_path(self, owner, asset):
        return self.owner_folder(owner) / '已选素材' / (
            safe_name(asset['id']) + '_' + safe_name(Path(asset['name']).stem) + Path(asset['path']).suffix)

    def copy(self, source, target):
        source, target = Path(source), Path(target)
        temp = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and filecmp.cmp(source, target, shallow=False):
                return str(target)
            temp = target.with_name('.' + uuid.uuid4().hex + '.part')
            shutil.copyfile(source, temp)
            os.replace(temp, target)
            return str(target)
        except OSError as exc:
            raise ValidationError(f'素材未能保存到桌面：{target.parent}。请检查文件夹权限和剩余空间后重试') from exc
        finally:
            if temp is not None:
                try: temp.unlink(missing_ok=True)
                except OSError: pass

    def select(self, owner, asset):
        return self.copy(asset['path'], self.selection_path(owner, asset))

    def prepare(self, owner, job_id, profile, name, assets):
        folder = self.owner_folder(owner)
        if profile:
            identity = hashlib.sha256(str(profile).encode()).hexdigest()[:8]
            folder = folder / '发帖任务' / (safe_name(name or profile, 24) + '_' + identity)
        else:
            folder = folder / '素材备稿'
        folder = folder / safe_name(job_id)
        staged = []
        for index, asset in enumerate(assets, 1):
            # A stable name and byte comparison let retries repair missing or
            # modified copies without picking or downloading another asset.
            target = folder / (f'{index:02d}_' + safe_name(Path(asset['name']).stem, 24) + Path(asset['path']).suffix)
            staged.append(dict(asset, path=self.copy(asset['path'], target)))
        return {'folder': str(folder), 'files': [{'asset_id': a['id'], 'path': a['path']} for a in staged]}, staged
