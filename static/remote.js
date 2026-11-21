import {classicRemote,toastParts,emptyNote,secondsLeft} from './classic-remote.js';
import {$,el,connect,api,clock,throttle,prepLabel,syncRanges,REDUCED,thumbnail,marquees,prepPercent} from './shared.js';
let state,classicUi;
const SPRING='cubic-bezier(.38,1.21,.22,1)';
const haptic=pattern=>{try{navigator.vibrate?.(pattern);}catch{}};
function showImage(img,src){if(img.getAttribute('src')!==src){if(src)img.src=src;else img.removeAttribute('src');}img.hidden=!src;}
// One toast for every message; an action (Undo) keeps it up a little longer.
let toastTimer;
function toast(text,action,kind){
 // Classic dresses the same message as the machine's toast: a label key, the title in bold, a Korean line.
 if(document.body.dataset.theme==='classic'){const parts=toastParts(text,kind);$('toast-key').textContent=parts.key;$('toast-key').className=`toast-key classic-only ${parts.tone}`;$('toast-text').replaceChildren(...parts.body);$('toast').classList.toggle('err',kind==='err');}
 else $('toast-text').textContent=text;
 $('toast').style.setProperty('--toast-ms',`${action?6000:2600}ms`);$('toast-action').hidden=!action;
 if(action){$('toast-action').textContent=action.label;$('toast-action').onclick=()=>{$('toast').classList.remove('on');action.run();};}
 $('toast').classList.add('on');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('on'),action?6000:2600);
}
const sendRaw=connect('remote',render,message=>toast(message,undefined,'err'),undefined,event=>onFun(event));
// karaoke-server: settings carry who's changing them; the server saves them to that singer's profile (live only on their own song).
const PROFILE_ACTIONS=new Set(['vocal','music','key','speed','lyric_scale','video_mode','lyric_mode']);
const send=message=>sendRaw(PROFILE_ACTIONS.has(message.action)&&singer()?{...message,singer:singer(),device:deviceId()}:message);
// What the settings sheet shows: the room while it's your song (or a song without a name), otherwise your own profile.
function editingOwn(){const me=singer().toLowerCase(),on=(state?.current?.singer||'').toLowerCase();return !!me&&(!state?.current||(!!on&&on!==me));}
function view(){if(!editingOwn())return state;const mine=state.profiles?.[singer().toLowerCase()];return {...state,...(mine||{})};}
let skew=0;  // server clock minus ours, for the progress estimate
function render(next){
 const before=state;state=next;state.received=Date.now()/1000;if(state.server_time)skew=state.server_time-state.received;
 // The header pill reports the TV, since that decides whether anything can play.
 $('connection').textContent=!state.player_connected?'TV not connected':state.player_audio?'TV ready':'TV connected · tap Enable sound on it';  // karaoke-server: says it's about the TV, not this phone$('connection').classList.toggle('warn',!state.player_audio);$('connection').classList.toggle('off',!state.player_connected);
 document.body.dataset.theme=state.theme||'default';showNow(!before);showControls(before);showQueue(before);showClassic();classicUi?.render(state);
 const ready=[state.current,...state.upcoming].filter(i=>i?.status==='ready').map(i=>i.key).join();
 if(ready!==readyKeys){readyKeys=ready;loadRecent();classicUi?.loadBook();}
}
// Now playing: a card at the top, handing over to the dock once scrolled away.
function showNow(first){
 const c=state.current,art=c?thumbnail(c.id):'';placeFun(!!c);
 showHero(!!c,first);$('hero').classList.toggle('paused',!state.playing);$('hero-state').textContent=(state.playing?'NOW SINGING':'PAUSED')+(c?.singer?` · ${c.singer.toUpperCase()}`:'');
 for(const id of ['hero-play','dock-play']){$(id).classList.toggle('playing',!!(c&&state.playing));$(id).setAttribute('aria-label',state.playing?'Pause':'Play');$(id).disabled=!c;}
 // The card keeps its last song while it folds away.
 for(const id of c?['hero-art','hero-glow','dock-art','sheet-art']:['dock-art','sheet-art'])showImage($(id),art);
 if(c){$('hero-title').textContent=c.title;$('hero-artist').textContent=c.artist;}$('hero-length').textContent=$('length').textContent=clock(c?.duration);
 $('dock-title').textContent=$('sheet-title').textContent=c?.title||'Nothing playing';$('dock-artist').textContent=c?.artist||'';
 showPosition();
}
// The card unfolds when a song starts on an empty stage and folds away when the stage empties:
// height, padding and margin open with a soft spring while the card fades and scales up.
let heroShown=false;
function showHero(on,first){
 if(on===heroShown)return;heroShown=on;
 const hero=$('hero');for(const animation of hero.getAnimations())animation.cancel();
 if(first||REDUCED.matches){hero.hidden=!on;return;}
 hero.hidden=false;
 const closed={height:'0px',paddingTop:'0px',paddingBottom:'0px',marginTop:'0px',opacity:0,transform:'scale(.94)'},open={height:`${hero.offsetHeight}px`,opacity:1,transform:'none'};
 const animation=on?hero.animate([closed,{opacity:1,offset:.6},open],{duration:600,easing:SPRING}):hero.animate([open,{opacity:0,offset:.5},closed],{duration:400,easing:'cubic-bezier(.4,0,.2,1)'});
 if(!on)animation.onfinish=()=>{if(!heroShown)hero.hidden=true;};
}
// Estimate the TV position between its two-second progress reports.
let scrubbing=false;  // karaoke-server: never true now; the position bar is read-only (no seeking)
function showPosition(){
 if(!state)return;
 const live=state.current&&state.playing&&state.position>0?Date.now()/1000-state.received:0;
 const position=Math.min(state.position+live,state.current?.duration||0),fraction=state.current?.duration?position/state.current.duration:0;
 for(const id of ['hero-bar','dock-bar'])$(id).style.setProperty('--p',fraction);
 $('hero-position').textContent=clock(position);$('hero-bar').setAttribute('aria-valuetext',clock(position));
 if(!scrubbing){$('scrub').value=position;$('position').textContent=clock(position);syncRanges();}
}
setInterval(showPosition,500);
const playPause=()=>{haptic(8);send({action:state?.playing?'pause':'play'});};
$('hero-play').onclick=$('dock-play').onclick=playPause;
$('skip').onclick=()=>{haptic(12);send({action:'skip'});};
// The dock hides while the card is on screen; when the card leaves, the dock rises and its art drops in.
let heroVisible=false;
new IntersectionObserver(([entry])=>{
 const was=heroVisible;heroVisible=entry.isIntersecting;$('dock').classList.toggle('away',heroVisible);
 if(was&&!heroVisible&&!REDUCED.matches){
  $('dock-art').animate([{transform:'translateY(-24px) scale(1.2)',opacity:0},{transform:'none',opacity:1}],{duration:450,delay:120,easing:'cubic-bezier(.34,1.3,.64,1)',fill:'backwards'});
  $('dock').querySelector('.info').animate([{opacity:0,transform:'translateX(-8px)'},{opacity:1,transform:'none'}],{duration:350,delay:200,easing:'ease-out',fill:'backwards'});
 }
},{rootMargin:'0px 0px -40px 0px'}).observe($('hero'));
$('dock-queue').onclick=()=>$('queue').scrollIntoView({behavior:REDUCED.matches?'auto':'smooth',block:'center'});
// Reactions float up on this phone and on the TV. Quick taps add up into one "×3" note.
function reactWith(button){  // karaoke-server: a function, so your own emoji buttons behave the same
 const emoji=button.dataset.emoji;send({action:'react',value:emoji});haptic(6);
 // karaoke-server: one floating emoji per tap (was 5), the same as the TV shows.
 const box=button.getBoundingClientRect(),count=1;
 for(let i=0;i<count;i++){
  const float=el('span',emoji,'float');float.style.left=`${box.left+box.width/2-15}px`;float.style.top=`${box.top}px`;document.body.append(float);
  const drift=(Math.random()-.5)*90,rise=160+Math.random()*120,spin=(Math.random()-.5)*50;
  float.animate([{transform:'scale(.4)',opacity:0},{transform:`translate(${drift*.3}px,-40px) scale(1.25) rotate(${spin*.3}deg)`,opacity:1,offset:.2},{transform:`translate(${drift}px,-${rise}px) scale(.9) rotate(${spin}deg)`,opacity:0}],
   {duration:REDUCED.matches?300:1100+Math.random()*400,delay:i*70,easing:'cubic-bezier(.2,.7,.3,1)',fill:'both'}).onfinish=()=>float.remove();
 }
 if(!REDUCED.matches)button.animate([{transform:'scale(1)'},{transform:'scale(1.3) rotate(-8deg)'},{transform:'scale(1)'}],{duration:400,easing:'cubic-bezier(.42,1.67,.21,.9)'});
 clearTimeout(button.timer);button.taps=(button.taps||0)+1;
 button.timer=setTimeout(()=>{toast(`Sent ${emoji}${button.taps>1?` ×${button.taps}`:''} to the TV`);button.taps=0;},600);
}
for(const button of document.querySelectorAll('.react[data-emoji]'))button.onclick=()=>reactWith(button);
// Controls sheet: mix, key, speed, TV display and lyric fixes.
let shownKey=null;
function showControls(before){
 if(state.version)$('app-version').textContent=`v${state.version}`;
 const v=view();$('sheet-scope').textContent=editingOwn()?`Your settings · used when ${singer()}'s songs play`:'Changes show on the device';
 for(const id of ['skip','earlier','later','lyric-version','scrub','key-down','key-up'])$(id).disabled=!state.current;
 $('scrub').max=state.current?.duration||1;
 for(const [id,value] of [['vocal',v.vocal],['music',v.music],['speed',v.speed],['lyric-scale',v.lyric_scale]]){
  if(document.activeElement!==$(id)&&!gliding)$(id).value=value;$(`${id}-value`).textContent=`${Math.round(value*100)}%`;
 }
 for(const b of document.querySelectorAll('.presets button'))b.setAttribute('aria-pressed',Math.abs(b.dataset.vocal-v.vocal)<.005&&Math.abs(b.dataset.music-v.music)<.005);
 if(editingOwn())$('key-down').disabled=$('key-up').disabled=false;  // your key can be set before your song
 $('key-down').disabled||=v.key<=-6;$('key-up').disabled||=v.key>=6;
 if(v.key!==shownKey){rollKey(v.key,shownKey===null?0:v.key-shownKey);shownKey=v.key;}
 $('offset-value').textContent=`${state.offset>=0?'+':''}${state.offset.toFixed(1)}s`;
 for(const seg of SEGMENTED){
  for(const b of seg.querySelectorAll('button')){b.setAttribute('aria-pressed',b.dataset.value===v[seg.dataset.action]);if(seg.dataset.action==='guide')b.hidden=!(state.guides||[]).includes(b.dataset.value);}
  if(document.body.classList.contains('sheet-open')||state.theme==='classic')placePill(seg);  // openSheet measures the rest; in Classic the settings are a page
 }
 syncRanges();
}
function rollKey(key,direction){
 const roll=$('roll'),old=roll.lastElementChild,next=el('span',key>0?`+${key}`:String(key));
 // Quick taps: drop anything still rolling out, roll the newest number away.
 while(roll.firstElementChild!==old)roll.firstElementChild.remove();
 roll.append(next);
 if(direction&&!REDUCED.matches){
  const d=Math.sign(direction);
  old.animate([{transform:'none',opacity:1},{transform:`translateY(${-60*d}px) scale(.6)`,opacity:0}],{duration:350,easing:'cubic-bezier(.4,0,.2,1)',fill:'forwards'}).onfinish=()=>old.remove();
  next.animate([{transform:`translateY(${60*d}px) scale(.6)`,opacity:0},{transform:'none',opacity:1}],{duration:500,easing:'cubic-bezier(.34,1.56,.64,1)'});
 }else old.remove();
 $('key-note').classList.toggle('orig',key===0);$('key-note').textContent=key===0?'Original key':`${Math.abs(key)} semitone${Math.abs(key)>1?'s':''} ${key>0?'higher':'lower'}`;
}
$('key-down').onclick=()=>{haptic(8);send({action:'key',value:view().key-1});};
$('key-up').onclick=()=>{haptic(8);send({action:'key',value:view().key+1});};
$('key-reset').onclick=()=>{send({action:'speed',value:1});send({action:'key',value:0});};
for(const [id,action] of [['vocal','vocal'],['music','music'],['speed','speed'],['lyric-scale','lyric_scale']]){const sendValue=throttle(value=>send({action,value}));$(id).oninput=()=>sendValue(Number($(id).value));}
// Presets set both sliders, gliding there so the change is visible; A cappella is the singer alone.
let gliding=false;
for(const b of document.querySelectorAll('.presets button'))b.onclick=()=>{
 const to=[Number(b.dataset.vocal),Number(b.dataset.music)],from=[Number($('vocal').value),Number($('music').value)],start=performance.now();
 haptic(6);send({action:'vocal',value:to[0]});send({action:'music',value:to[1]});
 if(REDUCED.matches)return;
 gliding=true;
 const step=now=>{
  const k=Math.min(1,(now-start)/350),e=1-(1-k)**3;
  $('vocal').value=from[0]+(to[0]-from[0])*e;$('music').value=from[1]+(to[1]-from[1])*e;syncRanges();
  if(k<1)requestAnimationFrame(step);else{gliding=false;if(state)showControls(state);}
 };
 requestAnimationFrame(step);
};
// Segmented: room display settings and the pronunciation guide.
const SEGMENTED=document.querySelectorAll('.seg');
function placePill(seg){
 const on=seg.querySelector('[aria-pressed=true]:not([hidden])'),pill=seg.querySelector('.pill');pill.hidden=!on;
 if(!on||!on.offsetWidth)return;
 pill.style.width=`${on.offsetWidth}px`;pill.style.transform=`translateX(${on.offsetLeft-3}px)`;
 // Slide only once the pill has a place, so it doesn't sweep in from the left on load.
 if(!seg.classList.contains('ready'))requestAnimationFrame(()=>seg.classList.add('ready'));
}
for(const seg of SEGMENTED)seg.onclick=e=>{const b=e.target.closest('button');if(b){haptic(6);send({action:seg.dataset.action,value:b.dataset.value});}};
// A control measured while hidden (the Classic settings tab, a closed sheet) gets its pill once it has a size.
addEventListener('resize',()=>SEGMENTED.forEach(placePill));
{const sized=new ResizeObserver(entries=>entries.forEach(entry=>placePill(entry.target)));SEGMENTED.forEach(seg=>sized.observe(seg));}
$('earlier').onclick=()=>send({action:'offset',delta:-0.5});$('later').onclick=()=>send({action:'offset',delta:0.5});
$('lyric-version').onclick=()=>state?.current&&pickLyrics(state.current);
function openSheet(){document.body.classList.add('sheet-open');$('sheet').inert=false;haptic(8);requestAnimationFrame(()=>SEGMENTED.forEach(placePill));}
function closeSheet(){document.body.classList.remove('sheet-open');$('sheet').inert=true;}
$('open-controls').onclick=$('dock-now').onclick=$('dock-settings').onclick=()=>document.body.dataset.theme==='classic'?classicUi.setTab('settings'):openSheet();
$('scrim').onclick=$('sheet-handle').onclick=$('sheet-close').onclick=closeSheet;  // karaoke-server: plus an ✕
// Classic theme: room time in Settings. Classic is always two-line lyrics.
function showClassic(){const on=state.theme==='classic';$('room-time').hidden=!on;document.querySelector('[data-action=lyric_mode]').closest('.seg-row').hidden=on;roomLeft();}
function roomLeft(){if(!state?.room_ends)return;const left=secondsLeft(state),end=new Date(state.room_ends*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
 if(state.theme==='classic'){const lcd=el('span',String(Math.ceil(left/60)).padStart(3,'0'),'lcd');lcd.dataset.ghost='888';$('room-left').replaceChildren(lcd,el('span','분','u'));$('room-time').classList.toggle('up',!left);$('room-end').textContent=left?`종료 예정\n${end}`:'시간 종료\nTIME\'S UP';return;}
 $('room-left').textContent=left?`${Math.ceil(left/60)}분 left`:'Time is up';$('room-end').textContent=`${end} 종료`;}
setInterval(roomLeft,1000);
for(const b of document.querySelectorAll('[data-minutes]'))b.onclick=()=>{haptic(6);send({action:'add_time',minutes:+b.dataset.minutes});};
addEventListener('keydown',e=>{if(e.key==='Escape'&&!document.querySelector('.lyric-sheet'))closeSheet();});
// Drag the sheet down by its top to dismiss it.
{
 const sheet=$('sheet');let startY=null;
 sheet.addEventListener('pointerdown',e=>{if(document.body.dataset.theme==='classic')return;if(sheet.scrollTop>0||e.target.closest('input,button:not(.handle)'))return;startY=e.clientY;sheet.style.transition='none';});
 addEventListener('pointermove',e=>{if(startY!==null)sheet.style.transform=`translateY(${Math.max(0,e.clientY-startY)}px)`;});
 addEventListener('pointerup',e=>{if(startY===null)return;const moved=e.clientY-startY;startY=null;sheet.style.transition='';sheet.style.transform='';if(moved>100)closeSheet();});
}
// Queue. Rebuilt only when its songs or statuses change, so position reports never swap a button
// out from under a tap; preparation progress updates in place. A drag holds rebuilds until it ends.
let queueShape,dragging=null;
const RING=2*Math.PI*23;
const statusText=item=>item.status==='ready'?'Ready':item.status==='error'?'Failed':prepLabel(item).text;
// Classic shows the machine's Korean status beside it.
const STATUS_KO={queued:'다운로드 대기',downloading:'다운로드 중',separating:'보컬 분리 중',syncing:'가사 찾는 중',ready:'준비 완료',error:'준비 실패'};
const statusKo=(item,index)=>index===0&&item.status==='ready'?'다음곡':STATUS_KO[item.status]||'';
function showQueue(before){
 const count=state.upcoming.length;$('queue-count').textContent=$('dock-count').textContent=count;
 if(before&&count>before.upcoming.length&&!REDUCED.matches)for(const id of ['queue-count','dock-count']){const c=$(id);c.classList.remove('bump');void c.offsetWidth;c.classList.add('bump');}
 const shape=JSON.stringify([!!state.current,...state.upcoming.map(i=>[i.key,i.title,i.artist,i.singer,i.status,i.error])]);  // the empty notice reads differently while a song plays
 if(shape!==queueShape&&!dragging){
  // A song that turns ready while queued flashes its ring once.
  const preparing=new Set(before?.upcoming.filter(i=>i.status!=='ready').map(i=>i.key));
  // FLIP: remember where each row was, rebuild, then animate rows from their old spots.
  const tops=new Map([...$('queue').children].filter(row=>row.dataset.key).map(row=>[row.dataset.key,row.getBoundingClientRect().top]));
  queueShape=shape;
  $('queue').replaceChildren(...state.upcoming.map((item,index)=>queueRow(item,index,preparing.has(item.key))));
  if(!count)$('queue').append(emptyQueue());
  if(tops.size&&!REDUCED.matches)for(const row of $('queue').children){
   if(!row.dataset.key)continue;
   const was=tops.get(row.dataset.key),top=row.getBoundingClientRect().top;
   if(was===undefined)row.animate([{opacity:0,transform:'translateY(-12px) scale(.96)'},{opacity:1,transform:'none'}],{duration:450,easing:SPRING});
   else if(Math.abs(was-top)>1)row.animate([{transform:`translateY(${was-top}px)`},{transform:'none'}],{duration:500,easing:SPRING});
  }
 }
 for(const [index,item] of state.upcoming.entries()){
  const row=$('queue').querySelector(`[data-key="${item.key}"]`);if(!row)continue;
  row.querySelector('.status-ko').textContent=statusKo(item,index);showReady(row,item);
 }
}
// An empty queue invites the next song, with a button straight to the search box.
function emptyQueue(){
 const box=el('div',undefined,'empty'),icon=el('span',undefined,'empty-icon'),find=el('button','Find a song','empty-action');
 icon.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>';
 find.onclick=()=>{if(document.body.dataset.theme==='classic')classicUi.setTab('book');$('search-form').scrollIntoView({behavior:REDUCED.matches?'auto':'smooth',block:'center'});$('query').focus({preventScroll:true});};
 const lcd=el('span','00',`lcd classic-only${state.current?'':' dim'}`);lcd.dataset.ghost='88';
 box.append(lcd,icon,el('b','예약곡이 없어요','classic-only'),el('strong',state.current?'Nothing up next':'The queue is empty'),el('small',state.current?'Add a song and it plays when this one ends.':'Add a song and it starts right away.'),find);
 return box;
}
const GRIP='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>';
function queueRow(item,index,justReady){
 const row=el('article',undefined,`row ${item.status}`);row.dataset.key=item.key;
 const thumb=el('div',undefined,'thumb'),image=el('img',undefined,'art');image.src=thumbnail(item.id);image.alt='';thumb.append(image);
 // The ring shows while the song prepares, and once more as it turns ready.
 if(item.status!=='ready'&&item.status!=='error'||justReady&&item.status==='ready')thumb.insertAdjacentHTML('beforeend',`<svg class="ring" viewBox="0 0 52 52" style="--c:${RING}" aria-hidden="true"><circle class="track" cx="26" cy="26" r="23"/><circle class="fill" cx="26" cy="26" r="23"/></svg>`);
 if(justReady&&item.status==='ready')haptic(10);
 if(item.status!=='ready'&&item.status!=='error')thumb.append(el('span','',`pct`));  // karaoke-server: % on the art
 const info=el('div',undefined,'info'),line=el('p');if(item.singer)line.append(el('span',`🎤 ${item.singer}`,'singer'),el('span',' · ','sep'));if(item.number)line.append(el('span',String(item.number),'qnum classic-only'),' ');line.append(item.artist,...(justReady&&item.status==='ready'?[el('em','준비 완료','ready-lamp classic-only')]:[]),el('span',' · ','sep'),el('span',statusKo(item,index),'status-ko classic-only'),el('span',readyText(item),'status'));info.append(el('h3',item.title),line);
 if(item.status!=='ready'&&item.status!=='error'){const bar=el('div',undefined,'prepbar classic-only');bar.append(el('i'));info.append(bar);}
 if(item.error){const retry=el('button','Retry','retry');retry.onclick=()=>send({action:'retry',key:item.key});info.append(el('p',item.error,'error'),retry);}
 const remove=el('button','×','row-button');remove.setAttribute('aria-label',`Remove ${item.title}`);
 remove.onclick=()=>{send({action:'remove',key:item.key});toast(`Removed ${item.title}`,{label:'Undo',run:()=>send({action:'undo'})});};
 const grip=el('button',undefined,'row-button grip');grip.innerHTML=GRIP;grip.setAttribute('aria-label',`Move ${item.title}. Arrow keys move it up or down.`);
 grip.onkeydown=e=>{const to=index+({ArrowUp:-1,ArrowDown:1}[e.key]||0);if(to!==index&&to>=0&&to<state.upcoming.length){e.preventDefault();move(index,to);}};
 // Classic: 우선 moves a song to sing next, as on the machine.
 const first=el('button','우선','row-button first classic-only');first.setAttribute('aria-label',`Play ${item.title} next`);first.hidden=index===0;
 first.onclick=()=>{haptic(8);move(index,0);toast(`${item.title} 우선예약`);};
 // karaoke-server: the queue is a read-only list of songs to play: no move, play-next or remove.
 row.append(el('span',String(index+1),'num'),thumb,info);
 return row;
}
function move(from,to){const keys=state.upcoming.map(i=>i.key);keys.splice(to,0,...keys.splice(from,1));send({action:'reorder',keys});}
// Drag to reorder: the grip lifts a row at once; a long press anywhere else on it lifts it after 350 ms.
document.addEventListener('touchmove',e=>{if(dragging)e.preventDefault();},{passive:false});
function dragHandlers(row,grip){
 let press,startX,startY;
 grip.addEventListener('pointerdown',e=>{e.preventDefault();startDrag(row,e);});
 row.addEventListener('pointerdown',e=>{
  if(e.target.closest('button'))return;
  startX=e.clientX;startY=e.clientY;press=setTimeout(()=>startDrag(row,e),350);
 });
 const cancel=e=>{if(e.type!=='pointermove'||Math.hypot(e.clientX-startX,e.clientY-startY)>8)clearTimeout(press);};
 for(const type of ['pointermove','pointerup','pointercancel'])row.addEventListener(type,cancel);
}
function startDrag(row,e){
 if(dragging)return;
 const rows=[...$('queue').children].filter(r=>r.dataset.key),from=rows.indexOf(row),boxes=rows.map(r=>r.getBoundingClientRect());
 dragging={row,from,to:from,y:e.clientY,rows,boxes,height:boxes[from].height};
 row.classList.add('lifted');for(const r of rows)if(r!==row)r.classList.add('shifting');
 haptic(15);
 addEventListener('pointermove',onDrag);addEventListener('pointerup',endDrag,{once:true});addEventListener('pointercancel',endDrag,{once:true});
}
function onDrag(e){
 const d=dragging,dy=e.clientY-d.y;d.row.style.translate=`0 ${dy}px`;
 const middle=d.boxes[d.from].top+d.height/2+dy;
 let to=d.boxes.findIndex(box=>middle<box.top+box.height/2);if(to<0)to=d.rows.length-1;else if(to>d.from)to--;
 if(to!==d.to){
  d.to=to;haptic(5);
  d.rows.forEach((r,i)=>{if(r===d.row)return;const shift=d.from<to&&i>d.from&&i<=to?-d.height:d.from>to&&i<d.from&&i>=to?d.height:0;r.style.transform=shift?`translateY(${shift}px)`:'';});
 }
 // Scroll when dragging near the top or bottom edge.
 if(e.clientY<80)scrollBy(0,-8);else if(e.clientY>innerHeight-110)scrollBy(0,8);
}
function endDrag(){
 const d=dragging;removeEventListener('pointermove',onDrag);removeEventListener('pointerup',endDrag);removeEventListener('pointercancel',endDrag);
 const target=d.boxes[d.to].top-d.boxes[d.from].top+(d.to>d.from?d.boxes[d.to].height-d.height:0);
 const settle=d.row.animate([{translate:d.row.style.translate||'0 0'},{translate:`0 ${target}px`}],{duration:REDUCED.matches?1:260,easing:'cubic-bezier(.34,1.3,.64,1)',fill:'forwards'});
 settle.onfinish=()=>{
  dragging=null;
  if(d.to!==d.from){
   // Show the new order now; the server's answer then matches it, so nothing jumps.
   const order=state.upcoming.map(i=>i.key);order.splice(d.to,0,...order.splice(d.from,1));
   state={...state,upcoming:order.map(key=>state.upcoming.find(i=>i.key===key))};queueShape=null;
   move(d.from,d.to);
  }else queueShape=null;
  showQueue(state);
 };
}
// Songs already prepared on the server, newest first.
// Rebuilt only when the list itself changes, so a just-tapped song keeps its added/failed icon.
let readyKeys,recentShape;
async function loadRecent(){
 return;  // karaoke-server: no "Recent" list on the remote (the elements stay, hidden, for the Classic theme code)
 try{
  const songs=await api('/api/recent'),shape=songs.map(song=>song.id).join();
  if(shape===recentShape)return;
  recentShape=shape;$('recent').replaceChildren();
  songs.forEach(song=>showResult({...song,channel:song.artist,parsed:{artist:song.artist,title:song.title}},$('recent'),row=>swipeable(row,song)));
  if(!songs.length)$('recent').append(emptyNote('♪','아직 부른 노래가 없어요','Nothing sung yet.'));
 }catch{}
}
// Search in pages of 10; "More results" appends the next page.
let search={query:'',page:0,seen:new Set()},searchRun=0;
const enqueue=song=>api('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:song.id,artist:song.artist,title:song.title,singer:singer(),device:deviceId()})});
// Result button states: add, added, failed.
const ICONS={add:'<path d="M12 5v14M5 12h14"/>',added:'<path d="M5 12.5l4.5 4.5L19 7.5"/>',failed:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>'};
function icon(node,name){node.className=`result-icon ${name}`;node.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;}
// One tap queues the song with the artist/title parsed from YouTube (or confirmed earlier, for recent songs).
function showResult(song,list=$('results'),wrap=row=>row,index=-1){
 const b=el('button',undefined,index<0?'result':'result enter');if(index>=0)b.style.setProperty('--i',index);
 const image=el('img');image.src=song.thumbnail;image.alt='';const info=el('div');const title=el('strong',song.title);if(song.cached)title.append(el('span','READY','tag'));const note=el('small',undefined,'result-note classic-only');info.append(title,el('small',`${song.channel} · ${clock(song.duration)}`),note);const status=el('span');icon(status,'add');b.append(image,info,status);
 // Classic swaps the channel line for what happened to the add.
 const mark=(name,...text)=>{b.classList.remove('added','failed');b.classList.add(name);note.replaceChildren(...text);};
 b.onclick=async()=>{
  // A swipe ends in a click; an open row closes on tap instead of queueing.
  if(b.dataset.dragged){delete b.dataset.dragged;return;}if(b.parentElement?.classList.contains('open')){closeSwipe();return;}
  b.disabled=true;
  try{const added=await enqueue({id:song.id,...song.parsed});icon(status,'added');mark('added',el('em','예약됨'),' · added');haptic([10,40,10]);flyToQueue(image);toast(`Added ${song.parsed.title}`);
   if(list===$('results'))setTimeout(()=>leaveSearch(added?.key),450);}
  catch(error){icon(status,'failed');mark('failed',"Couldn't add. Try again");toast(error.message,undefined,'err');}finally{b.disabled=false;}
 };
 list.append(wrap(b));
}
// The thumbnail arcs into whichever queue count is on screen: the outer box moves across evenly,
// the inner one up or down with an overshooting ease, which together trace a curve.
function flyToQueue(source){
 if(REDUCED.matches)return;
 const from=source.getBoundingClientRect(),count=$('queue-count').getBoundingClientRect();
 const target=(document.body.dataset.theme==='classic'?document.querySelector('#tabs [data-tab=queue]'):count.top>60&&count.bottom<innerHeight-90||$('dock').classList.contains('away')?$('queue-count'):$('dock-count')).getBoundingClientRect();
 const outer=el('div'),inner=source.cloneNode();
 inner.style.cssText=`display:block;width:${from.width}px;height:${from.height}px;border-radius:8px;object-fit:cover;box-shadow:0 10px 30px #000a`;
 outer.style.cssText=`position:fixed;left:${from.left}px;top:${from.top}px;z-index:55;pointer-events:none`;
 outer.append(inner);document.body.append(outer);
 const dx=target.left+target.width/2-(from.left+from.width/2),dy=target.top+target.height/2-(from.top+from.height/2);
 outer.animate([{transform:'none'},{transform:`translateX(${dx}px)`}],{duration:650,easing:'cubic-bezier(.3,0,.7,1)',fill:'forwards'});
 inner.animate([{transform:'none',borderRadius:'8px'},{transform:`translateY(${dy}px) scale(.25) rotate(25deg)`,borderRadius:'50%'}],{duration:650,easing:dy>0?'cubic-bezier(.55,-0.5,.9,.5)':'cubic-bezier(.1,.5,.45,1.4)',fill:'forwards'}).onfinish=()=>outer.remove();
}
// Recent songs swipe left to reveal Delete; tapping it deletes the download.
const REVEAL=96;let openSwipe=null;
function closeSwipe(){if(!openSwipe)return;openSwipe.classList.remove('open');openSwipe.querySelector('.result').style.transform='';openSwipe=null;}
document.addEventListener('pointerdown',e=>{if(openSwipe&&!openSwipe.contains(e.target))closeSwipe();});
// Lyrics picker: every LRCLIB version that fits the song (languages, editions), with its first lines.
async function pickLyrics(song){
 const sheet=el('div',undefined,'lyric-sheet'),panel=el('div',undefined,'lyric-sheet-panel'),list=el('div',undefined,'lyric-options'),close=el('button','Close');
 const shut=()=>{sheet.classList.add('out');sheet.addEventListener('animationend',()=>sheet.remove(),{once:true});};
 close.onclick=shut;sheet.onclick=e=>{if(e.target===sheet)shut();};
 const head=el('div',undefined,'lyric-sheet-head'),name=el('strong');name.append(el('small','가사 선택 · LYRICS','classic-only'),'Lyrics · ',el('span',song.title));head.append(name,close);close.setAttribute('aria-label','Close');
 // Search LRCLIB by hand when the automatic lookup found nothing or the wrong song.
 const form=el('form',undefined,'lyric-search'),input=el('input'),go=el('button','Search');
 input.type='search';input.value=`${song.artist} ${song.title}`;input.placeholder='Artist and song title';input.setAttribute('aria-label','Search lyrics');go.type='submit';
 form.append(input,go);form.onsubmit=e=>{e.preventDefault();input.blur();show(input.value.trim());};
 panel.append(head,form,list);sheet.append(panel);document.body.append(sheet);
 let latest=0;  // a slow earlier lookup must not replace newer results
 // karaoke-server: applying a version can take a while (for the song on stage the server times every word against
 // the vocals, and waits if another song is being timed), so show what's happening, then confirm it worked.
 async function applyLyrics(o,b){
  for(const other of list.children)other.disabled=true;b.classList.add('busy');
  const playing=state?.current?.id===song.id,seconds=playing?Math.max(15,Math.round((state.current.duration||200)*.17)):3;
  const card=el('div',undefined,'lyric-applying'),box=el('div',undefined,'la-box'),title=el('strong','Applying these lyrics…'),
    note=el('p',playing?'Timing each word to the singer’s voice. Usually under a minute; longer while another song is being prepared.':'Saving your choice…'),
    bar=el('div',undefined,'la-bar'),fill=el('i'),clock=el('small','0 s','la-clock');
  bar.append(fill);box.append(title,el('span',o.track||'','la-track'),note,bar,clock);card.append(box);panel.append(card);
  const started=Date.now(),tick=setInterval(()=>{const t=(Date.now()-started)/1000;fill.style.width=`${Math.min(95,100*t/seconds)}%`;clock.textContent=`${Math.floor(t)} s`;},250);
  try{
   await api(`/api/songs/${song.id}/lyrics`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:o.id})});
   clearInterval(tick);fill.style.width='100%';card.classList.add('done');
   title.textContent='✓ Lyrics applied';note.textContent=playing?'Showing on the TV now.':'They’ll show when this song plays.';clock.textContent='';haptic([10,40,10]);
   setTimeout(()=>{shut();toast(`Lyrics for ${song.title} updated`);},1600);
  }catch(error){
   clearInterval(tick);card.classList.add('failed');title.textContent='Couldn’t apply these lyrics';note.textContent=error.message;clock.textContent='';
   const again=el('button','Try again','la-again');again.onclick=()=>{card.remove();for(const other of list.children)other.disabled=false;b.classList.remove('busy');};
   box.append(again);
  }
 }
 async function show(query){
  const mine=++latest;
  const wait=el('p',query?'Searching…':'Looking up every version…','muted lyric-wait'),bar=el('span',undefined,'pbar busy classic-only');
  for(let i=0;i<40;i++){const cell=el('i');cell.style.animationDelay=`${(i*.045).toFixed(3)}s`;bar.append(cell);}
  wait.append(el('span',query?'검색 중':'모든 가사 버전을 찾는 중','classic-only'),bar);list.replaceChildren(wait);
  try{
   const options=await api(`/api/songs/${song.id}/lyrics${query?`?q=${encodeURIComponent(query)}`:''}`);
   if(mine!==latest)return;
   const none=el('p',undefined,'muted lyric-none');none.append(el('b','싱크 가사를 못 찾았어요','classic-only'),query?'Nothing found. Try fewer words, or the title in its original language.':'No synced lyrics found automatically. Try a search above.');
   list.replaceChildren(...(options.length?[]:[none]));
   // One row per language (its best version) first; other editions sit behind "Show all". Searches list everything.
   const seen=new Set(),extra=[];
   for(const o of options){
    // karaoke-server: "Latin script" (any A-Z language, so every English/Filipino version) says nothing to our singers:
    // no label for those, each version shows on its own (no "Show all"), headed by its own title.
    const lang=o.language==='Latin script'?'':o.language,key=lang||`version-${o.id}`;
    const first=!seen.has(key);seen.add(key);
    const b=el('button',undefined,'lyric-option'+(o.current?' current':'')),top=el('div');
    const gap=Math.abs(o.difference)>10?` · ${Math.abs(o.difference)}s ${o.difference>0?'longer':'shorter'} than this video`:'';
    const lines=el('span',undefined,'cnt classic-only'),figure=el('span',String(o.lines),'lcd');figure.dataset.ghost='88';lines.append(figure,'LINES',...(gap?[el('span',gap.slice(3),'gap')]:[]));
    top.append(el('b',lang||o.track||'Lyrics'),el('span',(lang?[o.track,o.artist,o.album]:[o.artist,o.album]).filter(Boolean).join(' · ')),...(o.current?[el('em','In use')]:[]),lines);
    b.append(top,...o.preview.map(line=>el('small',line)),el('small',`${o.lines} lines${gap}`,'count'));
    b.onclick=()=>applyLyrics(o,b);
    if(query||first||o.current)list.append(b);else extra.push(b);
   }
   if(extra.length){const all=el('button',undefined,'lyric-all');all.append(el('span',`${options.length}개 모두 보기 · `,'classic-only'),`Show all ${options.length} versions`);all.onclick=()=>{all.replaceWith(...extra);};list.append(all);}
  }catch(error){if(mine===latest)list.replaceChildren(el('p',error.message,'error'));}
 }
 show('');
}
// A ⋯ button on each recent song opens its advanced actions.
let openMenu=null;
function closeMenu(){openMenu?.remove();openMenu=null;}
document.addEventListener('pointerdown',e=>{if(openMenu&&!openMenu.contains(e.target)&&!e.target.closest('.song-more'))closeMenu();});
function songMenu(wrap,song,del){
 if(openMenu?.parentElement===wrap)return closeMenu();
 closeMenu();closeSwipe();
 const menu=el('div',undefined,'song-menu'),refetch=el('button'),remove=el('button',undefined,'danger'),label=el('div',undefined,'menu-head classic-only');
 if(song.number){const n=el('span',String(song.number),'lcd');n.dataset.ghost='88888';label.append(n);}label.append(song.title);
 refetch.innerHTML='<svg class="classic-only" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 10h16M4 14h10M4 18h7"/></svg>';refetch.append(el('span','가사 고르기','classic-only'),'Lyrics…');
 remove.innerHTML='<svg class="classic-only" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';remove.append(el('span','다운로드 삭제','classic-only'),'Delete download');
 menu.setAttribute('role','menu');for(const item of [refetch,remove])item.setAttribute('role','menuitem');
 refetch.onclick=()=>{closeMenu();pickLyrics(song);};
 remove.onclick=()=>{closeMenu();del.click();};
 menu.append(label,refetch,remove);wrap.append(menu);openMenu=menu;refetch.focus();
}
function swipeable(row,song){
 const wrap=el('div',undefined,'swipe'),del=el('button','Delete','swipe-delete'),more=el('button','⋯','song-more');del.setAttribute('aria-label',`Delete ${song.title} from this device`);
 more.setAttribute('aria-label',`More actions for ${song.title}`);more.setAttribute('aria-haspopup','menu');more.onclick=()=>songMenu(wrap,song,del);
 wrap.append(row,del,more);
 let start=null,dx=0,dragging=false;
 row.addEventListener('pointerdown',e=>{delete row.dataset.dragged;if(e.pointerType==='mouse'&&e.button)return;start={x:e.clientX,y:e.clientY,base:wrap.classList.contains('open')?-REVEAL:0};dx=start.base;dragging=false;});
 row.addEventListener('pointermove',e=>{
  if(!start)return;const mx=e.clientX-start.x,my=e.clientY-start.y;
  if(!dragging){if(Math.abs(my)>8&&Math.abs(my)>Math.abs(mx)){start=null;return;}if(Math.abs(mx)<8)return;dragging=true;row.setPointerCapture(e.pointerId);wrap.classList.add('dragging');if(openSwipe!==wrap)closeSwipe();}
  dx=Math.max(-REVEAL*1.3,Math.min(0,start.base+mx));row.style.transform=`translateX(${dx}px)`;
 });
 const end=()=>{
  if(!start)return;start=null;wrap.classList.remove('dragging');if(!dragging)return;
  row.dataset.dragged='1';
  if(dx<-REVEAL/2){wrap.classList.add('open');row.style.transform=`translateX(${-REVEAL}px)`;openSwipe=wrap;}
  else{wrap.classList.remove('open');row.style.transform='';if(openSwipe===wrap)openSwipe=null;}
 };
 row.addEventListener('pointerup',end);row.addEventListener('pointercancel',end);
 del.onclick=async()=>{
  del.disabled=true;
  try{
   await api(`/api/songs/${song.id}`,{method:'DELETE'});
   if(openSwipe===wrap)openSwipe=null;
   if(!REDUCED.matches)await wrap.animate([{height:`${wrap.offsetHeight}px`,opacity:1},{height:'0px',opacity:0,marginBottom:'-8px'}],{duration:300,easing:'cubic-bezier(.3,0,.8,.15)'}).finished;
   wrap.remove();recentShape=null;toast(`Deleted ${song.title}`);
   if(!$('recent').children.length)loadRecent();
  }catch(error){toast(error.message,undefined,'err');closeSwipe();}finally{del.disabled=false;}
 };
 return wrap;
}
async function loadPage(){
 const run=++searchRun,first=!search.page;
 $('more').disabled=true;if(first){$('results').dataset.state='loading';$('results-end').hidden=true;}
 // Placeholder rows hold the space while YouTube answers.
 if(first)$('results').replaceChildren(...Array.from({length:5},()=>{const row=el('div',undefined,'skeleton');row.setAttribute('aria-hidden','true');row.innerHTML='<i></i><div><i></i><i></i></div>';return row;}));
 try{
  const results=(await api(`/api/search?q=${encodeURIComponent(search.query)}&page=${search.page}`)).filter(s=>!search.seen.has(s.id));
  if(run!==searchRun)return;
  if(first)$('results').replaceChildren();
  results.forEach((s,i)=>{search.seen.add(s.id);showResult(s,$('results'),row=>row,i);});
  $('more').hidden=results.length===0||search.page>=9;$('results').dataset.state=search.seen.size?'':'none';
  $('results-end').hidden=!$('more').hidden||!search.seen.size;
  if(!search.seen.size)$('results').append(emptyNote('없음','검색 결과가 없어요','No songs found. Try the title in its original language, or just the artist.'));
  if(first)showResults();
 }catch(error){if(run!==searchRun)return;if(first){$('results').replaceChildren();$('results').dataset.state='';}toast(error.message,undefined,'err');}finally{$('more').disabled=false;}
}
// Autocomplete from YouTube's search box: type to see completions, tap or arrow+Enter to search one.
let suggestTimer,suggestRun=0,suggestIndex=-1;
function hideSuggestions(){$('suggestions').hidden=true;$('query').setAttribute('aria-expanded','false');suggestIndex=-1;}
function pickSuggestion(text){$('query').value=text;hideSuggestions();$('search-form').requestSubmit();}
function markSuggestion(index){const items=[...$('suggestions').children];suggestIndex=(index+items.length+1)%(items.length+1)-1;items.forEach((li,i)=>li.setAttribute('aria-selected',i===suggestIndex));}
$('query').oninput=()=>{
 clearTimeout(suggestTimer);const q=$('query').value.trim(),run=++suggestRun;$('clear').hidden=!$('query').value;
 if(!q)return hideSuggestions();
 suggestTimer=setTimeout(async()=>{
  const list=await api(`/api/suggest?q=${encodeURIComponent(q)}`).catch(()=>[]);
  if(run!==suggestRun||document.activeElement!==$('query'))return;
  $('suggestions').replaceChildren(...list.map(text=>{const li=el('li'),typed=text.toLowerCase().startsWith(q.toLowerCase())?text.slice(0,q.length):'';li.append(typed,el('b',text.slice(typed.length)));li.setAttribute('role','option');li.onpointerdown=e=>{e.preventDefault();pickSuggestion(text);};return li;}));
  suggestIndex=-1;$('suggestions').hidden=!list.length;$('query').setAttribute('aria-expanded',String(!!list.length));
 },150);
};
$('query').onkeydown=e=>{
 if($('suggestions').hidden)return;
 if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();markSuggestion(suggestIndex+(e.key==='ArrowDown'?1:-1));}
 else if(e.key==='Enter'&&suggestIndex>=0){e.preventDefault();pickSuggestion($('suggestions').children[suggestIndex].textContent);}
 else if(e.key==='Escape')hideSuggestions();
};
$('query').onblur=()=>setTimeout(hideSuggestions,100);
$('clear').onclick=()=>{$('query').value='';$('clear').hidden=true;searchRun++;$('results').replaceChildren();$('more').hidden=$('results-end').hidden=true;$('query').focus();};
$('search-form').onsubmit=e=>{
 e.preventDefault();suggestRun++;hideSuggestions();search={query:$('query').value.trim(),page:0,seen:new Set()};$('more').hidden=true;loadPage();
 $('query').blur();showResults();  // drop the keyboard so results have the screen
};
// Search stays pinned but results don't: from further down, bring them up under the bar.
function showResults(){
 const gap=$('results').getBoundingClientRect().top-($('search-form').getBoundingClientRect().bottom+12);
 if(gap<0)scrollBy({top:gap,behavior:REDUCED.matches?'auto':'smooth'});
}
$('more').onclick=()=>{search.page++;loadPage();};
// Keep the page's end clear of the dock, whatever its height.
new ResizeObserver(()=>document.body.style.setProperty('--dock',`${$('dock').offsetHeight}px`)).observe($('dock'));
classicUi=classicRemote({send,toast,haptic,swipeable});if(state)classicUi.render(state);

