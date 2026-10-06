import {contextBridge,ipcRenderer} from 'electron';
// This preload is never attached to account or task webContents.
contextBridge.exposeInIsolatedWorld(1009,'__juxinImmersiveBridge',{
 storage:(op:string,key:string,value?:unknown)=>ipcRenderer.invoke('immersive:storage',{op,key,value}),
 request:(input:unknown)=>ipcRenderer.invoke('immersive:request',input),
 abort:(id:string)=>ipcRenderer.invoke('immersive:abort',id),
 open:(url:string)=>ipcRenderer.invoke('immersive:open',url)
});
