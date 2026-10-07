"""Read-only signed-in navigation evidence; never discovers publishing controls."""
ACCOUNT_SESSION_PROBE = r'''() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && s.display !== 'none' && s.visibility !== 'hidden';
  };
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
  const navLabels=[];
  for(const icon of document.querySelectorAll('svg')){
    if(visible(icon) && navFor(icon))navLabels.push(String(icon.getAttribute('aria-label')||'(icon)'));
  }
  const login=[...document.querySelectorAll('input[type="password"]')].some(visible);
  return {nav_labels:navLabels.slice(0,24),login};
}'''