// karaoke-server: ask for the singer's name once per phone; every song queued from here carries it.
const SINGER='karaoke.singer';let singerName='';  // kept in memory too, for private tabs where storage fails
function singer(){try{return localStorage.getItem(SINGER)||singerName;}catch{return singerName;}}
function showSinger(){$('singer-name').textContent=singer()||'—';}
// Names are unique: the server lends a name to one phone (a random id kept here) until it's idle for 6 hours.
const DEVICE='karaoke.device';let deviceMemo='';
function deviceId(){try{let id=localStorage.getItem(DEVICE);if(!id){id=crypto.randomUUID?.()||`${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;localStorage.setItem(DEVICE,id);}return id;}catch{return deviceMemo||=`${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;}}
function setSinger(name){singerName=name;try{if(name)localStorage.setItem(SINGER,name);else localStorage.removeItem(SINGER);}catch{}showSinger();if(state)showControls();}  // whose settings the sheet shows depends on the name
async function claim(name){return api('/api/singer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,device:deviceId()})});}
function askName(message=''){$('name-input').value=singer();$('name-error').textContent=message;$('name-error').hidden=!message;$('name-gate').hidden=false;setTimeout(()=>$('name-input').focus(),50);}
$('name-form').onsubmit=async e=>{e.preventDefault();const name=$('name-input').value.split(/\s+/).filter(Boolean).join(' ').slice(0,40);if(!name)return;
 $('name-save').disabled=true;
 try{const r=await claim(name);setSinger(r.name);$('name-gate').hidden=true;toast(`Singing as ${r.name}`);}
 catch(error){$('name-error').textContent=error.message;$('name-error').hidden=false;}
 finally{$('name-save').disabled=false;}};
$('singer-change').onclick=()=>askName();
// Keep the name while the remote is open; if someone else took it meanwhile, ask again.
async function keepName(){const name=singer();if(!name)return askName();try{const r=await claim(name);setSinger(r.name);}catch(error){if(/already/.test(error.message)){setSinger('');askName(error.message);}}}
setInterval(()=>{if(!document.hidden)keepName();},15*60*1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)keepName();});
showSinger();keepName();

// karaoke-server: settings lock. Room settings (everything in the sheet but Singer) start locked so a pocket tap
// can't change them. Unlock asks to confirm, lasts 2 minutes, and locks again early when the phone sleeps.
const UNLOCK_MS=120000;let unlockedUntil=0,lockTimer=null,confirming=false;
const lockedSections=()=>[...$('sheet').querySelectorAll('.sg')].filter(sg=>sg.id!=='sg-singer');
function renderLock(){
 const open=Date.now()<unlockedUntil;
 for(const sg of lockedSections())sg.inert=!open;
 const bar=$('lockbar');bar.className=`lockbar ${open?'unlocked':confirming?'confirming':'locked'}`;
 $('lock-cancel').hidden=!confirming||open;
 if(open){const left=Math.ceil((unlockedUntil-Date.now())/1000);$('lock-text').textContent=`🔓 Unlocked · locks in ${Math.floor(left/60)}:${String(left%60).padStart(2,'0')}`;$('lock-go').textContent='Lock now';}
 else if(confirming){$('lock-text').textContent='Unlock settings for 2 minutes?';$('lock-go').textContent='Unlock';}
 else{$('lock-text').textContent='🔒 Settings are locked';$('lock-go').textContent='Unlock';}
}
function lockSettings(){unlockedUntil=0;confirming=false;clearInterval(lockTimer);lockTimer=null;renderLock();}
function unlockSettings(){unlockedUntil=Date.now()+UNLOCK_MS;confirming=false;clearInterval(lockTimer);
 lockTimer=setInterval(()=>{if(Date.now()>=unlockedUntil){lockSettings();toast('Settings locked');}else renderLock();},1000);
 renderLock();requestAnimationFrame(()=>SEGMENTED.forEach(placePill));haptic(8);}
$('lock-go').onclick=()=>{if(Date.now()<unlockedUntil)return lockSettings();if(confirming)return unlockSettings();confirming=true;renderLock();};
$('lock-cancel').onclick=()=>{confirming=false;renderLock();};
document.addEventListener('visibilitychange',()=>{if(document.hidden)lockSettings();});
lockSettings();

// karaoke-server: fun extras. A shout-out (short text) or a photo pops up on the TV with your singer name.
// Photos are shrunk on the phone first (longest side 1280 px, JPEG), so they upload fast even on the slow route.
function onFun(event){
 if(event.type!=='fun')return;
 // During this phone's own photo batch, only the last photo's notice shows (it carries the Hide).
 if(event.kind==='photo'&&photoBatch&&event.name===singer()&&--photoBatch.noticesLeft>0)return;
 const what=event.kind==='photo'?'Photo':'Shout-out';
 toast(`${what} from ${event.name} on the TV`,{label:'Hide',run:()=>send({action:'hide',id:event.id})});
}
$('shout-open').onclick=()=>{if(!singer())return askName();$('shout-input').value='';$('shout-count').textContent='0/60';$('shout-box').hidden=false;setTimeout(()=>$('shout-input').focus(),50);};
$('shout-cancel').onclick=()=>{$('shout-box').hidden=true;};
$('shout-input').oninput=()=>{$('shout-count').textContent=`${$('shout-input').value.length}/60`;};
$('shout-form').onsubmit=e=>{e.preventDefault();const text=$('shout-input').value.trim();if(!text)return;
 send({action:'shout',value:text,singer:singer(),device:deviceId()});$('shout-box').hidden=true;haptic([10,30,10]);};
async function shrinkPhoto(file){
 const bitmap=await createImageBitmap(file,{imageOrientation:'from-image'}).catch(()=>createImageBitmap(file));
 const scale=Math.min(1,1280/Math.max(bitmap.width,bitmap.height));
 const canvas=document.createElement('canvas');canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);
 canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close?.();
 for(const quality of [0.8,0.65,0.5]){const blob=await new Promise(r=>canvas.toBlob(r,'image/jpeg',quality));if(blob&&blob.size<=600000)return blob;}
 throw new Error('That photo is too big');
}
$('photo-open').onclick=()=>{if(!singer())return askName();$('photo-input').value='';$('photo-input').click();};  // pick one or several
// Several photos can be picked at once: they go one by one, spaced to the server's per-phone photo cooldown, and
// pile up on the TV. At most PHOTO_BATCH per pick, the number the TV shows at once.
const PHOTO_BATCH=6,PHOTO_GAP=3200;
// karaoke-server: the date the photo was taken, for the polaroid. Read from the JPEG's EXIF (DateTimeOriginal,
// else DateTime) before shrinking, which strips all EXIF; only this date is sent, never GPS or camera details.
// No EXIF (screenshots, PNG, HEIC): the file's date, else today.
async function photoDate(file){
 try{
  const v=new DataView(await file.slice(0,262144).arrayBuffer());
  if(v.getUint16(0)===0xFFD8){
   for(let o=2;o+10<v.byteLength;){
    const marker=v.getUint16(o),len=v.getUint16(o+2);
    if(marker===0xFFE1&&v.getUint32(o+4)===0x45786966){
     const t=o+10,le=v.getUint16(t)===0x4949,u16=p=>v.getUint16(p,le),u32=p=>v.getUint32(p,le);
     const find=(ifd,tags)=>{const n=u16(t+ifd);for(let i=0;i<n;i++){const e=t+ifd+2+i*12;if(tags.includes(u16(e)))return u32(e+8);}return null;};
     const text=off=>{let s='';for(let i=0;i<19;i++){const c=v.getUint8(t+off+i);if(!c)break;s+=String.fromCharCode(c);}return s;};
     const ifd0=u32(t+4),exif=find(ifd0,[0x8769]);let at=exif!=null?find(exif,[0x9003,0x9004]):null;if(at==null)at=find(ifd0,[0x0132]);
     const m=at!=null&&text(at).match(/^(\d{4}):(\d{2}):(\d{2})/);
     if(m&&m[1]!=='0000')return `${m[1]}-${m[2]}-${m[3]}`;
     break;
    }
    if((marker&0xFF00)!==0xFF00||!len)break;o+=2+len;
   }
  }
 }catch{}
 const d=new Date(file.lastModified||Date.now());return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
async function sendPhoto(file){
 const taken=await photoDate(file),blob=await shrinkPhoto(file);
 const r=await fetch('/api/photo',{method:'POST',headers:{'Content-Type':'image/jpeg','X-Singer':encodeURIComponent(singer()),'X-Device':deviceId(),'X-Photo-Date':taken},body:blob});
 if(!r.ok){let m=`Couldn't send (${r.status})`;try{m=(await r.json()).detail||m;}catch{}throw new Error(m);}
}
// karaoke-server: the Photo button itself shows the progress of a pick ("Sending 2 of 5" over a filling bar)
// instead of a toast that keeps changing. While your own batch goes out, the TV's "Photo from you" notices are
// held back on this phone except for the last one, which keeps its Hide. Only a cap or a failure gets a toast.
let photoBatch=null;  // {total, noticesLeft} while this phone is sending a pick
function showPhotoProgress(done,total,sending){
 const b=$('photo-open');
 if(!total){b.classList.remove('sending');b.style.removeProperty('--p');b.textContent='📷 Photo';return;}
 b.classList.add('sending');b.style.setProperty('--p',(done/total).toFixed(3));b.textContent=`Sending ${Math.min(done+(sending?1:0),total)} of ${total}`;
}
$('photo-input').onchange=async()=>{
 const files=[...($('photo-input').files||[])];if(!files.length)return;
 const batch=files.slice(0,PHOTO_BATCH);
 if(files.length>PHOTO_BATCH)toast(`Sending the first ${PHOTO_BATCH} of ${files.length} photos`);
 $('photo-open').disabled=true;let sent=0,failed=0;photoBatch={total:batch.length,noticesLeft:batch.length};
 try{
  for(const [i,file] of batch.entries()){
   if(i){  // the server takes one photo per 3 s from a phone: let the bar creep forward meanwhile
    const from=(i)/batch.length,to=(i+.5)/batch.length,t0=Date.now();
    await new Promise(r=>{const tick=()=>{const f=Math.min(1,(Date.now()-t0)/PHOTO_GAP);$('photo-open').style.setProperty('--p',(from+(to-from)*f).toFixed(3));f<1?requestAnimationFrame(tick):r();};tick();});
   }
   showPhotoProgress(i,batch.length,true);
   try{await sendPhoto(file);sent++;haptic(10);}catch(error){failed++;photoBatch.noticesLeft--;toast(error.message,undefined,'err');}
   showPhotoProgress(i+1,batch.length,false);
  }
  if(failed)toast(`${sent} of ${batch.length} photos sent`,undefined,'err');
 }finally{photoBatch=null;showPhotoProgress(0,0);$('photo-open').disabled=false;$('photo-input').value='';}
};

// karaoke-server: the cheers, Shout-out and Photo live in the Now singing card while a song plays. Between songs
// that card folds away, so they move into their own little panel and keep working; they move back on the next song.
const FUN_ROWS=[document.querySelector('.reactions'),$('photo-input').parentElement];  // emoji row, Shout-out/Photo row
// A bookmark (empty comment) marks each row's home in the card, so it can go back exactly there.
const funHome=FUN_ROWS.map(row=>{const mark=document.createComment('fun-row home');row.before(mark);return mark;});
function placeFun(playing){
 const idle=$('fun-idle');idle.hidden=playing;
 if(playing)FUN_ROWS.forEach((row,i)=>{if(funHome[i].nextSibling!==row)funHome[i].after(row);});
 else FUN_ROWS.forEach(row=>{if(row.parentElement!==idle)idle.append(row);});
}

// karaoke-server: one number for "how close is my song": the preparation steps (download, separate vocals, video,
// lyrics) folded into a single 0-100% ring. Separating and syncing have no progress of their own: the server says
// when they started and how long they should take, and we fill in between. It never goes backwards.
function readyPercent(item){return prepPercent(item,skew);}  // shared with the TV
function readyText(item){return item.status==='error'?'Couldn\u2019t prepare':item.status==='ready'?'\u2713 Ready':`${readyPercent(item)}%`;}
function showReady(row,item){
 const pct=item.status==='ready'?100:readyPercent(item);
 row.querySelector('.status').textContent=readyText(item);row.classList.toggle('is-ready',item.status==='ready');
 const ring=row.querySelector('.ring');if(ring){ring.style.setProperty('--p',pct/100);ring.classList.remove('busy');}
 const label=row.querySelector('.pct');if(label)label.textContent=item.status==='ready'?'\u2713':`${pct}%`;
}
setInterval(()=>{if(!state)return;for(const item of state.upcoming){const row=$('queue').querySelector(`[data-key="${item.key}"]`);if(row)showReady(row,item);}},1000);

// karaoke-server: long titles scroll: queued songs, the now-singing card and the mini player.
const REMOTE_MARQUEES='#queue [data-key] h3,#dock-title';  // #hero-title already wraps to 3 lines
setInterval(()=>marquees(REMOTE_MARQUEES),1500);addEventListener('resize',()=>marquees(REMOTE_MARQUEES));

// karaoke-server: your own emoji, up to 4 per phone (kept in this browser), next to the built-in cheers.
const MY_EMOJI='karaoke.emoji',EMOJI_MAX=4;let myEmojiMemo=[];
function myEmoji(){try{const list=JSON.parse(localStorage.getItem(MY_EMOJI)||'[]');return Array.isArray(list)?list:[];}catch{return myEmojiMemo;}}
function saveEmoji(list){myEmojiMemo=list;try{localStorage.setItem(MY_EMOJI,JSON.stringify(list));}catch{}showMyEmoji();}
// Exactly one emoji (skin tones, flags and combined ones like 👨‍🎤 count as one); no plain text.
function oneEmoji(text){
 const t=text.trim();if(!t||!/\p{Extended_Pictographic}|\p{Regional_Indicator}/u.test(t)||/[\p{L}\p{N}]/u.test(t.replace(/[#*0-9]\uFE0F?\u20E3/gu,'')))return null;
 const parts=typeof Intl.Segmenter==='function'?[...new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(t)].length:[...t].length<=8?1:2;
 return parts===1?t:null;
}
function showMyEmoji(){
 for(const old of document.querySelectorAll('.react.custom'))old.remove();
 const add=$('emoji-add'),list=myEmoji();
 for(const emoji of list){const b=el('button',emoji,'react custom');b.dataset.emoji=emoji;b.setAttribute('aria-label',`Cheer ${emoji}`);b.onclick=()=>reactWith(b);add.before(b);}
 $('emoji-mine').replaceChildren(...list.map((emoji,i)=>{const chip=el('button',undefined,'emoji-chip');chip.type='button';chip.append(el('span',emoji),el('b','×'));chip.setAttribute('aria-label',`Remove ${emoji}`);
  chip.onclick=()=>saveEmoji(myEmoji().filter((_,j)=>j!==i));return chip;}));
 $('emoji-mine').hidden=!list.length;
}
function emojiError(text){$('emoji-error').textContent=text;$('emoji-error').hidden=!text;}
$('emoji-add').onclick=()=>{emojiError('');$('emoji-input').value='';$('emoji-box').hidden=false;setTimeout(()=>$('emoji-input').focus(),50);};
$('emoji-done').onclick=()=>{$('emoji-box').hidden=true;};
$('emoji-form').onsubmit=e=>{e.preventDefault();
 const emoji=oneEmoji($('emoji-input').value),list=myEmoji();
 if(!emoji)return emojiError('Pick one emoji from your emoji keyboard');
 if(list.includes(emoji)||['👏','🔥'].includes(emoji))  // karaoke-server: only 👏 and 🔥 built in, to encourage your own
 return emojiError('You already have that one');
 if(list.length>=EMOJI_MAX)return emojiError(`Up to ${EMOJI_MAX}: remove one first (tap it above)`);
 saveEmoji([...list,emoji]);$('emoji-input').value='';emojiError('');haptic(8);};
showMyEmoji();

// karaoke-server: after a song is added from search, leave the search (results and box cleared, keyboard closed)
// and show where it landed: scroll to its queue row and give it a short highlight.
function leaveSearch(key){
 $('query').value='';$('query').blur();$('clear').hidden=true;searchRun++;suggestRun++;
 $('results').replaceChildren();$('more').hidden=$('results-end').hidden=true;$('suggestions').hidden=true;$('query').setAttribute('aria-expanded','false');
 let tries=0;
 const find=()=>{const row=key&&$('queue').querySelector(`[data-key="${CSS.escape(key)}"]`);
  if(!row){if(++tries<20)setTimeout(find,100);return;}
  row.scrollIntoView({behavior:REDUCED.matches?'auto':'smooth',block:'center'});
  row.classList.remove('just-added');void row.offsetWidth;row.classList.add('just-added');setTimeout(()=>row.classList.remove('just-added'),2600);};
 find();
}
