import {build} from 'esbuild';
import {resolve} from 'node:path';
await build({entryPoints:['renderer/tests/fixtures/studio-nurture-r41.tsx'],bundle:true,format:'iife',outfile:resolve(process.argv[2]),jsx:'automatic',loader:{'.png':'dataurl','.svg':'dataurl'},define:{'process.env.NODE_ENV':'"production"'}});
