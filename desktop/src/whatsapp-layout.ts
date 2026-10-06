import {whatsappBoundaryCleanup} from './whatsapp-boundary.js';
import {findWhatsAppColumns,findWhatsAppDrawers} from './whatsapp-columns.js';

export function whatsappColumnLayoutScript(){
  return `(${whatsappColumnLayout.toString()})(${whatsappBoundaryCleanup.toString()},${findWhatsAppColumns.toString()},${findWhatsAppDrawers.toString()})`;
}

/** Runs in an isolated world. Text selection remains available on both chat
 * platforms; only WhatsApp receives a draggable divider. Never move messages,
 * replace page components, reload the page or alter storage/worker APIs. */
export function whatsappColumnLayout(makeBoundary?:typeof whatsappBoundaryCleanup,findColumns?:typeof findWhatsAppColumns,findDrawers?:typeof findWhatsAppDrawers) {
  const wa=location.origin==='https://web.whatsapp.com';
  const ig=/(^|\.)instagram\.com$/.test(location.hostname);
  if (!wa&&!ig) return false;
  const world = globalThis as any, key = '__juxinChatColumnsV2';
  if (world[key]) { world[key].setEnabled(true); return true; }
  const selectionStyle=document.createElement('style');
  selectionStyle.textContent='.selectable-text,[data-juxin-chat-translation],main [dir="auto"],[role="main"] [dir="auto"]{-webkit-user-select:text!important;user-select:text!important}';
  document.head.append(selectionStyle);
  const selectionEnabled=(value:boolean)=>{if(!value)selectionStyle.remove();else if(!selectionStyle.isConnected)document.head.append(selectionStyle);};
  // Instagram keeps its native column widths. The existing task lifecycle can
  // still suspend text selection without installing observers, a handle or a
  // saved width preference on Instagram pages.
  if(!wa){world[key]={refresh:()=>{},setEnabled:selectionEnabled};return true;}
  const boundary=makeBoundary?.();
  const storageKey = 'juxin.whatsapp.column-ratio.v1';
  let enabled=true;
  let ratio: number | null = null;
  try { const value = Number(localStorage.getItem(storageKey)); if (value > 0.05 && value < 0.95) ratio = value; } catch {}
  type Columns = {left:HTMLElement; right:HTMLElement; parent:HTMLElement;grid:boolean};
  let columns:Columns|null = null, handle:HTMLDivElement|null = null;
  let drawers:HTMLElement[]=[],nativeWidthRatio=0;
  let queued = 0, timer:ReturnType<typeof setTimeout>|undefined;
  let scanNeeded=true,lastScan=0;
  let dragging:{id:number; offset:number; before:number|null}|null = null;
  const changes = new Map<HTMLElement,Map<string,{value:string; priority:string; written:string}>>();
  const ownedStyles=new WeakMap<HTMLElement,string|null>();
  const bounds = (node:Element) => node.getBoundingClientRect();
  const shown = (node:Element) => { const r=bounds(node); return r.width>0 && r.height>0 && getComputedStyle(node).visibility!=='hidden'; };
  const set = (node:HTMLElement,property:string,value:string) => {
    let row=changes.get(node); if(!row){row=new Map();changes.set(node,row);}
    if(!row.has(property))row.set(property,{value:node.style.getPropertyValue(property),priority:node.style.getPropertyPriority(property),written:''});
    const item=row.get(property)!;
    if(node.style.getPropertyValue(property)!==value||node.style.getPropertyPriority(property)!=='important')node.style.setProperty(property,value,'important');
    item.written=node.style.getPropertyValue(property);
    ownedStyles.set(node,node.getAttribute('style'));
  };
  const restore = (only?:HTMLElement[]) => {
    for(const [node,properties] of changes){
      if(only&&!only.includes(node))continue;
      for(const [property,item] of properties){
      // Do not undo a new value written independently by the site.
      if(node.style.getPropertyValue(property)!==item.written||node.style.getPropertyPriority(property)!=='important')continue;
      if(item.value)node.style.setProperty(property,item.value,item.priority);else node.style.removeProperty(property);
      }
      changes.delete(node);ownedStyles.set(node,node.getAttribute('style'));
    }
  };
  const locate = ():Columns|null => {
    return findColumns?.(columns)??null;
  };
  const metrics = () => {
    if(!columns)return null;
    const a=bounds(columns.left),b=bounds(columns.right),gap=Math.max(0,b.left-a.right);
    const total=a.width+b.width;
    if(total<440)return null;
    return {a,b,total,gap,min:180,max:total-260};
  };
  const persist = () => {try{if(ratio===null)localStorage.removeItem(storageKey);else localStorage.setItem(storageKey,String(ratio));}catch{}};
  const position = () => {
    if(!handle||!columns)return;
    const a=bounds(columns.left),b=bounds(columns.right);
    const top=Math.max(0,a.top,b.top),bottom=Math.min(innerHeight,a.bottom,b.bottom);
    handle.style.left=Math.round((a.right+b.left)/2-5)+'px';handle.style.top=Math.round(top)+'px';handle.style.height=Math.max(0,bottom-top)+'px';
    const percent=Math.round(a.width/(a.width+b.width)*100);
    handle.setAttribute('aria-valuenow',String(percent));handle.setAttribute('aria-valuetext',`联系人 ${percent}%，聊天 ${100-percent}%`);
    const m=metrics();if(m){handle.setAttribute('aria-valuemin',String(Math.round(m.min/m.total*100)));handle.setAttribute('aria-valuemax',String(Math.round(m.max/m.total*100)));}
  };
  const apply = () => {
    const m=metrics();if(!m||!columns)return;
    if(ratio===null)ratio=m.a.width/m.total;
    const width=Math.round(Math.max(m.min,Math.min(m.max,m.total*ratio)));
    if(columns.grid){
      const children=Array.from(columns.parent.children).filter((n):n is HTMLElement=>n instanceof HTMLElement&&shown(n)&&!['absolute','fixed'].includes(getComputedStyle(n).position));
      // Keep the chat track fluid. Fixing both tracks in pixels freezes their
      // measured total when the host resizes, so subsequent ratio updates
      // repeatedly reuse the old width and overflow the narrower window.
      const tracks=children.map(n=>n===columns!.left?width+'px':n===columns!.right?'minmax(0, 1fr)':bounds(n).width+'px');
      if(children.includes(columns.left)&&children.includes(columns.right))set(columns.parent,'grid-template-columns',tracks.join(' '));
    }
    set(columns.left,'flex',`0 0 ${width}px`);set(columns.left,'width',width+'px');
    set(columns.left,'min-width','0px');set(columns.left,'max-width','none');
    set(columns.right,'flex','1 1 0%');set(columns.right,'width','auto');
    set(columns.right,'min-width','0px');set(columns.right,'max-width','none');
    for(const drawer of drawers){set(drawer,'width',width+'px');set(drawer,'min-width','0px');set(drawer,'max-width',width+'px');set(drawer,'box-sizing','border-box');}
    position();
    const a=bounds(columns.left),b=bounds(columns.right);boundary?.sync((a.right+b.left)/2);
  };
  const finish = (save:boolean) => {
    if(!dragging)return;
    const active=dragging;dragging=null;
    if(!save)ratio=active.before;else persist();
    try{if(handle?.hasPointerCapture(active.id))handle.releasePointerCapture(active.id);}catch{}
    handle?.removeAttribute('data-dragging');scanNeeded=true;apply();schedule();
  };
  const detach = () => {finish(true);handle?.remove();handle=null;boundary?.clear();restore();drawers=[];columns=null;resize.disconnect();resize.observe(document.documentElement);};
  const reset = () => {finish(false);ratio=null;persist();restore();apply();};
  const makeHandle = () => {
    const node=document.createElement('div');node.dataset.juxinWaSplitter='true';
    node.setAttribute('role','separator');node.setAttribute('aria-orientation','vertical');
    node.setAttribute('aria-label','调整联系人列表和聊天区宽度');node.tabIndex=0;
    node.title='左右拖动调整宽度；双击恢复默认比例';
    node.style.cssText='position:fixed;width:10px;z-index:2147483000;pointer-events:auto;cursor:col-resize;touch-action:none;user-select:none;outline:none;background:transparent;';
    const shadow=node.attachShadow({mode:'closed'});
    shadow.innerHTML='<style>:host::after{content:"";position:absolute;left:4px;top:0;bottom:0;width:2px;background:transparent}:host(:hover)::after,:host(:focus-visible)::after,:host([data-dragging])::after{background:#25d366;box-shadow:0 0 0 1px rgba(37,211,102,.25)}</style>';
    node.addEventListener('pointerdown',event=>{
      if(event.button!==0||!columns)return;const m=metrics();if(!m)return;
      event.preventDefault();event.stopPropagation();node.focus({preventScroll:true});
      dragging={id:event.pointerId,offset:event.clientX-m.a.right,before:ratio};
      node.setPointerCapture(event.pointerId);node.setAttribute('data-dragging','true');
    });
    node.addEventListener('pointermove',event=>{
      if(!dragging||event.pointerId!==dragging.id)return;const m=metrics();if(!m)return;
      event.preventDefault();event.stopPropagation();
      ratio=Math.max(m.min,Math.min(m.max,event.clientX-m.a.left-dragging.offset))/m.total;apply();
    });
    node.addEventListener('pointerup',event=>{if(dragging?.id===event.pointerId){event.preventDefault();event.stopPropagation();finish(true);}});
    node.addEventListener('pointercancel',()=>finish(false));node.addEventListener('lostpointercapture',()=>finish(true));
    node.addEventListener('dblclick',event=>{event.preventDefault();event.stopPropagation();reset();});
    node.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&dragging){event.preventDefault();finish(false);return;}
      if(!['ArrowLeft','ArrowRight','Home','End','Enter'].includes(event.key))return;
      const m=metrics();if(!m)return;event.preventDefault();event.stopPropagation();
      if(event.key==='Enter'){reset();return;}
      const delta=event.shiftKey?30:10;
      const width=event.key==='Home'?m.min:event.key==='End'?m.max:m.a.width+(event.key==='ArrowLeft'?-delta:delta);
      ratio=Math.max(m.min,Math.min(m.max,width))/m.total;persist();apply();
    });
    document.body.append(node);return node;
  };
  const refresh = () => {
    queued=0;if(!enabled){if(columns)detach();return;}if(document.visibilityState==='hidden'){finish(true);return;}
    const found=locate();
    if(!found||bounds(found.left).width+bounds(found.right).width<440){if(columns)detach();return;}
    if(found.left!==columns?.left||found.right!==columns?.right||found.parent!==columns?.parent||found.grid!==columns?.grid){
      detach();columns=found;
      nativeWidthRatio=bounds(found.left).width/Math.max(1,bounds(found.parent).width);
      boundary?.capture(found.left,found.right,found.parent);
      scanNeeded=true;
      resize.observe(found.parent);resize.observe(found.left);resize.observe(found.right);
    }
    if(!metrics()){if(columns)detach();return;}
    const nextDrawers=findDrawers?.(found,nativeWidthRatio*bounds(found.parent).width)??[];
    restore(drawers.filter(node=>!nextDrawers.includes(node)));drawers=nextDrawers;
    if(!handle)handle=makeHandle();
    if(!dragging&&(scanNeeded||Date.now()-lastScan>500)){boundary?.rescan();lastScan=Date.now();scanNeeded=false;}
    // Native dialogs/popovers retain their own input layer.
    const modal=Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]')).some(shown);
    handle.style.display=modal?'none':'block';apply();
  };
  const schedule = () => {if(!queued)queued=requestAnimationFrame(refresh);};
  const resize=new ResizeObserver(schedule);resize.observe(document.documentElement);
  const mutations=new MutationObserver(records=>{
    // Ignore our width writes and handle updates, but detect theme changes and
    // class/style navigation that does not replace a DOM node.
    const relevant=records.some(record=>{
      if(record.target===handle)return false;
      if(record.type!=='attributes')return Array.from(record.addedNodes).concat(Array.from(record.removedNodes)).some(node=>node!==handle);
      const node=record.target;if(!(node instanceof HTMLElement))return false;
      if(record.attributeName==='style'&&ownedStyles.has(node)&&ownedStyles.get(node)===node.getAttribute('style'))return false;
      if(node===document.documentElement||node===document.body)return true;
      if(!columns)return false;
      const r=bounds(node),a=bounds(columns.left);return r.height>=200&&Math.abs(r.top-a.top)<=16&&Math.abs(r.bottom-a.bottom)<=16;
    });
    if(!relevant)return;
    scanNeeded=true;
    if(!timer)timer=setTimeout(()=>{timer=undefined;schedule();},100);
  });
  mutations.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['style','class']});
  mutations.observe(document.documentElement,{attributes:true,attributeFilter:['style','class']});
  addEventListener('resize',schedule);addEventListener('blur',()=>finish(true));
  document.addEventListener('visibilitychange',schedule);
  const scanAfterChange=()=>{scanNeeded=true;schedule();};
  document.addEventListener('load',scanAfterChange,true);
  document.addEventListener('transitionend',scanAfterChange);
  world[key]={refresh:schedule,setEnabled:(value:boolean)=>{enabled=value;selectionEnabled(value);if(!enabled){if(queued)cancelAnimationFrame(queued);queued=0;detach();}else schedule();}};schedule();return true;
}
