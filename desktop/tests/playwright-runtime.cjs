/* Build/test only. Use the driver installed with the original Python worker. */
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

function loadPlaywrightRuntime(options = {}) {
  const root = options.root || path.resolve(__dirname, '../..');
  const env = options.env || process.env;
  const python = env.JUXIN_PYTHON || path.join(root, '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (!fs.existsSync(python)) {
    throw new Error(`Embedded test Python was not found: ${python}. Install backend/requirements.txt in the project .venv first.`);
  }
  let directory;
  try {
    // Do not send an unescaped Windows path through PowerShell's native-output
    // decoding. JSON is ASCII even for Chinese names, spaces and backslashes.
    // No shell is involved, and JavaScript/Python use exactly the same runtime.
    const output = execFileSync(python, ['-c',
      "import json,pathlib,playwright;print(json.dumps(str(pathlib.Path(playwright.__file__).parent / 'driver' / 'package'),ensure_ascii=True))"], {
      cwd: root, encoding: 'utf8', windowsHide: true, timeout: 30000,
      env: { ...env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    directory = JSON.parse(output.trim());
    if (typeof directory !== 'string' || !path.isAbsolute(directory)) {
      throw new Error('Python did not return an absolute Playwright package path');
    }
  } catch (error) {
    throw new Error(`Cannot locate Python Playwright using ${python}. Reinstall backend/requirements.txt. ${error.message}`, { cause: error });
  }
  const entry = path.join(directory, 'index.js');
  try {
    const { chromium } = require(entry);
    if (typeof chromium?.connectOverCDP !== 'function') {
      throw new Error('chromium.connectOverCDP is unavailable');
    }
    return { chromium, python, directory };
  } catch (error) {
    throw new Error(`Cannot load the embedded test driver at ${entry}. Reinstall backend/requirements.txt. ${error.message}`, { cause: error });
  }
}

module.exports = { loadPlaywrightRuntime };

if (require.main === module) {
  try {
    const runtime = loadPlaywrightRuntime();
    console.log(`PASS embedded test driver loaded from ${runtime.directory}`);
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
}
