"use strict";
// A single synchronous IPC transaction checks the caller's snapshot before any
// mutation, so a stale editor cannot silently overwrite another window's work.
function mutateSnippets(database, input) {
  if (!input || !Array.isArray(input.expected)) throw new Error('Snippet snapshot is required');
  const current=database.getSnippets();
  if (JSON.stringify(current)!==JSON.stringify(input.expected)) throw new Error('The library changed. Close and reopen this editor to use the latest entries.');
  const cleanKey=s=>s.normalize('NFC').toLowerCase().replace(/[\p{P}\p{S}\s]/gu,'');
  const next=current.map(s=>({...s}));
  const index=input.previousTrigger==null?-1:next.findIndex(s=>s.trigger===input.previousTrigger);
  if (input.action==='delete') {
    if (index<0) throw new Error('Snippet no longer exists');
    next.splice(index,1);
  } else if (input.action==='save') {
    const trigger=typeof input.trigger==='string'?input.trigger.trim():'';
    const replacement=input.replacement;
    if (!trigger||!cleanKey(trigger)||trigger.length>100||typeof replacement!=='string'||!replacement.trim()) throw new Error('Enter a valid trigger and expansion');
    if (input.previousTrigger!=null&&index<0) throw new Error('Snippet no longer exists');
    if(next.some((s,i)=>i!==index&&cleanKey(s.trigger)===cleanKey(trigger))) throw new Error('Choose a distinctive trigger; a similar one already exists');
    const value={trigger,replacement};
    if(index<0) next.push(value);else next[index]=value;
  } else throw new Error('Unknown snippet action');
  database.setSnippets(next);
  return database.getSnippets();
}
module.exports={mutateSnippets};
