const assert=require('node:assert/strict');
const {waitRendererFixture,rendererFixtureRead}=require('./renderer-fixture.cjs');
const {waitTaskWatchCondition,closeFixtureContents}=require('./task-watch-fixture.cjs');
const {waitFixtureDocument,loadFixtureDocument}=require('./navigation-fixture.cjs');
module.exports=async({host,owner,base,timeoutMs=5000})=>{
 const id='native:dddddddd-dddd-4ddd-8ddd-dddddddddddd';
 let checkpoint='create-profile';
 const mark=name=>{checkpoint=name;console.log('CHECK task-watch '+name)};
 const previousInterference=host.onTaskInterference;
 try{
 mark('create-profile');
 const p=await host.ensure(id,owner,'',true);
 const manual=[...p.pages.values()][0];
 mark('manual-initial-document');
 await waitFixtureDocument(manual.view.webContents,'about:blank',{label:'manual initial blank',timeoutMs});
 mark('manual-document');
 await loadFixtureDocument(manual.view.webContents,base+'/manual',{label:'manual document',heading:'/manual',timeoutMs});
 mark('source-document');
 // newPage starts navigation itself. Request the real document once, without
 // racing an unfinished about:blank load with a second loadURL call.
 const source=await host.newPage(p,base+'/source');
 await waitFixtureDocument(source.view.webContents,base+'/source',{label:'source document',heading:'/source',timeoutMs});
 await host.control('label-task-page',{profile:id,target:source.targetId,role:'source'});
 const screens=[];
 // Creation order need not match the fixed slot order shown to the user.
 for(const i of [3,1,2]){
   const page=await host.newPage(p,base+'/screen-'+i);screens[i-1]=page;
   mark('screen-document-'+i);
   await waitFixtureDocument(page.view.webContents,base+'/screen-'+i,{label:'screen '+i,heading:'/screen-'+i,timeoutMs});
   await host.control('label-task-page',{profile:id,target:page.targetId,role:'screening',slot:i});
 }
 host.activate(p,manual);host.hide();const selected=p.selected;
 for(let workers=3;workers>=1;workers--){
   mark('labels-'+workers+'-workers');
   const frame=await host.control('watch-profile',{profile:id,owner});
   assert.equal(frame.target,source.targetId);assert.equal(frame.pages.length,workers+1);
   assert.deepEqual(frame.pages.map(x=>x.label),['采集页','1-1','1-2','1-3'].slice(0,workers+1));
   assert.equal(frame.image,'');
   const other=await host.control('watch-profile',{profile:id,owner,target:screens[0].targetId});
   assert.equal(other.target,screens[0].targetId);assert.equal(p.selected,selected);
   assert.ok(!host.visible); // Watching does not attach the interactive page.
   if(workers>1){
     mark('close-screen-'+workers);
     const page=screens.pop(),contents=page.view.webContents;
     await closeFixtureContents(contents,{label:'screen '+workers,timeoutMs});
     await waitTaskWatchCondition(()=>!p.pages.has(page.targetId),
       {label:'screen '+workers+' destroyed and removed',timeoutMs});
   }
 }
 mark('owner-and-target-isolation');
 await assert.rejects(host.control('watch-profile',{profile:id,owner:'22222222-2222-4222-8222-222222222222'}));
 await assert.rejects(host.control('watch-profile',{profile:id,owner,target:'foreign-target'}));
 const win=host.window();const bounds={x:100,y:140,width:900,height:650};
 mark('source-before-display');
 await waitFixtureDocument(source.view.webContents,base+'/source',{label:'source before display',heading:'/source',timeoutMs});
 const sourceFrame=await host.control('watch-profile',{profile:id,owner,target:source.targetId});
 assert.equal(sourceFrame.display_state,'ready','loaded source display state: '+JSON.stringify(sourceFrame));
 mark('read-only-surface');
 const grant=host.requestSurface();const shown=await host.control('show',{profile:id,grant,bounds,target:source.targetId,read_only:true});
 assert.equal(shown.attached,true,'source must be attached: '+JSON.stringify(shown));
 assert.equal(host.visible.readOnly,true);assert.equal(host.attached.targetId,source.targetId);
 const pane=host.panes.get(win),shield=host.watchShield;
 assert.equal(pane.children.at(-1),shield.view);assert.equal(pane.getVisible(),true);
 let clicks=0;host.onTaskInterference=target=>{assert.equal(target,source.targetId);clicks++};
 mark('read-only-shield-ready');
 await waitRendererFixture(shield.view.webContents,"Boolean(document.querySelector('a'))",{label:'task input shield DOM',timeoutMs});
 // Loading the local shield can outlive a display grant on a slow machine.
 // A new explicit request must still pass the unchanged production grant gate.
 const inputGrant=host.requestSurface();
 const inputShown=await host.control('show',{profile:id,grant:inputGrant,bounds,target:source.targetId,read_only:true});
 assert.equal(inputShown.attached,true);assert.equal(host.visible.readOnly,true);
 assert.equal(host.attached.targetId,source.targetId);assert.equal(pane.children.at(-1),shield.view);
 mark('read-only-shield-click');
 await rendererFixtureRead(shield.view.webContents,"document.querySelector('a').click()",{label:'task input shield click',timeoutMs});
 await waitTaskWatchCondition(()=>clicks>0,{label:'task input shield callback',timeoutMs});
 assert.equal(clicks,1);
 mark('interactive-display');
 const next=host.requestSurface();await host.control('show',{profile:id,grant:next,bounds,target:source.targetId});assert.notEqual(pane.children.at(-1),shield.view);
 mark('blank-target');
 const blank=await host.newPage(p,'about:blank');
 await waitFixtureDocument(blank.view.webContents,'about:blank',{label:'deliberately blank target',timeoutMs});
 await host.control('label-task-page',{profile:id,target:blank.targetId,role:'screening',slot:2});
 const blankGrant=host.requestSurface();
 const blankResult=await host.control('show',{profile:id,grant:blankGrant,bounds,target:blank.targetId,read_only:true});
 assert.equal(blankResult.attached,false);assert.equal(blankResult.display_state,'blank');assert.equal(pane.getVisible(),false);
 const blankFrame=await host.control('watch-profile',{profile:id,owner,target:blank.targetId});
 assert.equal(blankFrame.display_state,'blank');assert.equal(blankFrame.target,blank.targetId);
 mark('blank-to-ready');
 await loadFixtureDocument(blank.view.webContents,base+'/screen-ready',{label:'blank-to-ready document',heading:'/screen-ready',timeoutMs});
 const readyGrant=host.requestSurface();
 const readyResult=await host.control('show',{profile:id,grant:readyGrant,bounds,target:blank.targetId,read_only:true});
 assert.equal(readyResult.attached,true);
 assert.equal(host.attached?.targetId,blank.targetId);assert.equal(host.visible.readOnly,true);
 assert.equal((await host.control('watch-profile',{profile:id,owner,target:blank.targetId})).display_state,'ready');
 host.hide();assert.equal(pane.getVisible(),false);
 mark('close-profile');
 await host.closeProfile(p);
 console.log('PASS real read-only task display and native input shield, blank-to-ready native surface, exact 1-1/1-2/1-3 page labels, selection preserved, owner and target isolation');
 }catch(error){
   const failure=error instanceof Error?error:new Error(String(error));
   failure.integrationCheckpoint='task-watch/'+checkpoint;
   console.error('FAIL '+failure.integrationCheckpoint+': '+failure.name+': '+failure.message);
   throw failure;
 }finally{host.onTaskInterference=previousInterference}
};
