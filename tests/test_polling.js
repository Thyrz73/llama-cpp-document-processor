/* Dependency-free browser logic tests. Run with: node tests/test_polling.js
 * The runner also accepts the template source in any JavaScript test host.
 */
// Run scenarios against JavaScript extracted from the HTML template.
// Return successful test names; throw on the first failure.
async function runPollingTests(template, translations={en:{},fr:{}}) {
 const source=[...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
 // Throw a test error when condition is false, using the supplied message.
 const assert=(condition,message)=>{if(!condition)throw Error(message)};
 // Let promise continuations run without waiting for the network.
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
 // Build a reproducible job with a status, progress, and ID.
 const job=(status='running',progress=52,id='a'.repeat(32))=>({id,status,progress_percent:progress,filename:'example.pdf',created_at:'2026-10-31T12:00:00Z'});
 const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
 // Create a fresh environment with controlled DOM, requests, and timers.
 // These tests start neither a real browser nor an HTTP server.
 function setup(withSettings=false) {
  class Element {
   // Initialize a minimal DOM node and properties used by the scenarios.
   // classList provides no-op methods so CSS calls are accepted.
   constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.dataset={};this.attributes={};this.scrollTop=0;this.scrollHeight=200;this.clientHeight=100;this.classList={add(){},remove(){},toggle(){}};this.replacements=0;this.value='';this.options=[]}
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
   querySelectorAll(selector){const key=selector.slice(6,-1).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());return this.children.flatMap(c=>[...(c.dataset[key]!==undefined?[c]:[]),...c.querySelectorAll(selector)])}
   focus(){document.activeElement=this}
   showModal(){this.open=true}
   close(){this.open=false}
   // Accept form reset without additional simulation.
   reset(){}
  }
  const elements=Object.fromEntries(['file','drop','feedback','theme','uploadForm','fileLabel','send','refresh','jobs','jobsError','historyNotice','deleteDialog','deleteTitle','deleteMessage','deleteDocument','deleteCancel','deleteConfirm'].map(id=>[id,new Element('div')]));
  if(withSettings){
   for(const id of ['translations','uiLanguage','languageFlag','profile','reportLanguage','detailLevel','profileHelp','configPreview','configWarnings','diagnostics','diagnosticOutput','chunk_tokens','analysis_tokens','merge_tokens','final_tokens','temperature','top_p','top_k','min_p','step_timeout_seconds','max_calls','thinking_section','thinking_merge','thinking_final'])elements[id]=new Element('div');
   elements.translations.textContent=JSON.stringify(translations);
   elements.profile.value='low';elements.reportLanguage.value='auto';elements.detailLevel.value='standard';
   for(const name of ['profile','reportLanguage','detailLevel'])elements[name].options=[{value:elements[name].value}];
   for(const stage of ['section','merge','final'])elements['thinking_'+stage].value='inherit';
  }
  elements.file.files=[{name:'example.pdf'}];
  // Find a node connected to the tree by its ID.
  const find=(node,id)=>node.id===id?node:node.children.map(c=>find(c,id)).find(Boolean);
  const events={};
  // Expose DOM operations used by the template and store its events.
  const document={hidden:false,documentElement:new Element('html'),createElement:tag=>new Element(tag),createElementNS:(namespace,tag)=>new Element(tag),
   getElementById:id=>elements[id]||find(elements.jobs,id)||null,
   addEventListener(type,fn){events[type]=fn}};
  const timers=new Map();let timerId=0;
  // Register a simulated timer and return its ID without starting it.
  const timer=(fn,delay,interval)=>{timers.set(++timerId,{fn,delay,interval});return timerId};
  const requests=[];
  // Keep each request pending until the scenario resolves it.
  const fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
  const api=new Function('document','localStorage','matchMedia','fetch','setTimeout','clearTimeout','setInterval','FormData','alert',source+'\nreturn {refresh,toggleLive,loadLive,retry,cancelJob,selectedOptions,previewConfig,schedulePreview,confirmDeletion,submitDeletion,closeDeleteDialog};')(
   document,{getItem(){return null},setItem(){}},()=>({matches:false,addEventListener(){}}),fetch,
   (fn,delay)=>timer(fn,delay,false),id=>timers.delete(id),(fn,delay)=>timer(fn,delay,true),class {constructor(){this.values=new Map()}set(k,v){this.values.set(k,v)}get(k){return this.values.get(k)}},message=>{throw Error(message)});
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
  return {api,elements,document,requests,respond,polls,tick,visible,click,upload,article:()=>elements.jobs.children.flatMap(section=>section.children.flatMap(node=>node.children)).find(node=>node.tag==='article')};
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
 await test('active jobs outside the visible list keep polling without rebuilding cards',async()=>{
  const h=setup(),jobs=[job('done',100)];
  await h.respond(0,{jobs,has_active_jobs:true});
  assert(h.polls().length===1,'An older active job still needs polling');
  const article=h.article();
  await h.tick(6000);await h.respond(1,{jobs,has_active_jobs:false});
  assert(h.polls().length===0,'Stop once all jobs are terminal');
  assert(h.article()===article,'Metadata-only changes must not rebuild cards');
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
  const article=h.article(),replacements=h.elements.jobs.replacements;
  h.click();await h.respond(1,{jobs});
  assert(h.elements.jobs.replacements===replacements,'Unchanged data must not rebuild DOM');
  assert(h.article()===article,'Preserve article identity');
  const track=article.children.find(c=>c.attributes.role==='progressbar');
  assert(track.attributes['aria-valuenow']==='100'&&track.children[0].style.width==='100%','Completed progress');
  const link=descendants(article).find(c=>c.tag==='a'&&c.download!==undefined);
  assert(link.href===`/api/jobs/${jobs[0].id}/report`&&link.download==='','Native report download');
  const view=descendants(article).find(c=>c.tag==='a'&&c.target==='_blank');
  assert(view.href===`/api/jobs/${jobs[0].id}/report/view?lang=en`&&view.rel.includes('noopener'),'Secure separate preview link');
 });
 await test('active and history groups show empty states and move completed analyses',async()=>{
  const h=setup();await h.respond(0,{jobs:[]});
  const groups=h.elements.jobs.children;
  assert(groups[0].className==='job-group job-group-active'&&groups[1].className==='job-group job-group-history','Active group precedes distinct history');
  assert(groups[0].children[1].children[0].textContent==='No analyses in progress.','Empty active state');
  assert(groups[1].children[1].children[1].textContent==='No analysis history yet.','Empty history state');
  h.click();await h.respond(1,{jobs:[job('done'),job('running',25,'b'.repeat(32)),job('queued',0,'c'.repeat(32))]});
  assert(h.elements.jobs.children[0].children[1].children.length===2,'Both active analyses shown first');
  assert(h.elements.jobs.children[1].children[1].children.filter(c=>c.tag==='article').length===1,'Completed analysis in history');
  h.click();await h.respond(2,{jobs:[job('done'),job('done',100,'b'.repeat(32)),job('queued',0,'c'.repeat(32))]});
  assert(h.elements.jobs.children[1].children[1].children.filter(c=>c.tag==='article').length===2,'A finished analysis moves into history');
 });
 await test('saved prompt text is collapsible, literal and present for active and historical jobs',async()=>{
  for(const status of ['running','done']){
   const h=setup(),request={...job(status),question:'Private <script>alert(1)</script>\nDeuxième ligne',question_custom:1,report_name:'Subject_rapport.md'};
   await h.respond(0,{jobs:[request]});
   const details=descendants(h.elements.jobs).find(node=>node.className==='custom-prompt');
   assert(details.tag==='details'&&!details.open,'Prompt starts collapsed');
   assert(details.children[1].textContent===request.question&&details.children[1].children.length===0,'Prompt rendered as literal text');
   details.open=true;details.handlers.toggle[0]();
   h.click();await h.respond(1,{jobs:[{...request,progress_percent:70}]});
   assert(descendants(h.elements.jobs).find(node=>node.className==='custom-prompt').open,'Keep prompt expanded during progress updates');
   if(status==='done'){
    const download=descendants(h.elements.jobs).find(node=>node.download!==undefined);
    assert(download.download===request.report_name,'Displayed filename matches native download');
   }
  }
  for(const question_custom of [0,null]){
   const h=setup();await h.respond(0,{jobs:[{...job('done'),question:question_custom===0?'Default instructions':null,question_custom}]});
   assert(!descendants(h.elements.jobs).some(node=>node.className==='custom-prompt'),'No empty or default prompt blocks');
  }
 });
 await test('cancellation disables duplicate clicks and awaits backend confirmation',async()=>{
  const h=setup();await h.respond(0,{jobs:[job()]});
  const pending=h.api.cancelJob('a'.repeat(32));h.api.cancelJob('a'.repeat(32));
  const cancel=descendants(h.elements.jobs).find(node=>node.className==='button-danger'&&!node.id);
  assert(cancel.disabled&&cancel.attributes['aria-busy']==='true','Pending cancellation is disabled and accessible');
  assert(cancel.textContent==='Requesting cancellation…','Do not claim cancellation before confirmation');
  assert(h.requests.filter(request=>request.options?.method==='POST').length===1,'Only one cancellation request');
  await h.respond(1,{status:'running',cancel_requested:true});
  await h.respond(2,{jobs:[{...job(),cancel_requested:1}]});await pending;
  const requested=descendants(h.elements.jobs).find(node=>node.className==='button-danger'&&!node.id);
  assert(requested.disabled&&requested.textContent==='Cancellation requested','Running work remains pending after backend acknowledgment');
  h.api.cancelJob('a'.repeat(32));assert(h.requests.length===3,'Confirmed requests cannot be repeated');
  h.click();await h.respond(3,{jobs:[job('cancelled')]});
  assert(!descendants(h.elements.jobs).some(node=>node.className==='button-danger'&&!node.id),'Final jobs have no cancel action');
  h.api.cancelJob('a'.repeat(32));assert(h.requests.length===4,'Final jobs cannot be cancelled');
 });
 await test('failed cancellation restores the control without changing job status',async()=>{
  const h=setup();await h.respond(0,{jobs:[job()]});
  const pending=h.api.cancelJob('a'.repeat(32));h.requests[1].reject(Error('Offline'));await pending;
  const cancel=descendants(h.elements.jobs).find(node=>node.className==='button-danger'&&!node.id);
  assert(!cancel.disabled&&cancel.textContent==='Cancel','Failed cancellation remains retryable');
  assert(h.elements.feedback.textContent==='Offline','Error is visible');
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
  const h=setup();const jobs=[job()];await h.respond(0,{jobs});const article=h.article();
  await h.tick(6000);h.requests[1].reject(Error('Offline'));await flush();
  assert(h.article()===article&&!h.elements.jobsError.hidden,'Keep cards and show an error');
  assert(h.polls().length===1,'Keep polling last known active work');await h.tick(6000);await h.respond(2,{jobs});
  assert(h.article()===article&&h.elements.jobsError.hidden,'Recover without rebuilding cards');
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
 await test('Live follows a panel reopened or switched while a request is pending',async()=>{
  for(const nextId of ['a'.repeat(32),'b'.repeat(32)]){
   const h=setup();await h.respond(0,{jobs:[job(),job('running',20,'b'.repeat(32))]});
   h.api.toggleLive('a'.repeat(32));
   if(nextId==='a'.repeat(32))h.api.toggleLive(nextId);
   h.api.toggleLive(nextId);
   assert(h.requests.length===2,'Keep one pending Live request');
   await h.respond(1,{phase:'Old pass',content:'Old preview',active:true});
   const output=h.document.getElementById('live-content');
   assert(output.textContent==='Waiting for streamed output…','Ignore responses for a replaced panel');
   assert(h.requests.length===3,'Immediately load the current panel once the old request settles');
   assert(h.requests[2].url===`/api/jobs/${nextId}/live`,'Fetch the current job');
   await h.respond(2,{phase:'New pass',content:'Current preview',active:true});
   assert(output.textContent==='Current preview','Update the current panel');
  }
 });

 await test('a stalled jobs request times out and releases its shared promise',async()=>{
  const h=setup();await h.tick(15000);assert(!h.elements.jobsError.hidden,'Timeout must be visible');
  h.click();assert(h.requests.length===2,'Retry after a timeout must issue a new request');
  await h.respond(1,{jobs:[job('queued')]});assert(h.polls().length===1,'Polling recovers');
  await h.respond(0,{jobs:[]});assert(h.polls().length===1,'Late timed-out response must not overwrite current work');
 });
 await test('JSON decoding is also covered by the request deadline',async()=>{
  const h=setup();h.requests[0].resolve({ok:true,status:200,json:()=>new Promise(()=>{})});await flush();
  await h.tick(15000);h.click();assert(h.requests.length===2,'Stalled response body must release the lock');
  await h.respond(1,{jobs:[]});assert(h.elements.jobsError.hidden,'Recover after stalled JSON');
 });
 await test('a stalled Live request can be retried without starting inference',async()=>{
  const h=setup();await h.respond(0,{jobs:[job()]});h.api.toggleLive('a'.repeat(32));
  await h.tick(15000);await h.tick(1700);assert(h.requests.length===3,'Live timeout must release liveBusy');
  assert(h.requests.every(r=>!r.options?.method||r.options.method==='GET'),'Live must issue only GETs');
  await h.respond(2,{content:'Recovered',active:true});assert(h.document.getElementById('live-content').textContent==='Recovered','Live recovers');
 });

 const checks=h=>descendants(h.elements.jobs).filter(node=>node.dataset.selectJob);
 const select=(node,value=true)=>{node.checked=value;node.handlers.change[0]()};
 await test('selection is visible-only, supports indeterminate and survives progress refreshes',async()=>{
  const h=setup();const list=[job('done'),job('failed',10,'b'.repeat(32)),job('running',20,'c'.repeat(32))];
  await h.respond(0,{jobs:list});assert(checks(h).length===2,'Active analyses are not selectable');
  select(checks(h)[0]);assert(h.document.getElementById('selectAllJobs').indeterminate,'Partial selection');
  assert(h.document.getElementById('selectionCount').textContent==='1 analyses selected','Selection count');
  checks(h)[0].focus();
  h.click();await h.respond(1,{jobs:[...list.slice(0,2),{...list[2],progress_percent:60}]});
  assert(checks(h)[0].checked,'Selection survives a progress refresh');
  assert(h.document.activeElement===checks(h)[0],'Keyboard focus survives a progress refresh');
  select(h.document.getElementById('selectAllJobs'));
  assert(checks(h).every(n=>n.checked),'Select all visible terminal analyses');
  h.document.getElementById('clearSelection').handlers.click[0]();
  assert(checks(h).every(n=>!n.checked)&&h.document.getElementById('selectionActions').hidden,'Deselect all');
  select(checks(h)[0]);h.click();await h.respond(2,{jobs:[list[1]]});
  assert(h.document.getElementById('selectionActions').hidden,'Invisible selection is discarded');
 });
 await test('individual and bulk confirmations never delete on cancel or Escape',async()=>{
  const h=setup();await h.respond(0,{jobs:[job('done')]});
  const remove=descendants(h.elements.jobs).find(n=>n.dataset.deleteJob);
  remove.handlers.click[0]();assert(h.elements.deleteDialog.open&&h.elements.deleteDocument.textContent==='example.pdf','Named confirmation');
  assert(h.requests.length===1,'No deletion before confirmation');
  h.elements.deleteCancel.handlers.click[0]();assert(!h.elements.deleteDialog.open&&h.requests.length===1,'Cancelled without request');
  select(checks(h)[0]);h.document.getElementById('deleteSelection').handlers.click[0]();
  assert(h.elements.deleteMessage.textContent.includes('1 analyses'),'Exact bulk count');
  h.elements.deleteDialog.handlers.cancel[0]({preventDefault(){}});
  assert(!h.elements.deleteDialog.open&&checks(h)[0].checked&&h.requests.length===1,'Escape preserves selection');
 });
 await test('individual deletion removes only the confirmed card and shows an empty state',async()=>{
  const h=setup();const id='a'.repeat(32);await h.respond(0,{jobs:[job('done')]});
  h.api.confirmDeletion([id],false);const pending=h.api.submitDeletion();h.api.submitDeletion();
  assert(h.requests.length===2&&h.requests[1].url===`/api/jobs/${id}/delete`,'One individual POST');
  assert(checks(h)[0].disabled,'Selection disabled while deleting');
  await h.respond(1,{id,deleted:true,warning_code:null});
  assert(!h.article()&&!h.elements.historyNotice.hidden,'Immediately remove successful entry');
  assert(h.elements.historyNotice.textContent==='Deleted: 1.','Success notice');
  await h.respond(2,{jobs:[]});await pending;
  assert(descendants(h.elements.jobs).some(n=>n.textContent==='No analysis history yet.'),'Empty history');
  assert(h.polls().length===0,'Deletion must not start active polling');
 });
 await test('partial batch deletion preserves failed entries and their selection',async()=>{
  const h=setup();const first='a'.repeat(32),second='b'.repeat(32);
  await h.respond(0,{jobs:[job('done'),job('failed',0,second)]});
  select(h.document.getElementById('selectAllJobs'));h.document.getElementById('deleteSelection').handlers.click[0]();
  const pending=h.api.submitDeletion();
  assert(JSON.parse(h.requests[1].options.body).ids.length===2,'Exact bulk IDs');
  await h.respond(1,{results:[{id:first,deleted:true},{id:second,deleted:false,error_code:'delete_database'}]});
  assert(checks(h).length===1&&checks(h)[0].checked,'Only failed analysis stays selected');
  assert(h.elements.historyNotice.textContent.includes('Could not delete: 1.'),'Partial success is explicit');
  await h.respond(2,{jobs:[job('failed',0,second)]});await pending;
  assert(checks(h)[0].checked&&!checks(h)[0].disabled,'Failed analysis remains retryable');
 });
 await test('network deletion errors preserve the card and selection',async()=>{
  const h=setup();await h.respond(0,{jobs:[job('done')]});select(checks(h)[0]);
  h.api.confirmDeletion(['a'.repeat(32)],false);const pending=h.api.submitDeletion();
  h.requests[1].reject(Error('Offline'));await pending;
  assert(h.article()&&checks(h)[0].checked&&!checks(h)[0].disabled,'Failed request keeps visible selection');
  assert(h.elements.historyNotice.textContent==='Offline','Visible error');
 });
 await test('cleanup warnings are shown and old GETs cannot resurrect deleted cards',async()=>{
  const h=setup();const id='a'.repeat(32);await h.respond(0,{jobs:[job('done')]});
  h.click();h.api.confirmDeletion([id],false);const pending=h.api.submitDeletion();
  await h.respond(2,{id,deleted:true,warning_code:'delete_files_failed'});
  assert(!h.article()&&h.elements.historyNotice.textContent.includes('Some associated files were retained.'),'Durable removal and explicit cleanup warning');
  await h.respond(1,{jobs:[job('done')]});assert(!h.article(),'Ignore stale GET');
  await h.respond(3,{jobs:[]});await pending;
 });
 await test('report buttons follow all content and unavailable reports have no links',async()=>{
  const h=setup();await h.respond(0,{jobs:[{...job('done'),question:'Custom',question_custom:1}]});
  const article=h.article(),footer=article.children[article.children.length-1];
  assert(footer.className==='actions report-actions'&&footer.children.length===2,'Two report buttons at card bottom');
  assert(footer.children.every(n=>n.className.includes('report-button')),'Explicit button styling');
  h.click();await h.respond(1,{jobs:[{...job('done'),report_available:false}]});
  assert(!descendants(h.article()).some(n=>n.tag==='a'),'No unavailable report links');
  assert(descendants(h.article()).some(n=>n.textContent==='Report unavailable'),'Unavailable message');
 });

 const resolved={profile:'low',chunk_tokens:3000,analysis_tokens:3072,merge_tokens:2048,final_tokens:3072,context_tokens:16384,warnings:[],thinking_section:'inherit',thinking_merge:'inherit',thinking_final:'inherit'};
 async function settingsReady(){const h=setup(true);await h.respond(0,{jobs:[]});await h.respond(1,{thinking_supported:false});await h.respond(2,{configuration:resolved});return h}
 await test('profile preview is server-resolved and keeps advanced reasoning disabled when unsupported',async()=>{
  const h=await settingsReady();assert(!h.elements.send.disabled,'Validated preview enables submission');
  assert(h.elements.thinking_section.disabled,'Unsupported reasoning must be disabled');
  h.elements.analysis_tokens.value='1200';h.elements.reportLanguage.value='fr';
  const pending=h.upload();const request=h.requests[3];const options=JSON.parse(request.options.body.get('options'));
  assert(options.profile==='low'&&options.overrides.analysis_tokens===1200&&options.report_language==='fr','Only explicit controls are submitted');
  assert(!('system_prompt' in options)&&!('snapshot' in options),'Private/resolved snapshot must not be client-authoritative');
  await h.respond(3,{id:'b'.repeat(32)});await h.respond(4,{jobs:[job('queued',0,'b'.repeat(32))]});await pending;
 });
 await test('invalid configuration keeps submission disabled and late previews are ignored',async()=>{
  const h=await settingsReady();const first=h.api.previewConfig();h.elements.analysis_tokens.value='-1';h.api.schedulePreview();
  await h.respond(3,{configuration:resolved});await first;assert(h.elements.send.disabled,'A stale valid response must not enable changed settings');
  await h.tick(250);await h.respond(4,{error_code:'invalid_budget',error:'Invalid budget'},false);
  assert(h.elements.send.disabled,'Rejected budgets must disable submission');
 });
 await test('language and theme changes preserve the open Live text and scroll position',async()=>{
  const h=await settingsReady();h.click();await h.respond(3,{jobs:[job()]});h.api.toggleLive('a'.repeat(32));
  const text='L’assemblée générale a décidé de vérifier la présence des élèves à Grenoble. Résumé : température −2,5 °C, échantillon n° 3, précision ±0,2 %. English œ «quotes» µm ∑ 中文 العربية 😀';
  // Parse the JSON wire representation before passing it to the browser logic.
  await h.respond(4,JSON.parse(JSON.stringify({content:text,active:true})));const panel=h.document.getElementById('live-'+'a'.repeat(32)),output=h.document.getElementById('live-content');output.scrollTop=17;
  assert(output.textContent===text,'JSON preview must retain every Unicode character');
  h.elements.uiLanguage.value='fr';h.elements.uiLanguage.handlers.change[0]();
  assert(h.document.documentElement.lang==='fr','HTML language must update');
  assert(h.document.getElementById('live-'+'a'.repeat(32))===panel&&output.textContent===text&&output.scrollTop===17,'Language change must preserve live content');
  h.elements.theme.value='dark';h.elements.theme.handlers.change[0]();assert(h.document.documentElement.dataset.theme==='dark','Theme must update');
  assert(output.textContent===text,'Theme must not alter model output');
 });
 return results;
}

if(typeof module!=='undefined'&&typeof require==='function'){
 module.exports=runPollingTests;
 if(require.main===module){
  const fs=require('node:fs'),path=require('node:path');
  runPollingTests(fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8').replace(/<script src=[^>]*><\/script>/,()=>'<script>'+fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8')+'</script>'),JSON.parse(fs.readFileSync(path.join(__dirname,'../static/translations.json'),'utf8')))
   .then(results=>{for(const name of results)console.log('ok - '+name);console.log(`${results.length} tests passed`);})
   .catch(error=>{console.error(error);process.exitCode=1;});
 }
}
