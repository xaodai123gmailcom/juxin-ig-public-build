import type {ContextMenuParams, MenuItemConstructorOptions} from 'electron';

export function webPageMenu(params:Pick<ContextMenuParams,'selectionText'|'isEditable'|'editFlags'|'linkURL'|'srcURL'|'mediaType'|'x'|'y'>,
  run:(action:string,value?:string)=>void, valid:()=>boolean):MenuItemConstructorOptions[] {
  const item=(label:string,action:string,enabled=true,value?:string):MenuItemConstructorOptions=>({label,enabled,click:()=>{if(valid())run(action,value)}});
  const rows:MenuItemConstructorOptions[]=[];
  if(params.selectionText)rows.push(item('复制选中文字','copy-text',true,params.selectionText));
  if(params.isEditable){
    rows.push(item('撤销','undo',params.editFlags.canUndo),item('重做','redo',params.editFlags.canRedo),
      item('剪切','cut',params.editFlags.canCut),item('复制','copy',params.editFlags.canCopy),
      item('粘贴','paste',params.editFlags.canPaste),item('全选','selectAll',params.editFlags.canSelectAll));
  }else if(!params.selectionText)rows.push(item('复制此处消息','copy-message'));
  if(/^https?:\/\//i.test(params.linkURL))rows.push(item('复制链接地址','copy-text',true,params.linkURL));
  if(params.mediaType==='image'){
    rows.push(item('复制图片','copy-image'));
    if(/^(https?:|blob:)/i.test(params.srcURL))rows.push(item('图片另存为…','save-image',true,params.srcURL));
  }
  rows.push({type:'separator'},item('上一页','back'),item('下一页','forward'),item('刷新网页','refresh'));
  return rows;
}

/** Find only the clicked message, never copy the entire conversation/sidebar. */
export function contextMessage(x:number,y:number) {
  const clicked=document.elementFromPoint(x,y);
  if(!clicked||clicked.closest('input,textarea,[contenteditable="true"],nav,aside,button'))return '';
  const node=clicked.closest('.selectable-text,[data-juxin-chat-translation],.juxin-chat-translation,div[dir="auto"],span[dir="auto"]');
  return (node?.textContent||'').trim().slice(0,20000);
}
