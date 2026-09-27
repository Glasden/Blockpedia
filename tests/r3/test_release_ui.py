from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'src/blockpedia/static'

def test_import_identity_survives_response_loss_reload_and_new_directory_ref():
    """Run the real submit code against a server model that has accepted the POST."""
    import subprocess
    script = r'''
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[2], 'utf8');
const vm = require('node:vm');
const storage = new Map();
const runs = new Map();
const requests = [];
let redirect, sequence = 0, failRead = false;
const context = {
  sessionStorage: { getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v) },
  crypto: { randomUUID: () => (++sequence).toString(16).padStart(32, '0') },
  setText: (element, value) => { element.textContent = value; },
  window: { location: { assign: value => { redirect = value; } } },
  fetchJsonEnvelope: async url => {
    requests.push(['GET', url]);
    if (failRead) throw {status:503};
    const run = runs.get(url.split('/').pop());
    if (!run) throw {status:404};
    return {...run};
  },
  postJsonEnvelope: async (url, body) => {
    requests.push(['POST', body]);
    runs.set(body.run_id, {run_id:body.run_id, minecraft_version:body.minecraft_version, export_id:'export_A', status:'running'});
    throw new Error('server accepted; response lost');
  },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  const storedOperation'), source.indexOf('  const initializeDirectoryChooser')) +
  source.slice(source.indexOf('  const performSelectedAction'), source.indexOf('  const submitRunCommand')) +
  '\nthis.submit = performSelectedAction;', context);
const form = (ref, version='26.2', exportId='export_A', retryId=null) => {
  const feedback = {textContent:''};
  const fields = {'[data-directory-ref]':{value:ref}, '[data-directory-version]':{value:version}};
  return {dataset:{selectedExportId:exportId, ...(retryId?{retryRunId:retryId}:{})}, feedback,
    reportValidity:()=>true, setAttribute:()=>{}, removeAttribute:()=>{},
    querySelector:selector=>fields[selector], querySelectorAll:()=>[],
    closest:()=>({querySelector:()=>feedback})};
};
(async()=>{
  await context.submit(form('dir_first'));
  const id = JSON.parse(storage.get('blockpedia.import')).id;
  assert.equal(runs.size, 1);
  // A new form models the reload; directory browsing yields a different ref.
  await context.submit(form('dir_second'));
  assert.equal(redirect, '/imports/'+id);
  assert.equal(requests.filter(x=>x[0]==='POST').length, 1);
  assert.equal(JSON.parse(storage.get('blockpedia.import')).id, id);
  assert.equal(runs.size, 1);
  for (const changed of [form('dir_new','26.2','export_B'), form('dir_new','1.21','export_A')]) {
    redirect = null;
    await context.submit(changed);
    assert.match(changed.feedback.textContent, /IMPORT_SOURCE_SELECTION_CHANGED/);
    assert.equal(redirect, null);
    assert.equal(runs.size, 1);
  }
  // A read failure must never be treated as permission to POST a new run.
  failRead = true;
  await context.submit(form('dir_third'));
  assert.equal(requests.filter(x=>x[0]==='POST').length, 1);
  failRead = false;
  // A missing operation retries its saved identity, even with a fresh ref.
  runs.delete(id);
  await context.submit(form('dir_fourth'));
  assert.equal(requests.at(-1)[1].run_id, id);
  // Explicit retry URLs also preserve the identity of interrupted operations.
  runs.get(id).status = 'interrupted';
  await context.submit(form('dir_retry','26.2','export_A',id));
  assert.equal(requests.at(-1)[1].run_id, id);
  const explicitId = 'run_'+'d'.repeat(32);
  await context.submit(form('dir_retry_missing','26.2','export_A',explicitId));
  assert.equal(requests.at(-1)[1].run_id, explicitId);
  // Only the new-action control's storage reset permits another ID.
  storage.delete('blockpedia.import');
  await context.submit(form('dir_new_action'));
  assert.notEqual(requests.at(-1)[1].run_id, id);
  assert.notEqual(requests.at(-1)[1].run_id, explicitId);
  await context.submit(form('dir_return','26.2','export_A',explicitId));
  assert.equal(redirect, '/imports/'+explicitId);
  assert.equal(JSON.parse(storage.get('blockpedia.import')).id, explicitId);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run(['node', '-', str(STATIC / 'studio.js')], input=script, text=True, check=True)
