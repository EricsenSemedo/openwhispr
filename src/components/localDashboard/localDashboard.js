import { expandSnippets, validateSnippet } from "../../utils/snippets";
import "./localDashboard.css";

const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  wave:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 10v4m4-8v12m4-15v18m4-15v12m4-8v4"/></svg>',
  home:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M4 10h16M10 10v10"/></svg>',
  snippets:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="13" height="13" rx="3"/><path d="M9 20h8a3 3 0 0 0 3-3V9M8 9h5m-5 4h3"/></svg>',
  history:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 7v5h5M4 12a8 8 0 1 0 2-6M12 7v5l3 2"/></svg>'
};

export function mountLocalDashboard(store, api=window.electronAPI) {
  if (!api || document.getElementById('local-dictation')) return;
  const root=document.getElementById('root');
  const app=document.createElement('div');app.id='local-dictation';
  app.innerHTML=`<div class="ld-window"><div class="ld-brand">${icons.wave}<span>Local Dictation</span></div><div class="ld-window-actions"><button id="ld-min" aria-label="Minimize">−</button><button id="ld-max" aria-label="Maximize">□</button><button id="ld-close" aria-label="Close to tray">×</button></div></div><div class="ld-layout"><aside><div class="ld-sidebar-title">YOUR WORKSPACE</div><nav aria-label="Main navigation"><button data-page="home">${icons.home}Overview</button><button data-page="snippets">${icons.snippets}Snippets<span id="ld-count"></span></button><button data-page="history">${icons.history}History</button></nav><div class="ld-sidebar-bottom"><div class="ld-local"><span class="ld-dot"></span> On this PC</div><p>Your voice. Your device.</p><button id="ld-settings">Settings ↗</button></div></aside><main id="ld-main"></main></div><div id="ld-notice" role="status" aria-live="polite"></div><dialog id="ld-dialog"></dialog>`;
  document.body.append(app);
  const main=app.querySelector('#ld-main'), dialog=app.querySelector('#ld-dialog');
  const state={page:'home',snippets:[],loaded:false,query:'',testInput:'',history:[],health:'checking',disposed:false,busy:false};
  let restoreFocus=null;
  const listen=(id,event,fn)=>app.querySelector('#'+id)?.addEventListener(event,fn);
  const notice=(text,error=false)=>{const el=app.querySelector('#ld-notice');el.textContent=text;el.classList.toggle('ld-error',error);};
  const hideLegacy=()=>{if(root){root.style.display='none';root.inert=true;}app.hidden=false;document.getElementById('ld-return')?.remove();};
  const showLegacy=()=>{
    app.hidden=true;root.style.display='';root.inert=false;
    const back=document.createElement('button');back.id='ld-return';back.textContent='← Local Dictation';back.onclick=hideLegacy;document.body.append(back);
  };
  const closeDialog=()=>{if(state.busy)return;dialog.close();restoreFocus?.focus();};
  const refresh=async()=>{try{state.snippets=await api.getSnippets();state.loaded=true;app.querySelector('#ld-count').textContent=state.snippets.length;if(state.page==='snippets'&&app.querySelector('#ld-list')){renderList();app.querySelector('#ld-add').disabled=false;app.querySelector('#ld-test-result').textContent=expandSnippets(state.testInput,state.snippets)||'Your expanded text will appear here.';}else if(state.page==='home')render();}catch{notice('Could not load snippets. Reopen the dashboard to retry.',true);}};
  const localConfiguration=()=>{const s=store.getState();return s.transcriptionMode==='self-hosted'&&/^http:\/\/127\.0\.0\.1:18765\/?$/.test(s.remoteTranscriptionUrl)&&s.useDictationAgent===false&&s.allowOpenAIFallback===false&&s.allowLocalFallback===false;};
  const checkHealth=async()=>{
    state.health='checking';render();
    try{const r=await fetch('http://127.0.0.1:18765/health',{signal:AbortSignal.timeout(4000)});const h=await r.json();state.health=r.ok&&h.ready===true?'ready':'unavailable';}catch{state.health='unavailable';}
    if(!state.disposed&&state.page==='home')render();
  };
  const header=(eyebrow,title,description,action='')=>`<header class="ld-page-header"><div><div class="ld-eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${action}</header>`;
  function render() {
    if(state.disposed)return;
    app.querySelectorAll('[data-page]').forEach(b=>{const selected=b.dataset.page===state.page;b.classList.toggle('ld-selected',selected);selected?b.setAttribute('aria-current','page'):b.removeAttribute('aria-current');});
    app.querySelector('#ld-count').textContent=state.loaded?state.snippets.length:'';
    if(state.page==='home') {
      const ready=state.health==='ready'&&localConfiguration();
      main.innerHTML=header('MAKE ROOM FOR YOUR WORDS','Speak freely. Keep your flow.','Fast dictation, thoughtful cleanup, and your words exactly where you need them.')+`<section class="ld-hero"><div><span class="ld-status ${ready?'':'ld-status-muted'}"><span class="ld-dot"></span>${ready?'Ready to dictate':state.health==='checking'?'Checking local connection…':'Local connection needs attention'}</span><h2>From thought<br>to text.</h2><p>Click into any text field, then use your shortcut.<br>We take care of the little “uh”s and “um”s.</p><div class="ld-key-row"><kbd>Ctrl</kbd><span>+</span><kbd>Alt</kbd><span>+</span><kbd>D</kbd></div></div><div class="ld-hero-art" aria-hidden="true"><div class="ld-orbit"></div><div class="ld-audio-mark">${icons.wave}</div><div class="ld-art-caption">ALL LOCAL · ALL YOURS</div></div></section><div class="ld-shortcuts"><article><span class="ld-step">01</span><h3>Hold to talk</h3><p>Hold Ctrl + Alt + D while speaking.<br>Release to insert your text.</p></article><article><span class="ld-step">02</span><h3>Go hands-free</h3><p>Double-press the shortcut to start.<br>Press it again when you’re finished.</p></article><article><span class="ld-step">03</span><h3>Change your mind</h3><p>Press Esc to cancel.<br>Nothing is inserted.</p></article></div><section class="ld-bottom-card"><div>${icons.snippets}<div><h3>Your words, on cue.</h3><p>Say a short phrase. Insert a link, signature, or saved reply.</p></div></div><button id="ld-open-snippets">Explore snippets <span>→</span></button></section><footer class="ld-footer"><span>Parakeet · Local processing · ${ready?'Connected':state.health==='checking'?'Checking':'Check connection'}</span><button id="ld-recheck">Check connection</button></footer>`;
      listen('ld-open-snippets','click',()=>navigate('snippets'));listen('ld-recheck','click',checkHealth);
    } else if(state.page==='snippets') {
      main.innerHTML=header('LESS REPETITION. MORE FLOW.','Snippets','A spoken shortcut for the text you use every day.',`<button class="ld-primary" id="ld-add" ${!state.loaded?'disabled':''}>+ Add snippet</button>`)+`<div class="ld-library-toolbar"><label class="ld-search"><span aria-hidden="true">⌕</span><input id="ld-search" placeholder="Search triggers or saved text" aria-label="Search snippets" value="${esc(state.query)}"></label><span id="ld-result-count"></span></div><div id="ld-list"></div><section class="ld-test"><div><div class="ld-eyebrow">TRY IT HERE</div><h3>See what your words become.</h3><p>Type a trigger or a sentence. This uses the same matcher as dictation.</p></div><label for="ld-test-input">Text to test</label><textarea id="ld-test-input" placeholder="Try: my portfolio"></textarea><div class="ld-test-label">RESULT <span>Text only · microphone stays off</span></div><pre id="ld-test-result" aria-live="polite">Your expanded text will appear here.</pre></section>`;
      renderList();app.querySelector('#ld-test-input').value=state.testInput;app.querySelector('#ld-test-result').textContent=expandSnippets(state.testInput,state.snippets)||'Your expanded text will appear here.';listen('ld-add','click',()=>editor());listen('ld-search','input',e=>{state.query=e.target.value;renderList();});listen('ld-test-input','input',e=>{state.testInput=e.target.value;app.querySelector('#ld-test-result').textContent=expandSnippets(state.testInput,state.snippets)||'Your expanded text will appear here.';});
    } else {
      main.innerHTML=header('A LITTLE PEACE OF MIND','History','Your finished text, with the original transcript always within reach.')+`<div class="ld-history">${state.history.length?state.history.map((h,i)=>`<article><div class="ld-history-date">${esc(h.timestamp||h.created_at||'Saved dictation')}</div><p>${esc(h.text)}</p><button data-raw="${i}">View raw transcript ↗</button></article>`).join(''):'<div class="ld-empty"><h3>Your next thought starts here.</h3><p>Dictations saved on this PC appear here.</p></div>'}</div>`;
      main.querySelectorAll('[data-raw]').forEach(b=>b.onclick=()=>rawDialog(state.history[+b.dataset.raw]));
    }
  }
  function renderList() {
    const list=app.querySelector('#ld-list');if(!list)return;
    const found=state.snippets.map((s,i)=>({...s,i})).filter(s=>(s.trigger+' '+s.replacement).toLowerCase().includes(state.query.toLowerCase()));
    app.querySelector('#ld-result-count').textContent=`${found.length} snippet${found.length===1?'':'s'}`;
    list.innerHTML=found.length?`<div class="ld-table-head"><span>WHEN YOU SAY</span><span>WE INSERT</span><span></span></div>${found.map(s=>`<article class="ld-snippet"><div class="ld-trigger"><span class="ld-quote">“</span>${esc(s.trigger)}</div><pre>${esc(s.replacement)}</pre><div class="ld-row-actions"><button data-test="${s.i}" aria-label="Test ${esc(s.trigger)}">Test</button><button data-edit="${s.i}" aria-label="Edit ${esc(s.trigger)}">Edit</button><button data-delete="${s.i}" aria-label="Delete ${esc(s.trigger)}">Delete</button></div></article>`).join('')}`:`<div class="ld-empty"><h3>${state.loaded?(state.query?'No matches yet.':'Make your first shortcut.'):'Loading your library…'}</h3><p>${state.query?'Try another phrase or clear your search.':'Save a portfolio link, your LinkedIn, or a reply you use often.'}</p></div>`;
    list.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editor(state.snippets[+b.dataset.edit]));
    list.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>deleteDialog(state.snippets[+b.dataset.delete]));
    list.querySelectorAll('[data-test]').forEach(b=>b.onclick=()=>{const s=state.snippets[+b.dataset.test],input=app.querySelector('#ld-test-input');state.testInput=s.trigger;input.value=s.trigger;app.querySelector('#ld-test-result').textContent=expandSnippets(s.trigger,state.snippets);input.focus();});
  }
  function openDialog(html) {restoreFocus=document.activeElement;dialog.innerHTML=html;dialog.showModal();dialog.querySelector('[autofocus]')?.focus();dialog.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=closeDialog);}
  function editor(snippet=null) {
    const expected=state.snippets.map(s=>({...s}));
    openDialog(`<form id="ld-editor"><div class="ld-eyebrow">YOUR WORDS, SAVED</div><h2>${snippet?'Edit snippet':'Add a snippet'}</h2><p>Choose a phrase that feels natural and is easy to recognize.</p><label for="ld-trigger">When you say</label><input autofocus id="ld-trigger" maxlength="100" placeholder="e.g. my portfolio" value="${esc(snippet?.trigger||'')}"><label for="ld-expansion">Insert this exact text</label><textarea id="ld-expansion" placeholder="A link, signature, paragraph, or code…">${esc(snippet?.replacement||'')}</textarea><p class="ld-field-note">Line breaks, spacing, and casing stay exactly as saved.</p><div id="ld-editor-preview" class="ld-preview"></div><p id="ld-form-error" role="alert"></p><div class="ld-dialog-actions"><button type="button" data-cancel>Cancel</button><button class="ld-primary" id="ld-save" type="submit">${snippet?'Save changes':'Add snippet'}</button></div></form>`);
    const trigger=dialog.querySelector('#ld-trigger'),expansion=dialog.querySelector('#ld-expansion'),error=dialog.querySelector('#ld-form-error');
    const update=()=>{error.textContent=validateSnippet(trigger.value,expansion.value,expected,snippet?.trigger);dialog.querySelector('#ld-save').disabled=!!error.textContent;dialog.querySelector('#ld-editor-preview').textContent=expansion.value?'PREVIEW\n'+expansion.value:'Your saved text will appear here.';};
    trigger.oninput=expansion.oninput=update;update();error.textContent='';
    dialog.querySelector('form').onsubmit=async e=>{
      e.preventDefault();if(state.busy)return;update();if(error.textContent)return;
      state.busy=true;dialog.querySelectorAll('button,input,textarea').forEach(x=>x.disabled=true);
      try{state.snippets=await api.localSnippetMutation({action:'save',expected,previousTrigger:snippet?.trigger??null,trigger:trigger.value,replacement:expansion.value});state.busy=false;dialog.close();render();notice('Snippet saved. Ready for your next dictation.');app.querySelector('#ld-add')?.focus();}
      catch(err){state.busy=false;dialog.querySelectorAll('button,input,textarea').forEach(x=>x.disabled=false);error.textContent=err.message||'Could not save. Your original entry is unchanged.';await refresh();}
    };
    dialog.querySelector('form').onkeydown=e=>{if(e.ctrlKey&&e.key==='Enter'){e.preventDefault();dialog.querySelector('form').requestSubmit();}};
  }
  function deleteDialog(snippet) {
    const expected=state.snippets.map(s=>({...s}));
    openDialog(`<div class="ld-eyebrow">REMOVE A SHORTCUT</div><h2>Delete “${esc(snippet.trigger)}”?</h2><p>This removes the snippet from this PC.</p><pre class="ld-preview">${esc(snippet.replacement)}</pre><p id="ld-form-error" role="alert"></p><div class="ld-dialog-actions"><button data-cancel autofocus>Keep snippet</button><button id="ld-confirm-delete" class="ld-danger">Delete snippet</button></div>`);
    dialog.querySelector('#ld-confirm-delete').onclick=async()=>{if(state.busy)return;state.busy=true;dialog.querySelectorAll('button').forEach(b=>b.disabled=true);try{state.snippets=await api.localSnippetMutation({action:'delete',expected,previousTrigger:snippet.trigger});state.busy=false;dialog.close();render();notice('Snippet deleted.');app.querySelector('#ld-add')?.focus();}catch(err){state.busy=false;dialog.querySelectorAll('button').forEach(b=>b.disabled=false);dialog.querySelector('#ld-form-error').textContent=err.message;await refresh();}};
  }
  function rawDialog(h) {openDialog(`<div class="ld-eyebrow">ORIGINAL WORDS</div><h2>Raw transcript</h2><p>Before cleanup and snippet expansion.</p><pre class="ld-raw">${esc(h.raw_text??h.rawText??h.text)}</pre><div class="ld-dialog-actions"><button data-cancel autofocus>Done</button></div>`);}
  async function navigate(page) {state.page=page;notice('');if(page==='history'){try{state.history=await api.getTranscriptions(50);}catch{notice('Could not load history.',true);}}render();}
  app.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>navigate(b.dataset.page));
  listen('ld-min','click',()=>api.windowMinimize());listen('ld-max','click',()=>api.windowMaximize());listen('ld-close','click',()=>api.windowClose());listen('ld-settings','click',showLegacy);
  dialog.addEventListener('cancel',e=>{e.preventDefault();closeDialog();});
  const onKeys=e=>{if((e.ctrlKey&&e.key.toLowerCase()==='f'||e.key==='/')&&state.page==='snippets'&&!dialog.open&&!/INPUT|TEXTAREA/.test(document.activeElement?.tagName)){e.preventDefault();app.querySelector('#ld-search')?.focus();}};
  app.addEventListener('keydown',onKeys);
  const unsubscribe=api.onSnippetsUpdated?.(snippets=>{state.snippets=snippets;app.querySelector('#ld-count').textContent=snippets.length;if(state.page==='snippets'){renderList();app.querySelector('#ld-test-result').textContent=expandSnippets(state.testInput,snippets)||'Your expanded text will appear here.';}});
  const historySub=api.onTranscriptionAdded?.(()=>{if(state.page==='history')navigate('history');});
  const settingsSub=store.subscribe(()=>{if(state.page==='home')render();});
  hideLegacy();render();refresh();checkHealth();
  return ()=>{state.disposed=true;unsubscribe?.();historySub?.();settingsSub?.();app.remove();if(root){root.style.display='';root.inert=false;}document.getElementById('ld-return')?.remove();};
}
