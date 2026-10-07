const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const {build}=require('esbuild');
const {RETAINED_NAVIGATION,assertNavigationStructure,assertRetainedNavigation}=require('./navigation-contract-r65.cjs');
const root=path.resolve(__dirname,'../..');
const valid=()=>RETAINED_NAVIGATION.map((row,index)=>({...row,iconCount:1,
  text:`rgb(${index}, 80, 120)`,anchor:`rgb(${index}, 80, 120)`,icon:`rgb(${index}, 80, 120)`}));

test('independent navigation contract retains exactly eleven routes and full accent checks',()=>{
  assert.equal(RETAINED_NAVIGATION.length,11);
  assertRetainedNavigation(valid());
  assert.ok(Object.isFrozen(RETAINED_NAVIGATION));
  assert.ok(RETAINED_NAVIGATION.every(Object.isFrozen));
});

test('navigation contract rejects each missing, duplicated, mislinked or recolored retained entry',()=>{
  for(let index=0;index<RETAINED_NAVIGATION.length;index++){
    const mutations=[
      rows=>rows.splice(index,1),
      rows=>rows.splice(index,0,{...rows[index]}),
      rows=>{rows[index].href='#/wrong'},
      rows=>{rows[index].label='wrong'},
      rows=>{rows[index].id='wrong'},
      rows=>{rows[index].iconCount=0},
      rows=>{rows[index].iconCount=2},
      rows=>{rows[index].text=rows[index].anchor=rows[index].icon=''},
      rows=>{rows[index].anchor='rgb(250, 0, 0)'},
      rows=>{rows[index].icon='rgb(250, 0, 0)'},
      rows=>{rows[index].text=rows[index].anchor=rows[index].icon=rows[(index+1)%rows.length].text},
    ];
    for(const mutate of mutations){const rows=valid();mutate(rows);assert.throws(()=>assertRetainedNavigation(rows));}
  }
  const reordered=valid();[reordered[0],reordered[1]]=[reordered[1],reordered[0]];
  assert.throws(()=>assertRetainedNavigation(reordered));
  for(const id of ['posting','data','unexpected']){
    const rows=valid();rows.push({id,href:'#/'+id,label:id,iconCount:1,text:'red',icon:'red'});
    assert.throws(()=>assertRetainedNavigation(rows));
  }
  assert.throws(()=>assertRetainedNavigation(null));
});

test('actual production Rail SSR has the exact retained structure and defined distinct static accents',async()=>{
  // Server rendering exercises the actual React component; native computed
  // styles remain mandatory in embedded-browser.integration.cjs, not faked here.
  const result=await build({stdin:{contents:
    "import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {Rail} from './renderer/src/formal-workbench';export const html=renderToStaticMarkup(<Rail mode=\"settings\" refresh={async()=>{}}/>);",
    resolveDir:root,sourcefile:'retained-navigation-contract.tsx',loader:'tsx'},
    bundle:true,platform:'node',format:'cjs',write:false,jsx:'automatic',
    define:{'process.env.NODE_ENV':'"production"'},loader:{'.css':'empty'},logLevel:'silent'});
  const loaded={exports:{}};
  new Function('require','module','exports',result.outputFiles[0].text)(require,loaded,loaded.exports);
  const rows=[...loaded.exports.html.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/g)].map(([,attrs,body])=>({
    id:/data-nav="([^"]+)"/.exec(attrs)?.[1],href:/href="([^"]+)"/.exec(attrs)?.[1],
    label:/<span>(.*?)<\/span>/.exec(body)?.[1],iconCount:(body.match(/<svg\b/g)||[]).length}));
  assertNavigationStructure(rows);
  const css=fs.readFileSync(path.join(root,'renderer/src/formal-workbench.css'),'utf8');
  const accents=Object.fromEntries([...css.matchAll(/\.formal-nav a\[data-nav="([^"]+)"\]\{--nav-accent:([^}]+)\}/g)].map(row=>[row[1],row[2]]));
  const colors=rows.map(row=>accents[row.id]);
  assert.ok(colors.every(color=>typeof color==='string'&&color.trim()));
  assert.equal(new Set(colors).size,RETAINED_NAVIGATION.length);
});

test('native acceptance uses actual observed routes, links, icon counts and computed colors',()=>{
  const source=fs.readFileSync(path.join(__dirname,'embedded-browser.integration.cjs'),'utf8');
  assert.match(source,/require\('\.\/navigation-contract-r65\.cjs'\)/);
  assert.match(source,/assertRetainedNavigation\(navColors\)/);
  for(const token of ["id:a.dataset.nav","href:a.getAttribute('href')","label:a.querySelector('span')?.textContent.trim()",
    "iconCount:a.querySelectorAll('svg').length","anchor:getComputedStyle(a).color",
    "getComputedStyle(a.querySelector('span')).color",
    "getComputedStyle(a.querySelector('svg')).color"])assert.ok(source.includes(token),token);
  assert.doesNotMatch(source,/new Set\(navColors\.map\(c=>c\.text\)\)\.size,12/);
  assert.ok(JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8')).scripts['test:source']
    .includes('desktop/tests/navigation-contract-r65.test.cjs'));
});
