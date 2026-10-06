import {WebContentsView} from 'electron';

/** A separate native input layer; automation still addresses the task page. */
export class TaskWatchShield {
  readonly view:WebContentsView;
  private readonly contents:Electron.WebContents;
  private failed=false;
  private closed=false;
  constructor(onInterfere:()=>void,private onUnavailable:(shield:TaskWatchShield)=>void=()=>{}){
    this.view=new WebContentsView({webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
    this.view.setBackgroundColor('#00000000');
    const wc=this.contents=this.view.webContents;
    wc.setWindowOpenHandler(()=>({action:'deny'}));
    wc.on('will-navigate',(event,url)=>{event.preventDefault();if(this.isUsable()&&url==='https://task-watch.invalid/interfere')onInterfere()});
    wc.on('context-menu',()=>{if(this.isUsable())onInterfere()});
    wc.on('destroyed',()=>this.invalidate());
    wc.on('render-process-gone',()=>this.invalidate());
    wc.on('unresponsive',()=>this.invalidate());
    wc.on('did-fail-load',(_event,code,_description,_url,main)=>{if(main&&code!==-3)this.invalidate()});
    try {
      void wc.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>html,body{margin:0;width:100%;height:100%;background:transparent}a{position:fixed;inset:0;display:block;cursor:default;outline:none}a:focus-visible{outline:2px solid #9a7bff;outline-offset:-3px}</style><a href="https://task-watch.invalid/interfere" aria-label="任务正在执行，点击确认是否手动干涉"></a>`)).catch(()=>this.invalidate());
    } catch { this.invalidate(); }
  }
  private invalidate(){if(this.failed||this.closed)return;this.failed=true;this.onUnavailable(this)}
  isUsable(){return !this.closed&&!this.failed&&!this.contents.isDestroyed()}
  hasFocus(){return !this.contents.isDestroyed()&&this.contents.isFocused()}
  close(){this.closed=true;const wc=this.contents;if(!wc.isDestroyed())wc.close({waitForBeforeUnload:false})}
}
