/** Serialized into WhatsApp's isolated world. Only suppress full-height paint
 * at the native split; never add an opaque mask, hide content or remove widths. */
export function whatsappBoundaryCleanup() {
  type Box={left:number;right:number;top:number;bottom:number;width:number;height:number};
  type Stroke={node:HTMLElement;pseudo:string;edge:'left'|'right'|'fill';property:string;value:string};
  const attribute='data-juxin-wa-native-boundary';
  let strokes:Stroke[]=[],sheet:HTMLStyleElement|null=null;
  let pair:{left:HTMLElement;right:HTMLElement;parent:HTMLElement;nativeRatio:number}|null=null;
  const marked=new Map<HTMLElement,{token:string;previous:string|null}>();
  const px=(value:string,span:number):number|null=>{
    const source=value.trim().replace(/^calc\((.*)\)$/,'$1').replace(/\s+/g,'');
    if(source==='0')return 0;
    const terms=source.match(/[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:px|%)/g);
    if(!terms||terms.join('')!==source)return null;
    const sum=terms.reduce((n,term)=>n+parseFloat(term)*(term.endsWith('%')?span/100:1),0);
    return Number.isFinite(sum)?sum:null;
  };
  const visibleColor=(color:string)=>!!color&&color!=='transparent'&&!/rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(color)&&!/\/\s*0(?:\.0+)?\s*\)$/.test(color);
  const translation=(transform:string,width:number,height:number):{x:number;y:number}|null=>{
    if(!transform||transform==='none')return {x:0,y:0};
    const matrix=transform.match(/^matrix\(([^)]+)\)$/);
    if(matrix){const v=matrix[1].split(',').map(Number);return v.length===6&&v.every(Number.isFinite)&&v[0]===1&&v[1]===0&&v[2]===0&&v[3]===1?{x:v[4],y:v[5]}:null;}
    const move=transform.match(/^translate(X|Y)?\(([^)]+)\)$/);
    if(!move)return null;
    const values=move[2].trim().split(/,\s*|\s+/);
    const x=move[1]==='Y'?0:px(values[0],width),y=move[1]==='X'?0:px(move[1]==='Y'?values[0]:values[1]||'0',height);
    return x===null||y===null?null:{x,y};
  };
  const box=(node:HTMLElement,pseudo:string):Box|null=>{
    if(!node.isConnected)return null;
    const style=getComputedStyle(node,pseudo||null);
    if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity||1)===0)return null;
    if(!pseudo)return node.getBoundingClientRect();
    if(!['""',"''"].includes(style.content)||!['absolute','fixed'].includes(style.position))return null;
    let containing=node;
    while(getComputedStyle(containing).position==='static'&&containing.parentElement)containing=containing.parentElement;
    const anchor=style.position==='fixed'?{left:0,top:0,width:innerWidth,height:innerHeight}:containing.getBoundingClientRect();
    const l=px(style.left,anchor.width),r=px(style.right,anchor.width),t=px(style.top,anchor.height),b=px(style.bottom,anchor.height);
    let width=px(style.width,anchor.width),height=px(style.height,anchor.height);
    const horizontal=['borderLeftWidth','borderRightWidth','paddingLeft','paddingRight'].reduce((n,k)=>n+parseFloat((style as any)[k]||'0'),0);
    const vertical=['borderTopWidth','borderBottomWidth','paddingTop','paddingBottom'].reduce((n,k)=>n+parseFloat((style as any)[k]||'0'),0);
    if(width===null){if(l!==null&&r!==null)width=anchor.width-l-r-(style.boxSizing==='border-box'?0:horizontal);else if(style.width==='auto')width=0;}
    if(height===null&&t!==null&&b!==null)height=anchor.height-t-b-(style.boxSizing==='border-box'?0:vertical);
    if(width===null||height===null)return null;
    if(style.boxSizing!=='border-box'){width+=horizontal;height+=vertical;}
    else {width=Math.max(width,horizontal);height=Math.max(height,vertical);}
    const ml=px(style.marginLeft||'0',anchor.width),mr=px(style.marginRight||'0',anchor.width),mt=px(style.marginTop||'0',anchor.width),mb=px(style.marginBottom||'0',anchor.width);
    if([ml,mr,mt,mb].some(v=>v===null))return null;
    const offset=translation(style.transform,width,height);if(!offset)return null;
    const independent=translation(style.translate&&style.translate!=='none'?'translate('+style.translate+')':'none',width,height);if(!independent)return null;
    const x=l!==null?anchor.left+l+ml!:r!==null?anchor.left+anchor.width-r-width-mr!:null;
    const y=t!==null?anchor.top+t+mt!:b!==null?anchor.top+anchor.height-b-height-mb!:null;
    if(x===null||y===null||![x,y,width,height].every(Number.isFinite))return null;
    const left=x+offset.x+independent.x,top=y+offset.y+independent.y;
    return {left,right:left+width,top,bottom:top+height,width,height};
  };
  const clear=()=>{
    sheet?.remove();sheet=null;strokes=[];pair=null;
    for(const [node,item] of marked)if(node.getAttribute(attribute)===item.token){
      if(item.previous===null)node.removeAttribute(attribute);else node.setAttribute(attribute,item.previous);
    }
    marked.clear();
  };
  const fullHeight=(rect:Box,top:number,bottom:number)=>bottom-top>=200&&Math.abs(rect.top-top)<=8&&Math.abs(rect.bottom-bottom)<=8;
  const remember=(stroke:Stroke)=>{if(!strokes.some(s=>s.node===stroke.node&&s.pseudo===stroke.pseudo&&s.property===stroke.property))strokes.push(stroke);};
  // A zero-blur horizontal offset casts a one/two-pixel vertical stroke. Do not
  // alter blurred, multi-layer or top/bottom shadows on ordinary content.
  const shadowEdge=(value:string):'left'|'right'|null=>{
    if(!value||value==='none')return null;
    const colors=value.match(/(?:rgba?|hsla?)\([^)]*\)/g)||[];
    if(colors.length>1||colors.some(c=>!visibleColor(c)))return null;
    const plain=value.replace(/(?:rgba?|hsla?)\([^)]*\)/g,'').trim();
    if(plain.includes(','))return null;
    const nums=plain.match(/[+-]?(?:\d+(?:\.\d*)?|\.\d+)px/g)?.map(parseFloat);
    if(!nums||nums.length<2||nums.length>4||!nums.every(Number.isFinite)||Math.abs(nums[0])<.1||Math.abs(nums[0])>2||nums.slice(1).some(n=>n!==0))return null;
    const inset=/\binset\b/.test(plain);
    return (inset?nums[0]>0:nums[0]<0)?'left':'right';
  };
  const rescan=()=>{
    if(!pair)return;
    const a=pair.left.getBoundingClientRect(),b=pair.right.getBoundingClientRect(),p=pair.parent.getBoundingClientRect();
    const split=p.left+p.width*pair.nativeRatio,top=Math.max(a.top,b.top),bottom=Math.min(a.bottom,b.bottom);
    if(bottom-top<200)return;
    strokes=strokes.filter(s=>s.node.isConnected);
    const candidates=new Set<HTMLElement>(),queue:{node:HTMLElement;depth:number}[]=[];
    let ancestor:HTMLElement|null=pair.parent;
    for(let depth=0;ancestor&&depth<5;depth++,ancestor=ancestor.parentElement){queue.push({node:ancestor,depth:0});if(ancestor===document.body)break;}
    // Structural descendants may own the paint, including late-mounted archive
    // shells. Follow only full-height shells and cap work independently of the
    // number of messages; pointer moves only remeasure remembered strokes.
    let inspected=0;
    while(queue.length&&inspected++<140){
      const {node,depth}=queue.shift()!;if(candidates.has(node))continue;
      const rect=box(node,'');if(!rect)continue;candidates.add(node);
      if(depth<6&&(fullHeight(rect,top,bottom)||node===pair.parent||node===document.body)){
        for(const child of Array.from(node.children).slice(0,40))if(child instanceof HTMLElement)queue.push({node:child,depth:depth+1});
      }
    }
    for(const node of candidates)for(const pseudo of ['', '::before','::after']){
      const rect=box(node,pseudo);if(!rect||!fullHeight(rect,top,bottom))continue;
      const style=getComputedStyle(node,pseudo||null);
      for(const edge of ['left','right'] as const){
        const title=edge==='left'?'Left':'Right',width=parseFloat((style as any)['border'+title+'Width']);
        if(width>0&&width<=2&&Math.abs(rect[edge]-split)<=3&&['solid','dashed','dotted'].includes((style as any)['border'+title+'Style'])&&visibleColor((style as any)['border'+title+'Color'])){
          remember({node,pseudo,edge,property:'border-'+edge+'-color',value:'transparent'});
        }
      }
      const empty=pseudo||(!node.children.length&&!node.textContent?.trim()&&!node.hasAttribute('role')&&!node.hasAttribute('tabindex')&&!node.hasAttribute('href'));
      if(empty&&rect.width>0&&rect.width<=3&&Math.abs((rect.left+rect.right)/2-split)<=3){
        if(visibleColor(style.backgroundColor))remember({node,pseudo,edge:'fill',property:'background-color',value:'transparent'});
        if(style.backgroundImage&&style.backgroundImage!=='none')remember({node,pseudo,edge:'fill',property:'background-image',value:'none'});
      }
      const edge=shadowEdge(style.boxShadow);
      if(edge&&Math.abs(rect[edge]-split)<=3)remember({node,pseudo,edge,property:'box-shadow',value:'none'});
    }
  };
  const capture=(left:HTMLElement,right:HTMLElement,parent:HTMLElement)=>{
    clear();const a=left.getBoundingClientRect(),b=right.getBoundingClientRect(),p=parent.getBoundingClientRect();
    if(!p.width||!fullHeight(a,Math.max(a.top,b.top),Math.min(a.bottom,b.bottom)))return;
    pair={left,right,parent,nativeRatio:((a.right+b.left)/2-p.left)/p.width};rescan();
  };
  const sync=(split:number)=>{
    if(!pair)return;
    const a=pair.left.getBoundingClientRect(),b=pair.right.getBoundingClientRect(),top=Math.max(a.top,b.top),bottom=Math.min(a.bottom,b.bottom);
    const rules:string[]=[];
    for(const stroke of strokes){
      const rect=box(stroke.node,stroke.pseudo);if(!rect||!fullHeight(rect,top,bottom))continue;
      const x=stroke.edge==='fill'?(rect.left+rect.right)/2:rect[stroke.edge];
      if(Math.abs(x-split)<=3)continue;
      // If the site repurposes a thin paint element into content, release it.
      if(stroke.edge==='fill'&&(rect.width>3||(!stroke.pseudo&&(stroke.node.children.length||stroke.node.textContent?.trim()))))continue;
      let item=marked.get(stroke.node);
      if(!item){item={token:String(marked.size+1),previous:stroke.node.getAttribute(attribute)};marked.set(stroke.node,item);stroke.node.setAttribute(attribute,item.token);}
      if(stroke.node.getAttribute(attribute)!==item.token)continue;
      rules.push(`[${attribute}="${item.token}"]${stroke.pseudo}{${stroke.property}:${stroke.value}!important}`);
    }
    const css=rules.join('\n');if(!css){sheet?.remove();return;}
    if(!sheet)sheet=document.createElement('style');
    if(sheet.textContent!==css)sheet.textContent=css;
    if(!sheet.isConnected)document.head.append(sheet);
  };
  return {capture,rescan,sync,clear};
}
