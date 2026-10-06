import {Menu,type BrowserWindow,type MenuItemConstructorOptions} from 'electron';
export type AccountMenuInput={name:string;opened:boolean;disabled:boolean;configDisabled?:boolean;documentUrl:string};
/** Native menu floats above WebContentsView without hiding or resizing it. */
export class AccountContextMenu {
  private current?:Menu;
  close(){this.current?.closePopup();this.current=undefined;}
  show(win:BrowserWindow,input:AccountMenuInput):Promise<string|null>{
    this.close();
    if(!input||input.documentUrl!==win.webContents.getURL())return Promise.resolve(null);
    return new Promise(resolve=>{
      let chosen:string|null=null;
      const action=(label:string,id:string,enabled=true):MenuItemConstructorOptions=>({label,enabled:!input.disabled&&enabled,click:()=>{chosen=id}});
      const menu=Menu.buildFromTemplate([
        {label:String(input.name||'窗口').replace(/[\r\n\t]/g,' ').slice(0,80),enabled:false},
        action('刷新','refresh',input.opened),action('上一页','back',input.opened),action('下一页','forward',input.opened),
        action(input.opened?'关闭':'打开',input.opened?'close':'open'),action('编辑','edit',!input.configDisabled),{type:'separator'},action('删除','delete',!input.configDisabled)
      ]);
      this.current=menu;
      menu.popup({window:win,callback:()=>{if(this.current===menu)this.current=undefined;resolve(chosen)}});
    });
  }
}
