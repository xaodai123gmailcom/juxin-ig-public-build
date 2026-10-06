import {createContext,useContext,useEffect,useState,useCallback,useMemo,useRef,type ReactNode} from 'react';
import {shareUnchangedJson} from './snapshot-sharing';
import {getCollectorCoreClient} from './core-client';
import './account-unread.css';
type Observation={count:number|null;capped:boolean;status:string;observed_at:string|null};
type Snapshot={windows:Record<string,Observation>;total:number;capped:boolean;unknown:number};
const empty:Snapshot={windows:{},total:0,capped:false,unknown:0};
const Context=createContext({snapshot:empty,refresh:async()=>{}});
export function AccountUnreadProvider({children}:{children:ReactNode}) {
  const [snapshot,setSnapshot]=useState(empty),live=useRef(false),pending=useRef(false);
  const refresh=useCallback(async()=>{
    if(pending.current)return;pending.current=true;
    try{const value=await getCollectorCoreClient().accountUnread<Snapshot>();if(live.current)setSnapshot(previous=>shareUnchangedJson(previous,value))}
    catch{if(live.current)setSnapshot(previous=>shareUnchangedJson(previous,{...previous,windows:Object.fromEntries(Object.entries(previous.windows).map(([id,r])=>[id,{...r,status:r.status==='live'?'stale':r.status}]))}))}
    finally{pending.current=false}
  },[]);
  useEffect(()=>{live.current=true;void refresh();const timer=setInterval(()=>void refresh(),1500);return()=>{live.current=false;clearInterval(timer)}},[refresh]);
  useEffect(()=>window.collectorCore?.onAccountFocus?.(profile=>{sessionStorage.setItem('account-focus-profile',profile);location.hash='#/accounts';window.dispatchEvent(new CustomEvent('account:focus',{detail:profile}))}),[]);
  const value=useMemo(()=>({snapshot,refresh}),[snapshot,refresh]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export const useAccountUnread=()=>useContext(Context);
export function UnreadBadge({profile,inheritTitle=false}:{profile?:string;inheritTitle?:boolean;compact?:boolean}) {
  const {snapshot}=useAccountUnread(),r=profile?snapshot.windows[profile]:undefined;
  const count=profile?r?.count:snapshot.total;
  if(!count)return null;
  const capped=profile?r?.capped:snapshot.capped;
  const stale=profile?r?.status!=='live':Object.values(snapshot.windows).some(x=>x.count!=null&&x.status!=='live');
  const text=`${count}${capped?'+':''}`;
  const title=`${profile?'本窗口':'全部窗口'}未读消息 ${text}${stale?'（含最近一次记录）':''}${!profile&&snapshot.unknown?'；部分窗口待识别':''}${r?.observed_at?' · '+new Date(r.observed_at).toLocaleString():''}`;
  return <span className={`unread-badge ${profile?'window-unread-badge':'total-unread-badge'}`} aria-label={title} title={inheritTitle?undefined:title}>{text}</span>;
}
