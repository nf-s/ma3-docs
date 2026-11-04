import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const { VirtualListLayout, CommandPageCache, validateCommandPage } = runInNewContext(`${readFileSync(new URL('../web/virtual-command-list.js', import.meta.url), 'utf8')}\n({ VirtualListLayout, CommandPageCache, validateCommandPage });`, { AbortController });
const item = (index) => ({ id: `command/${index}`, document_id: 'manual/keyword_store', text: `Store Cue ${index}`, type: 'example', authority: 'manual', version: '2.5' });
const page = (offset, total = 1000, limit = 40) => ({ offset, total, limit, results: Array.from({ length: Math.min(limit, total - offset) }, (_, index) => item(offset + index)) });
const settle = () => new Promise((resolve) => setImmediate(resolve));

test('virtual ranges preserve the whole catalog height while bounding rendered rows', () => {
  const layout = new VirtualListLayout(3000, 40);
  assert.equal(layout.height, 120000);
  const range = layout.range(60000, 800);
  assert.equal(range.start, 1485);
  assert.equal(range.end, 1536);
  assert.ok(range.end - range.start < 60);
  assert.equal(layout.range(120000, 800).end, 3000);
  assert.equal(new VirtualListLayout(0, 40).range(0, 800).end, 0);
});

test('measured multiline examples adjust subsequent positions without losing indices', () => {
  const layout = new VirtualListLayout(100, 100);
  assert.equal(layout.measure(2, 350), true);
  assert.equal(layout.offsets[3], 550);
  assert.equal(layout.height, 10250);
  assert.equal(layout.indexAt(549), 2);
  assert.equal(layout.indexAt(550), 3);
  assert.equal(layout.measure(2, 350.2), false);
  assert.equal(layout.measure(2, 0), false);
});

test('command page boundary rejects malformed, incomplete and shifting catalogs', () => {
  assert.equal(validateCommandPage(page(0), 0, 40).total, 1000);
  for (const invalid of [null, { ...page(0), offset: 40 }, { ...page(0), limit: 200 }, { ...page(0), results: [] }, { ...page(0), results: [null, ...page(0).results.slice(1)] }, { ...page(0), results: [{ ...item(0), authority: 'unknown' }, ...page(0).results.slice(1)] }]) {
    assert.throws(() => validateCommandPage(invalid, 0, 40), /invalid|incomplete/);
  }
  assert.throws(() => validateCommandPage(page(40, 900), 40, 40, 1000), /invalid/);
});

test('scrolling directly to a distant range fetches its bounded pages with at most two requests', async () => {
  const calls = [];
  const resolutions = [];
  const cache = new CommandPageCache(page(0), (offset, signal) => {
    calls.push({ offset, signal });
    return new Promise((resolve) => resolutions.push(() => resolve(page(offset))));
  }, () => {});
  cache.ensure(800, 920);
  await settle();
  assert.deepEqual(calls.map((call) => call.offset), [800, 840]);
  assert.equal(cache.pending.size, 2);
  resolutions.shift()();
  await settle();
  assert.deepEqual(calls.map((call) => call.offset), [800, 840, 880]);
  assert.equal(cache.item(801).id, 'command/801');
  assert.equal(cache.item(400), undefined);
  cache.destroy();
  assert.ok(calls.filter((call) => call.offset !== 800).every((call) => call.signal.aborted));
  for (const resolve of resolutions) resolve();
  await settle();
});

test('scrolling away aborts stale pages and failed pages retry only on request', async () => {
  const calls = [];
  const cache = new CommandPageCache(page(0), (offset, signal) => {
    calls.push({ offset, signal });
    if (offset === 400) return Promise.reject(new Error('Network unavailable'));
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('Aborted'))));
  }, () => {});
  cache.ensure(200, 240);
  await settle();
  cache.ensure(400, 440);
  await settle();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(cache.errors.get(400).message, 'Network unavailable');
  cache.ensure(400, 440);
  await settle();
  assert.equal(calls.filter((call) => call.offset === 400).length, 1);
  cache.retry(400);
  await settle();
  assert.equal(calls.filter((call) => call.offset === 400).length, 2);
  cache.destroy();
});

test('visited pages are evicted while measured list height can remain independent', async () => {
  const cache = new CommandPageCache(page(0), (offset) => Promise.resolve(page(offset)), () => {});
  for (let offset = 40; offset < 1000; offset += 40) {
    cache.ensure(offset, offset + 40);
    await settle();
  }
  cache.ensure(960, 1000);
  assert.ok(cache.pages.size <= 12);
  assert.equal(cache.item(0), undefined);
  assert.equal(cache.item(999).id, 'command/999');
  cache.destroy();
});

