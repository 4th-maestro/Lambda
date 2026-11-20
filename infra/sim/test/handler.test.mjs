// infra/sim 핸들러 테스트(Node 내장 테스트, 패키지 설치 없음)
//   bash infra/sim/prepare.sh && node --test infra/sim/test/handler.test.mjs
//   ENERGYPLUS_DIR이 있으면(이 PC: C:/Users/<사용자>/tools/EnergyPlus-24.2.0-...) 실제 EnergyPlus 계산까지 확인
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
process.env.TWIN_SRC = path.join(HERE, '..', 'twin');
process.env.TWIN_WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'sim-test-'));
process.env.SIM_SECRET = 's3cret';
const { handler, hostDir, demoDir, BadRequest, load } = await import('../handler.mjs');
await load();

const DEMO = JSON.parse(fs.readFileSync(path.join(process.env.TWIN_SRC, 'data', 'busan-station', 'building.json'), 'utf8'));
const host = id => ({ ...structuredClone(DEMO), building:{ ...DEMO.building, id } });
const ev = (method, rawPath, body, secret = 's3cret') => ({ rawPath, requestContext:{ http:{ method } }, headers:secret ? { 'X-Maestro-Sim':secret } : {},
  body:body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body), isBase64Encoded:false });
const json = r => JSON.parse(r.body);

test('비밀 헤더가 없거나 다르면 403', async () => {
  assert.equal((await handler(ev('GET', '/api/sim/status', undefined, null))).statusCode, 403);
  assert.equal((await handler(ev('GET', '/api/sim/status', undefined, 'wrong'))).statusCode, 403);
});

test('status와 알 수 없는 경로', async () => {
  const r = await handler(ev('GET', '/api/sim/status'));
  assert.equal(r.statusCode, 200);
  assert.equal(typeof json(r).ok, 'boolean');
  assert.equal((await handler(ev('GET', '/api/sim/compare'))).statusCode, 404);
  assert.equal((await handler(ev('POST', '/other'))).statusCode, 404);
});

test('데모 폴더는 buildings.json 목록만 허용', () => {
  assert.equal(demoDir('data/busan-station/'), 'data/busan-station/');
  assert.equal(demoDir('data/busan-station'), 'data/busan-station/');
  assert.equal(demoDir('data/'), 'data/');
  for (const bad of ['../', 'data/../../etc', 'data/host/x', '/etc/passwd', ''])
    assert.throws(() => demoDir(bad), BadRequest, bad);
});

test('등록 건물 도면 검사', () => {
  const rel = hostDir(host('b-test01'));
  assert.match(rel, /^data\/host\/[0-9a-f]{12}$/);
  const dir = path.join(process.env.TWIN_WORK, rel);
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, 'building.json'), 'utf8')).building.id, 'b-test01');
  assert.equal(JSON.parse(fs.readFileSync(path.join(dir, 'tariff.json'), 'utf8')).wonPerKwh, 134.2);
  assert.equal(hostDir(host('b-test01')), rel);   // 같은 도면은 같은 폴더(결과 캐시 재사용)
  const bad = [null, [], { ...DEMO, schema:'x' }, host('../../etc'), host('a/b'), host(''), { ...DEMO, floors:[] },
    { ...DEMO, building:{ ...DEMO.building, size:{ w:0, d:10 } } }, { ...DEMO, floors:DEMO.floors.map(f => ({ ...f, rooms:[] })) },
    { ...DEMO, floors:Array.from({ length:61 }, () => DEMO.floors[0]) }];
  for (const b of bad) assert.throws(() => hostDir(b), BadRequest);
});

test('compare 입력 오류는 400', async () => {
  for (const body of ['{', { data:'../x' }, { building:host('../x') }, { building:{ schema:'twin.building/v1' } }]) {
    const r = await handler(ev('POST', '/api/sim/compare', body));
    assert.equal(r.statusCode, 400, JSON.stringify(body).slice(0, 40));
    assert.ok(json(r).error);
  }
  const big = await handler(ev('POST', '/api/sim/compare', 'x'.repeat((3 << 20) + 1)));
  assert.equal(big.statusCode, 413);
});

const EP = process.env.ENERGYPLUS_DIR && fs.existsSync(process.env.ENERGYPLUS_DIR);
test('EnergyPlus 실제 계산: 데모 건물과 등록 건물 도면', { skip:!EP && 'ENERGYPLUS_DIR 없음', timeout:300_000 }, async () => {
  const scenario = { name:'내 선택', mode:'heat', coolSetpoint:20 };
  const demo = await handler(ev('POST', '/api/sim/compare', { data:'data/busan-station/', scenario }));
  assert.equal(demo.statusCode, 200, demo.body.slice(0, 300));
  const d = json(demo);
  assert.equal(d.schema, 'twin.scenario-compare/v1');
  assert.equal(d.building, DEMO.building.id);
  const reg = await handler(ev('POST', '/api/sim/compare', { building:host('b-test01'), scenario }));
  assert.equal(reg.statusCode, 200, reg.body.slice(0, 300));
  const g = json(reg);
  assert.equal(g.building, 'b-test01');
  assert.ok(Number.isFinite(g.compare.savedKwh));
  assert.ok(JSON.stringify(g.baseline).includes('134.2원/kWh'));   // 등록 건물은 기본 요금표(일반용 갑)
});
