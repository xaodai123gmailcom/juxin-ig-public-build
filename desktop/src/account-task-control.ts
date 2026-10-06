/** Manual operation belongs to the current task lease, never a new account job. */
export type TaskControlInput={id:string;documentUrl:string;taskKey:string;target?:string;grant?:string};
type Dependencies={
  validate:(event:unknown,input:TaskControlInput)=>void;
  session:()=>string|null;
  confirm:(input:TaskControlInput)=>Promise<boolean>;
  hide:(input:TaskControlInput)=>Promise<unknown>;
  request:(path:string,payload:Record<string,unknown>,authorization:string)=>Promise<unknown>;
};
export function createAccountTaskControl(deps:Dependencies){
  const handle=async(event:unknown,input:TaskControlInput,resume:boolean)=>{
    deps.validate(event,input);
    if(typeof input.taskKey!=='string'||!/^[a-f0-9]{24}$/.test(input.taskKey))throw new Error('任务标识无效');
    if(resume){if(typeof input.grant!=='string'||!input.grant||input.grant.length>256)throw new Error('手动操作授权无效');}
    else if(typeof input.target!=='string'||!input.target||input.target.length>128)throw new Error('任务页面标识无效');
    const authorization=deps.session();if(!authorization)throw new Error('请先登录软件');
    const current=()=>{deps.validate(event,input);if(deps.session()!==authorization)throw new Error('登录状态已变化');};
    // Cancels pending presentation and removes input before Core resumes its
    // worker. Core independently revokes and hides under the original lease.
    if(resume){await deps.hide(input);current();}
    else {const confirmed=await deps.confirm(input);current();if(!confirmed)return {cancelled:true};}
    const result=await deps.request(resume?'/api/accounts/interference/resume':'/api/accounts/interference',
      {id:input.id,task_key:input.taskKey,...(resume?{grant:input.grant}:{target:input.target})},authorization);
    current();return result;
  };
  return {begin:(event:unknown,input:TaskControlInput)=>handle(event,input,false),resume:(event:unknown,input:TaskControlInput)=>handle(event,input,true)};
}
