/* Test readiness only: never supplies or retries an expected HTTP status. */
const {performance} = require('node:perf_hooks');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const recoveryProbe = `(() => ({
 protocol: location.protocol,
 title: document.title,
 charset: document.characterSet,
 ready: document.readyState,
 links: Array.from(document.querySelectorAll('a[href]'), link => link.href)
}))()`;

async function readReviewRecoveryFixture(wc, options) {
 const {target, start, readState, timeout = 5000, now = () => performance.now(), sleep = pause} = options;
 if (typeof target !== 'string' || !/^[A-Za-z0-9._]{1,30}$/.test(target)) throw new Error('Invalid review recovery fixture target');
 const expectedUrl = 'https://www.instagram.com/' + target + '/';
 let committed = false, recoveryCommitted = false, responseStatus = null, document = null, stopped = false, timer;
 const navigated = (_event, url, status) => {
  if (url === expectedUrl) { committed = true; recoveryCommitted = false; responseStatus = status; }
  else if (committed && url.startsWith('data:text/html')) recoveryCommitted = true;
 };
 // Subscribe BEFORE opening: identical error titles may still belong to the
 // previous case while openTarget has correctly reset the new status to null.
 wc.on('did-navigate', navigated);
 const began = now();
 const check = async () => {
  start();
  while (true) {
   if (stopped) return;
   if (wc.isDestroyed()) throw new Error('Review recovery fixture ' + target + ': renderer closed');
   if (committed && recoveryCommitted) {
    document = await wc.executeJavaScript(recoveryProbe);
    if (stopped) return;
    if (now() - began >= timeout) throw new Error('Review recovery fixture ' + target + ': current response/document not ready; renderer/readiness deadline exceeded');
    if (document?.protocol === 'data:' && document.ready === 'complete' &&
        document.title === '审核网页暂时无法打开' && document.links?.includes(expectedUrl)) {
     if (String(document.charset).toUpperCase() !== 'UTF-8') throw new Error('Review recovery fixture ' + target + ': expected UTF-8');
     // Read once after document identity is established. Null / wrong statuses
     // remain null / wrong so the unchanged caller assertions still fail.
     const state = readState();
     console.log('CHECK review recovery fixture', JSON.stringify({target, responseStatus, ready: document.ready, httpStatus: state.httpStatus}));
     return {state, responseStatus, document};
    }
   }
   if (now() - began >= timeout) throw new Error('Review recovery fixture ' + target + ': current response/document not ready; ' +
    JSON.stringify({committed, recoveryCommitted, responseStatus, protocol: document?.protocol, title: document?.title, ready: document?.ready}));
   await sleep(20);
  }
 };
 try {
  // A renderer evaluation can itself remain pending. Bound the whole operation,
  // including that await, rather than checking time only between evaluations.
  return await Promise.race([check(), new Promise((_, reject) => {
   timer = setTimeout(() => reject(new Error('Review recovery fixture ' + target + ': current response/document not ready; renderer/readiness deadline exceeded')), timeout);
  })]);
 } finally { stopped = true; clearTimeout(timer); wc.removeListener('did-navigate', navigated); }
}
module.exports = {readReviewRecoveryFixture, recoveryProbe};
