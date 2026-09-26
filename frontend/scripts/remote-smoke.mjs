// Exercises the built app through a prefix-stripping HTTP proxy with a fresh library.
// This models Jupyter URL rewriting, not its authentication service.
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
const project=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const temp=mkdtempSync(path.join(tmpdir(),'studio-proxy-smoke-'));
const backendPort=8768, proxyPort=8769, debugPort=9768;
const prefix='/user/fixture/proxy/8768';
const origin=`http://127.0.0.1:${proxyPort}`;
const base=origin+prefix;
const escaped=[];
let backend, browser, ws, proxy, output='';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn, label) {
  for(let i=0;i<200;i++){try {if(await fn())return;}catch{} await pause(150);}
  throw new Error('Timed out: '+label);
}
try {
  for(const port of [backendPort,proxyPort,debugPort]) {
    const probe=http.createServer();
    await new Promise((resolve,reject)=>{probe.once('error',reject);probe.listen(port,'127.0.0.1',resolve);});
    await new Promise(resolve=>probe.close(resolve));
  }
  const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!key.startsWith('ARTIST_')));
  backend=spawn(path.join(project,'.venv','Scripts','python.exe'),[path.join(project,'studio.py'),'run','--library',path.join(temp,'library'),'--port',String(backendPort),'--base-path',prefix,'--no-browser'],{cwd:project,env,windowsHide:true});
  backend.stdout.on('data',data=>output+=data);backend.stderr.on('data',data=>output+=data);
  await until(async()=> (await fetch(`http://127.0.0.1:${backendPort}/api/health`)).ok,'backend');
  writeFileSync(path.join(temp,'library','thumbnails','fixture.png'),Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64'));
  proxy=http.createServer((req,res)=>{
    if(!req.url.startsWith(prefix+'/')) { if(req.url!='/favicon.ico') escaped.push(req.url);res.writeHead(404);res.end();return; }
    if(!['GET','HEAD'].includes(req.method) && req.headers['x-xsrftoken']!=='fixture-xsrf') {res.writeHead(403);res.end('Missing proxy XSRF header');return;}
    const upstream=http.request({hostname:'127.0.0.1',port:backendPort,path:req.url.slice(prefix.length),method:req.method,headers:req.headers},reply=>{res.writeHead(reply.statusCode,{...reply.headers,'set-cookie':'_xsrf=fixture-xsrf; Path=/; SameSite=Lax'});reply.pipe(res);});
    upstream.on('error',()=>{res.writeHead(502);res.end();});req.pipe(upstream);
  });
  await new Promise(resolve=>proxy.listen(proxyPort,'127.0.0.1',resolve));
  browser=spawn(process.env.EDGE_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',['--headless=new','--disable-gpu','--no-first-run',`--remote-debugging-port=${debugPort}`,`--user-data-dir=${path.join(temp,'edge')}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let tab;
  await until(async()=>{tab=(await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item=>item.type==='page');return tab;},'browser');
  ws=new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let sequence=0;const pending=new Map(),errors=[];
  ws.addEventListener('message',event=>{const msg=JSON.parse(event.data);if(pending.has(msg.id)){const p=pending.get(msg.id);clearTimeout(p.timer);pending.delete(msg.id);msg.error?p.reject(new Error(JSON.stringify(msg.error))):p.resolve(msg.result);}if(msg.method==='Runtime.exceptionThrown')errors.push(msg.params);});
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout'));},10000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
  async function js(expression){const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;}
  await cdp('Runtime.enable');await cdp('Page.enable');
  await cdp('Page.navigate',{url:base+'/settings'});
  await until(()=>js("!!document.querySelector('[aria-label=\"Maximum longest side\"]')"),'processing UI at deep link');
  if(!await js("document.body.innerText.includes('Active server and storage') && document.title==='Illustration Scrapping Studio'"))throw new Error('Missing server panel/title');
  await js("(()=>{const el=document.querySelector('[aria-label=\"Storage reserve GiB\"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,'7');el.dispatchEvent(new Event('input',{bubbles:true}));})()");
  await js("[...document.querySelectorAll('button')].find(e=>e.textContent==='Save storage reserve').click()");
  await until(async()=> (await (await fetch(base+'/api/settings/server')).json()).reserve_gib===7,'saved storage reserve');
  await js("(()=>{const el=document.querySelector('[aria-label=\"Maximum longest side\"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,'3072');el.dispatchEvent(new Event('input',{bubbles:true}));})()");
  await js("[...document.querySelectorAll('button')].find(e=>e.textContent==='Save processing settings').click()");
  await until(async()=> (await (await fetch(base+'/api/settings/processing')).json()).max_dimension===3072,'saved processing');
  if(!await js(`new Promise(resolve=>{const image=new Image();image.onload=()=>resolve(true);image.onerror=()=>resolve(false);image.src=document.querySelector('meta[name="studio-base"]').content+'/static/thumbnails/fixture.png';})`))throw new Error('Prefixed thumbnail failed');
  await js(`[...document.querySelectorAll('a')].find(el=>el.textContent.trim()==='Dashboard').click()`);
  await until(()=>js(`[${JSON.stringify(prefix)},${JSON.stringify(prefix+'/')}].includes(location.pathname)`),'dashboard navigation');
  await cdp('Page.navigate',{url:base+'/settings'});
  await until(()=>js("document.querySelector('[aria-label=\"Maximum longest side\"]')?.value==='3072'"),'reconnect retained settings');
  const events=await (await fetch(base+'/api/diagnostics?limit=100')).json();
  if(!events.items.some(item=>item.event==='settings.storage' || item.kind==='settings.storage')) {
    // Event name is schema-owned; inspect serialized entries for the exact event code.
    if(!JSON.stringify(events.items).includes('settings.storage'))throw new Error('Missing prefixed request diagnostics');
  }
  if(errors.length || escaped.length)throw new Error(JSON.stringify({errors,escaped}));
  const shot=await cdp('Page.captureScreenshot',{format:'png'});writeFileSync(path.join(temp,'proxy-settings.png'),Buffer.from(shot.data,'base64'));
  console.log(JSON.stringify({status:'passed',checks:['deep-link UI','settings load/save','server storage reserve','thumbnail URL','router prefix','browser reconnect','diagnostics','no escaped requests'],artifacts:temp},null,2));
} catch(error) {console.error(error);console.error(output.slice(-4000));process.exitCode=1;}
finally {ws?.close();browser?.kill();backend?.kill();proxy?.close();setTimeout(()=>process.exit(process.exitCode||0),1000);}
