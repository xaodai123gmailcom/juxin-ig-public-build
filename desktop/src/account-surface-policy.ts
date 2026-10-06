export type SurfaceInput={surfaceId?:string;capture?:boolean;readOnly?:boolean;interferenceGrant?:string;viewTarget?:string;id?:string;visible:boolean;documentUrl?:string;bounds?:{x:number;y:number;width:number;height:number}};
export function surfaceRectangle(input:SurfaceInput,size:{width:number;height:number},zoom=1){
  if(!input||typeof input.visible!=='boolean'||(input.id!==undefined&&(typeof input.id!=='string'||input.id.length>128)))throw new Error('窗口显示请求无效');
  if(!input.visible)return null;
  if(!input.id||!input.bounds)throw new Error('请选择窗口');
  const b=input.bounds;
  if(![b.x,b.y,b.width,b.height,zoom].every(Number.isFinite)||b.width<=0||b.height<=0||zoom<=0)throw new Error('网页显示区域无效');
  const x=Math.max(0,Math.round(b.x*zoom)),y=Math.max(0,Math.round(b.y*zoom));
  const right=Math.min(size.width,Math.round((b.x+b.width)*zoom));
  const bottom=Math.min(size.height,Math.round((b.y+b.height)*zoom));
  if(right-x<100||bottom-y<100)return null;
  return {x,y,width:right-x,height:bottom-y};
}
