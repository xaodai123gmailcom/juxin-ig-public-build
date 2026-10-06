import {FloatingWebPage} from './floating-web-page.js';
export class GoogleTranslator extends FloatingWebPage {
 constructor(changed:(visible:boolean)=>void){super(changed,{partition:'persist:translation',url:'https://translate.google.com/?hl=zh-CN&sl=auto&tl=zh-CN&op=translate',title:'谷歌翻译',width:660,height:578})}
}
