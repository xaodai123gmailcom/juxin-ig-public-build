import {build} from 'esbuild';
import {resolve} from 'node:path';
await build({entryPoints:[resolve('renderer/tests/fixtures/studio-cleanup.tsx')],bundle:true,outfile:process.argv[2],format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
