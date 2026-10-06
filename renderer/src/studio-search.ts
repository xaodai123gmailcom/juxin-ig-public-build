export type SearchItem={id:string;name:string;preview:string;attribution:string;url:string;media_type:string};
export type SearchRequest={query:string;media_type:string;page:number};
export type SearchResponse={items:SearchItem[];page:number;has_more:boolean;total:number};
export type MediaSearch=SearchRequest & {rows:SearchItem[];seen:string[];hasMore:boolean;total:number};

export function nextMediaSearch(previous:MediaSearch|null,query:string,media_type:string,more:boolean):SearchRequest|null {
  query=query.trim();
  if(!query)return null;
  const continuing=more&&previous?.query===query&&previous.media_type===media_type;
  if(continuing&&!previous.hasMore)return null;
  return {query,media_type,page:continuing?previous.page+1:1};
}

export function acceptMediaSearch(previous:MediaSearch|null,request:SearchRequest,response:SearchResponse):MediaSearch {
  const continuing=request.page>1&&previous?.query===request.query&&previous.media_type===request.media_type;
  const seen=new Set(continuing?previous.seen:[]);
  const rows=response.items.filter(item=>{const key=item.media_type+':'+item.id;if(seen.has(key))return false;seen.add(key);return true});
  return {...request,rows,seen:[...seen],hasMore:response.has_more,total:response.total};
}
