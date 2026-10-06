import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {build} from 'esbuild';
test('production PostingWorkspace starts with four unknown counters and blocked unconfigured generation',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'posting-react-'));
 try{
  const output=join(folder,'fixture.cjs');
  await build({stdin:{contents:`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {PostingWorkspace,postingSourceUrl} from './renderer/src/posting-workspace';export {postingSourceUrl};export const html=renderToStaticMarkup(<PostingWorkspace snapshot={{windows:[]} as any}/>);`,resolveDir:resolve('.'),sourcefile:'posting-test.tsx',loader:'tsx'},bundle:true,platform:'node',format:'cjs',outfile:output,jsx:'automatic',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
  globalThis.window={collectorCore:{request:async()=>{throw Error('no requests during SSR')},secureGet:async()=>null,secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({})}};
  const {html,postingSourceUrl}=createRequire(import.meta.url)(output);
  assert.equal((html.match(/<strong(?: [^>]*)?>—<\/strong>/g)||[]).length,4);
  for(const label of ['等待发布','发布成功','发布失败','当日成功','上一页','下一页'])assert.ok(html.includes(label));
  assert.match(html,/须由本人安全配置有效 API 凭据/);assert.doesNotMatch(html,/网页免密钥通道/);
  assert.match(html,/disabled=""[^>]*><svg[^]*?生成任务并后台准备素材/);
  for(const url of ['javascript:alert(1)','http://www.pexels.com/x','https://evil.test/x','https://www.pexels.com.evil.test/x','https://user@www.pexels.com/x'])assert.equal(postingSourceUrl(url),undefined);
  assert.equal(postingSourceUrl('https://www.pexels.com/photo/123'),'https://www.pexels.com/photo/123');
 }finally{delete globalThis.window;rmSync(folder,{recursive:true,force:true})}
});
test('review payload freezes exact tuple and read generations discard stale responses',()=>{
 const source=readFileSync(new URL('../src/posting-workspace.tsx',import.meta.url),'utf8');
 assert.match(source,/reviewed:jobs.filter[^\n]*map\(t=>\(\{\.\.\.t\}\)\)/);
 assert.match(source,/reviewed:active.map\(t=>\(\{id:t.id,caption:t.caption,asset_id:t.asset_id,profile_id:t.profile_id,expected_username:t.expected_username,queue_revision:t.queue_revision\?\?0\}\)\)/);
 assert.match(source,/epoch===mutationEpoch.current&&generation===readGeneration.current/);
 assert.match(source,/postingSnapshot<Data>\(zone,cursor,50\)/);assert.match(source,/setPages\(\[\.\.\.pages,cursor\]\)/);
});

test('secure save invalidates older reads and forces a guarded fresh snapshot',()=>{const source=readFileSync(new URL('../src/posting-workspace.tsx',import.meta.url),'utf8');const save=source.slice(source.indexOf('async function saveKey'),source.indexOf('async function validate'));assert.match(save,/mutationEpoch.current\+\+/);assert.match(save,/await refresh\(true\)/);assert.match(save,/if\(!alive.current\)return/);assert.match(save,/if\(alive.current\)/);});
