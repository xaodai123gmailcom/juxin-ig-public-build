import test from 'node:test';
import assert from 'node:assert/strict';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {existsSync,mkdtempSync,mkdirSync,readFileSync,rmSync,writeFileSync} from 'node:fs';
import {PRODUCT_NAME,STABLE_APPLICATION_ID,selectUserDataDirectory} from '../../dist-electron/app-identity.js';

test('renaming the product preserves existing database and encrypted settings directory',()=>{
  assert.equal(PRODUCT_NAME,'聚鑫国际');
  assert.equal(STABLE_APPLICATION_ID,'com.juxin.igaudiencecollector.newgen');
  const base='/app-data',current=join(base,PRODUCT_NAME),legacy=join(base,'聚鑫国际 新一代 IG 采集器'),stable=join(base,'juxin-ig-audience-collector-newgen');
  assert.equal(selectUserDataDirectory(base,current,p=>p===legacy),legacy);
  assert.equal(selectUserDataDirectory(base,current,p=>p===stable),stable);
  assert.equal(selectUserDataDirectory(base,current,p=>p===current||p===legacy),current);
  assert.equal(selectUserDataDirectory(base,current,()=>false),stable);
});

test('a settings-only current directory cannot hide the legacy collection database',()=>{
  const base='/app-data',current=join(base,PRODUCT_NAME),legacy=join(base,'聚鑫国际 新一代 IG 采集器');
  assert.equal(selectUserDataDirectory(base,current,p=>p===current||p===legacy,p=>p===legacy),legacy);
});

test('a stable-name database takes priority over settings-only current and legacy directories',()=>{
  const base='/app-data',current=join(base,PRODUCT_NAME),stable=join(base,'juxin-ig-audience-collector-newgen');
  assert.equal(selectUserDataDirectory(base,current,()=>true,p=>p===stable),stable);
});

test('multiple databases preserve current then legacy preference without merging',()=>{
  const base='/app-data',current=join(base,PRODUCT_NAME),legacy=join(base,'聚鑫国际 新一代 IG 采集器');
  assert.equal(selectUserDataDirectory(base,current,()=>true,()=>true),current);
  assert.equal(selectUserDataDirectory(base,current,()=>true,p=>p!==current),legacy);
});

test('settings-only fallback remains compatible when no database exists',()=>{
  const base='/app-data',current=join(base,PRODUCT_NAME),legacy=join(base,'聚鑫国际 新一代 IG 采集器'),stable=join(base,'juxin-ig-audience-collector-newgen');
  assert.equal(selectUserDataDirectory(base,current,p=>p===current,()=>false),current);
  assert.equal(selectUserDataDirectory(base,current,p=>p===legacy,()=>false),legacy);
  assert.equal(selectUserDataDirectory(base,current,()=>false,()=>false),stable);
});

test('filesystem selection retains the historical database and its own encrypted store intact',()=>{
  const base=mkdtempSync(join(tmpdir(),'juxin-data-choice-'));
  try {
    const current=join(base,PRODUCT_NAME),legacy=join(base,'聚鑫国际 新一代 IG 采集器');
    mkdirSync(current,{recursive:true});mkdirSync(join(legacy,'data'),{recursive:true});
    const currentSettings=join(current,'secure-store.json'),legacySettings=join(legacy,'secure-store.json'),database=join(legacy,'data','collector.sqlite3');
    writeFileSync(currentSettings,'current-settings-fixture');
    writeFileSync(legacySettings,'legacy-settings-fixture');
    writeFileSync(database,'historical-database-fixture');
    const hasDatabase=directory=>existsSync(join(directory,'data','collector.sqlite3'));
    const chosen=selectUserDataDirectory(base,current,directory=>hasDatabase(directory)||existsSync(join(directory,'secure-store.json')),hasDatabase);
    assert.equal(chosen,legacy);
    assert.equal(readFileSync(database,'utf8'),'historical-database-fixture');
    assert.equal(readFileSync(legacySettings,'utf8'),'legacy-settings-fixture');
    assert.equal(readFileSync(currentSettings,'utf8'),'current-settings-fixture');
    assert.equal(existsSync(join(current,'data','collector.sqlite3')),false);
  } finally {rmSync(base,{recursive:true,force:true})}
});
