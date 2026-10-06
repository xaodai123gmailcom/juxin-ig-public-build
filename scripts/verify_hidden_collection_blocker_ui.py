"""Offline Chromium interaction proof for the production locator component."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, threading
from playwright.sync_api import sync_playwright, expect
root=Path(__file__).resolve().parents[1]
server=ThreadingHTTPServer(('127.0.0.1',0),partial(SimpleHTTPRequestHandler,directory=str(root/'hidden-ui-proof')))
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1080,'height':720});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  url=f'http://127.0.0.1:{server.server_port}/'
  page.goto(url);page.get_by_role('button',name='定位关联采集任务').click()
  expect(page.get_by_text('任务编号：86feb8fc-f2a6-404f-9459-11dd3f884103')).to_be_visible()
  page.screenshot(path=str(root/'hidden-ui-proof/located.png'),full_page=True)
  page.once('dialog',lambda dialog:dialog.dismiss());page.get_by_role('button',name='停止这条暂停采集任务').click()
  assert page.evaluate('calls.length')==1,'cancel sent a mutation'
  dialogs=[]
  def accept(dialog):dialogs.append(dialog.message);dialog.accept()
  page.once('dialog',accept);page.get_by_role('button',name='停止这条暂停采集任务').click()
  expect(page.get_by_text('已正常停止这条暂停采集任务，请再次核验窗口清理')).to_be_visible()
  assert '86feb8fc-f2a6-404f-9459-11dd3f884103' in dialogs[0] and '1号窗口' in dialogs[0]
  assert page.evaluate('calls[1].body')=={'action':'stop_cleanup_collection','job_id':'held','task_id':'86feb8fc-f2a6-404f-9459-11dd3f884103','version':7}
  assert page.evaluate('refreshes')==1
  page.reload();page.evaluate("mode='live'");page.get_by_role('button',name='定位关联采集任务').click()
  expect(page.get_by_role('button',name='停止这条暂停采集任务')).to_be_disabled()
  page.reload();page.evaluate('delay=250');button=page.get_by_role('button',name='定位关联采集任务');button.click();button.evaluate('(el)=>el.click()')
  expect(page.get_by_role('button',name='停止这条暂停采集任务')).to_be_visible();assert page.evaluate('calls.length')==1
  page.evaluate("mode='error'");page.once('dialog',accept);page.get_by_role('button',name='停止这条暂停采集任务').click()
  expect(page.get_by_text('Error: 关联任务已变化，请重新定位后再确认')).to_be_visible()
  expect(page.get_by_role('button',name='停止这条暂停采集任务')).to_have_count(0)
  assert page.evaluate('refreshes')==0
  page.reload();page.evaluate('delay=250');page.get_by_role('button',name='定位关联采集任务').click();page.evaluate('unmount()');page.wait_for_timeout(300)
  assert errors==[],errors
  browser.close()
 print('HIDDEN_COLLECTION_UI=PASS '+json.dumps({'checks':['exact ID and windows visible','cancel no mutation','confirmed exact ID/version','refresh after stop','live disabled','double-click collapsed','stale lookup cleared','unmount guarded'],'screenshot':'hidden-ui-proof/located.png'}))
finally:server.shutdown();server.server_close()
