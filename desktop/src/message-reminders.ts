/** Only newly observed unread increases trigger reminders; snapshots establish a baseline. */
export class MessageReminders {
  enabled:boolean;
  private counts=new Map<string,number>();
  private exactCounts=new Map<string,boolean>();
  private activities=new Map<string,{epoch:string;serial:number}>();
  constructor(enabled:boolean,private deliver:(profile:string,count:number)=>void,private persist:(enabled:boolean)=>void){this.enabled=enabled}
  setEnabled(enabled:boolean){this.persist(enabled);this.enabled=enabled;this.reset()}
  reset(){this.counts.clear();this.exactCounts.clear();this.activities.clear()}
  forget(profile:string){this.counts.delete(profile);this.exactCounts.delete(profile);this.activities.delete(profile)}
  observe(profile:string,row:{status?:string;count?:number|null;capped?:boolean;activity?:{epoch:string;serial:number}}){
    if(!this.enabled)return;
    if(row.status==='signed_out'){this.forget(profile);return}
    if(row.status!=='live'||!Number.isSafeInteger(row.count)||row.count!<0)return;
    const count=row.count!,old=this.counts.get(profile);this.counts.set(profile,count);
    const oldExact=this.exactCounts.get(profile);this.exactCounts.set(profile,row.capped!==true);
    const activity=row.activity,previous=this.activities.get(profile);
    const valid=activity&&typeof activity.epoch==='string'&&activity.epoch.length<=80&&Number.isSafeInteger(activity.serial)&&activity.serial>=0;
    if(valid)this.activities.set(profile,{...activity});
    if(valid&&previous&&previous.epoch!==activity.epoch)return;
    const changed=valid&&previous&&previous.epoch===activity.epoch&&activity.serial>previous.serial;
    if(old!==undefined&&((count>old&&oldExact!==false&&row.capped!==true)||changed))this.deliver(profile,count);
  }
}
