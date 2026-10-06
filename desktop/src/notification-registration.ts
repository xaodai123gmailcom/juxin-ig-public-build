import {mkdirSync} from 'node:fs';
import {join,dirname} from 'node:path';
import {STABLE_APPLICATION_ID,PRODUCT_NAME} from './app-identity.js';

/** Packaged portable apps also need a Start Menu identity for Windows toasts. */
export function ensureNotificationRegistration(input:{platform:string;packaged:boolean;appData:string;executable:string},links:{readShortcutLink:(file:string)=>{target:string;appUserModelId?:string};writeShortcutLink:(file:string,operation:'create',details:{target:string;cwd:string;appUserModelId:string;description:string})=>boolean},mkdir:(path:string)=>void=path=>{mkdirSync(path,{recursive:true})}){
 if(input.platform!=='win32'||!input.packaged)return;
 const programs=join(input.appData,'Microsoft','Windows','Start Menu','Programs');
 for(const name of [PRODUCT_NAME,PRODUCT_NAME+'-消息提醒']){
  try{const link=links.readShortcutLink(join(programs,name+'.lnk'));if(link.appUserModelId===STABLE_APPLICATION_ID&&link.target.toLowerCase()===input.executable.toLowerCase())return}catch{}
 }
 const file=join(programs,PRODUCT_NAME+'-消息提醒.lnk');
 let existing;try{existing=links.readShortcutLink(file)}catch{}
 if(existing&&existing.appUserModelId!==STABLE_APPLICATION_ID)throw new Error('消息提醒快捷方式名称已被占用，请使用安装版注册 Windows 提醒。');
 mkdir(programs);
 if(!links.writeShortcutLink(file,'create',{target:input.executable,cwd:dirname(input.executable),appUserModelId:STABLE_APPLICATION_ID,description:PRODUCT_NAME+'消息提醒'}))throw new Error('无法注册 Windows 消息提醒快捷方式，请使用安装版后重试测试提醒。');
}
