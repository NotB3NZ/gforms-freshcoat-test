// Synthetic-only end-to-end runner. It creates its own DB, secret, fixture and server.
// Install tooling separately as described in docs/WEB_APP.md; no FreshCoat changes.
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {mkdtemp, mkdir, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=fileURLToPath(new URL('../../',import.meta.url));
const python=process.env.FRESHCOAT_WEB_PYTHON || resolve(root,'.venv-web/bin/python');
const {chromium}=await import(process.env.FRESHCOAT_PLAYWRIGHT_MODULE || 'playwright');
const directory=await mkdtemp(resolve(tmpdir(),'freshcoat-browser-'));
const port=process.env.FRESHCOAT_BROWSER_PORT || '18080';
const origin=`http://127.0.0.1:${port}`;
const password=randomBytes(24).toString('hex');
const env={...process.env, PYTHONPYCACHEPREFIX:resolve(directory,'pycache'), WEB_BROWSER_PASSWORD:password,
  WEB_ENV_FILE:resolve(directory,'isolated.env'), WEB_READ_ONLY_PREVIEW:'false', WEB_API_ORIGIN:'',
  WEB_ORIGIN:origin,WEB_TIMEZONE:'Asia/Manila',WEB_SESSION_SECONDS:'28800',WEB_SYNC_SECONDS:'45',WEB_SYNTHETIC:'true',WEB_LIVE_EXPORT:'false',WEB_LIVE_CONFIG:'',WEB_EVENT_ID:'freshcoat-browser-synthetic',
  WEB_FIXTURE:resolve(directory,'sheets.json'),WEB_DATABASE_URL:`sqlite:///${resolve(directory,'web.sqlite3')}`};
function run(args) {
  const result=spawnSync(python,args,{cwd:root,env,encoding:'utf8'});
  if(result.status!==0) throw new Error(result.stderr || result.error?.message || 'Python setup failed');
  return result.stdout.trim();
}
env.WEB_PASSWORD_HASH=run(['-c',"import os;from event_checkin.web.security import hash_password;print(hash_password(os.environ['WEB_BROWSER_PASSWORD']))"]);
run(['-m','event_checkin.web.cli','seed']);
const qrPath=resolve(directory,'lite.png');
env.WEB_QR_PATH=qrPath;
run(['-c',"import os,qrcode;from event_checkin.web.seed import token;qrcode.make(token('lite')).save(os.environ['WEB_QR_PATH'])"]);
const kioskQrs = {};
for (const key of ['pass', 'cancelled', 'unknown', 'special']) {
  env.WEB_QR_KEY = key; env.WEB_QR_PATH = resolve(directory, `${key}.png`);
  run(['-c', "import os,qrcode;from event_checkin.web.seed import token;qrcode.make(token(os.environ['WEB_QR_KEY'])).save(os.environ['WEB_QR_PATH'])"]);
  kioskQrs[key] = 'data:image/png;base64,' + (await readFile(env.WEB_QR_PATH)).toString('base64');
}
let server, browser, logs='';
try {
  server=spawn(python,['-m','uvicorn','event_checkin.web.app:app','--host','127.0.0.1','--port',port,'--no-proxy-headers'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',chunk=>{logs+=chunk;});server.stderr.on('data',chunk=>{logs+=chunk;});
  let ready=false;
  for(let n=0;n<100;n++){
    try { if((await fetch(origin+'/api/session')).ok){ready=true;break;} } catch {}
    if(server.exitCode!==null) break;
    await new Promise(r=>setTimeout(r,100));
  }
  if(!ready) throw new Error('Local test server could not listen. '+logs.slice(-1000));
  browser=await chromium.launch({headless:true,...(process.env.FRESHCOAT_CHROME ? {executablePath:process.env.FRESHCOAT_CHROME}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);
  await page.getByLabel('Shared staff password').fill(password);
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByLabel('Your staff nickname').fill('Browser QA');
  await page.getByRole('button',{name:'Start check-in',exact:true}).click();
  await page.getByText('Signed in as Browser QA').waitFor();
  const count=async()=> (await (await page.request.get(origin+'/api/dashboard')).json()).counts.arrived;
  assert.equal(await count(),1);
  await page.getByLabel('Scan a QR image instead').setInputFiles(qrPath);
  await page.getByRole('dialog').waitFor();
  assert.match(await page.locator('#review-title').textContent(),/Alex Rivera/);
  assert.equal(await count(),1);
  await page.getByRole('button',{name:'Cancel / Scan another',exact:true}).click();
  assert.equal(await count(),1);
  await page.getByLabel('Scan a QR image instead').setInputFiles(qrPath);
  await page.getByRole('button',{name:'Check in',exact:true}).click();
  await page.getByText(/Check-in confirmed/).waitFor();
  assert.equal(await count(),2);
  await page.getByRole('button',{name:'Scan next',exact:true}).click();
  await page.getByRole('button',{name:'Participants',exact:true}).click();
  await page.getByLabel('Name or email').fill('maria.');
  await page.getByRole('button',{name:'Search',exact:true}).click();
  await page.getByRole('button',{name:/Review María/}).click();
  await page.getByRole('dialog').waitFor();
  await mkdir(resolve(root,'frontend/test-results'),{recursive:true});
  for(const width of [320,390,768,1440]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`page overflow at ${width}`);
    const actions=await page.locator('#review .dialog-actions').boundingBox();
    assert.ok(actions.x>=0 && actions.x+actions.width<=width+1 && actions.y+actions.height<=900,`clipped dialog actions at ${width}`);
    await page.screenshot({path:resolve(root,`frontend/test-results/review-${width}.png`),fullPage:true});
  }
  // Server commits, then the browser loses its response. Refresh must reconcile the same ID.
  await page.route('**/api/checkins',async route=>{await route.fetch();await route.abort('timedout');});
  await page.getByRole('button',{name:'Check in',exact:true}).click();
  await page.getByText('Unable to confirm — retry. Keep this check-in open until its result is confirmed.').waitFor();
  const unresolved=await page.evaluate(()=>Object.entries(localStorage).find(([k])=>k.startsWith('freshcoat.pending:'))?.[1]);
  assert.ok(unresolved);
  await page.reload();
  await page.getByText(/Check-in confirmed/).waitFor();
  assert.equal(await count(),3);
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('freshcoat.pending:')).length),0);
  await page.getByRole('button',{name:'Scan next',exact:true}).click();
  await page.getByRole('button',{name:'Overview',exact:true}).click();
  for(const width of [320,390,768,1440]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:resolve(root,`frontend/test-results/overview-${width}.png`),fullPage:true});
  }
  await page.getByRole('button',{name:'Logout',exact:true}).click();
  await page.getByLabel('Shared staff password').waitFor();
  assert.equal((await page.request.get(origin+'/api/dashboard')).status(),401);
  // Simulated camera: real canvas video stream, real decoder, no physical device.
  await page.addInitScript(({qr}) => {
    window.__testStreams=[];
    const fake={
      getUserMedia:async () => {
        if (window.__denyCamera) throw new DOMException('Synthetic permission denial', 'NotAllowedError');
        const canvas=document.createElement('canvas');canvas.width=640;canvas.height=480;
        const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,640,480);
        window.__setCameraQr=async value=>{
          ctx.fillStyle='white';ctx.fillRect(0,0,640,480);
          if(value){const image=new Image();image.src=value;await image.decode();ctx.drawImage(image,160,80,320,320);}
        };
        if(!sessionStorage.getItem('synthetic-camera-used')){sessionStorage.setItem('synthetic-camera-used','yes');await window.__setCameraQr(qr);}
        const stream=canvas.captureStream(5);window.__testStreams.push(stream);return stream;
      },
      enumerateDevices:async()=>[{kind:'videoinput',deviceId:'synthetic-rear',label:'Synthetic rear camera'}]
    };
    Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:fake});
  },{qr:'data:image/png;base64,'+(await readFile(qrPath)).toString('base64')});
  await page.reload();
  await page.getByLabel('Shared staff password').fill(password);
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByLabel('Your staff nickname').fill('Simulated camera');
  await page.getByRole('button',{name:'Start check-in',exact:true}).click();
  await page.getByText('Signed in as Simulated camera').waitFor();
  let cameraLookups=0;
  page.on('request',request=>{if(request.url().endsWith('/api/lookup'))cameraLookups++;});
  await page.getByRole('button',{name:'Start camera',exact:true}).click();
  await page.getByRole('dialog').waitFor();
  await page.getByText(/Already checked in at/).waitFor();
  assert.equal(await page.getByRole('button',{name:'Check in',exact:true}).isDisabled(),true);
  assert.equal(await page.evaluate(()=>window.__testStreams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))),true);
  await page.waitForTimeout(800);
  assert.equal(cameraLookups,1,'decoding must pause throughout review');
  for(let n=0;n<5;n++){await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>Boolean(document.activeElement.closest('dialog'))),true);}
  await page.getByRole('button',{name:'Cancel / Scan another',exact:true}).click();
  await page.getByRole('button',{name:'Participants',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.__testStreams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))),true);
  await page.unroute('**/api/checkins');
  const kioskRequests=[];
  page.on('request', request=>{if(request.url().endsWith('/api/checkins'))kioskRequests.push(request.postDataJSON());});
  await page.getByRole('button',{name:'Kiosk Mode',exact:true}).click();
  await page.getByLabel('Result display time (seconds)').fill('2');
  await page.getByLabel('Mute feedback sounds').check();
  await page.getByRole('button',{name:'Start kiosk and allow camera'}).click();
  await page.getByText('Camera ready · Remove your ticket after the result.',{exact:true}).waitFor();
  const putQr=async key=>page.evaluate(qr=>window.__setCameraQr(qr),key ? kioskQrs[key] : null);
  const next=async()=>page.locator('#kiosk-title').filter({hasText:'Ready to welcome you'}).waitFor();
  await putQr('pass');
  await page.getByRole('heading',{name:'Welcome, Charlie Lim!',exact:true}).waitFor();
  const firstId=kioskRequests.at(-1).request_id;
  const committed=(await (await page.request.get(origin+'/api/requests/'+firstId)).json()).result;
  assert.equal(committed.participant.checked_in_by,'self checkin');
  assert.equal(committed.participant.method,'qr'); assert.equal(committed.participant.email,undefined);
  assert.equal((await page.request.get(origin+'/api/dashboard')).status(),403);
  assert.equal(await page.locator('#workspace').isVisible(),false);
  assert.doesNotMatch(await page.locator('#kiosk-result').innerText(),/@/);
  await next();const lookups=cameraLookups;
  await page.waitForTimeout(1400);assert.equal(cameraLookups,lookups,'held QR must not retrigger');
  assert.equal(await page.evaluate(()=>window.__testStreams.length),2,'results must retain the same camera grant');
  await putQr(null);await page.waitForTimeout(2200);await putQr('pass');
  await page.getByRole('heading',{name:'Charlie Lim is already checked in',exact:true}).waitFor();
  assert.match(await page.locator('#kiosk-detail').innerText(),/Original arrival:.*self checkin/);
  const duplicate=(await (await page.request.get(origin+'/api/requests/'+kioskRequests.at(-1).request_id)).json()).result;
  assert.equal(duplicate.participant.checked_in_at,committed.participant.checked_in_at);
  await next();await putQr('cancelled');
  await page.getByRole('heading',{name:'Ticket not eligible',exact:true}).waitFor();
  await next();await putQr('unknown');
  await page.getByRole('heading',{name:'Invalid QR',exact:true}).waitFor();
  await next();
  await page.route('**/api/checkins',async route=>{await route.fetch();await route.abort('timedout');});
  await putQr('special');await page.getByRole('heading',{name:'Confirmation pending',exact:true}).waitFor();
  const recoveryId=kioskRequests.at(-1).request_id;
  await page.reload();
  await page.getByRole('heading',{name:'Welcome, Sam Bautista!',exact:true}).waitFor();
  assert.equal(kioskRequests.filter(r=>r.request_id===recoveryId).length,1,'refresh must retrieve the committed request');
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.startsWith('freshcoat.pending:')).length),0);
  for(const width of [320,390,768,1440]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`kiosk overflow at ${width}`);
    await page.screenshot({path:resolve(root,`frontend/test-results/kiosk-${width}.png`),fullPage:true});
  }
  await next();
  await page.getByRole('button',{name:'Staff controls',exact:true}).click();
  await page.getByLabel('Staff password',{exact:true}).fill('wrong');
  await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByText('Incorrect shared password.',{exact:true}).waitFor();
  assert.equal(await page.locator('#workspace').isVisible(),false);
  await page.getByLabel('Staff password',{exact:true}).fill(password);
  await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByText('Signed in as Simulated camera',{exact:true}).waitFor();
  await page.unroute('**/api/checkins');
  await page.evaluate(()=>{window.__denyCamera=true;});
  await page.getByRole('button',{name:'Kiosk Mode',exact:true}).click();
  await page.getByRole('button',{name:'Start kiosk and allow camera'}).click();
  await page.getByRole('heading',{name:'Camera unavailable',exact:true}).waitFor();
  assert.match(await page.locator('#kiosk-camera-message').innerText(),/permission.*Restart camera/);
  await page.evaluate(()=>{window.__denyCamera=false;});
  await page.getByRole('button',{name:'Restart camera',exact:true}).click();
  await page.getByText('Camera ready · Remove your ticket after the result.',{exact:true}).waitFor();
  await page.evaluate(()=>window.__testStreams.at(-1).getTracks()[0].dispatchEvent(new Event('ended')));
  await page.getByRole('heading',{name:'Camera unavailable',exact:true}).waitFor();
  await page.getByRole('button',{name:'Staff controls',exact:true}).click();
  await page.getByLabel('Staff password',{exact:true}).fill(password);
  await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByText('Signed in as Simulated camera',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Logout',exact:true}).click();
  assert.deepEqual(errors,[]);
  console.log('Browser flow passed: staff review/commit/recovery, automatic kiosk admission, held-QR suppression, duplicate/invalid results, server attribution/privacy, kiosk refresh recovery, authenticated exit, permission/lost-camera recovery, and responsive layouts. Screenshots: frontend/test-results/');
  await context.close();
} finally {
  await browser?.close();server?.kill('SIGTERM');
  await new Promise(r=>setTimeout(r,200));
  await rm(directory,{recursive:true,force:true});
}
