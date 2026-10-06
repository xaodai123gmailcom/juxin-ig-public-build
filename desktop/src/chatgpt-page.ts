import {FloatingWebPage} from './floating-web-page.js';
/** Official ChatGPT page, with its own persistent browser session. */
export class ChatGPTPage extends FloatingWebPage {
 constructor(changed:(visible:boolean)=>void){super(changed,{partition:'persist:chatgpt',url:'https://chatgpt.com/',title:'ChatGPT',width:780,height:760,loginPopups:true,retainWhileHidden:true})}
}
