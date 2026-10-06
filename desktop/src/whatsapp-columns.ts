/** Serialized into WhatsApp's isolated world. The archive/welcome combination
 * has neither #side nor #main, so retain structural anchors across navigation. */
export function findWhatsAppColumns(previous?:{left:HTMLElement;right:HTMLElement;parent:HTMLElement}|null) {
  const bounds=(node:Element)=>node.getBoundingClientRect();
  const shown=(node:Element)=>{const r=bounds(node),s=getComputedStyle(node);return node.isConnected&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const valid=(left:HTMLElement,right:HTMLElement,parent:HTMLElement)=>{
    const s=getComputedStyle(parent),a=bounds(left),b=bounds(right),grid=['grid','inline-grid'].includes(s.display);
    if((!grid&&(!['flex','inline-flex'].includes(s.display)||!['row','row-reverse'].includes(s.flexDirection)))||!shown(left)||!shown(right))return null;
    if(a.width<140||b.width<200||Math.abs(a.right-b.left)>16||Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)<200)return null;
    // Positioned drawers are not flex/grid tracks. Fit them to the underlying
    // column separately, instead of using them as the column itself.
    if([left,right].some(n=>['absolute','fixed'].includes(getComputedStyle(n).position)))return null;
    return {left,right,parent,grid};
  };
  const fromRight=(start:HTMLElement)=>{
    for(let right=start;right.parentElement&&right.parentElement!==document.body;right=right.parentElement){
      const parent=right.parentElement;
      for(const sibling of parent.children)if(sibling instanceof HTMLElement&&sibling!==right){const found=valid(sibling,right,parent);if(found)return found;}
    }
    return null;
  };
  const fromLeft=(start:HTMLElement)=>{
    for(let left=start;left.parentElement&&left.parentElement!==document.body;left=left.parentElement){
      const parent=left.parentElement;
      for(const sibling of parent.children)if(sibling instanceof HTMLElement&&sibling!==left){const found=valid(left,sibling,parent);if(found)return found;}
    }
    return null;
  };
  const main=document.querySelector<HTMLElement>('#main,[data-testid="conversation-panel-wrapper"]');
  const side=document.querySelector<HTMLElement>('#side,#pane-side');
  if(main){const found=fromRight(main);if(found)return found;}
  // Keep the welcome pane as an anchor when the list is replaced by Archive.
  if(previous?.right.isConnected){const found=fromRight(previous.right);if(found)return found;}
  if(side){const found=fromLeft(side);if(found)return found;}
  if(previous?.left.isConnected){const found=fromLeft(previous.left);if(found)return found;}
  // Also handle opening the app directly on Archive, and React replacing both
  // columns. Only visit large structural shells, never individual message rows.
  const root=document.querySelector<HTMLElement>('#app')||document.body;
  const queue=[{node:root,depth:0}];let inspected=0;
  while(queue.length&&inspected++<100){
    const {node,depth}=queue.shift()!,r=bounds(node);
    if(!shown(node)||r.width<innerWidth*.65||r.height<Math.min(400,innerHeight*.65))continue;
    const children=Array.from(node.children).filter((n):n is HTMLElement=>n instanceof HTMLElement&&shown(n));
    const tracks=children.filter(n=>{const b=bounds(n);return !['absolute','fixed'].includes(getComputedStyle(n).position)&&b.width>=140&&Math.abs(b.top-r.top)<20&&Math.abs(b.bottom-r.bottom)<20;}).sort((a,b)=>bounds(a).left-bounds(b).left);
    for(let i=0;i<tracks.length-1;i++){
      const found=valid(tracks[i],tracks[i+1],node);if(found)return found;
    }
    if(depth<7)for(const child of children.slice(0,30))queue.push({node:child,depth:depth+1});
  }
  return null;
}

/** Some archive/search revisions use an absolutely positioned left drawer.
 * Match only full-height overlays occupying the existing left column. */
export function findWhatsAppDrawers(columns:{left:HTMLElement;right:HTMLElement;parent:HTMLElement},nativeWidth:number) {
  const a=columns.left.getBoundingClientRect(),result:HTMLElement[]=[];
  const queue=[{node:columns.parent,depth:0}];let count=0;
  while(queue.length&&count++<80){
    const {node,depth}=queue.shift()!,r=node.getBoundingClientRect();
    if(!node.isConnected||r.height<200||Math.abs(r.top-a.top)>16||Math.abs(r.bottom-a.bottom)>16)continue;
    if(node===columns.right||node.getAttribute('role')==='dialog'||node.getAttribute('aria-modal')==='true')continue;
    const style=getComputedStyle(node);if(style.display==='none'||style.visibility==='hidden')continue;
    if(node!==columns.left&&node!==columns.parent&&['absolute','fixed'].includes(style.position)&&Math.abs(r.left-a.left)<=3&&r.width>=140&&Math.min(Math.abs(r.width-nativeWidth),Math.abs(r.width-a.width))<=16)result.push(node);
    if(depth<6)for(const child of Array.from(node.children).slice(0,30))if(child instanceof HTMLElement)queue.push({node:child,depth:depth+1});
  }
  return result;
}
