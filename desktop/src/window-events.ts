import type {BrowserWindow} from 'electron';

/** During native shutdown webContents may be null before isDestroyed is true. */
export function sendWindowEvent(win:BrowserWindow|null,channel:string,value:unknown):boolean {
 try {
  if(!win||win.isDestroyed())return false;
  const contents=win.webContents;
  if(!contents||contents.isDestroyed())return false;
  contents.send(channel,value);return true;
 } catch(error) {
  // isDestroyed and send are separate native calls; closing may race them.
  if(error instanceof Error&&error.message.includes('Object has been destroyed'))return false;
  throw error;
 }
}
