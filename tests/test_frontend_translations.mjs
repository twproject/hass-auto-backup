import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../custom_components/auto_backup/frontend/translations.js',import.meta.url),'utf8');
const {en,it,languageOf,translate,escapeHtml}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
assert.deepEqual(Object.keys(it).sort(),Object.keys(en).sort(),'Both supported languages must define the complete interface');
assert.equal(languageOf({language:'it-IT'}),'it');
assert.equal(languageOf({locale:{language:'en-GB'},language:'it'}),'en');
assert.equal(languageOf({language:'de'}),'en');
assert.equal(translate('it','testOk',{count:3}),'Connesso · 3 copie');
assert.equal(translate('en','testOk',{count:3}),'Connected · 3 backups');
assert.equal(escapeHtml('<img src=x onerror="bad">'), '&lt;img src=x onerror=&quot;bad&quot;&gt;');
assert.equal(escapeHtml(null),'');
console.log(`Frontend translations: ${Object.keys(en).length} matching English/Italian entries; locale selection, fallback, interpolation and HTML escaping passed.`);
import vm from 'node:vm';
const panelSource=await readFile(new URL('../custom_components/auto_backup/frontend/panel-v2.js',import.meta.url),'utf8');
let Panel;
class ElementStub {
  attachShadow(){this.shadowRoot={};}
  set lang(value){throw new Error('Custom element constructor must not set reflected attributes');}
}
vm.runInNewContext(panelSource.replace(/^import .*;\r?\n/,''),{HTMLElement:ElementStub,languageOf,translate,esc:escapeHtml,customElements:{get:()=>undefined,define:(name,type)=>{Panel=type;}}});
assert.doesNotThrow(()=>new Panel(),'The panel must be constructible before HA connects it');
console.log('Custom-element construction passed without modifying host attributes.');

const instance=new Panel();instance.data={busy:false,capabilities:{nas_enabled:false},nas:{status:'disabled'}};assert.equal(instance.displayStatus(),'standardBackup');instance.data.busy=true;assert.equal(instance.displayStatus(),'running');instance.data.busy=false;instance.data.capabilities.nas_enabled=true;instance.data.nas.status='ok';assert.equal(instance.displayStatus(),'ok');console.log('Standard/NAS header status follows the active mode.');
