// TypeScript does not remove output for deleted source files. Rebuild this
// generated directory from scratch; user profiles live outside the source tree.
import {existsSync, rmSync} from 'node:fs';
const output = new URL('../dist-electron/', import.meta.url);
rmSync(output, {recursive: true, force: true, maxRetries: 3, retryDelay: 100});
if (existsSync(output)) {
  throw new Error('Desktop output cleanup did not complete; refusing to compile over stale files');
}
