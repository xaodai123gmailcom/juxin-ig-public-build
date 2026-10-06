import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const {stripTypeScriptTypes}=await import('node:module');
const source=readFileSync(new URL('../src/chat-translation-page.ts',import.meta.url),'utf8');
const pageCode=typeof stripTypeScriptTypes==='function'?stripTypeScriptTypes(source,{mode:'transform'}).replace(/^export /m,''):('const chatTranslationPage='+String((await import('../../dist-electron/chat-translation-page.js')).chatTranslationPage));

// Small DOM tree with selector/ancestor behavior, not a captured WhatsApp DOM.
// Assertions exercise the real page adapter; native Electron layout remains in
// the Windows integration gate. No account is opened and no message is sent.
function selectorParts(selector){
 const result=[];let value='',depth=0,quote='';
 for(const char of selector){if(quote){value+=char;if(char===quote)quote='';continue}if(char==='"'||char==="'"){quote=char;value+=char;continue}if(char==='[')depth++;if(char===']')depth--;if(/\s/.test(char)&&!depth){if(value){result.push(value);value=''}}else value+=char}
 if(value)result.push(value);return result;
}
class Element {
 constructor(tag='div',attributes={},text=''){this.tagName=tag;this.attributes={...attributes};this.ownText=text;this.children=[];this.parentElement=null;this.style={};this.dataset=new Proxy({}, {set:(_obj,key,value)=>{this.attributes['data-'+String(key).replace(/[A-Z]/g,c=>'-'+c.toLowerCase())]=value;return true}});this.nodeType=1}
 getAttribute(name){return this.attributes[name]??null}
 get childNodes(){return [...(this.ownText?[{nodeType:3,textContent:this.ownText,parentElement:this}]:[]),...this.children]}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent).join('')}
 set textContent(value){this.ownText=String(value);for(const child of this.children)child.parentElement=null;this.children=[]}
 get innerText(){return this.textContent}
 get isConnected(){return this.connected===true||Boolean(this.parentElement?.isConnected)}
 append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node)}return this}
 remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(node=>node!==this);this.parentElement=null}}
 insertAdjacentElement(position,node){assert.equal(position,'afterend');const parent=this.parentElement,index=parent.children.indexOf(this);node.remove();node.parentElement=parent;parent.children.splice(index+1,0,node)}
 contains(node){return node===this||this.children.some(child=>child.contains(node))}
 getClientRects(){return this.isConnected?[{}]:[]}
 getBoundingClientRect(){return this.tagName==='footer'||this.getAttribute('contenteditable')==='true'?{top:500,bottom:540,left:0,right:600}:{top:100,bottom:120,left:0,right:600}}
 addEventListener(){} removeEventListener(){}
 simple(selector){
  const attributes=[...selector.matchAll(/\[[^\]]+\]/g)].map(match=>match[0]);let rest=selector.replace(/\[[^\]]+\]/g,'');
  const tag=/^[\w-]+/.exec(rest);if(tag&&this.tagName!==tag[0])return false;
  for(const match of rest.matchAll(/([.#])([\w-]+)/g)){if(match[1]==='#'&&this.getAttribute('id')!==match[2])return false;if(match[1]==='.'&&!String(this.getAttribute('class')||'').split(/\s+/).includes(match[2]))return false}
  for(const attr of attributes){const match=/^\[([\w-]+)(?:\s*(\*=|\^=|\$=|~=|=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*(i)?)?\]$/.exec(attr);assert.ok(match,'supported fixture selector '+attr);let actual=this.getAttribute(match[1]);if(actual===null)return false;if(!match[2])continue;let expected=match[3]??match[4]??match[5];if(match[6]){actual=actual.toLowerCase();expected=expected.toLowerCase()}if(match[2]==='='&&actual!==expected)return false;if(match[2]==='*='&&!actual.includes(expected))return false;if(match[2]==='^='&&!actual.startsWith(expected))return false;if(match[2]==='$='&&!actual.endsWith(expected))return false;if(match[2]==='~='&&!actual.split(/\s+/).includes(expected))return false}
  return true;
 }
 matches(selector){return selector.split(',').some(part=>{const parts=selectorParts(part.trim());let node=this;if(!node.simple(parts.pop()))return false;while(parts.length){const next=parts.pop();node=node.parentElement;while(node&&!node.simple(next))node=node.parentElement;if(!node)return false}return true})}
 closest(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null}
 querySelectorAll(selector){const result=[];const walk=node=>{for(const child of node.children){if(child.matches(selector))result.push(child);walk(child)}};walk(this);return result}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null}
}
function fixture(){
 const document=new Element('document');document.connected=true;document.createElement=tag=>new Element(tag);
 const main=new Element('main',{id:'main'}),header=new Element('header',{},'Alice'),footer=new Element('footer'),box=new Element('div',{contenteditable:'true',role:'textbox'},'Unsent draft');footer.append(box);main.append(header,footer);document.append(main);
 const timers=new Map();let serial=0;
 const context={crypto:globalThis.crypto,document,location:{hostname:'web.whatsapp.com',pathname:'/'},innerHeight:600,HTMLTextAreaElement:class{},Node:{ELEMENT_NODE:1,TEXT_NODE:3},MutationObserver:class{observe(){}disconnect(){}},getComputedStyle:()=>({overflowY:'visible'}),setTimeout:callback=>{const id=++serial;timers.set(id,callback);return id},clearTimeout:id=>timers.delete(id)};
 runInNewContext(pageCode,context);
 const run=input=>{context.input=input;return runInNewContext('chatTranslationPage(input)',context)};
 const settings={enabled:true,engine:'immersive',incomingLang:'zh-CN',color:'#993333',fontSize:12};
 const poll=()=>run({kind:'poll',settings});
 const text=value=>new Element('span',{class:'selectable-text copyable-text',dir:'auto'},value);
 const message=(value,quoteAttributes)=>{
  const bubble=new Element('div',{class:'message-in'}),content=new Element('div',{'data-pre-plain-text':'[10:04, Alice]'});bubble.append(content);
  let quote;if(quoteAttributes){quote=new Element('div',quoteAttributes).append(new Element('span',{},'Sharon Ann'),text('No I do that later'));content.append(quote)}
  const body=text(value);content.append(body,new Element('time',{},'10:04'));main.children.splice(main.children.indexOf(footer),0,bubble);bubble.parentElement=main;
  return {bubble,content,body,quote};
 };
 return {run,poll,settings,message,text,main,box,context,document,Element};
}
const originals=batch=>Array.from(batch.messages,message=>message.original);

test('reply previews are excluded while the original message and current reply body each translate once',()=>{
 const f=fixture(),original=f.message('No I do that later'),reply=f.message('Okay, my babe',{'data-testid':'quoted-message'});
 const beforeQuote=reply.quote.textContent,beforeDraft=f.box.textContent,batch=f.poll();
 console.log(JSON.stringify({case:'reply-quote-body',queued:originals(batch)}));
 assert.deepEqual(originals(batch),['Okay, my babe','No I do that later']);
 for(const job of batch.messages)assert.equal(f.run({kind:'apply',conversation:batch.conversation,...job,translation:'译文'}).applied,true);
 assert.equal(reply.quote.textContent,beforeQuote);assert.equal(reply.quote.querySelectorAll('[data-juxin-chat-translation]').length,0);assert.equal(original.body.innerText,'No I do that later');assert.equal(f.box.textContent,beforeDraft);
 assert.equal(f.poll().messages.length,0);assert.deepEqual(Array.from(f.run({kind:'export'}).messages,row=>row.original),['No I do that later','Okay, my babe']);
});

test('nested English/Chinese quote semantics exclude quoted text, names and time without excluding the body',()=>{
 for(const attributes of [{'aria-label':'Quoted message from Sharon Ann'},{'aria-label':'引用的消息'},{'data-testid':'quoted-message'},{class:'quoted-mention'}]){
  const f=fixture(),reply=f.message('Actual body',attributes);reply.quote.append(new Element('div',{'data-testid':'quoted-message'}).append(f.text('Nested quoted words')));reply.quote.append(f.text('Nickname'),new Element('time',{class:'selectable-text copyable-text'},'Yesterday 10:04'));
  assert.deepEqual(originals(f.poll()),['Actual body'],JSON.stringify(attributes));
 }
});

test('old quote annotations and records are removed; original quote nodes and body annotations survive',()=>{
 const f=fixture(),reply=f.message('Current body',{'data-testid':'quoted-message'}),batch=f.poll();
 const bodyJob=batch.messages.find(job=>job.original==='Current body');f.run({kind:'apply',conversation:batch.conversation,...bodyJob,translation:'正文译文'});
 const quoted=reply.quote.querySelector('.selectable-text'),oldTranslation=new Element('div',{'data-juxin-chat-translation':'true'},'旧引用译文'),oldStatus=new Element('div',{'data-juxin-chat-status':'true'},'旧状态');reply.quote.append(oldTranslation,oldStatus);
 const state=f.context.__juxinChatTranslationV2;state.records.set('old-quote',{id:'old-quote',original:quoted.innerText,node:quoted,conversation:batch.conversation,label:oldTranslation,statusLabel:oldStatus,translation:'旧引用译文'});state.currentRecords.push(state.records.get('old-quote'));state.dirty=true;
 f.poll();assert.equal(oldTranslation.isConnected,false);assert.equal(oldStatus.isConnected,false);assert.equal(state.records.has('old-quote'),false);assert.equal(quoted.innerText,'No I do that later');assert.equal(reply.quote.isConnected,true);assert.equal(reply.content.querySelectorAll('[data-juxin-chat-translation]').length,1);assert.equal(f.box.textContent,'Unsent draft');
});

test('a node moved into a reply preview rejects a queued late apply before the next poll',()=>{
 const f=fixture(),message=f.message('Was a body'),batch=f.poll(),job=batch.messages[0];
 const quote=new Element('div',{'data-testid':'quoted-message'});message.content.append(quote);quote.append(message.body);
 const applied=f.run({kind:'apply',conversation:batch.conversation,...job,translation:'must not be inserted'});
 assert.equal(applied.applied,false);assert.equal(quote.querySelectorAll('[data-juxin-chat-translation]').length,0);assert.equal(f.context.__juxinChatTranslationV2.records.has(job.id),false);assert.equal(message.body.innerText,'Was a body');
});

test('normal messages including equal text remain distinct bodies, with no accidental time/name extraction',()=>{
 const f=fixture(),first=f.message('Same body'),second=f.message('Same body');
 first.content.append(new Element('time',{class:'selectable-text copyable-text'},'Yesterday 10:04'));
 first.content.append(new Element('span',{'data-testid':'author',class:'selectable-text copyable-text'},'Group member'));
 const batch=f.poll();assert.deepEqual(originals(batch),['Same body','Same body']);assert.notEqual(batch.messages[0].id,batch.messages[1].id);assert.equal(second.body.innerText,'Same body');
});

test('WhatsApp-only quote exclusions do not change the Instagram message selector',()=>{
 const f=fixture();f.context.location={hostname:'www.instagram.com',pathname:'/direct/t/fixture'};
 f.message('Current Instagram body',{'data-testid':'quoted-message'});
 assert.deepEqual(originals(f.poll()),['Current Instagram body','No I do that later']);
});
