"""Crop-ratio discovery scoped to one composer, with no screenshot coordinates."""

CROP_BUTTON = r'''(root, marker) => {
 const clean=value=>String(value||'').normalize('NFKC').replace(/\s+/g,' ').trim();
 const rendered=el=>{
  for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0)return false;}
  return true;
 };
 const visible=el=>{
  if(!el||!rendered(el))return false;
  const r=el.getBoundingClientRect(),s=getComputedStyle(el);
  return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none';
 };
 const label=/^(select crop|choose crop|select aspect ratio|crop options|aspect ratio|crop|选择裁剪|选择裁剪尺寸|选择裁切|選擇裁切|选择纵横比|选择宽高比|选择比例|裁剪尺寸|裁剪比例|裁切比例|裁剪|裁切|長寬比|宽高比|比例)$/i;
 const name=el=>{
  const refs=el.getAttribute('aria-labelledby');
  if(refs)return clean(refs.split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '))||'(referenced label)';
  return clean(el.getAttribute('aria-label')||el.getAttribute('title')||el.querySelector('title')?.textContent||el.innerText);
 };
 const control=el=>{
  let target=el.closest('button,[role="button"],[tabindex]');
  if(!target){for(let p=el.parentElement;p&&p!==root;p=p.parentElement){if(getComputedStyle(p).cursor==='pointer'){target=p;break;}}}
  if(!target||target===root||!root.contains(target)||!visible(target)||target.matches(':disabled')||target.closest('[aria-disabled="true"],[inert]'))return null;
  const r=target.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
  return hit&&(hit===target||target.contains(hit))?target:null;
 };
 const labelled=[],icons=[],labels=[];
 const rejected={labelled:0,not_actionable:0,wrong_size:0,outside_photo:0,wrong_shape:0};
 for(const el of root.querySelectorAll('button,[role="button"],svg,[aria-label],[aria-labelledby],[title]')){
  if(!visible(el))continue;
  const text=name(el);
  if(text&&labels.length<16&&!labels.includes(text))labels.push(text.slice(0,80));
  if(!label.test(text))continue;
  const target=control(el);
  if(target&&!labelled.includes(target))labelled.push(target);
 }
 // Some translated composers expose only the two opposing crop corners. Use
 // the rendered vector shape plus its position over the photo, never color,
 // an unscoped "first SVG", a stored screen coordinate or image recognition.
 const corners=svg=>{
  const points=[];
  for(const shape of svg.querySelectorAll('path,line,polyline,polygon,rect,circle,ellipse')){
   if(typeof shape.getTotalLength!=='function'||!shape.getClientRects().length||shape.closest('defs,clipPath,mask,pattern,marker,symbol'))continue;
   const style=getComputedStyle(shape);
   if(!rendered(shape)||(style.fill==='none'&&style.stroke==='none'))continue;
   const length=shape.getTotalLength(),matrix=shape.getScreenCTM();if(!length||!matrix)continue;
   for(let i=0;i<=64;i++){const point=shape.getPointAtLength(length*i/64);points.push(new DOMPoint(point.x,point.y).matrixTransform(matrix));}
  }
  if(points.length<2)return false;
  const xs=points.map(p=>p.x),ys=points.map(p=>p.y),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys),w=x1-x0,h=y1-y0;
  if(w<4||h<4||w/h<.75||w/h>1.33)return false;
  const normalized=points.map(p=>({x:(p.x-x0)/w,y:(p.y-y0)/h}));
  const arms=[p=>p.x<.10&&p.y>.55,p=>p.y>.90&&p.x<.45,p=>p.x>.90&&p.y<.45,p=>p.y<.10&&p.x>.55];
  return normalized.filter(p=>arms.some(test=>test(p))).length/normalized.length>.96&&arms.every(test=>normalized.some(test));
 };
 const cropStage=[...root.querySelectorAll('h1,h2,h3,div,span')].some(el=>visible(el)&&/^(Crop|裁剪|裁切)$/i.test(clean(el.innerText)));
 let mediaCount=0;
 if(!labelled.length&&cropStage){
  const media=[...root.querySelectorAll('img,canvas')].filter(visible).map(el=>el.getBoundingClientRect()).filter(r=>r.width>=160&&r.height>=160);
  mediaCount=media.length;
  for(const svg of root.querySelectorAll('svg')){
   if(!visible(svg))continue;
   const target=control(svg);if(!target){rejected.not_actionable++;continue;}
   // An explicit different purpose, such as Zoom, must beat a shape guess.
   if(name(svg)||name(target)){rejected.labelled++;continue;}
   const r=target.getBoundingClientRect(),s=svg.getBoundingClientRect(),cx=r.x+r.width/2,cy=r.y+r.height/2;
   if(r.width<16||r.width>64||r.height<16||r.height>64||s.width<8||s.width>40||s.height<8||s.height>40){rejected.wrong_size++;continue;}
   if(!media.some(m=>cx>=m.left&&cx<=m.left+m.width*.32&&cy>=m.top+m.height*.68&&cy<=m.bottom)){rejected.outside_photo++;continue;}
   if(!corners(svg)){rejected.wrong_shape++;continue;}
   if(!icons.includes(target))icons.push(target);
  }
 }
 const candidates=labelled.length?labelled:icons;
 root.querySelectorAll('[data-juxin-crop]').forEach(el=>el.removeAttribute('data-juxin-crop'));
 if(candidates.length===1)candidates[0].setAttribute('data-juxin-crop',marker);
 return {count:candidates.length,method:candidates.length===1?(labelled.length?'crop_label':'crop_corner_icon'):null,labelled_count:labelled.length,icon_count:icons.length,crop_stage:cropStage,photo_count:mediaCount,rejected,labels};
}'''
