"""Small DOM probes scoped to the active post composer."""

LOCATION_BEGIN = r'''(field,marker)=>{
 const composer=field.closest('[data-juxin-composer],[role="dialog"],[aria-modal="true"]');
 let section=field.parentElement;
 if(section.querySelector('textarea,[contenteditable="true"]'))section=field;
 section.setAttribute('data-juxin-location-section',marker);
 const r=field.getBoundingClientRect();
 composer.__juxinLocation={marker,rect:{left:r.left,right:r.right,top:r.top,bottom:r.bottom},
  before:new WeakSet([...document.querySelectorAll('div,ul,[role="listbox"],[role="menu"]')].filter(el=>el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden'))};
}'''

LOCATION_OPTIONS_PROBE = r'''(field, marker) => {
 const visible=el=>!!el&&el.getClientRects().length>0&&getComputedStyle(el).visibility!=='hidden';
 const fr=field.getBoundingClientRect();
 const composer=field.closest('[data-juxin-composer],[role="dialog"],[aria-modal="true"]');
 if(!composer||!visible(field))return null;
 const roots=[],seen=new Set();
 const add=(el,method,structural=false)=>{if(el&&!seen.has(el)){seen.add(el);roots.push({el,method,structural});}};
 const linked=(field.getAttribute('aria-controls')||field.getAttribute('aria-owns')||'').split(/\s+/).map(id=>document.getElementById(id)).filter(Boolean);
 linked.forEach(el=>add(el,'linked_popup',true));
 // An anchored popup can be outside the composer with no aria relationship.
 // Only newly visible external popups near the focused location field qualify.
 function anchored(el){
  if(!visible(el)||el.contains(field)||el.querySelector('input,textarea,[contenteditable="true"],video,img'))return false;
  const r=el.getBoundingClientRect(),s=getComputedStyle(el),overlap=Math.min(r.right,fr.right)-Math.max(r.left,fr.left);
  const gap=Math.min(Math.abs(r.bottom-fr.top),Math.abs(r.top-fr.bottom));
  return r.height>=24&&r.height<=450&&r.width>=fr.width*.65&&r.width<=fr.width*1.8&&overlap>=Math.min(r.width,fr.width)*.7&&gap<=85&&
    (el.matches('[role="listbox"],[role="menu"]')||/fixed|absolute/.test(s.position)||/auto|scroll/.test(s.overflowY));
 }
 for(const el of composer.querySelectorAll('div,ul,[role="listbox"],[role="menu"]'))if(anchored(el))add(el,'composer_popup',true);
 if(document.activeElement===field&&composer.__juxinLocation?.marker===marker){
  for(const el of document.querySelectorAll('div,ul,[role="listbox"],[role="menu"]')){
   if(composer.contains(el)||composer.__juxinLocation.before.has(el))continue;
   if(anchored(el))add(el,'anchored_portal',true);
  }
 }
 for(let el=field.parentElement;el&&el!==document.body&&el!==composer;el=el.parentElement){
  if(el.querySelector('textarea,[contenteditable="true"]'))break;
  add(el,'location_section');
 }
 const ignored=/^(分享|发布|完成|取消|关闭|添加地点|添加位置|添加合作者|添加合作作者|添加 AI 标签|高级设置|辅助功能|Share|Done|Cancel|Close|Add location|Add collaborators|Accessibility|Advanced settings)$/i;
 function point(el){
  const r=el.getBoundingClientRect();
  for(const f of [.5,.2,.8]){
   const x=Math.min(innerWidth-2,Math.max(2,r.left+r.width*.35)),y=Math.min(innerHeight-2,Math.max(2,r.top+r.height*f));
   if(x<r.left||x>r.right||y<r.top||y>r.bottom)continue;
   const hit=document.elementFromPoint(x,y);
   if(hit&&(hit===el||el.contains(hit)))return {x:x-r.left,y:y-r.top};
  }
  return null;
 }
 function eligible(el){
  if(!visible(el)||el===field||el.contains(field)||el.matches('input,textarea,[contenteditable="true"],:disabled,[aria-disabled="true"]')||el.querySelector('input,textarea,[contenteditable="true"]'))return false;
  const text=el.innerText?.trim(),r=el.getBoundingClientRect();
  return !!text&&text.length<=500&&!ignored.test(text)&&r.height>=16&&r.height<=180&&r.width>=Math.min(100,fr.width*.45)&&r.right>fr.left&&r.left<fr.right&&r.bottom>=fr.top-450&&r.top<=fr.bottom+450&&!!point(el);
 }
 for(const {el:root,method,structural} of roots){
  let options=[...root.querySelectorAll('[role="option"]')].filter(eligible);
  if(!options.length)options=[...root.querySelectorAll('button,[role="button"],a,[role="menuitem"],li,[tabindex]')].filter(eligible);
  if(!options.length)options=[...root.querySelectorAll('div,span')].filter(el=>eligible(el)&&getComputedStyle(el).cursor==='pointer');
  if(!options.length&&structural){
   const queue=[root];let single=[];
   while(queue.length){
    const container=queue.shift(),rows=[...container.children].filter(eligible);
    const separate=rows.length>1&&rows.every((row,i)=>!i||row.getBoundingClientRect().top>=rows[i-1].getBoundingClientRect().bottom-2);
    if(separate){options=rows;break;}
    if(!single.length&&rows.length===1)single=rows;
    queue.push(...container.children);
   }
   if(!options.length)options=single;
  }
  options=options.filter(el=>!options.some(other=>other!==el&&other.contains(el)));
  options.sort((a,b)=>a.getBoundingClientRect().top-b.getBoundingClientRect().top);
  if(!options.length)continue;
  document.querySelectorAll('[data-juxin-location-option],[data-juxin-location-results]').forEach(el=>{el.removeAttribute('data-juxin-location-option');el.removeAttribute('data-juxin-location-results');});
  const first=options[0];first.setAttribute('data-juxin-location-option',marker);
  const popup=structural?root:first.parentElement;
  if(popup&&!popup.contains(field))popup.setAttribute('data-juxin-location-results',marker);
  const lines=first.innerText.trim().split(/\n/).map(s=>s.trim()).filter(Boolean);
  const title=[...first.querySelectorAll('span,strong')].find(el=>visible(el)&&!el.children.length&&el.innerText?.trim());
  const name=lines.length>1?lines[0]:(title?.innerText?.trim()||lines[0]);
  return {name,count:options.length,method,point:point(first)};
 }
 return null;
}'''

