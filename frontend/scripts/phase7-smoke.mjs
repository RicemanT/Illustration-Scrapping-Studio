// Offline end-to-end test using installed Edge + Node's built-in CDP WebSocket.
// Temporary fixture library/profile are retained under OS temp for diagnostics.
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const temp = mkdtempSync(path.join(tmpdir(), 'artist-phase7-smoke-'));
const port = 8766, debugPort = 9766;
const python = path.join(project,'.venv','Scripts','python.exe');
const edgePath = process.env.EDGE_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const origin = `http://127.0.0.1:${port}`;
let backend, browser, ws;
let inspectFailure;
let output = '';
const pause = ms => new Promise(r => setTimeout(r,ms));
async function until(fn, description, timeout=30000) {
  const start=Date.now();
  while (Date.now()-start < timeout) { try { const result=await fn(); if (result) return result; } catch {} await pause(150); }
  throw new Error(`Timed out: ${description}`);
}
try {
  // Fail if another application owns either port; never attach to user sessions.
  for (const url of [origin,`http://127.0.0.1:${debugPort}/json/version`]) {
    try { await fetch(url); throw new Error(`Port already in use: ${url}`); } catch(e) { if (!String(e).includes('fetch failed')) throw e; }
  }
  backend = spawn(python,[path.join(project,'backend','benchmarks','browser_fixture.py')], { cwd:path.join(project,'backend'), windowsHide:true,
    env:{...process.env, ARTIST_TEST_FIXTURE:'phase7', ARTIST_TEST_UI:process.argv.includes('--ui') ? '1' : '0', ARTIST_TEST_COUNTS:(process.argv.includes('--counts') || process.argv.includes('--groups') || process.argv.includes('--ui') || process.argv.includes('--collections')) ? '1' : '0', ARTIST_LIBRARY_PATH:path.join(temp,'library'), ARTIST_DB_PATH:path.join(temp,'library','index.db'), ARTIST_TEST_PORT:String(port)} });
  backend.stdout.on('data',d => output+=d); backend.stderr.on('data',d => output+=d);
  await until(async () => (await fetch(`${origin}/api/health`)).ok,'fixture backend');
  browser = spawn(edgePath,['--headless=new','--window-size=1440,1000','--disable-gpu','--no-first-run','--no-default-browser-check',`--remote-debugging-port=${debugPort}`,`--user-data-dir=${path.join(temp,'edge')}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
  const tab = await until(async () => (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(t => t.type==='page'),'isolated browser');
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => { ws.addEventListener('open',resolve,{once:true}); ws.addEventListener('error',reject,{once:true}); });
  let sequence=0; const pending=new Map(); const runtimeErrors=[];
  ws.addEventListener('message',event => { const msg=JSON.parse(event.data); if(msg.id && pending.has(msg.id)) { const {resolve,reject,timer}=pending.get(msg.id); clearTimeout(timer); pending.delete(msg.id); msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result); } if(msg.method==='Runtime.exceptionThrown') runtimeErrors.push(msg.params); });
  function cdp(method,params={}) { return new Promise((resolve,reject) => { const id=++sequence; const timer=setTimeout(() => {pending.delete(id);reject(new Error(`CDP timeout: ${method}`));},10000); pending.set(id,{resolve,reject,timer}); ws.send(JSON.stringify({id,method,params})); }); }
  async function js(expression) { const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true}); if(result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value; }
  inspectFailure = async () => { console.error('Browser errors:',JSON.stringify(runtimeErrors)); console.error('Page:',await js('document.body.innerText')); };
  async function click(text) { await until(() => js(`(() => {const e=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)} && !e.disabled); if(e){e.click();return true;}return false;})()`),`button ${text}`); }
  async function contains(text) { return until(() => js(`document.body.innerText.includes(${JSON.stringify(text)})`),`text ${text}`); }
  await cdp('Runtime.enable'); await cdp('Page.enable');
  await cdp('Page.navigate',{url:origin});
  await until(() => js(`(() => {const a=document.querySelector('a[href="/folder/1"]');if(a){a.click();return true;}return false;})()`),'folder link');
  await contains('Local view filters');
  if (process.argv.includes('--ui')) {
    await until(() => js(`document.querySelectorAll('[data-testid="justified-gallery"] img').length===2`),'proportional gallery');
    const ratios=await js(`[...document.querySelectorAll('[data-testid="justified-gallery"] img')].map(e=>{const r=e.getBoundingClientRect();return r.width/r.height;})`);
    if(!ratios.some(r=>Math.abs(r-2/3)<0.01) || !ratios.some(r=>Math.abs(r-2)<0.01)) throw new Error('Gallery aspect ratios are incorrect');
    await js(`(() => {const e=document.querySelector('[aria-label="Thumbnail size"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'320');e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await until(() => js(`localStorage.getItem('artist.tileHeight')==='320'`),'saved thumbnail size');
    await js(`document.querySelector('[aria-controls="folder-sources"]').click()`);
    await until(() => js(`document.querySelector('#folder-sources').hidden && localStorage.getItem('artist.sourcesOpen')==='false'`),'collapsed sources');
    await cdp('Page.navigate',{url:origin});
    await until(() => js(`(() => {const a=document.querySelector('a[href="/folder/1"]');if(a){a.click();return true;}return false;})()`),'reopen folder');
    await contains('Local view filters');
    if(!await js(`document.querySelector('#folder-sources').hidden && document.querySelector('[aria-label="Thumbnail size"]').value==='320'`)) throw new Error('UI preferences did not survive reload');
    await cdp('Emulation.setDeviceMetricsOverride',{width:900,height:800,deviceScaleFactor:1,mobile:false});
    await until(() => js(`(() => {const gallery=document.querySelector('[data-testid="justified-gallery"]');return gallery && gallery.scrollWidth<=gallery.clientWidth+1;})()`),'responsive gallery width');
    await cdp('Emulation.clearDeviceMetricsOverride');
    await js(`document.querySelector('[aria-controls="folder-sources"]').click()`);
    await js(`document.querySelector('img[alt="Image 1"]').click()`);
    await contains('File locations');
    await js(`document.querySelector('[role="dialog"] button[aria-label="Previous image"]:not(:disabled), [role="dialog"] button[aria-label="Next image"]:not(:disabled)').click()`);
    await until(() => js(`Boolean(document.querySelector('[role="dialog"] img[alt="Image 2"]'))`),'viewer next/previous image');
    await click('Hide information');
    await until(() => js(`localStorage.getItem('artist.viewerInfo')==='false'`),'viewer info preference');
    await click('Actual size'); await click('Fit image'); await click('Show information');
    const viewerShot=await cdp('Page.captureScreenshot',{format:'png'}); writeFileSync(path.join(temp,'viewer.png'),Buffer.from(viewerShot.data,'base64'));
    await until(() => js(`Boolean(document.querySelector('[role="dialog"] textarea[placeholder="1girl, solo, blue hair"]:not(:disabled)'))`),'viewer metadata loaded');
    await js(`(() => {const e=document.querySelector('[role="dialog"] textarea[placeholder="1girl, solo, blue hair"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,e.value+', unsaved fixture');e.dispatchEvent(new Event('input',{bubbles:true}));window.discardPrompts=0;window.confirm=()=>{window.discardPrompts++;return false;};})()`);
    await click('Close');
    if(!await js(`window.discardPrompts===1 && Boolean(document.querySelector('[role="dialog"]'))`)) throw new Error('Unsaved tag navigation guard failed');
    await js('window.confirm=()=>true'); await click('Close');
    if(await js(`document.querySelector('[data-app-shell]').inert`)) throw new Error('Viewer left background inert');
    await click('import'); await click('Preview'); await contains('Select visible new');
    await js(`(() => {const e=document.querySelector('[aria-label="Import result filter"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,'new');e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await until(() => js(`document.querySelectorAll('[aria-label="Remote preview gallery"] button[aria-pressed]').length===1`),'new-only import filter');
    await click('Select visible new'); await contains('1 selected across pages');
    const importShot=await cdp('Page.captureScreenshot',{format:'png'}); writeFileSync(path.join(temp,'import.png'),Buffer.from(importShot.data,'base64'));
    await click('Clear selection'); await click('gallery');
  }
  await click('tags'); await contains('blue hair');
  await until(() => js(`(() => {const b=document.querySelector('button[title="Require this tag in Gallery"]');const e=[...document.querySelectorAll('button[title="Require this tag in Gallery"]')].find(b=>b.textContent.includes('blue hair'));if(e){e.click();return true;}return false;})()`),'tag click');
  await contains('AND blue hair'); await contains('1 matching images');
  await until(() => js(`(() => {const img=document.querySelector('img[alt="Image 1"]');if(img){img.click();return true;}return false;})()`),'image detail');
  await contains('File locations');
  await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))`);
  await click('dataset'); await click('Validate current filter scope');
  await contains('scan: completed'); await contains('missing_thumbnail');
  await until(() => js(`(() => {const box=document.querySelector('input[aria-label="Select repair for image 1 missing_thumbnail"]');if(box){box.click();return true;}return false;})()`),'explicit repair selection');
  await js('window.confirm=()=>true'); await click('Repair 1 selected issues');
  await contains('repair: completed'); await contains('repaired');
  await click('Validate current filter scope'); await contains('scan: completed');
  await until(() => js(`!document.body.innerText.includes('missing_thumbnail')`),'thumbnail repaired');
  await click('gallery'); await click('Preview reapply filters'); await contains('1 matched');
  await click('Select preview page'); await click('Confirm review of 1 selected'); await contains('Undo filter review'); await click('Undo filter review');
  await until(() => js(`!document.body.innerText.includes('Undo filter review')`),'review undo');
  const query=await (await fetch(`${origin}/api/folders/1/images/query`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filters:{required_tags:['blue hair']}})})).json();
  if(query.total!==1 || query.items[0].review_status!=='pending') throw new Error('Review undo / filter result mismatch');
  if (process.argv.includes('--counts')) {
    async function sidebarCount(count) {
      await until(() => js(`document.querySelector('a[href="/folder/1"]')?.innerText.includes('${count} images')`),`sidebar count ${count}`);
      await contains(`Images: ${count}`);
    }
    await click('Clear filters'); await contains('2 matching images');
    await click('Select page (Q)'); await click('Delete selected (W)'); await sidebarCount(0);
    await click('Sync now'); await contains('20 matching images'); await sidebarCount(20);
    await until(() => js(`document.querySelector('h1')?.innerText.includes('20 images')`),'folder header 20');
    await click('Select page (Q)'); await click('Delete selected (W)'); await sidebarCount(0);
    await click('Recover last deletion (20)'); await sidebarCount(20); await contains('20 matching images');
    await js(`document.querySelector('header a[href="/"]').click()`);
    const response = await fetch(`${origin}/api/sync/folder/1/danbooru?limit=20`,{method:'POST'});
    if (!response.ok) throw new Error('Off-page fixture sync failed');
    await sidebarCount(40);
    await until(() => js(`Array.from(document.querySelectorAll('h3')).find(e=>e.textContent==='Total Images')?.parentElement.innerText.includes('40')`),'dashboard total 40');
  }
  if (process.argv.includes('--groups')) {
    async function setValue(selector, value) {
      await js(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); const proto=e.tagName==='SELECT' ? HTMLSelectElement.prototype : e.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value').set.call(e,${JSON.stringify(value)}); e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    }
    async function link(href) { await until(() => js(`(() => {const e=document.querySelector('a[href="${href}"]'); if(e){e.click();return true;}return false;})()`),`link ${href}`); }
    const get = async route => (await (await fetch(origin+route)).json());
    await link('/groups'); await contains('Collection groups');
    await setValue('[aria-label="Group name"]','Danbooru batch'); await click('Create group');
    await contains('Import collection list');
    let allGroups=await get('/api/groups'); const dan=allGroups.find(group=>group.name==='Danbooru batch');
    const listPath=path.join(temp,'artists.txt'); writeFileSync(listPath,'\ufefffirst_artist\r\nsecond artist\r\nFIRST ARTIST\r\n');
    const doc=await cdp('DOM.getDocument');
    const input=await cdp('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'input[type=file]'});
    await cdp('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[listPath]});
    await click('Preview list'); await contains('2 new'); await contains('1 duplicate lines');
    await click('Create collections'); await contains('Created 2 collections.');
    await click('Preview list'); await contains('2 existing');
    await setValue('[aria-label="Group name"]','E621 batch');
    await setValue('[aria-label="Group provider"]','e621'); await click('Create group');
    await until(() => js(`document.querySelector('main h2')?.innerText.includes('E621 batch')`),'e621 group selected');
    allGroups=await get('/api/groups'); const e621=allGroups.find(group=>group.name==='E621 batch');
    await setValue('[aria-label="Collection list"]','first_artist'); await click('Preview list'); await contains('1 new');
    await click('Create collections'); await contains('Created 1 collections.');
    await link(`/groups/${dan.id}`); await contains('2 collections');
    await click('Scrape group'); await contains('Start group scrape');
    await setValue('input[type=number]','2'); await click('Start group scrape');
    await contains('Group Danbooru batch completed');
    let folders=await get('/api/folders/');
    if(folders.filter(f=>f.group_id===dan.id).some(f=>f.image_count!==2) || folders.find(f=>f.group_id===e621.id).image_count!==0) throw new Error('Group scrape crossed group boundaries');
    for(const folder of folders.filter(f=>f.group_id===dan.id)) {
      if(!existsSync(path.join(temp,'library','images',folder.slug))) throw new Error('Missing group directory');
      if(folder.sources.length!==1 || folder.sources[0].provider!=='danbooru') throw new Error('Incorrect group source');
    }
    await link(`/groups/${e621.id}`); await click('Scrape group');
    await setValue('input[type=number]','1'); await click('Start group scrape'); await contains('Group E621 batch completed');
    folders=await get('/api/folders/');
    if(folders.find(f=>f.group_id===e621.id).image_count!==1 || folders.filter(f=>f.group_id===dan.id).some(f=>f.image_count!==2)) throw new Error('Second group scrape isolation failed');
    await link(`/groups/${dan.id}`); await setValue('[aria-label="Existing folder to move"]','1'); await click('Move into group');
    await contains('Folder and its image/thumbnail directories moved.');
    const moved=await get('/api/folders/1');
    if(moved.group_id!==dan.id || !moved.slug.startsWith(dan.slug+'/') || !existsSync(path.join(temp,'library','images',moved.slug))) throw new Error('Physical folder move failed');
    await link('/folder/1'); await contains('Local view filters');
  }
  if (process.argv.includes('--collections')) {
    async function setValue(selector, value) {
      await js(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); const proto=e.tagName==='SELECT' ? HTMLSelectElement.prototype : e.tagName==='TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value').set.call(e,${JSON.stringify(value)}); e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    }
    await click('+ New Collection');
    await setValue('[aria-label="Collection type"]', 'character');
    await setValue('form input[type=text]', 'Miku collection');
    await setValue('form input[placeholder="hatsune miku"]', 'hatsune miku');
    await click('Create Collection');
    await contains('Miku collection');
    const folders = await (await fetch(origin + '/api/folders/')).json();
    const character = folders.find(f => f.name === 'Miku collection');
    if (character?.type !== 'character' || character.artist_tag_template !== null) throw new Error('Character collection inherited an artist caption');
    await js(`document.querySelector('a[href="/groups"]').click()`); await contains('Collection groups');
    await setValue('[aria-label="Group name"]', 'Topic collection tests'); await click('Create group');
    await contains('Import collection list');
    await setValue('[aria-label="List type"]', 'tag');
    await setValue('[aria-label="Collection list"]', 'landscape sunset\nblue_hair -comic');
    await click('Preview list'); await contains('2 new'); await click('Create collections'); await contains('Created 2 collections');
    const topics = (await (await fetch(origin + '/api/folders/')).json()).filter(f => f.type === 'tag');
    if (topics.length !== 2 || !topics.some(f => f.query === 'blue_hair -comic')) throw new Error('Bulk tag query changed');
  }
  if (process.argv.includes('--preferences')) {
    await js(`document.querySelector('a[href="/settings"]').click()`); await contains('Import processing');
    await until(() => js(`!!document.querySelector('[aria-label="Parallel workers"]')`), 'worker input');
    await js(`(() => {const e=document.querySelector('[aria-label="Parallel workers"]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'12'); e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await click('Save worker count'); await contains('next job will use 12 workers');
    await until(() => js(`!!document.querySelector('[aria-label="Maximum longest side"]')`), 'processing loaded');
    await js(`(() => {const e=document.querySelector('[aria-label="Maximum longest side"]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'3072'); e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await click('Save processing settings'); await contains('Processing settings saved for future jobs.');
    const prefs = await (await fetch(origin+'/api/settings/processing')).json();
    const workers = await (await fetch(origin+'/api/settings/parallelism')).json();
    if (prefs.max_dimension !== 3072 || workers.workers !== 12 || workers.maximum !== null) throw new Error('Processing/worker settings did not persist');
  }
  if (process.argv.includes('--logs')) {
    const failure = await js(`fetch('/api/folders/999999999').then(async response => ({ status: response.status, data: await response.json(), id: response.headers.get('x-request-id') }))`);
    if (failure.status !== 404 || !failure.data.diagnostic || failure.id !== failure.data.request_id) throw new Error('Diagnostic response missing correlation');
    await js(`window.dispatchEvent(new CustomEvent('artist-notice', { detail: { message: 'Fixture error', error: true, requestId: '${failure.id}' } }))`);
    await until(() => js(`(() => { const link = document.querySelector('a[href="/logs?request_id=${failure.id}"]'); if (!link) return false; link.click(); return true; })()`), 'error diagnostics link');
    await contains('Logs & Diagnostics');
    await contains('The requested resource was not found');
    await js(`document.querySelectorAll('details').forEach(item => { item.open = true; })`);
    await contains('Possible causes:');
    const history = await js(`fetch('/api/diagnostics?request_id=${failure.id}').then(r => r.json())`);
    if (history.items.length !== 3 || history.items.some(e => e.request_id !== failure.id)) throw new Error('Request filter mismatch');
    await click('Export this page');
    const screenshot = await cdp('Page.captureScreenshot', { format: 'png' });
    writeFileSync(path.join(temp, 'diagnostics.png'), Buffer.from(screenshot.data, 'base64'));
  }
  if(runtimeErrors.length) throw new Error(`Browser runtime errors: ${JSON.stringify(runtimeErrors)}`);
  const screenshot=await cdp('Page.captureScreenshot',{format:'png'});
  writeFileSync(path.join(temp,'phase7-smoke.png'),Buffer.from(screenshot.data,'base64'));
  console.log(JSON.stringify({status:'passed',uiRegression:process.argv.includes('--ui'),countRegression:process.argv.includes('--counts'),groupRegression:process.argv.includes('--groups'),checks:['tag click → filtered gallery','image detail → file locations','QA scan → explicit thumbnail repair → revalidate','filter preview → selected review → undo'],artifacts:temp},null,2));
} catch(error) {
  await inspectFailure?.().catch(() => {});
  console.error(error); console.error(output.slice(-8000)); process.exitCode=1;
} finally {
  ws?.close(); browser?.kill(); backend?.kill();
  setTimeout(() => process.exit(process.exitCode || 0), 1000);
}
