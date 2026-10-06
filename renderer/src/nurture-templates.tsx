import {nurtureScheduleDefaults} from './nurture-schedule';
type Config = Record<string,string|number|boolean|string[]>;
const base = { rounds:1,...nurtureScheduleDefaults,targets:[] as string[],keywords:"",comments:[] as string[],like_probability:0,like_limit:0,save_probability:0,save_limit:0,follow_probability:0,follow_limit:0,comment_probability:0,comment_limit:0 };
export const nurturePresets = [
  { title:"适应期", values:{...base,stage:"适应期",minutes:5,dwell_min:8,dwell_max:20,surfaces:["feed"]} },
  { title:"稳定期", values:{...base,stage:"稳定期",minutes:8,dwell_min:10,dwell_max:25,surfaces:["feed","reels"]} },
  { title:"维护期", values:{...base,stage:"维护期",minutes:10,dwell_min:12,dwell_max:30,surfaces:["feed","stories"]} },
];
export function NurtureTemplates({onApply,busy}:{onApply:(values:Config)=>void;busy:boolean}) {
  return <div className="nurture-presets">{nurturePresets.map((p,i)=><button type="button" className={`nurture-preset preset-${i}`} disabled={busy} key={p.title} onClick={()=>onApply(p.values)}><span>0{i+1} · 预设</span><strong>{p.title}</strong><p>{p.values.minutes} 分钟／轮 · 停留 {p.values.dwell_min}–{p.values.dwell_max} 秒</p><b>应用模板 →</b></button>)}</div>;
}
