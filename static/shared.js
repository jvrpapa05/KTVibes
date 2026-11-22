export const $ = id => document.getElementById(id);
export function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; }
// A page left open across a KTVibes update reloads once to pick up the new files.
let build;
function reloadIfUpdated(next){if(!next)return false;if(build===undefined)build=next;if(next===build)return false;location.reload();return true;}
// onEvent gets any other server message, such as a cheer for the TV.
// karaoke-server: phones pause hidden pages and drop their connection, so coming back to the remote briefly had
// no socket. Now: reconnect the moment the page is visible again (and quickly, with back-off, otherwise); keep the
// last status on screen and only say "Reconnecting…" if it takes over 3 s; hold anything tapped meanwhile (up to
// 5 s old) and send it once connected. "Disconnected" is only for a real takeover (another TV, code 4001).
export function connect(role, onState, onError, onOpen, onEvent) {
  let socket, retry = null, lostTimer = null, everOpen = false, attempts = 0, outbox = [];
  const label = () => $('connection');
  const live = () => socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING);
  function showLost(){label().textContent='Reconnecting…';label().classList.remove('online');label().classList.add('lost');}
  function waitThenShowLost(){if(!lostTimer&&!document.hidden)lostTimer=setTimeout(showLost,3000);}
  function open(){
    clearTimeout(retry);retry=null;
    if(live())return;
    socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws?role=${role}`);
    socket.onopen=()=>{
      clearTimeout(lostTimer);lostTimer=null;attempts=0;
      if(!everOpen)label().textContent='Connected';  // later reconnects keep the status the page already shows
      everOpen=true;label().classList.add('online');label().classList.remove('lost');onOpen?.();
      const pending=outbox.filter(m=>Date.now()-m.at<5000);outbox=[];for(const m of pending)socket.send(m.text);
    };
    socket.onmessage=e=>{const data=JSON.parse(e.data);if(data.type==='state'){if(reloadIfUpdated(data.build))return;onState(data);}else if(data.type==='error')onError(data.message);else onEvent?.(data);};
    socket.onclose=e=>{
      if(e.code===4001){label().textContent='Disconnected';label().classList.remove('online');label().classList.add('lost');return;}
      waitThenShowLost();
      if(!document.hidden)retry=setTimeout(open,Math.min(5000,250*2**attempts++));
    };
  }
  const wake=()=>{if(!document.hidden&&!live()){waitThenShowLost();open();}};
  document.addEventListener('visibilitychange',wake);addEventListener('pageshow',wake);addEventListener('focus',wake);addEventListener('online',wake);
  open();
  return message=>{
    if(socket.readyState===WebSocket.OPEN)return socket.send(JSON.stringify(message));
    outbox.push({text:JSON.stringify(message),at:Date.now()});if(outbox.length>20)outbox.shift();
    if(!document.hidden)open();
  };
}
export async function api(path, options){
  const response=await fetch(path,options);let data;
  // A proxy or crash can answer with plain text; report the status rather than a JSON parse error.
  try{data=await response.json();}catch{throw new Error(`Server error (${response.status}). Please try again.`);}
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Please check the song details.');return data;
}
export const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
export const thumbnail = (id, size='mqdefault') => `https://i.ytimg.com/vi/${id}/${size}.jpg`;
export const clock = seconds => `${Math.floor((seconds||0)/60)}:${String(Math.floor((seconds||0)%60)).padStart(2,'0')}`;
// Sliders fire many input events; send at most one every `ms`, always ending on the final value.
export function throttle(fn, ms=120){let last=0,timer;return value=>{clearTimeout(timer);const wait=last+ms-Date.now();if(wait<=0){last=Date.now();fn(value);}else timer=setTimeout(()=>{last=Date.now();fn(value);},wait);};}
// Preparation status, as both screens describe it.
const STEPS={queued:'Waiting to download',downloading:'Downloading',separating:'Separating vocals',syncing:'Finding lyrics',ready:'Ready',error:'Failed'};
export function prepLabel(item){const percent=item.progress?Math.round(item.progress*100):null;return {percent,text:`${STEPS[item.status]||item.status}${item.step?` ${item.step}`:''}${percent==null?'':` · ${percent}%`}`};}
// Sliders paint their own filled track from --v (0–1); call after setting a value in code.
export function syncRanges(){for(const range of document.querySelectorAll('input[type=range]'))range.style.setProperty('--v',(range.value-range.min)/((range.max-range.min)||1));}
document.addEventListener('input',e=>{if(e.target.type==='range')syncRanges();});
// 신곡: songs prepared in the last week, newest first; the newest numbers when none are that recent.
export function newSongs(songs,now=Date.now()/1000){const week=songs.filter(s=>now-s.prepared<7*86400).sort((a,b)=>b.prepared-a.prepared);return (week.length?week:[...songs].sort((a,b)=>b.number-a.number)).slice(0,10);}

// karaoke-server: long titles scroll instead of ending in "…". A node whose content doesn't fit gets its content
// wrapped in a strip that glides to the end and back, pausing at each end; content that fits stays still.
// Pages call marquees(selectors) on a timer, so new titles and resizes are picked up.
export function marquee(node){
 let strip=node.firstElementChild;
 if(!(strip&&strip.classList.contains('mq')&&node.childNodes.length===1)){strip=document.createElement('span');strip.className='mq';strip.append(...node.childNodes);node.append(strip);}
 const over=strip.scrollWidth-node.clientWidth;
 if(over>2&&!REDUCED.matches){node.style.setProperty('--mq-shift',`${-over}px`);node.style.setProperty('--mq-time',`${(6+over/8).toFixed(1)}s`);node.classList.add('mq-on');}
 else node.classList.remove('mq-on');
}
export function marquees(selectors){for(const node of document.querySelectorAll(selectors))if(node.getClientRects().length)marquee(node);}  // laid out (offsetParent is null under fixed headers)

// karaoke-server: "how close is this song": the preparation steps (download, separate vocals, video, lyrics) as one
// 0-100%. Separating and syncing have no progress of their own: the server sends when they started (since) and how
// long they should take (expect), and we fill in between. Never goes backwards. skew = server clock minus ours.
const prepShown=new Map();
export function prepPercent(item,skew=0){
 if(item.status==='ready')return 100;
 const now=Date.now()/1000+skew,span=(a,b,f)=>a+(b-a)*Math.max(0,Math.min(1,f||0));
 const timed=item.since&&item.expect?Math.min(.97,(now-item.since)/item.expect):0;
 const pct={queued:0,downloading:item.step==='video'?span(70,74,item.progress):span(0,4,item.progress),separating:span(4,70,timed),syncing:span(74,99,timed)}[item.status]??0;
 const best=Math.max(prepShown.get(item.key)||0,Math.floor(pct));prepShown.set(item.key,best);return best;
}
