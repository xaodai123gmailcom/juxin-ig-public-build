import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { builtinModules } from 'node:module';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const read = name => readFileSync(join(root,name),'utf8');
const pkg = JSON.parse(read('package.json'));

test('desktop external imports remain declared runtime dependencies after bundling UI packages', () => {
  const externals = new Set();
  const builtin = new Set([...builtinModules,'electron']);
  const collect = name => {
    if(name.startsWith('.') || name.startsWith('node:') || builtin.has(name)) return;
    externals.add(name.startsWith('@') ? name.split('/').slice(0,2).join('/') : name.split('/')[0]);
  };
  for(const name of readdirSync(join(root,'desktop/src')).filter(n=>/\.(?:ts|cts)$/.test(n))) {
    const source = ts.createSourceFile(name,read('desktop/src/'+name),ts.ScriptTarget.Latest,true);
    const visit = node => {
      if(ts.isImportDeclaration(node) && !node.importClause?.isTypeOnly && ts.isStringLiteral(node.moduleSpecifier)) collect(node.moduleSpecifier.text);
      if(ts.isCallExpression(node) && node.arguments.length===1 && ts.isStringLiteral(node.arguments[0]) &&
          (node.expression.kind===ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text==='require'))) collect(node.arguments[0].text);
      ts.forEachChild(node,visit);
    };
    visit(source);
  }
  assert.deepEqual([...externals].sort(),Object.keys(pkg.dependencies).sort());
});

test('bundled UI packages remain installed for builds but are excluded from the Node runtime graph', () => {
  for(const name of ['react','react-dom','radix-ui','lucide-react','sonner','clsx','tailwind-merge','class-variance-authority']) {
    assert.ok(pkg.devDependencies[name],name);
    assert.equal(pkg.dependencies[name],undefined,name);
  }
  const source=read('scripts/install_windows.ps1');
  assert.match(source,/npm ci --include=dev --include=optional --ignore-scripts=false/);
});

test('all previously required backend gate files execute once in the Windows build', () => {
  const source=read('scripts/build_windows.ps1');
  const files=readdirSync(join(root,'backend/tests'));
  const counts=new Map();
  for(const line of source.split(/\r?\n/).filter(l=>l.includes('scripts\\run_backend_tests.py'))) {
    const match=line.match(/-p(?:",)?\s+"([^"\n]+\.py)"/);
    if(!match) continue;
    const pattern=new RegExp('^'+match[1].replace(/[.+^${}()|[\]\\]/g,'\\$&').replace(/\*/g,'.*').replace(/\?/g,'.')+'$');
    const selected=files.filter(f=>pattern.test(f));
    assert.ok(selected.length,match[1]);
    for(const name of selected) counts.set(name,(counts.get(name)||0)+1);
  }
  const baseline=JSON.parse(read('scripts/tests/support/backend_gate_files_r94.json'));
  for(const name of baseline) assert.equal(counts.get(name),1,`${name} must still run exactly once`);
  for(const [name,count] of counts) assert.equal(count,1,`${name} was scheduled more than once`);
});

test('native platform preflight uses Electron lazy-install launcher rather than an assumed binary', () => {
 const build=readFileSync(new URL('../build_windows.ps1',import.meta.url),'utf8');
 const start=build.indexOf('$PlatformNativePreflightExitCode =');
 const end=build.indexOf('# Pure IG deliberately retires',start);
 assert.ok(start>=0&&end>start);
 const preflight=build.slice(start,end);
 assert.ok(preflight.includes('node_modules\\.bin\\electron.cmd'));
 assert.ok(!preflight.includes('electron\\dist\\electron.exe'));
 assert.ok(preflight.includes('workbench-platform.preflight.cjs'));
});

test('fresh Windows cleanup preflight compiles its native host before invoking Electron', () => {
 const build=read('scripts/build_windows.ps1');
 const installed=build.indexOf('& "$PSScriptRoot\\install_windows.ps1"');
 const compiled=build.indexOf('$NativePreflightBuildExitCode =');
 const native=build.indexOf('$CleanupNativeExitCode =');
 const full=build.indexOf('$DesktopBuildExitCode =');
 assert.ok(installed>=0&&compiled>installed&&native>compiled&&full>native,'native cleanup needs its own unconditional successful compile after dependency installation and before the later full build');
 const prerequisite=build.slice(compiled,native);
 assert.match(prerequisite,/-ArgumentList @\("run", "build:electron"\)/);
 assert.match(prerequisite,/if \(\$NativePreflightBuildExitCode -ne 0\) \{[\s\S]*?throw /);
 assert.match(pkg.scripts['build:electron'],/clean_desktop_output\.mjs.*tsc -p desktop\/tsconfig\.json/,'preflight must clean stale generated files and compile real desktop sources');
});
