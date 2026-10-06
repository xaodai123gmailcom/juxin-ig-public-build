import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { verifyRendererReleaseCopy, verifyRendererProductionRuntime } from '../renderer_release_contract.mjs';
import { resolveConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const read = relative => readFileSync(new URL(`../../${relative}`, import.meta.url), 'utf8');
const currentCopy = read('renderer/src/formal-workbench.tsx')
  + '\n' + read('renderer/src/greeting-messages.ts');

test('current direct-discard UI satisfies release copy without reviving the retired qualification label', () => {
  assert.equal(currentCopy.includes('0=不限'), false);
  assert.ok(currentCopy.includes('全部不限'));
  assert.ok(currentCopy.includes('公开帖子活跃度上限（天）'));
  assert.doesNotThrow(() => verifyRendererReleaseCopy(currentCopy, 'current source'));
});

for (const [name, text] of [
  ['all-unlimited action', '全部不限'],
  ['public post activity input label', '公开帖子活跃度上限（天）'],
  ['deduplication data label', '全局去重'],
  ['preview-only cleanup action', '仅清理预览缓存'],
]) {
  test(`release copy rejects removal of ${name} in source and output checks`, () => {
    assert.ok(currentCopy.includes(text), 'mutation must remove actual UI text');
    const damaged = currentCopy.replaceAll(text, 'removed-required-copy');
    for (const phase of ['renderer source release copy', 'production capacity and safe cleanup copy']) {
      assert.throws(() => verifyRendererReleaseCopy(damaged, phase), error => {
        assert.equal(error.message,
          `NewGen desktop verification failed: ${phase} is missing contract marker: ${text}`);
        return true;
      });
    }
  });
}

test('the real release entry uses one copy contract before source success and inside artifact verification', () => {
  const entry = read('scripts/verify_desktop_build.mjs');
  const artifactStart = entry.indexOf('async function verifyCompiledArtifacts()');
  const sourceSuccess = entry.indexOf('console.log("NewGen desktop source verification: PASS")');
  assert.ok(artifactStart > 0 && sourceSuccess > artifactStart);
  const sourcePhase = entry.slice(0, artifactStart);
  const artifactPhase = entry.slice(artifactStart, sourceSuccess);
  assert.match(sourcePhase, /import\s*\{\s*verifyRendererReleaseCopy\s*\}\s*from\s*"\.\/renderer_release_contract\.mjs"/);
  assert.match(sourcePhase, /verifyRendererReleaseCopy\(workbenchSource \+ "\\n" \+ greetingMessagesSource, "renderer source release copy"\)/);
  assert.match(artifactPhase, /verifyRendererReleaseCopy\(productionBundleText, "production capacity and safe cleanup copy"\)/);
  assert.match(artifactPhase, /verifyRendererProductionRuntime\(productionBundleText\)/);
  assert.equal(artifactPhase.includes('"0=不限"'), false);
  const dispatch = entry.slice(sourceSuccess);
  const sourceOnlyBranch = dispatch.slice(dispatch.indexOf('if (sourceOnly)'), dispatch.indexOf('} else {'));
  assert.ok(sourceOnlyBranch.includes('DESKTOP_ARTIFACTS_CHECK=NOT_RUN'));
  assert.equal(sourceOnlyBranch.includes('NewGen desktop build verification: PASS'), false);
  assert.ok(dispatch.indexOf('await verifyCompiledArtifacts();')
    < dispatch.indexOf('console.log("NewGen desktop build verification: PASS")'));
});

for (const marker of ['react_stack_bottom_frame', 'react.development.js',
  'react-dom-client.development.js', 'react.dev/link/react-devtools']) {
  test(`release verifier rejects React development marker ${marker}`, () => {
    assert.throws(() => verifyRendererProductionRuntime('minified runtime ' + marker), /development runtime/);
  });
}

test('production runtime verifier accepts an output without development runtime', () => {
  assert.doesNotThrow(() => verifyRendererProductionRuntime('production application code'));
});

test('actual Vite config forces production builds despite inherited development or test settings', async () => {
  const before = process.env.NODE_ENV;
  try {
    for (const inherited of ['development', 'test', 'staging']) {
      process.env.NODE_ENV = inherited;
      const config = await resolveConfig({
        configFile: fileURLToPath(new URL('../../renderer/vite.config.ts', import.meta.url)),
        logLevel: 'silent',
      }, 'build');
      assert.equal(config.isProduction, true, inherited);
      assert.equal(config.env.PROD, true, inherited);
      assert.equal(config.env.DEV, false, inherited);
      assert.equal(config.define['process.env.NODE_ENV'], '"production"', inherited);
    }
    process.env.NODE_ENV = 'development';
    const development = await resolveConfig({
      configFile: fileURLToPath(new URL('../../renderer/vite.config.ts', import.meta.url)),
      logLevel: 'silent',
    }, 'serve');
    assert.equal(development.isProduction, false, 'development server stays available');
  } finally {
    if (before === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = before;
  }
});

for (const marker of ['部分列表显示最近', '前往设置', '界面 r94 / Core', '刷新页面']) {
  test(`source and bundled renderer reject reintroducing retired shell text ${marker}`, () => {
    for (const phase of ['renderer source release copy', 'production capacity and safe cleanup copy'])
      assert.throws(() => verifyRendererReleaseCopy(currentCopy + '\n' + marker, phase), /contains retired shell marker/);
  });
}
