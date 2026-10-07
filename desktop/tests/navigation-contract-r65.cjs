/* Independent retained-product contract, not derived from the renderer under test. */
const assert=require('node:assert/strict');
const RETAINED_NAVIGATION=Object.freeze([
  ['home','#/','首页'],['accounts','#/accounts','账号'],
  ['follow-monitor','#/follow-monitor','检查'],['nurture','#/nurture','养号'],
  ['collection','#/collection','采集'],['review','#/review','审核'],
  ['public','#/public','公开'],['private','#/private','私密'],
  ['reports','#/reports','报表'],['history','#/history','历史'],
  ['settings','#/settings','设置'],
].map(([id,href,label])=>Object.freeze({id,href,label})));

function assertNavigationStructure(rows){
  assert.ok(Array.isArray(rows),'Navigation observations must be an array');
  assert.deepEqual(rows.map(({id,href,label})=>({id,href,label})),RETAINED_NAVIGATION,
    'Retained navigation routes, order, labels and links must match exactly');
  for(const row of rows){
    assert.equal(row.iconCount,1,'Each retained navigation entry needs exactly one icon');
  }
}
function assertRetainedNavigation(rows){
  assertNavigationStructure(rows);
  for(const row of rows){
    assert.equal(typeof row.text,'string');
    assert.ok(row.text.trim(),'Navigation color must be present');
    assert.equal(row.anchor,row.text,'Navigation label and link accents must match');
    assert.equal(row.icon,row.text,'Navigation text and icon accents must match');
  }
  assert.equal(new Set(rows.map(row=>row.text)).size,RETAINED_NAVIGATION.length,
    'Every retained navigation entry needs a distinct accent');
}
module.exports={RETAINED_NAVIGATION,assertNavigationStructure,assertRetainedNavigation};