test('article extracts sort alphabetically while retaining grouped provenance and separate Lua', () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const extracts = source.slice(source.indexOf('function commandExtracts('), source.indexOf('\nasync function collectCommandPages('));
  const makeNode = (tag, className, text) => ({ tag, className, text, children: [], setAttribute() {}, append(...children) { this.children.push(...children); } });
  const commandExtracts = runInNewContext(`${extracts}\ncommandExtracts;`, {
    node: makeNode,
    commandSources: (entry) => entry.source_records || [entry],
    commandCode: (entry) => ({ code: entry.text, language: entry.language }),
    commandMetadata: (sources) => ({ sources }),
  });
  const first = { ...item(1), text: 'Store Cue 1' };
  const duplicate = { ...first, id: 'release/store', authority: 'release-note' };
  const section = commandExtracts([first, { ...item(2), text: 'ClearAll' }, duplicate, { ...item(3), text: 'Store Cue 1', language: 'lua' }, { ...item(4), text: 'Attribute 1' }]);
  assert.deepEqual(Array.from(section.children.slice(1), (row) => row.children[1].code), ['Attribute 1', 'ClearAll', 'Store Cue 1', 'Store Cue 1']);
  const group = section.children.find((row) => row.children[0]?.sources?.length === 2);
  assert.equal(group.children[0].sources[0].authority, 'manual');
  assert.equal(group.children[0].sources[1].authority, 'release-note');
});

test('keyboard targets fetch even when estimated viewport measurements end before the target', async () => {
  const offsets = [];
  const cache = new CommandPageCache(page(0, 444), (offset) => {
    offsets.push(offset);
    return Promise.resolve(page(offset, 444));
  }, () => {});
  cache.ensure(374, 426, 443);
  await settle();
  assert.equal(offsets[0], 440);
  assert.equal(cache.item(443).id, 'command/443');
  assert.ok(offsets.length <= 3);
  cache.destroy();
});

test('provenance count deduplicates sources by document, edition and page instead of link count', () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const provenance = source.slice(source.indexOf('function commandProvenanceSources('), source.indexOf('\nfunction commandMetadataRows('));
  const commandProvenanceSources = runInNewContext(`${provenance}\ncommandProvenanceSources;`, { sourceHref: (entry) => entry.source_url || `/?doc=${entry.document_id}` });
  const first = { ...item(0), source_url: '/manual/store' };
  assert.equal(commandProvenanceSources([first, { ...first, id: 'another/syntax' }]).length, 1);
  const historical = { ...item(1), document_id: 'release/store', authority: 'release-note', source_url: '/release.pdf#page=7', source_page: 7, publication_version: '2.5', provenance: [{ source_url: '/release.pdf#page=7', source_page: 7, publication_version: '2.5' }, { source_url: '/release.pdf#page=8', source_page: 8, publication_version: '2.5' }, { source_url: '/other.pdf#page=7', source_page: 7, publication_version: '2.4' }] };
  const sources = commandProvenanceSources([first, historical]);
  assert.equal(sources.length, 4);
  assert.deepEqual(Array.from(sources, (entry) => entry.source_page || null), [null, 7, 8, 7]);
});

test('live API details stay collapsed and use exact captured signatures and internal token metadata', () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const detailsCode = source.slice(source.indexOf('function validLiveBuild('), source.indexOf('\nfunction occurrencesPanel('));
  const makeNode = (tag, className, text) => ({ tag, className, text, children: [], append(...children) { this.children.push(...children); } });
  const liveApiDetails = runInNewContext(`${detailsCode}\nliveApiDetails;`, {
    node: makeNode,
    sourceLink: (text, href) => ({ tag: 'a', text, href }),
    docUrl: (id) => `/?doc=${encodeURIComponent(id)}`,
    document: { createTextNode: (text) => ({ text }) },
  });
  assert.equal(liveApiDetails({}), null);
  const build = { version: '0.0.1', codeType: 'test', hostType: 'test' };
  const signature = { name: 'Example', scope: 'global', signature: 'Example(number: n): string', source_function: 'ExampleCapture', source_url: '/example/capture.txt', captured_at: '2000-01-01', build };
  const luaDetails = liveApiDetails({ live_lua_signature: signature });
  assert.equal(luaDetails.tag, 'details');
  assert.equal(luaDetails.open, undefined);
  assert.equal(luaDetails.children[0].text, 'Live API details');
  assert.equal(luaDetails.children[2].text, signature.signature);
  assert.equal(luaDetails.children[1].children[0].href, signature.source_url);
  const token = { name: 'ExampleToken', index: 7, source_function: 'ExampleCapture', behaviour_verified: false, build };
  const tokenDetails = liveApiDetails({ live_keyword_tokens: [token] });
  assert.ok(tokenDetails.children[2].text.includes('internal metadata, not command syntax or aliases'));
  assert.equal(tokenDetails.children[1].children[0].href, '/?doc=live-lua%2Fcoverage-audit');
  assert.ok(tokenDetails.children[3].children[0].children[1].text.includes(`internal token index ${token.index}`));
  assert.throws(() => liveApiDetails({ live_keyword_tokens: [{ ...token, behaviour_verified: true }] }), /invalid metadata/);
});