LOCATION_SELECTED_PROBE = r'''(root,{marker,place,query})=>{
 const visible=el=>!!el&&el.getClientRects().length>0&&getComputedStyle(el).visibility!=='hidden';
 const popup=document.querySelector(`[data-juxin-location-results="${marker}"]`),option=document.querySelector(`[data-juxin-location-option="${marker}"]`);
 if(visible(option)||visible(popup)&&popup.innerText?.trim())return null;
 const section=root.querySelector(`[data-juxin-location-section="${marker}"]`),rect=root.__juxinLocation?.rect;
 const near=el=>{if(!rect)return false;const r=el.getBoundingClientRect();return r.right>rect.left&&r.left<rect.right&&r.top>=rect.top-35&&r.bottom<=rect.bottom+45;};
 const fields=section?(section.matches('input')?[section]:[...section.querySelectorAll('input')]):[...root.querySelectorAll('input')].filter(near);
 for(const field of fields){
  if(!visible(field)||field.type==='hidden')continue;
  const value=field.value?.trim();
  if(value===place&&(value!==query||field.readOnly||field.getAttribute('aria-selected')==='true'))return {method:'location_field',value};
 }
 const chips=[...(section||root).querySelectorAll('span,strong,button,[role="button"]')];
 for(const el of chips){
  if(!visible(el)||el.closest('[contenteditable="true"],textarea,[data-juxin-location-results]')||!section&&!near(el))continue;
  if(el.innerText?.trim()===place)return {method:'location_chip',value:place};
 }
 return null;
}'''

# A roleless result panel can replace the entire editing modal. Only accept a
# centred panel inside a fixed overlay, not a matching sentence in the feed.
RESULT_PANEL = r'''el => {
 const r=el.getBoundingClientRect();
 if(r.width<240||r.height<150||r.width>=innerWidth*.95||r.height>=innerHeight*.98)return false;
 if(r.left>innerWidth/2||r.right<innerWidth/2)return false;
 for(let p=el;p&&p!==document.body;p=p.parentElement){if(getComputedStyle(p).position==='fixed')return true;}
 return false;
}'''
