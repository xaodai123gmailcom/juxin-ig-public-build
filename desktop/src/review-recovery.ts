import {profilePreviewUrl} from './profile-preview.js';

export type ReviewAccountAction='status'|'login'|'home'|'target'|'hide'|'reset';
export function reviewAccountAction(input:unknown):ReviewAccountAction {
 if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).some(k=>k!=='action')||!['status','login','home','target','hide','reset'].includes((input as any).action))throw new Error('审核账号操作无效');
 return (input as {action:ReviewAccountAction}).action;
}
const escape=(value:string)=>value.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
/** Recovery stays in the same review session; links never carry credentials. */
export function reviewRecoveryHtml(message:string,target:string){
 const url=target?profilePreviewUrl(target,'target'):'';
 return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>审核网页暂时无法打开</title><style>body{font:15px/1.7 system-ui;margin:0;padding:28px;background:#101827;color:#e2e8f0}main{max-width:680px;margin:auto}h1{font-size:22px}p{overflow-wrap:anywhere}.actions{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}a{padding:8px 14px;border:1px solid #465777;border-radius:8px;color:#b8d9ff;text-decoration:none}</style></head><body><main><h1>审核网页暂时无法打开</h1><p role="alert">${escape(message)}</p><div class="actions"><a href="https://www.instagram.com/accounts/login/">登录 / 检查</a><a href="https://www.instagram.com/">账号主页 / 切换</a>${url?`<a href="${escape(url)}">重新打开 @${escape(target)}</a>`:''}</div></main></body></html>`;
}
