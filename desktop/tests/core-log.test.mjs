import assert from 'node:assert/strict';
import test from 'node:test';
import { PassThrough } from 'node:stream';
import { captureCoreLog } from '../../dist-electron/core-log.js';

test('r18 split chunks retain stack lines and redact credentials', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stderr',line=>lines.push(line),['test-secret']);
  pipe.write(Buffer.from('Traceback\napi_key=test-sec'));
  pipe.write(Buffer.from('ret\nAuthorization: Bearer x.y.z\nError: failure\n'));
  pipe.end();await new Promise(r=>pipe.on('end',r));
  const text=lines.join('\n');assert.match(text,/Traceback/);assert.match(text,/Error: failure/);
  assert.doesNotMatch(text,/test-secret|x\.y\.z/);
});

test('r18 newline-free output stays bounded without failing child drain', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stdout',line=>lines.push(line),[]);
  for(let i=0;i<20;i++)pipe.write('a'.repeat(4096));
  pipe.end('\nfinished\n');await new Promise(r=>pipe.on('end',r));
  assert.ok(lines.every(line=>line.length<8300));assert.match(lines.at(-1),/finished/);
});

test('quoted JSON credentials and full cookie headers are redacted', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stderr',line=>lines.push(line),[]);
  pipe.end('{"api_key":"json-secret","password":"two secret words","reason":"timeout"}\nCookie: session=one-secret; other=two-secret\nError: still available\n');
  await new Promise(r=>pipe.on('end',r));
  const text=lines.join('\n');
  assert.doesNotMatch(text,/json-secret|two secret words|one-secret|two-secret/);
  assert.match(text,/timeout/);assert.match(text,/Error: still available/);
});

test('an overlong credential discards its remaining chunks until the newline', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stderr',line=>lines.push(line),[]);
  pipe.write('api_key='+ 'x'.repeat(17000));
  pipe.write('credential-tail-that-must-not-be-logged');
  pipe.end('\nnext safe message\n');await new Promise(r=>pipe.on('end',r));
  assert.equal(lines.length,2);
  assert.match(lines[0],/overlong Core log record omitted/);
  assert.doesNotMatch(lines.join('\n'),/credential-tail|x{20}/);
  assert.match(lines[1],/next safe message/);
});

test('large single chunks and unterminated oversized records stay omitted', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stdout',line=>lines.push(line),[]);
  pipe.end('q'.repeat(20000)+'\nnormal\n'+'z'.repeat(20000));
  await new Promise(r=>pipe.on('end',r));
  assert.equal(lines.length,3);assert.match(lines[1],/normal/);
  for(const line of [lines[0],lines[2]])assert.match(line,/overlong Core log record omitted/);
  assert.doesNotMatch(lines.join('\n'),/q{20}|z{20}/);
});

test('split multibyte text survives and a throwing log sink does not stop draining', async () => {
  const pipe=new PassThrough();const lines=[];
  captureCoreLog(pipe,'stderr',line=>{lines.push(line);if(lines.length===1)throw new Error('disk full')},[]);
  const text=Buffer.from('检查进度\nnext line\n');
  pipe.write(text.subarray(0,2));pipe.end(text.subarray(2));
  await new Promise(r=>pipe.on('end',r));
  assert.match(lines[0],/检查进度/);assert.match(lines[1],/next line/);
});
