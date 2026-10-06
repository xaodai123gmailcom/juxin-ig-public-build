const assert=require('node:assert/strict');
const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const {prepareVisibleFixture}=require('./visible-fixture.cjs');

// sendInputEvent sends exactly the requested event. Unlike an OS key press,
// keyDown alone does not include the char/keypress phase used by native buttons.
async function activateNativeButton(win,selector,key,{label='native button',timeoutMs=5000}={}){
 if(!['Enter','Space'].includes(key))throw Error('Unsupported button activation key');
 const read=source=>rendererFixtureRead(win.webContents,source,{label,timeoutMs});
 const wait=(source,step)=>waitRendererFixture(win.webContents,source,{label:label+' '+step,timeoutMs});
 const target=`document.querySelector(${JSON.stringify(selector)})`;
 await prepareVisibleFixture(win,{label,timeoutMs});
 win.focus();win.webContents.focus();
 await read(`${target}.focus();true`);
 await wait(`document.visibilityState==='visible'&&document.hasFocus()&&document.activeElement===${target}&&!${target}.disabled`,'DOM focus');
 assert.equal(win.isFocused(),true,label+': BrowserWindow must own native focus');
 assert.equal(win.webContents.isFocused(),true,label+': WebContents must own native focus');
 await read(`(()=>{
   window.__nativeButtonKeyEvents=[];
   window.__nativeButtonKeyObserver=event=>{
     if(event.target?.closest?.(${JSON.stringify(selector)}))window.__nativeButtonKeyEvents.push({type:event.type,key:event.key,code:event.code,trusted:event.isTrusted});
   };
   for(const type of ['keydown','keypress','keyup','click'])document.addEventListener(type,window.__nativeButtonKeyObserver,true);
 })()`);
 try{
  win.webContents.sendInputEvent({type:'keyDown',keyCode:key});
  win.webContents.sendInputEvent({type:'char',keyCode:key==='Enter'?'\r':' '});
  win.webContents.sendInputEvent({type:'keyUp',keyCode:key});
  await wait("window.__nativeButtonKeyEvents.some(event=>event.type==='click'&&event.trusted)",'trusted keyboard click');
  const events=await read('window.__nativeButtonKeyEvents');
  assert.equal(events.filter(event=>event.type==='click'&&event.trusted).length,1,label+': expected exactly one trusted click');
  assert.ok(events.some(event=>event.type==='keydown'&&event.trusted),label+': trusted keydown missing');
  assert.ok(events.some(event=>event.type==='keypress'&&event.trusted),label+': trusted keypress missing');
  console.log('CHECK native keyboard button',JSON.stringify({label,key,events}));
 }catch(error){
  let renderer;
  try{renderer=await read(`({visibility:document.visibilityState,focus:document.hasFocus(),active:document.activeElement?.outerHTML?.slice(0,500),events:window.__nativeButtonKeyEvents})`)}catch(reason){renderer={error:String(reason)}}
  console.error('CHECK native keyboard failure',JSON.stringify({label,key,visible:win.isVisible(),focused:win.isFocused(),contentsFocused:win.webContents.isFocused(),renderer}));
  throw error;
 }finally{
  await read(`for(const type of ['keydown','keypress','keyup','click'])document.removeEventListener(type,window.__nativeButtonKeyObserver,true);true`);
 }
}
module.exports={activateNativeButton};
