/* Dependency-free browser logic tests. Run with: node tests/test_polling.js
 * The runner also accepts the template source in any JavaScript test host.
 */
// Run scenarios against JavaScript extracted from the HTML template.
// Return successful test names; throw on the first failure.
async function runPollingTests(template) {
 const source=[...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
 // Throw a test error when condition is false, using the supplied message.
 const assert=(condition,message)=>{if(!condition)throw Error(message)};
 // Let promise continuations run without waiting for the network.
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
 // Build a reproducible job with a status, progress, and ID.
 const job=(status='running',progress=52,id='a'.repeat(32))=>({id,status,progress_percent:progress,filename:'example.pdf',created_at:'2026-10-31T12:00:00Z'});
 // Create a fresh environment with controlled DOM, requests, and timers.
 // These tests start neither a real browser nor an HTTP server.
 function setup() {
  class Element {
   // Initialize a minimal DOM node and properties used by the scenarios.
   // classList provides no-op methods so CSS calls are accepted.
   constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.dataset={};this.attributes={};this.scrollTop=0;this.scrollHeight=200;this.clientHeight=100;this.classList={add(){},remove(){},toggle(){}};this.replacements=0}
   // Append child nodes to the current node.
   append(...children){this.children.push(...children)}
   // Replace children and count simulated DOM rebuilds.
   replaceChildren(...children){this.children=children;this.replacements++}
   // Assign plain text and remove children, as the DOM does.
   set textContent(value){this.text=value;this.children=[]}
   // Read assigned text, or an empty string when none exists.
   get textContent(){return this.text||''}
   // Store an attribute after converting its value to text.
   setAttribute(key,value){this.attributes[key]=String(value)}
   // Store a handler so the event can be triggered manually.
   addEventListener(type,fn){(this.handlers[type]??=[]).push(fn)}
   // Recursively find Live-channel buttons among the children.
   querySelectorAll(selector){return this.children.flatMap(c=>[...(selector==='[data-tab]'&&c.dataset.tab?[c]:[]),...c.querySelectorAll(selector)])}
   // Accept form reset without additional simulation.
   reset(){}
  }
  const elements=Object.fromEntries(['file','drop','feedback','theme','uploadForm','fileLabel','send','refresh','jobs','jobsError'].map(id=>[id,new Element('div')]));
  elements.file.files=[{name:'example.pdf'}];
  // Find a node connected to the tree by its ID.
  const find=(node,id)=>node.id===id?node:node.children.map(c=>find(c,id)).find(Boolean);
  const events={};
  // Expose DOM operations used by the template and store its events.
  const document={hidden:false,documentElement:new Element('html'),createElement:tag=>new Element(tag),
   getElementById:id=>elements[id]||find(elements.jobs,id)||null,
   addEventListener(type,fn){events[type]=fn}};
  const timers=new Map();let timerId=0;
  // Register a simulated timer and return its ID without starting it.
  const timer=(fn,delay,interval)=>{timers.set(++timerId,{fn,delay,interval});return timerId};
  const requests=[];
  // Keep each request pending until the scenario resolves it.
  const fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
  const api=new Function('document','localStorage','matchMedia','fetch','setTimeout','clearTimeout','setInterval','FormData','alert',source+'\nreturn {refresh,toggleLive,loadLive,retry};')(
   document,{getItem(){return null},setItem(){}},()=>({matches:false,addEventListener(){}}),fetch,
   (fn,delay)=>timer(fn,delay,false),id=>timers.delete(id),(fn,delay)=>timer(fn,delay,true),class {},message=>{throw Error(message)});
  // Resolve request index with payload, then run its asynchronous continuations.
  const respond=async(index,payload,ok=true)=>{assert(!!requests[index],'Missing request '+index);requests[index].resolve({ok,status:ok?200:503,json:async()=>payload});await flush()};
  // List only six-second timers dedicated to the jobs list.
  const polls=()=>[...timers.values()].filter(t=>t.delay===6000);
  // Trigger the single timer for the requested delay; keep interval timers.
  const tick=async delay=>{const matches=[...timers.entries()].filter(([,t])=>t.delay===delay);assert(matches.length===1,'Expected one timer at '+delay);const [id,t]=matches[0];if(!t.interval)timers.delete(id);t.fn();await flush()};
  // Simulate a visibility change and run the page handler.
  const visible=async value=>{document.hidden=!value;events.visibilitychange();await flush()};
  // Simulate a click on the manual refresh button.
  const click=()=>elements.refresh.handlers.click[0]({type:'click'});
  // Simulate form submission and return its processing promise.
  const upload=()=>elements.uploadForm.handlers.submit[0]({preventDefault(){},target:elements.uploadForm});
  return {api,elements,document,requests,respond,polls,tick,visible,click,upload};
 }
 const results=[];
 // Run scenario fn and record its name only after it succeeds.
 async function test(name,fn){await fn();results.push(name)}

 await test('initial load stops for empty, completed, failed and mixed terminal lists',async()=>{
  for(const jobs of [[],[job('done',100)],[job('failed')],[job('done',100),job('failed',20,'b'.repeat(32))]]){
   const h=setup();assert(h.requests.length===1,'Initial request required');
   await h.respond(0,{jobs});assert(h.polls().length===0,'Terminal list must not poll');
  }
 });
 await test('queued and running jobs poll until the last active job terminates',async()=>{
  for(const status of ['queued','running']){
   const h=setup();await h.respond(0,{jobs:[job(status)]});assert(h.polls().length===1,'Active list needs one timer');
   await h.tick(6000);assert(h.polls().length===0,'No timer during request');
   await h.respond(1,{jobs:[job('done',100),job('running',80,'b'.repeat(32))]});
   assert(h.polls().length===1,'Another active job still needs polling');
   await h.tick(6000);await h.respond(2,{jobs:[job('done',100),job('failed',80,'b'.repeat(32))]});
   assert(h.polls().length===0,'Last active job terminated');
  }
 });
 await test('manual and timer refreshes share an in-flight request and a single timer',async()=>{
  const h=setup();const pending=h.api.refresh();assert(h.api.refresh()===pending,'Requests must share a promise');h.click();
  assert(h.requests.length===1,'Concurrent initial refreshes must coalesce');
  await h.respond(0,{jobs:[job()]});h.click();h.click();h.api.refresh();
  assert(h.requests.length===2&&h.polls().length===0,'Only one manual request');
  await h.respond(1,{jobs:[job()]});assert(h.polls().length===1,'Only one replacement timer');
 });
 await test('unchanged data preserves DOM and completed report attributes',async()=>{
  const h=setup();const jobs=[job('done',100)];await h.respond(0,{jobs});
  const article=h.elements.jobs.children[0],replacements=h.elements.jobs.replacements;
  h.click();await h.respond(1,{jobs});
  assert(h.elements.jobs.replacements===replacements,'Unchanged data must not rebuild DOM');
  assert(h.elements.jobs.children[0]===article,'Preserve article identity');
  const track=article.children.find(c=>c.attributes.role==='progressbar');
  assert(track.attributes['aria-valuenow']==='100'&&track.children[0].style.width==='100%','Completed progress');
  const link=article.children.find(c=>c.className==='actions').children.find(c=>c.tag==='a');
  assert(link.href===`/api/jobs/${jobs[0].id}/report`&&link.download==='analysis_aaaaaaaa.md','Native report download');
 });
 await test('hidden tabs stop polling and visibility performs one fresh load',async()=>{
  const h=setup();await h.respond(0,{jobs:[job()]});await h.visible(false);
  assert(h.polls().length===0,'Hidden page must stop its timer');await h.visible(true);h.click();
  assert(h.requests.length===2,'Visibility and manual requests must coalesce');await h.respond(1,{jobs:[job()]});
  assert(h.polls().length===1,'Visibility resumes active polling');
  await h.visible(false);h.api.refresh();await h.respond(2,{jobs:[job()]});
  assert(h.polls().length===0,'Response received while hidden must not schedule a timer');
  await h.visible(true);await h.respond(3,{jobs:[job('done',100)]});
  assert(h.polls().length===0,'Visibility must not resume a terminal list');
 });
 await test('new uploads immediately refresh an idle list and start polling',async()=>{
  const h=setup();await h.respond(0,{jobs:[]});const pending=h.upload();
  assert(h.requests[1].options.method==='POST','Upload request');await h.respond(1,{id:'b'.repeat(32)});
  assert(h.requests[2].url==='/api/jobs','Immediate refresh after upload');
  await h.respond(2,{jobs:[job('queued',0,'b'.repeat(32))]});await pending;
  assert(h.polls().length===1,'New queued job must start polling');
 });
 await test('upload during an old list request queues a fresh non-concurrent load',async()=>{
  const h=setup();const pending=h.upload();await h.respond(1,{id:'b'.repeat(32)});
  assert(h.requests.length===2,'Wait for old GET before issuing another');
  await h.respond(0,{jobs:[]});assert(h.requests.length===3,'Mutation needs a fresh GET');
  assert(h.elements.jobs.replacements===0,'Do not render the stale list');
  await h.respond(2,{jobs:[job('queued',0,'b'.repeat(32))]});await pending;
  assert(h.polls().length===1,'Poll the newly created job');
 });
 await test('a mutation arriving as a list request settles still triggers a fresh load',async()=>{
  const h=setup(),host=h.elements.jobs,replace=host.replaceChildren;
  host.replaceChildren=function(...children){
   replace.apply(this,children);this.replaceChildren=replace;
   // Model a successful POST continuation queued just before GET cleanup.
   Promise.resolve().then(()=>h.api.refresh(true));
  };
  await h.respond(0,{jobs:[]});assert(h.requests.length===2,'Do not lose a refresh during promise cleanup');
  await h.respond(1,{jobs:[job('queued',0)]});assert(h.polls().length===1,'New work needs polling');
 });
 await test('retry restarts polling even if the immediate list refresh fails',async()=>{
  const h=setup();await h.respond(0,{jobs:[job('failed')]});const pending=h.api.retry('a'.repeat(32));
  await h.respond(1,{status:'queued'});assert(h.requests[1].url.endsWith('/retry'),'Retry endpoint');
  await h.respond(2,{},false);await pending;assert(h.polls().length===1,'Known new work must retry the failed GET');
  await h.tick(6000);await h.respond(3,{jobs:[job('running')]});assert(h.polls().length===1,'Recovered active list');
 });
 await test('transient list errors preserve cards and recover without unnecessary rendering',async()=>{
  const h=setup();const jobs=[job()];await h.respond(0,{jobs});const article=h.elements.jobs.children[0];
  await h.tick(6000);h.requests[1].reject(Error('Offline'));await flush();
  assert(h.elements.jobs.children[0]===article&&!h.elements.jobsError.hidden,'Keep cards and show an error');
  assert(h.polls().length===1,'Keep polling last known active work');await h.tick(6000);await h.respond(2,{jobs});
  assert(h.elements.jobs.children[0]===article&&h.elements.jobsError.hidden,'Recover without rebuilding cards');
 });
 await test('initial failure can be recovered manually without idle polling',async()=>{
  const h=setup();await h.respond(0,{},false);assert(h.polls().length===0,'No active work is known');
  h.click();await h.respond(1,{jobs:[job('queued',0)]});assert(h.polls().length===1,'Manual recovery starts active polling');
 });
 await test('Live uses its existing timer without overlapping requests or list reloads',async()=>{
  const h=setup();await h.respond(0,{jobs:[job()]});h.api.toggleLive('a'.repeat(32));
  assert(h.requests[1].url.endsWith('/live'),'Opening Live only loads its endpoint');h.api.loadLive();await h.tick(1700);
  const reasoningTab=h.document.getElementById('live-'+'a'.repeat(32)).children[0].children[0].children[1];
  reasoningTab.handlers.click[0]();assert(h.requests.length===2,'Live requests must not overlap');
  await h.respond(1,{phase:'Analysis',content:'Preview',reasoning:'Reasoning preview',active:true});
  const panel=h.document.getElementById('live-'+'a'.repeat(32)),output=h.document.getElementById('live-content');output.scrollTop=25;
  await h.tick(6000);await h.respond(2,{jobs:[job('running',70)]});
  assert(h.document.getElementById('live-'+'a'.repeat(32))===panel,'Reuse the Live panel when progress changes');
  assert(output.textContent==='Reasoning preview'&&output.scrollTop===25,'Preserve the selected Live tab, content and scroll position');
  await h.respond(3,{phase:'Analysis',content:'Preview',reasoning:'Reasoning preview',active:true});await h.visible(false);await h.tick(1700);
  assert(h.requests.length===4,'No hidden Live requests');h.api.toggleLive('a'.repeat(32));await h.tick(1700);
  assert(h.requests.length===4,'No requests when Live is closed');
 });
 return results;
}

if(typeof module!=='undefined'&&typeof require==='function'){
 module.exports=runPollingTests;
 if(require.main===module){
  const fs=require('node:fs'),path=require('node:path');
  runPollingTests(fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8'))
   .then(results=>{for(const name of results)console.log('ok - '+name);console.log(`${results.length} tests passed`);})
   .catch(error=>{console.error(error);process.exitCode=1;});
 }
}
