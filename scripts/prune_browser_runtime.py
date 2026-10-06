"""Keep pinned runtimes; preserve interrupted downloads outside release inputs."""
import json
import re
import shutil
import stat
from pathlib import Path
from uuid import uuid4


def is_reparse(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def prune(root, registry):
    if is_reparse(root):
        raise RuntimeError('Browser staging directory must not be a link or reparse point')
    keep = {f"{r['name']}-{r['revision']}" for r in registry['browsers']
            if r['name'] in {'chromium', 'ffmpeg', 'winldd'}}
    operations = []
    # Validate all entries before mutation, including kept names. Otherwise
    # Copy-Item could include external data through a kept Windows junction.
    for entry in sorted(root.iterdir()):
        linked = is_reparse(entry)
        if entry.name in keep or entry.name == '.links':
            if linked or not entry.is_dir():
                raise RuntimeError(f'Kept browser runtime must be a real directory, not a link/reparse point: {entry.name}')
            continue
        if re.fullmatch(r'juxin-browser-download-[a-z0-9_]{8}', entry.name):
            # Legacy downloads lived inside build/browsers. A killed process
            # bypasses cleanup. Preserve these as siblings, outside the package.
            operations.append(('preserve', entry, linked))
        elif re.fullmatch(r'(?:chromium|chromium_headless_shell|chromium-headless-shell|firefox|webkit|ffmpeg|winldd)-\d+', entry.name):
            operations.append(('remove', entry, linked))
        else:
            raise RuntimeError('Unexpected file in generated browser runtime; inspect build/browsers before packaging')
    removed = []
    for action, entry, linked in operations:
        if action == 'preserve':
            entry.rename(root.parent / (entry.name + '-recovered-' + uuid4().hex))
        elif linked:
            # Remove only the link/junction, never traverse its target.
            if entry.is_symlink():
                entry.unlink()
            else:
                entry.rmdir()
        elif entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)
        removed.append(entry.name)
    return removed

if __name__=='__main__':
    import playwright
    root=Path(__file__).resolve().parents[1]/'build'/'browsers'
    registry=json.loads((Path(playwright.__file__).parent/'driver'/'package'/'browsers.json').read_text())
    removed=prune(root,registry)
    print(f'Browser runtime staging: excluded {len(removed)} obsolete or interrupted entries; user data untouched')
