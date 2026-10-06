/** Startup failures must not put healthy translation providers into cooldown. */
export class ImmersiveStartupError extends Error {
 readonly retryAfterMs=3000;
 constructor(readonly phase:string,readonly reason:string){
  super(`沉浸式翻译启动失败（${phase} / ${reason}），请关闭插件设置后重试；详细原因已记录到日志`);
  this.name='ImmersiveStartupError';
 }
}
// Never copy vendor messages, URLs, account text or configuration values into logs.
export function startupReason(error:unknown){
 if(error instanceof ImmersiveStartupError)return error.reason;
 const code=(error as {code?:unknown})?.code;
 if(typeof code==='number'&&Number.isFinite(code))return String(code);
 if(typeof code==='string'&&/^(?:ERR_|E)[A-Z0-9_]{1,60}$/.test(code))return code;
 return error instanceof TypeError?'TypeError':error instanceof SyntaxError?'SyntaxError':'execution-failed';
}
export async function startupStep<T>(phase:string,run:()=>Promise<T>,timeoutMs=5000):Promise<T>{
 let timer:ReturnType<typeof setTimeout>|undefined;
 try{return await Promise.race([Promise.resolve().then(run),new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new ImmersiveStartupError(phase,'timeout')),timeoutMs)})])}
 catch(error){if(error instanceof ImmersiveStartupError)throw error;throw new ImmersiveStartupError(phase,startupReason(error))}
 finally{clearTimeout(timer)}
}
