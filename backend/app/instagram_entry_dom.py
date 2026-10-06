"""DOM probes for Instagram's icon-only sidebar; no screen coordinates."""
ENTRY_PROBE = r'''({marker}) => {
  const clean = value => String(value || '').normalize('NFKC').replace(/\s+/g, ' ').trim();
  const createName = /^(create|new post|create new post|创建|建立|新建|创建新帖子|新建帖子|新增貼文|建立新貼文|新貼文)$/i;
  const uploadName = /^(select from computer|从电脑上选择|从电脑中选择|从计算机选择|從電腦選擇|從電腦中選擇)$/i;
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const label = el => clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.querySelector('title')?.textContent);
  const navFor = el => {
    for(let p=el.parentElement;p && p!==document.body;p=p.parentElement){
      const r=p.getBoundingClientRect();
      if(r.width>Math.min(330,innerWidth*.34) || r.height<innerHeight*.4 || r.left>innerWidth*.2)continue;
      const routes=new Set([...p.querySelectorAll('a[href]')].filter(visible).map(a=>{
        try { const u=new URL(a.href,location.href);return /^(www\.)?instagram\.com$/.test(u.hostname) && /^\/(?:$|explore\/|reels\/|direct\/|accounts\/activity\/)/.test(u.pathname)?u.pathname.split('/')[1]||'home':'';}catch{return '';}
      }).filter(Boolean));
      if(routes.size>=2 || (p.matches('nav,[role="navigation"],aside') && p.querySelectorAll('svg').length>=3))return p;
    }
    return null;
  };
  const isPlus = svg => {
    const samples=[];
    for(const shape of svg.querySelectorAll('path,line,polyline,polygon,rect,circle,ellipse')){
      if(typeof shape.getTotalLength!=='function')continue;
      const length=shape.getTotalLength();if(!length)continue;
      for(let i=0;i<=40;i++){const p=shape.getPointAtLength(length*i/40);samples.push(p);}
    }
    if(samples.length<2)return false;
    const xs=samples.map(p=>p.x),ys=samples.map(p=>p.y),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const w=x1-x0,h=y1-y0,cx=(x0+x1)/2,cy=(y0+y1)/2;
    if(w<3 || h<3 || w/h<.65 || w/h>1.5)return false;
    const onAxis=p=>Math.abs(p.x-cx)<w*.12 || Math.abs(p.y-cy)<h*.12;
    if(samples.filter(onAxis).length/samples.length<.90)return false;
    return samples.some(p=>p.x<x0+w*.15 && Math.abs(p.y-cy)<h*.12) && samples.some(p=>p.x>x1-w*.15 && Math.abs(p.y-cy)<h*.12)
      && samples.some(p=>p.y<y0+h*.15 && Math.abs(p.x-cx)<w*.12) && samples.some(p=>p.y>y1-h*.15 && Math.abs(p.x-cx)<w*.12);
  };
  const candidates=[],navLabels=[];
  for(const icon of document.querySelectorAll('svg')){
    if(!visible(icon))continue;
    const nav=navFor(icon);if(!nav)continue;
    const name=label(icon);if(navLabels.length<24)navLabels.push(name.slice(0,120)||'(无名称图标)');
    const rect=icon.getBoundingClientRect();
    if(rect.width<10 || rect.width>52 || rect.height<10 || rect.height>52)continue;
    const control=icon.closest('a,button,[role="button"],[role="link"],[tabindex]') || icon;
    const semantic=createName.test(name)||createName.test(label(control))||createName.test(clean(control.textContent));
    if(semantic || isPlus(icon))candidates.push({el:control,method:semantic?'sidebar_label':'sidebar_plus_shape',name});
  }
  // The sidebar can render the plus as text instead of SVG.
  for(const el of document.querySelectorAll('button,[role="button"],a,span')){
    if(el.childElementCount || !/^[+＋]$/.test(clean(el.textContent)) || !visible(el) || !navFor(el))continue;
    candidates.push({el:el.closest('a,button,[role="button"],[tabindex]')||el,method:'sidebar_plus_text',name:'+'});
  }
  const unique=candidates.filter((c,i)=>candidates.findIndex(x=>x.el===c.el)===i);
  if(marker && unique.length===1)unique[0].el.setAttribute('data-juxin-create-entry',marker);
  const upload=[...document.querySelectorAll('button,[role="button"],a,span')].some(el=>visible(el)&&uploadName.test(clean(el.textContent)));
  const login=[...document.querySelectorAll('input[type="password"]')].some(visible);
  return {entry_count:unique.length,method:unique.length===1?unique[0].method:null,nav_labels:navLabels,
    upload,login,active:document.visibilityState==='visible',page_path:location.pathname};
}'''

# An optional create menu may include icons in its accessible name. Match the
# visible Post label inside the menu/sidebar, not an exact combined aria name.
POST_MENU_PROBE = r'''({marker}) => {
  const clean = value => String(value || '').normalize('NFKC').replace(/\s+/g, ' ').trim();
  const names = /^(Post|帖子|发帖|貼文)$/i;
  const entry = document.querySelector(`[data-juxin-create-entry="${marker}"]`);
  const visible = el => {
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>0 && r.height>0 && r.top<innerHeight && r.bottom>0 && r.left<innerWidth && r.right>0 && s.visibility!=='hidden' && s.display!=='none';
  };
  const candidates=[];
  for(const label of document.querySelectorAll('a,button,[role="menuitem"],[role="button"],span,div')) {
    if(!names.test(clean(label.textContent)) || !visible(label))continue;
    const control=label.closest('a,button,[role="menuitem"],[role="button"],[role="link"],[tabindex]');
    if(!control || !visible(control) || control.closest('[role="dialog"],main,article') || control.matches(':disabled,[aria-disabled="true"]'))continue;
    let scoped=Boolean(control.closest('[role="menu"],nav,aside,[role="navigation"]'));
    if(!scoped && entry) {
      for(let p=control.parentElement;p && p!==document.body;p=p.parentElement) {
        const r=p.getBoundingClientRect();
        if(p.contains(entry) && r.width<=Math.min(360,innerWidth*.35) && r.left<innerWidth*.25) {scoped=true;break;}
      }
    }
    if(scoped && !candidates.includes(control))candidates.push(control);
  }
  document.querySelectorAll('[data-juxin-post-menu]').forEach(el=>el.removeAttribute('data-juxin-post-menu'));
  if(candidates.length===1)candidates[0].setAttribute('data-juxin-post-menu',marker);
  return {count:candidates.length};
}'''
