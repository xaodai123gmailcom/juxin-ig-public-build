'use strict';

/** First-party offline test adapter. This is synthetic protocol coverage, not
 * Immersive Translate code or evidence that an official vendor build ran. */
function syntheticImmersiveProtocol() {
 const gm=globalThis.GM;
 if(!gm)throw new Error('Synthetic adapter requires the real GM preload bridge');
 const host=document.createElement('div');host.id='immersive-translate-browser-popup';
 const shadow=host.attachShadow({mode:'open'}),mount=document.createElement('div');mount.id='mount';
 const panel=document.createElement('section');panel.className='popup-container';panel.style.display='none';
 const title=document.createElement('h2');title.textContent='Synthetic offline translation fixture';
 const service=document.createElement('select');service.setAttribute('aria-label','Fixture service');
 for(const value of ['bing','google']){const option=document.createElement('option');option.value=value;option.textContent=value;service.append(option)}
 const close=document.createElement('button');close.type='button';close.textContent='Close fixture';close.addEventListener('click',()=>{panel.style.display='none'});
 panel.append(title,service,close);mount.append(panel);shadow.append(mount);
 gm.addElement(shadow,'style',{textContent:'.popup-container{box-sizing:border-box;width:360px;min-height:220px;padding:20px;background:#fff;color:#111}.popup-container select,.popup-container button{display:block;margin:12px 0}'});
 document.body.append(host);
 gm.registerMenuCommand('Synthetic fixture settings',()=>{panel.style.display='block'});
 let ready=false,pageStatus='Original',translationService='bing';
 const reply=(request,payload)=>document.dispatchEvent(new CustomEvent('immersiveTranslateDocumentMessageTellThirdParty',{
  detail:JSON.stringify({type:request.type,id:request.id,payload})
 }));
 document.addEventListener('immersiveTranslateDocumentMessageThirdPartyTell',event=>{
  const request=JSON.parse(event.detail);
  if(request.type==='getAsyncTranslationMeta'&&ready)reply(request,{translationService});
  if(request.type==='getPageStatusAsync')reply(request,pageStatus);
  if(request.type==='openPopup')panel.style.display='block';
  if(request.type==='restorePage'){
   for(const node of document.querySelectorAll('.immersive-translate-target-inner'))node.remove();
   pageStatus='Original';
  }
  if(request.type==='translatePage'){
   const job=document.querySelector('#juxin-job');
   if(job){const target=document.createElement('span');target.className='immersive-translate-target-inner';target.textContent='[synthetic] '+(job.querySelector('.juxin-source')?.textContent||'');job.append(target);pageStatus='Translated'}
  }
 });
 service.addEventListener('change',async()=>{
  const config=await gm.getValue('fullLocalUserConfig',{});
  await gm.setValue('fullLocalUserConfig',{...config,translationService:service.value});
  translationService=service.value;
 });
 // Read through the unchanged GM preload and IPC before declaring readiness.
 void gm.getValue('fullLocalUserConfig',{}).then(config=>{
  translationService=config.translationService||'bing';service.value=translationService;ready=true;
  document.dispatchEvent(new CustomEvent('immersiveTranslateDocumentMessagePluginReady'));
 });
}

module.exports=Object.freeze({
 status:()=>({available:true,version:'synthetic-offline-fixture',message:'First-party synthetic adapter; no vendor code or network'}),
 load:()=>`(${syntheticImmersiveProtocol.toString()})();`,
});
