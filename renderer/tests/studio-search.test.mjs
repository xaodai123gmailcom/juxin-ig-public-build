import assert from 'node:assert/strict';
import test from 'node:test';
import {nextMediaSearch,acceptMediaSearch} from '../src/studio-search.ts';
const item=(id,type='photo')=>({id,media_type:type,name:id,preview:'',attribution:'',url:''});
const request={query:'海滩',media_type:'photo',page:1};
const first=()=>acceptMediaSearch(null,request,{items:[item('1'),item('2')],page:1,total:30,has_more:true});
test('换一批 advances pages, filters repeated provider IDs and preserves previous state',()=>{
 const previous=first(),next=nextMediaSearch(previous,' 海滩 ','photo',true);
 assert.equal(next.page,2);
 const result=acceptMediaSearch(previous,next,{items:[item('2'),item('3'),item('3')],page:2,total:30,has_more:true});
 assert.deepEqual(result.rows.map(x=>x.id),['3']);
 assert.deepEqual(previous.rows.map(x=>x.id),['1','2']);
 assert.equal(nextMediaSearch(result,'海滩','photo',true).page,3);
});
test('keyword or media change restarts page one; explicit search can restart same keyword',()=>{
 const previous=first();
 assert.equal(nextMediaSearch(previous,'森林','photo',true).page,1);
 assert.equal(nextMediaSearch(previous,'海滩','video',true).page,1);
 assert.equal(nextMediaSearch(previous,'海滩','photo',false).page,1);
 assert.equal(nextMediaSearch(previous,' ','photo',true),null);
});
test('last batch stops instead of silently repeating page one',()=>{
 const last={...first(),hasMore:false};
 assert.equal(nextMediaSearch(last,'海滩','photo',true),null);
 assert.equal(nextMediaSearch(last,'森林','photo',true).page,1);
});
test('empty duplicate batch retains ability to continue to provider next page',()=>{
 const result=acceptMediaSearch(first(),{...request,page:2},{items:[item('1')],page:2,total:30,has_more:true});
 assert.deepEqual(result.rows,[]);
 assert.equal(nextMediaSearch(result,'海滩','photo',true).page,3);
});
