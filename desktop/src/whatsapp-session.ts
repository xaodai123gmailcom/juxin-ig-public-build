import {createHash, randomBytes} from 'node:crypto';
import {existsSync, lstatSync, mkdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {writePrivateFileAtomically} from './atomic-secure-store.js';

const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** WhatsApp has its own persistent profile. The old account partition is never
 * opened, copied, cleared or deleted by this module. */
export function whatsappSessionPath(root:string,owner:string,profile:string,rotate=false){
  if(!uuid.test(owner)||!profile.startsWith('native:')||!uuid.test(profile.slice(7)))throw new Error('WhatsApp 账号标识无效');
  const key=createHash('sha256').update(owner.toLowerCase()+'\0'+profile.toLowerCase()).digest('hex').slice(0,32);
  const directory=join(root,'WA');
  if(existsSync(directory)&&lstatSync(directory).isSymbolicLink())throw new Error('WhatsApp 存储目录不可用');
  mkdirSync(directory,{recursive:true,mode:0o700});
  const marker=join(directory,key+'.json');let active=key;
  if(existsSync(marker)){
    if(lstatSync(marker).isSymbolicLink())throw new Error('WhatsApp 存储记录不可用');
    const saved=JSON.parse(readFileSync(marker,'utf8'));
    if(saved.owner!==owner||saved.profile!==profile||typeof saved.active!=='string'||!new RegExp('^'+key+'(?:-[0-9a-f]{16})?$').test(saved.active))throw new Error('WhatsApp 存储记录不匹配');
    active=saved.active;
  }
  if(rotate)active=key+'-'+randomBytes(8).toString('hex');
  const path=join(directory,active);
  if(existsSync(path)&&lstatSync(path).isSymbolicLink())throw new Error('WhatsApp 存储目录不可用');
  mkdirSync(path,{recursive:true,mode:0o700});
  if(!existsSync(marker)||rotate)writePrivateFileAtomically(marker,JSON.stringify({schema:1,owner,profile,active}));
  return path;
}

export function whatsappErrorText(value:unknown){
  return String(value??'').slice(0,12000)
    .replace(/\b(?:https?|wss?):\/\/[^\s<>"']+/gi,raw=>{try{const u=new URL(raw);return u.origin+u.pathname}catch{return '[url]'}})
    .replace(/[A-Z]:\\Users\\[^\\\s]+/gi,'[USER]')
    .replace(/\bBearer\s+\S+/gi,'Bearer [redacted]')
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi,'[email]')
    .replace(/\b[A-Za-z0-9_+/=-]{64,}\b/g,'[long-value]')
    .replace(/\b\d{8,}\b/g,'[number]').slice(0,6000);
}
