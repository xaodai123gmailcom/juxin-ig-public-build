import { cpSync, existsSync, lstatSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Only the first-party host and notice may ship. A locally installed optional
// proprietary userscript (or any other file in its folder) is never copied.
const assets = ['desktop/assets', 'desktop/vendor/immersive-translate/host.html',
  'desktop/vendor/immersive-translate/NOTICE.txt'];
const requiredAssets = ['desktop/assets/war-wolf.ico',
  'desktop/vendor/immersive-translate/host.html',
  'desktop/vendor/immersive-translate/NOTICE.txt'];
const inside = (parent, child) => {
  const value = relative(parent, child);
  return value === '' || (!isAbsolute(value) && value !== '..' && !value.startsWith('..' + (process.platform === 'win32' ? '\\' : '/')));
};
const metadata = folder => JSON.parse(readFileSync(join(folder, 'package.json'), 'utf8'));

function locateDependency(root, from, name) {
  if (!/^(?:@[a-zA-Z0-9_.-]+\/)?[a-zA-Z0-9_.-]+$/.test(name)
      || name.split('/').some(part => part === '.' || part === '..')) {
    throw new Error(`Invalid portable dependency name: ${name}`);
  }
  for (let current = from; inside(root, current); current = dirname(current)) {
    if (basename(current) !== 'node_modules') {
      const candidate = join(current, 'node_modules', name);
      if (existsSync(join(candidate, 'package.json'))) return candidate;
    }
    if (current === root) break;
  }
  return null;
}

export function stagePortableResources(projectRoot, appRoot) {
  const root = resolve(projectRoot), destination = resolve(appRoot);
  if (destination === root || inside(join(root, 'node_modules'), destination)) {
    throw new Error('Portable destination must be separate from source dependencies');
  }
  if (existsSync(join(destination, 'node_modules'))) {
    throw new Error('Portable destination already contains dependencies; use a fresh build directory');
  }
  try {
    lstatSync(join(destination, 'desktop/vendor/immersive-translate'));
    throw new Error('Portable destination already contains translator resources; use a fresh build directory');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  for (const name of requiredAssets) {
    if (!existsSync(join(root, name)) || !statSync(join(root, name)).isFile()) {
      throw new Error(`Missing portable asset: ${name}`);
    }
  }
  const selected = new Set();
  function visit(folder, topLevel = false) {
    const pkg = metadata(folder);
    const optional = pkg.optionalDependencies || {};
    const wanted = { ...pkg.dependencies, ...optional };
    // Installed runtime peers are also needed; absent optional peers (such as
    // ws native accelerators) must not turn a complete JS runtime into a failure.
    if (!topLevel) Object.assign(wanted, pkg.peerDependencies || {});
    for (const name of Object.keys(wanted).sort()) {
      const dependency = locateDependency(root, folder, name);
      if (!dependency) {
        if (Object.hasOwn(optional, name)
            || (!Object.hasOwn(pkg.dependencies || {}, name) && pkg.peerDependenciesMeta?.[name]?.optional)) continue;
        throw new Error(`Missing portable dependency: ${name} required by ${pkg.name || folder}`);
      }
      if (selected.has(dependency)) continue;
      selected.add(dependency);
      visit(dependency);
    }
  }
  visit(root, true);
  mkdirSync(destination, { recursive: true });
  for (const asset of assets) {
    // Keep Unicode Windows paths on Node's JS copy implementation. The native
    // unfiltered copyDir fast path can terminate the process (nodejs/node#59636,
    // nodejs/node#63970); accepting every entry preserves the complete assets.
    cpSync(join(root, asset), join(destination, asset), {
      recursive: true, dereference: true, filter: () => true,
    });
  }
  // Keep Node's original nested/hoisted layout. Copy only the runtime graph,
  // not the source checkout's dev tools or unrelated nested node_modules.
  for (const folder of selected) {
    cpSync(folder, join(destination, relative(root, folder)), {
      recursive: true, dereference: true,
      filter: path => path === folder || basename(path) !== 'node_modules',
    });
  }
  const require = createRequire(join(destination, 'package.json'));
  const wsPath = require.resolve('ws');
  if (!inside(join(destination, 'node_modules'), wsPath)) {
    throw new Error('Portable ws resolved outside the packaged application');
  }
  const { WebSocket, WebSocketServer } = require('ws');
  if (typeof WebSocket !== 'function' || typeof WebSocketServer !== 'function') {
    throw new Error('Portable ws runtime is not usable');
  }
  return { packages: [...selected].map(folder => relative(root, folder)).sort(), assets };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if (process.argv.length !== 4) throw new Error('Usage: node stage_portable_resources.mjs PROJECT_ROOT APP_ROOT');
    const result = stagePortableResources(process.argv[2], process.argv[3]);
    console.log(`PORTABLE_RESOURCES_CHECK=PASS packages=${result.packages.length} asset_entries=${result.assets.length}`);
  } catch (error) {
    console.error(`PORTABLE_RESOURCES_CHECK=FAIL: ${error.message}`);
    process.exitCode = 1;
  }
}
