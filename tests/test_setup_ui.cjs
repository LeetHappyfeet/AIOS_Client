// Exercise the shipped setup script without a network or browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const page = fs.readFileSync(path.join(__dirname, '../src/aios_client/static/setup.html'), 'utf8');
const source = page.split('<script>')[1].split('</script>')[0];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', className: '', children: [],
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
    querySelector() { return {}; },
  });
  return elements.get(id);
}
const calls = [];
let failStorage = false;
const context = vm.createContext({
  document: {getElementById: element, querySelectorAll: () => [], createElement: () => ({})},
  structuredClone, setTimeout, setInterval: () => {}, console,
});
// Run definitions; initial page loading is covered by the API tests.
vm.runInContext(source.slice(0, source.lastIndexOf('(async()=>{try{config=await')), context);
context.api = async (route, body) => {
  calls.push({route, body});
  if (route === 'secret') {
    if (failStorage) throw Error('No usable OS credential store');
    return {secret_ref: body.storage + ':' + body.name};
  }
  if (route === 'models') return {models: ['fixture-model']};
  if (route === 'test-model') return {message: 'Tools work'};
  return {ok: true};
};
vm.runInContext('config={version:1,aios:{},endpoints:{},agents:{}}', context);
async function invoke(code) { await vm.runInContext(code, context); }

(async () => {
  for (const [id, value] of Object.entries({
    aName: 'AIOS', aHost: '192.0.2.1', aPort: '8000', aScheme: 'http',
    aStorage: 'file', aKey: 'workspace-fixture-key',
    eName: 'DeepSeek', eURL: 'https://model.example/v1', eModel: 'fixture-model',
    eTimeout: '120', eStorage: 'file', eKey: 'provider-fixture-key',
  })) element(id).value = value;
  await invoke('saveAIOS()');
  await invoke('saveEndpoint()');
  assert.equal(element('aRef').value, 'file:a:AIOS');
  assert.equal(element('eRef').value, 'file:e:DeepSeek');
  assert.equal(element('aKey').value, '');
  assert.equal(element('eKey').value, '');
  assert.equal(element('aSaved').value, 'AIOS');
  assert.equal(element('eSaved').value, 'DeepSeek');
  for (const call of calls.filter(c => c.route === 'config')) {
    assert.ok(!JSON.stringify(call.body).includes('fixture-key'));
  }
  assert.equal(calls.filter(c => c.route === 'secret').length, 2);
  await invoke('saveEndpoint()'); // blank password preserves the existing credential
  assert.equal(calls.filter(c => c.route === 'secret').length, 2);
  await invoke('loadModels()');
  assert.equal(element('models').children[0].value, 'fixture-model');
  element('eKey').value = 'replacement';
  const before = calls.length;
  await invoke('loadModels()');
  await invoke('testModel()');
  assert.equal(calls.length, before); // never test with a stale key silently
  assert.match(element('notice').textContent, /Save the connection or endpoint/);
  failStorage = true;
  element('eStorage').value = 'keyring';
  await invoke('saveEndpoint()');
  assert.equal(element('eKey').value, 'replacement'); // retry without retyping
  assert.equal(element('eRef').value, 'file:e:DeepSeek');
  assert.match(element('notice').textContent, /No usable OS credential store/);
  failStorage = false;
  element('eStorage').value = 'file';
  await invoke("credential('e')");
  assert.equal(element('eKey').value, '');
  assert.equal(calls.at(-1).route, 'config');
  element('gName').value = 'worker';
  element('gRouting').value = 'automatic';
  element('gUser').value = 'human';
  element('gScope').value = 'conversation';
  element('gHTTP').value = 'http://127.0.0.1:8080\n  https://app.example  \n';
  await invoke('saveAgent()');
  const savedAgent = calls.filter(c => c.route === 'config').at(-1).body.agents.worker;
  assert.equal(savedAgent.routing_mode, 'automatic');
  assert.equal(savedAgent.link_id, '');
  assert.equal(element('gSaved').value, 'worker');
  assert.deepEqual(Array.from(savedAgent.http_origins), ['http://127.0.0.1:8080', 'https://app.example']);
  element('gSaved').value = 'worker';
  element('gHTTP').value = '';
  await invoke("$('gSaved').onchange()");
  assert.equal(element('gHTTP').value, 'http://127.0.0.1:8080\nhttps://app.example');
  assert.equal(element('manualRouting').hidden, true);
  element('gRouting').value = 'manual';
  await invoke('routingVisibility()');
  assert.equal(element('manualRouting').hidden, false);
  console.log('Setup credential and HTTP policy UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
