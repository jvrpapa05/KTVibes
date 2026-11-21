import {$,el,connect,api,clock,throttle,prepLabel,syncRanges,REDUCED,thumbnail,newSongs,marquees,prepPercent} from './shared.js';
const music=$('instrumental'),voice=$('vocals'),video=$('backdrop');
video.muted=true;
let breather=0,guide,state,lyrics=[],firstLine=-1,stemGain=1,currentKey=null,context,musicGain,voiceGain,shifter,enabled=false,activeLine=-1,starting=false,generation=0,lastReport=0,seekTo=null,serverSkew=0,buffering=false,videoStarting=false,lyricsPositioned=false,lastSeek=0,shownPair='',fitKey='';
const error=text=>$('tv-error').textContent=text;
// This screen remembers its own display settings and restores them each time it (re)connects.
// karaoke-server: the TV no longer restores vocals (they belong to singer profiles, and a TV reload must not overwrite one) or guides (off).
let deckShown='';  // karaoke-server: what the Up next list last showed
const SETTINGS='ktvibes.tv-settings',SETTING_KEYS=['lyric_scale','video_mode','lyric_mode'];let restoring=false;
// On (re)connect, also tell the server whether sound is on, so remotes know if playback can start.
// Many music videos have black bars baked into the frame; sample a few frames, find the bars,
// then zoom past them so the picture itself fills the screen. The smallest bars seen win, so a dark scene can't over-crop.
const probe=document.createElement('canvas');probe.width=160;probe.height=90;const probeContext=probe.getContext('2d',{willReadFrequently:true});
function fillFrame(v){
 let bars,samples=0,lastSample=0;
 const reset=()=>{bars=null;samples=0;lastSample=0;v.style.transform='';};
 const apply=()=>{
  if(!bars||!v.videoWidth)return;
  const W=innerWidth,H=innerHeight,w=v.videoWidth,h=v.videoHeight,cw=w*(1-bars.left-bars.right),ch=h*(1-bars.top-bars.bottom);
  const full=Math.max(W/w,H/h),crop=Math.max(W/cw,H/ch)*1.01,scale=crop/full;
  const dx=(bars.right-bars.left)/2*w*crop,dy=(bars.bottom-bars.top)/2*h*crop;
  v.style.transform=scale>1.01?`translate(${dx}px,${dy}px) scale(${scale})`:'';
 };
 const sample=()=>{
  try{probeContext.drawImage(v,0,0,160,90);}catch{return;}
  const {data}=probeContext.getImageData(0,0,160,90),dark=(x0,y0,x1,y1)=>{for(let y=y0;y<y1;y++)for(let x=x0;x<x1;x++){const i=(y*160+x)*4;if(data[i]+data[i+1]+data[i+2]>72)return false;}return true;};
  let top=0,bottom=0,left=0,right=0;
  while(top<45&&dark(0,top,160,top+1))top++;while(bottom<45&&dark(0,89-bottom,160,90-bottom))bottom++;
  while(left<80&&dark(left,0,left+1,90))left++;while(right<80&&dark(159-right,0,160-right,90))right++;
  if(top+bottom>60||left+right>110)return;  // a mostly black frame (fade, title card) says nothing about the bars
  const found={top:top/90,bottom:bottom/90,left:left/160,right:right/160};
  bars=bars?Object.fromEntries(Object.keys(found).map(k=>[k,Math.min(bars[k],found[k])])):found;samples++;apply();
 };
 v.addEventListener('loadstart',reset);
 v.addEventListener('timeupdate',()=>{if(samples<8&&v.currentTime-lastSample>1.5||v.currentTime<lastSample){lastSample=v.currentTime;sample();}});
 addEventListener('resize',apply);
}
fillFrame(video);
// An empty stage loops random cached music videos, muted, behind the idle screen.
const ambient=$('ambient');ambient.muted=true;fillFrame(ambient);let ambientIds=[],ambientOn=false;
function nextAmbient(){
 if(!ambientOn||!ambientIds.length){ambient.hidden=true;return;}
 const last=ambient.dataset.id,choices=ambientIds.length>1?ambientIds.filter(id=>id!==last):ambientIds,id=choices[Math.floor(Math.random()*choices.length)];
 ambient.dataset.id=id;ambient.src=`/media/${id}/video.mp4`;ambient.play().then(()=>{if(ambientOn)ambient.hidden=false;}).catch(()=>ambient.hidden=true);
}
function setAmbient(on){
 if(on===ambientOn)return;ambientOn=on;
 if(!on){ambient.pause();ambient.hidden=true;ambient.removeAttribute('src');ambient.load();return;}
 // Refetch each time the stage empties, so songs downloaded since join the rotation.
 api('/api/ambient').then(ids=>{ambientIds=ids;nextAmbient();}).catch(()=>{});
 loadSongbook();
}
ambient.onended=nextAmbient;
ambient.onerror=()=>{if(!ambientOn)return;ambientIds=ambientIds.filter(id=>id!==ambient.dataset.id);nextAmbient();};
const send=connect('tv',render,error,()=>{restoring=true;send({action:'audio',value:enabled});if(currentKey&&finishedKey===currentKey)send({action:'ended',key:currentKey});},event=>{if(event.type==='react')cheer(event.value);else if(event.type==='shout')showShout(event);else if(event.type==='photo')showPhoto(event);else if(event.type==='hide')hideFun(event.id);else if(event.type==='dial')dial(event.digits);else if(event.type==='reserved')reserved(event.number);});
function saved(){try{return JSON.parse(localStorage.getItem(SETTINGS))||{};}catch{return {};}}
function remember(s){try{localStorage.setItem(SETTINGS,JSON.stringify(Object.fromEntries(SETTING_KEYS.map(k=>[k,s[k]]))));}catch{}}
function restore(s){const mine=saved();for(const action of SETTING_KEYS){const value=mine[action];if(value!==undefined&&value!==s[action])send({action,value});}}
api('/api/config').then(c=>$('remote-url').textContent=$('sticker-url').textContent=c.remote_url).catch(e=>error(e.message));
function render(next){
 state=next;if(state.version)$('app-version').textContent=`v${state.version}`;showPhones(state.phones);showAfter(state);if(restoring){restoring=false;restore(state);}else remember(state);if(state.guide!==guide){if(guide!==undefined)showGuideBadge(state.guide);guide=state.guide;document.body.dataset.guide=guide;}showChanges(state);showQueueChange(state);document.body.style.setProperty('--lyric-scale',state.lyric_scale??1.5);document.body.dataset.video=state.video_mode||'show';document.body.dataset.lyrics=state.lyric_mode||'two';document.body.dataset.theme=state.theme||'default';for(const b of document.querySelectorAll('[data-theme-pick]'))b.setAttribute('aria-pressed',b.dataset.themePick===document.body.dataset.theme);showClassicBar(state);showSpecs(state);showBoot();roomClock();serverSkew=state.server_time-Date.now()/1000;$('tv-play').classList.toggle('paused',!state.playing);$('tv-play').setAttribute('aria-label',state.playing?'Pause':'Play');$('tv-guide').textContent=GUIDE_BADGES[state.guide]?.[0]??'Aa';$('tv-guide').hidden=(state.guides||[]).length<2;syncRanges();applyGain();
 const queued=state.upcoming.filter(i=>i.status!=='error');
 // karaoke-server: rebuild only when what it shows changes (a rebuild restarts the scrolling of long titles),
 // and start scrolling the new rows straight away.
 const deck=JSON.stringify([queued.length,...queued.slice(0,3).map(i=>[i.singer,i.title,i.artist,i.status==='ready'?'':'prep'])]);  // the % updates in place (deckPercents)
 if(deck!==deckShown){deckShown=deck;
 // The number and the % stay put; only the text between them scrolls (clipped, never behind the number).
 $('on-deck').replaceChildren(...queued.slice(0,3).map((i,n)=>{const row=el('li'),text=el('span',undefined,'deck-text');text.append(...(i.singer?[el('em',`${i.singer}: `,'deck-singer')]:[]),el('b',i.title),el('span',` ${i.artist}`));row.append(text,...(i.status==='ready'?[]:[(()=>{const pct=el('small',`${prepPercent(i,serverSkew)}%`,'deck-pct');pct.dataset.key=i.key;return pct;})()]),el('span',`${n+1}`,'deck-num'));return row;}));  // karaoke-server: number on the right, like the panel
 if(queued.length>3)$('on-deck').append(el('li',`+${queued.length-3} more`,'more'));
 if(!queued.length)$('on-deck').append(el('li','Nothing queued','more'));
 marquees('#on-deck .deck-text');}
 // The two songs after the next one peek out from under its card between songs.
 [1,2].forEach(n=>{const item=queued[n-1],peek=$(`peek-${n}`),line=el('span');peek.hidden=!item;if(item){line.append(el('b',String(n+1)),`${item.title} · ${item.artist}`);peek.replaceChildren(line);}});
 showPrep(queued);showBoard(state,queued);setAmbient(!state.current);
 $('now-card').hidden=!state.current;
 // The idle screen already shows a large QR code.
 $('corner-join').hidden=$('qr-mini').hidden=!state.current;
 $('tv-bottom').hidden=!state.current;
 if((state.current?.key||null)!==currentKey){
  endScore();  // skipped: the score stays through the breather
  generation++;music.pause();voice.pause();video.pause();starting=false;buffering=false;videoStarting=false;currentKey=state.current?.key||null;activeLine=-1;relayout();lastSeek=state.seek_id;$('lyrics').replaceChildren();$('lyrics').scrollTo({top:0,behavior:'instant'});error('');
  if(state.current){
   const item=state.current;
   video.hidden=!item.video;$('video-shade').hidden=!item.video;
   if(item.video){video.src=`/media/${item.id}/video.mp4`;video.load();}else{video.removeAttribute('src');video.load();}
   stemGain=item.gain||1;applyGain();seekTo=state.position||0;music.src=`/media/${item.id}/no_vocals`;voice.src=`/media/${item.id}/vocals`;music.load();voice.load();
   $('song-title').textContent=item.title;$('song-artist').textContent=item.artist;$('now-art').src=thumbnail(item.id);$('song-singer').textContent=item.singer||'';$('song-singer').hidden=!item.singer;$('next-singer').textContent=item.singer?`🎤 ${item.singer}`:'';$('next-singer').hidden=!item.singer;$('next-title').textContent=$('card-title').textContent=item.title;$('next-art').src=thumbnail(item.id,'hqdefault');$('next-artist').textContent=$('card-artist').textContent=item.artist;$('card-number').textContent=item.number??'-----';$('card-credit').textContent=`원곡 ${item.artist}`;$('spec-length').textContent=clock(item.duration);
   showLyrics(item);$('duration').textContent=clock(item.duration);
  }else{lyrics=[];firstLine=-1;video.pause();video.removeAttribute('src');video.load();video.hidden=true;$('video-shade').hidden=true;music.removeAttribute('src');voice.removeAttribute('src');music.load();voice.load();$('elapsed').textContent='0:00';$('duration').textContent='0:00';$('progress').parentElement.style.setProperty('--p',0);}
 }
 else if(state.current&&(state.current.lyrics_rev||0)!==lyricsRev){activeLine=-1;relayout();showLyrics(state.current);}
 // "Hide video" keeps the plain stage layout; the video element stays loaded so it can come back in sync.
 document.body.classList.toggle('has-video',showVideo()&&!video.error);
 // Switching layout, size or guide changes how wide each line is: place and fit the lines again.
 const nextFit=`${state.lyric_mode}|${state.lyric_scale}|${state.guide}`;
 if(nextFit!==fitKey){fitKey=nextFit;relayout();}
 if(state.current&&state.seek_id!==lastSeek){
  lastSeek=state.seek_id;
  if(music.readyState&&seekTo===null){music.currentTime=voice.currentTime=state.position;if(state.current.video&&video.readyState)video.currentTime=state.position;}else seekTo=state.position;
  lyricsPositioned=false;
 }
 if(!state.playing){music.pause();voice.pause();video.pause();}
}
// Lyrics for the song on stage; a remote can swap in another version mid-song (lyrics_rev).
let lyricsRev=0,songLyrics=[],splitDone=false,slots=[],counting=null,still=false;  // counting: the line showing count-in dots; still: place the next lines without animating (after a resize)
const showVideo=()=>!!state?.current?.video&&state.video_mode!=='hide';
// Place the lyrics from scratch (new song or lyrics, layout, size, guide or window change).
function relayout(){shownPair='';lyricsPositioned=false;splitDone=false;if(lyrics!==songLyrics){activeLine=-1;placeLyrics(songLyrics);}}
// Two-line KTV, like a karaoke machine: lines take turns in a top and a bottom slot, and the next
// line is always up before it is sung. A line gives its slot to the line after next as soon as the
// following line starts. Over an instrumental break (GAP), the screen clears once the last line is
// sung, and the next two lines come up together for the count-in; each section starts in the top slot.
// A backing-vocal line "(…)" keeps the lead line it is sung over on screen.
const GAP=4,HOLD=1,LEAD=1.5;  // seconds: a break this long clears the screen, HOLD after the last line ends; LEAD: least warning before a line is sung
const startOf=i=>lyrics[i]?(lyrics[i].start??lyrics[i].t):Infinity;
const endOf=i=>lyrics[i]?.units?.at(-1)?.[2]??startOf(i+1);
const breakAfter=i=>i>=0&&startOf(i+1)-endOf(i)>GAP;
function twoLines(index,time){
 if(firstLine<0)return;
 if(!splitDone&&$('lyrics').clientWidth){splitDone=true;fitSong();if(splitLong()){activeLine=-1;lyricsPositioned=false;return;}}
 let shown;
 if(index<0)shown=[firstLine,firstLine+1];
 else if(lyrics[index].over!=null)shown=[index-1,index];
 // The previous line keeps its slot until its last word has finished filling.
 else if(index>0&&time<endOf(index-1)&&!breakAfter(index-1))shown=[index-1,index];
 // Both halves of a line split to fit stay up together while the second is sung, so the sentence reads as one,
 // unless the next line needs its slot sooner to come up LEAD seconds before it is sung.
 else if(lyrics[index].continues&&time<Math.min(endOf(index),startOf(index+1)-LEAD))shown=[index-1,index];
 else if(breakAfter(index)&&time>endOf(index)+HOLD)shown=time<startOf(index+1)-COUNT_IN?[]:[index+1,index+2];
 // The last line before a break has no next line to show, so the line before it stays up.
 else shown=breakAfter(index)?(index>0&&!breakAfter(index-1)?[index-1,index]:[index]):[index,index+1];
 shown=shown.filter(i=>i>=0&&i<lyrics.length&&(i===shown.at(-1)||!breakAfter(i)));
 const pair=shown.join();if(pair===shownPair)return;shownPair=pair;
 [...$('lyrics').children].forEach((line,i)=>{
  const on=shown.includes(i);
  if(on!==line.classList.contains('shown')){line.classList.toggle('shown',on);if(!still)swapLine(line,on);}
  if(on){line.dataset.slot=slots[i];condense(line);}
 });
 still=false;
}
// A line rises into its slot as the old one lifts away; the new one waits a beat so the slot clears first.
// The leaving line stays drawn (.leaving) until it has faded, sharing the grid cell with its replacement.
const SWAP_IN=[{opacity:0,transform:'translateY(.55em)'},{opacity:1,transform:'none'}],SWAP_OUT=[{opacity:1,transform:'none'},{opacity:0,transform:'translateY(-.45em)'}];
function swapLine(line,on){
 for(const animation of line.getAnimations())if(animation.id==='swap')animation.cancel();
 line.classList.remove('leaving');
 if(REDUCED.matches)return;
 if(on)line.animate(SWAP_IN,{id:'swap',duration:480,delay:110,easing:'cubic-bezier(.2,0,0,1)',fill:'backwards'});
 else{line.classList.add('leaving');const out=line.animate(SWAP_OUT,{id:'swap',duration:240,easing:'cubic-bezier(.4,0,1,1)',fill:'forwards'});out.onfinish=()=>{line.classList.remove('leaving');out.cancel();};}
}
// Two-line lyrics share one size for the whole song, so lines don't change size as they swap.
// The size fits every line on one line, down to 70% of the chosen lyric size, so one long line
// can't shrink the whole song much. Lines still too long are split in two
// (splitLong); a line that can't be split is condensed sideways like a KTV machine does (same
// height, narrower letters), and only extreme ones wrap.
// The width a two-line lyric has: the lyrics' content box.
function lyricRoom(){const box=$('lyrics'),style=getComputedStyle(box);return box.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);}
function fitSong(){
 const container=$('lyrics'),lines=[...container.children];
 container.style.setProperty('--fit',1);
 for(const line of lines)delete line.dataset.fit;
 container.classList.add('measuring');
 const room=lyricRoom(),ratios=lines.filter(line=>line.textContent.trim()).map(line=>room/Math.max(1,line.scrollWidth)).sort((a,b)=>a-b);
 const ratio=Math.min(1,ratios[0]??1);
 container.classList.remove('measuring');
 container.style.setProperty('--fit',Math.max(0.7,Math.floor(ratio*98)/100));
}
// Measured once per layout; two-line CSS applies the result (data-fit), so other layouts ignore it.
function condense(line){
 if(line.dataset.fit!==undefined)return;
 const ratio=line.clientWidth/Math.max(1,line.scrollWidth);
 line.dataset.fit=ratio>=1?'':ratio>=0.7?'squeeze':'wrap';
 line.style.setProperty('--squeeze',Math.floor(Math.min(1,ratio)*1000)/1000);
}
let resizeTimer;addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{still=document.body.dataset.lyrics==='two';relayout();},150);});
// A line too long for the screen at the song's size is split at a word boundary, like a KTV machine:
// each part takes its own turn in the slots, with its own word timing. Prefers a break after
// punctuation, then the break that makes the parts most even. Only lines with word timing split.
// Empty break lines are dropped. Returns whether anything changed (the lyrics are then rendered again).
function splitLong(){
 const container=$('lyrics'),parts=[],index=[],room=lyricRoom()+1;let split=false;
 container.classList.add('measuring');
 [...container.children].forEach((line,i)=>{
  const source=lyrics[i],units=source.units,spans=line.children;
  index.push(parts.length);
  if(!source.text){split=true;return;}  // an empty "♪" line only marks a break, which the screen clearing shows
  if(!units||units.length<2||line.scrollWidth<=room||spans.length!==units.length){parts.push(source);return;}
  const left=[...spans].map(span=>span.offsetLeft),right=[...spans].map(span=>span.offsetLeft+span.offsetWidth);
  const width=(a,b)=>right[b-1]-left[a];
  const cut=(a,b)=>{
   if(b-a<2||width(a,b)<=room)return [[a,b]];
   let best=a+1,score=Infinity;
   for(let k=a+1;k<b;k++){
    const w1=width(a,k),w2=width(k,b),text=units[k-1][0];
    const value=Math.abs(w1-w2)/width(a,b)-(/[,，、;；:：!！?？。]\s*$/.test(text)?0.3:0)-(/\s$/.test(text)?0.1:0)+(w1>room||w2>room?1:0);
    if(value<score){score=value;best=k;}
   }
   return [...cut(a,best),...cut(best,b)];
  };
  const ranges=cut(0,units.length);if(ranges.length>1)split=true;
  ranges.forEach(([a,b],n)=>{const part=units.slice(a,b);part[part.length-1]=[part.at(-1)[0].trimEnd(),...part.at(-1).slice(1)];parts.push({...source,units:part,text:part.map(unit=>unit[0]).join('').trim(),...n?{t:part[0][1],start:part[0][1],continues:true}:{}});});
 });
 container.classList.remove('measuring');
 if(!split)return false;
 // A backing-vocal line points at its lead line; point it at the lead's last part.
 for(const part of parts)if(part.over!=null)part.over=(index[part.over+1]??parts.length)-1;
 placeLyrics(parts);
 return true;
}
function showLyrics(item){
 songLyrics=item.lyrics||[];lyricsRev=item.lyrics_rev||0;placeLyrics(songLyrics);
}
// Classic theme: the noraebang top strip and the room's clock, which counts down between broadcasts.
const classic=()=>document.body.dataset.theme==='classic';
function showClassicBar(s){$('cb-number').textContent=s.current?.number??'';$('cb-title').textContent=s.current?`${s.current.title} - ${s.current.artist}`:s.time_up?'이용해 주셔서 감사합니다':'노래를 선택해 주세요 · Pick a song';$('cb-title').classList.toggle('idle',!s.current);$('cb-reserved').textContent=String(s.upcoming.length).padStart(2,'0');}
const SPEC_GUIDE={off:'끔',latin:'로마자 Aa',jyutping:'粵拼',hangul:'한글 가'};
function showSpecs(s){$('spec-key').textContent=!s.key?'원키':`${s.key>0?'♯':'♭'}${Math.abs(s.key)}`;$('spec-tempo').textContent=`${Math.round((s.speed??1)*100)}%`;$('spec-guide').textContent=SPEC_GUIDE[s.guide]??'—';}
// Between songs Classic shows the reservation table: the next song (already on stage) first, then the queue.
const lcdSpan=(text,ghost)=>{const span=el('span',text,'lcd');span.dataset.ghost=ghost;return span;};
function showBoard(s,queued){
 const row=(item,n)=>{const li=el('li',undefined,n?'':'next'),title=el('span',item.title,'t');title.append(el('span',` · ${item.artist}`,'a'));
  li.append(el('span',n?String(n+1):'다음','ord'),lcdSpan(item.number?String(item.number):'-----','88888'),title);
  if(!n){const go=el('span','시작까지','go'),seconds=lcdSpan('','88');seconds.id='board-go';go.append(seconds);li.append(go);}
  return li;};
 const items=s.current?[s.current,...queued.slice(0,4)]:[];
 $('board-rows').replaceChildren(...items.map(row));$('board-count').textContent=String(items.length&&queued.length+1).padStart(2,'0');
}
function roomClock(){
 if(!state)return;const left=state.room_ends==null?null:Math.max(0,state.room_ends-(Date.now()/1000+serverSkew));
 $('cb-clock').hidden=left==null;if(left!=null){$('cb-clock').textContent=`⏱${String(Math.ceil(left/60)).padStart(3,'0')}분`;$('cb-clock').classList.toggle('low',left<=300);}
 $('time-up').hidden=!(classic()&&left===0&&!state.current);
}
setInterval(roomClock,1000);
// The attract screen's ticker lists the newest songbook entries; the boot screen counts the book.
let songbookCount=0;
function loadSongbook(){api('/api/songbook').then(songs=>{songbookCount=songs.length;$('ticker-track').replaceChildren(...[0,1].flatMap(()=>newSongs(songs).map(s=>{const row=el('span');row.append(el('span',String(s.number),'lcd'),s.title,el('em',s.artist));return row;})));showBoot();}).catch(()=>{});}
// The boot screen's status line, like a machine's self-check.
function showBoot(){if(!state)return;const left=state.room_ends==null?null:Math.max(0,state.room_ends-(Date.now()/1000+serverSkew)),item=(label,value,cls)=>{const s=el('span',`${label} `);s.append(el('b',value,cls));return s;};
 $('boot-status').replaceChildren(item('SONGBOOK',`${songbookCount}곡`),...(left==null?[]:[item('ROOM',`${String(Math.ceil(left/60)).padStart(3,'0')}분`)]),item('SOUND','OFF','off'));}
loadSongbook();
// The big LCD fills in as someone dials: lit digits, a blinking cursor, blank digits after it.
function showAttractDial(digits){$('attract-dial').replaceChildren(digits,...(digits.length<5?[el('span','8','blink')]:[]),'!'.repeat(Math.max(0,4-digits.length)));}
showAttractDial('');
// Score after each song: random like the machines, mostly flattering, a perfect 100 now and then.
const SCORE_TIERS=[[100,'퍼펙트!!','완벽 그 자체!','가수 데뷔 하셔도 되겠어요!','Perfect score!'],[95,'앵콜곡!!','듣는 사람들 모두 감동!','남다른 멋진 목소리!','What a voice! Encore!'],[85,'훌륭해요!','분위기 최고!','노래 실력이 대단해요!','Great singing!'],[75,'좋아요!','즐거운 노래였어요!','다음 곡도 기대할게요!','Nice one!'],[60,'힘내요!','연습하면 더 잘할 수 있어요!','다시 한 번 도전!','Keep going!']];
function randomScore(){const r=Math.random(),pick=(low,count)=>low+Math.floor(Math.random()*count);return r<.03?100:r<.6?pick(85,15):r<.85?pick(75,10):pick(60,15);}
// It shows once the singing is over (the outro plays under it) and leaves after the breather that follows the song.
let scoreTimer=null,scoredKey=null;
function showScore(){
 const score=randomScore(),[,label,,highlight,en]=SCORE_TIERS.find(([min])=>score>=min),start=performance.now();
 scoredKey=currentKey;clearTimeout(scoreTimer);scoreTimer=null;
 $('score-label').textContent=label;$('score-msg').replaceChildren(el('b',highlight),` · ${en}`);$('score').hidden=false;
 (function roll(now){const k=Math.min(1,(now-start)/1200);$('score-value').textContent=Math.round(score*(1-(1-k)**3));if(k<1)requestAnimationFrame(roll);})(start);
}
function endScore(){if(!$('score').hidden&&!scoreTimer)scoreTimer=setTimeout(()=>{$('score').hidden=true;scoreTimer=null;},5000);}  // SCORE_HOLD in queue.py: Classic adds it to the breather, so the 예약곡 board follows
{const colours=['#ffe033','#ff3ea5','#63dcff','#7dff9a','#fff'];for(let i=0;i<40;i++){const bit=el('i');bit.style.cssText=`left:${Math.random()*100}%;background:${colours[i%5]};animation-delay:${-Math.random()*3.2}s;animation-duration:${2.6+Math.random()*1.6}s`;$('score-confetti').append(bit);}}
// Songbook dialling from the remote: digits show in the strip as they're typed.
let dialTimer;
function dial(digits){
 $('cb-dial').textContent=digits;$('cb-dial').hidden=!digits;showAttractDial(digits);
 clearTimeout(dialTimer);dialTimer=setTimeout(()=>{$('cb-dial').hidden=true;showAttractDial('');},8000);  // a keypad left mid-number
}
function reserved(){$('cb-dial').hidden=true;showAttractDial('');}
function placeLyrics(list){
 lyrics=list;firstLine=lyrics.findIndex(line=>line.text);$('lyrics').replaceChildren();
 slots=[];let slot=0;lyrics.forEach((line,i)=>{if(breakAfter(i-1))slot=0;slots[i]=slot;slot^=1;});
 lyrics.forEach((line,i)=>{const p=el('p',line.units?undefined:line.text||'♪','lyric');p.dir='auto';p.dataset.slot=slots[i];for(const [text,,,latin,hangul,jyutping] of line.units||[]){const span=el('span',undefined,latin||hangul||jyutping?'unit ruby':'unit');if(latin||hangul||jyutping){const word=text.trimEnd(),ruby=el('ruby',word),rt=el('rt');rt.append(el('span',latin||'','latin'),el('span',jyutping||'','jyutping'),el('span',hangul||'','hangul'));ruby.append(rt);span.append(ruby);p.append(span,text.slice(word.length));}else{span.textContent=text;p.append(span);}}$('lyrics').append(p);});$('no-lyrics').hidden=!!lyrics.length;$('lyrics').hidden=!lyrics.length;
}
// Stems are stored scaled down to fit FLAC; the gain restores their original level.
// Speed changes tempo only (the browser keeps pitch); the key shift is applied by the worklet.
function applyTempo(){const speed=state?.speed??1;for(const media of [music,voice]){  // karaoke-server: the video's rate is set by the sync loop
 video.defaultPlaybackRate=speed;media.defaultPlaybackRate=speed;if(media.playbackRate!==speed)media.playbackRate=speed;}if(shifter)shifter.parameters.get('ratio').value=2**((state?.key??0)/12);}
function applyGain(){applyTempo();if(!musicGain)return;musicGain.gain.value=(state?.music??1)*stemGain;voiceGain.gain.value=(state?.vocal??0.1)*stemGain;}
// The theme can be picked before sound is on; it switches the whole room (TV and phones).
for(const b of document.querySelectorAll('[data-theme-pick]'))b.onclick=()=>send({action:'theme',value:b.dataset.themePick});
async function enableSound(){
 try{
  if(!context){
   context=new AudioContext();musicGain=context.createGain();voiceGain=context.createGain();
   // Both stems run through one pitch shifter for key changes; without worklets, play at the original key.
   let out=context.destination;
   try{await context.audioWorklet.addModule('/static/pitch.js');shifter=new AudioWorkletNode(context,'pitch-shifter',{outputChannelCount:[2]});shifter.connect(out);out=shifter;}catch(e){console.warn('Key change unavailable',e);}
   context.createMediaElementSource(music).connect(musicGain).connect(out);context.createMediaElementSource(voice).connect(voiceGain).connect(out);applyGain();
  }
  await context.resume();enabled=true;send({action:'audio',value:true});$('enable').hidden=true;$('audio-error').textContent='';
 }catch(e){$('audio-error').textContent=e.message;}
}
$('enable-button').onclick=enableSound;
// karaoke-server: skip the Enable sound click when this browser already allows sound for the site (Chrome/Edge
// "Sound: Allow" site setting or --autoplay-policy=no-user-gesture-required; Firefox "Autoplay: Allow Audio and
// Video"). Without a click the AudioContext stays suspended, so the prompt shows as before.
(async()=>{
 try{
  const probe=new AudioContext();
  await Promise.race([probe.resume(),new Promise(r=>setTimeout(r,400))]);
  const allowed=probe.state==='running';probe.close();
  if(allowed)await enableSound();
 }catch{}
})();
$('fullscreen').onclick=()=>{const result=document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();result.catch(e=>error(e.message));};
addEventListener('fullscreenchange',()=>{const full=!!document.fullscreenElement,label=full?'Exit fullscreen':'Fullscreen';$('fullscreen').classList.toggle('full',full);$('fullscreen').setAttribute('aria-label',label);$('fullscreen').title=label;});
// karaoke-server: no Music/Vocals sliders on the TV; phones control the mix (and each singer's profile).
// Bottom-bar controls for whoever sits at the TV computer.
$('tv-play').onclick=()=>send({action:state?.playing?'pause':'play'});
$('tv-skip').onclick=()=>send({action:'skip'});
$('tv-guide').onclick=()=>send({action:'guide',value:'cycle'});
music.onwaiting=()=>{buffering=true;voice.pause();video.pause();};
music.onplaying=()=>{buffering=false;if(enabled&&state?.playing&&currentKey){voice.currentTime=music.currentTime;voice.play().catch(()=>error('Vocals could not resume. Pause and play to retry.'));}};
// A song ends when its file does, or in Classic where its music stops (music_end), so the score never waits through trailing silence.
let finishedKey=null;
function finish(){if(!currentKey||finishedKey===currentKey)return;finishedKey=currentKey;if(classic()){if(scoredKey!==currentKey)showScore();endScore();}music.pause();voice.pause();video.pause();send({action:'ended',key:currentKey});}
music.onended=finish;
for(const audio of [music,voice])audio.onerror=()=>{if(currentKey)error('Audio could not load. Check the server or skip this song.');};
video.onerror=()=>{video.hidden=true;$('video-shade').hidden=true;document.body.classList.remove('has-video');};
async function start(){
 // A finished song stays paused while the server moves on, even if it stopped short of its file's end.
 if(starting||(finishedKey&&finishedKey===currentKey)||!music.paused||music.ended||music.readyState<3||voice.readyState<3)return;
 const token=generation;starting=true;
 try{
  if(seekTo!==null){music.currentTime=seekTo;seekTo=null;}
  voice.currentTime=music.currentTime;
  await Promise.all([music.play(),voice.play()]);
  if(token===generation&&!state?.playing){music.pause();voice.pause();video.pause();}
 }catch(e){
  console.warn('Playback start failed',e.name,e.message);
  if(token===generation&&e.name==='NotAllowedError'){music.pause();voice.pause();enabled=false;send({action:'audio',value:false});$('enable').hidden=false;$('audio-error').textContent='Tap Enable sound to resume playback.';}
 }finally{if(token===generation)starting=false;}
}
const COUNT_IN=4;  // seconds before the first line that the lyrics appear
function frame(now){
 if(state){
  const waiting=state.current&&(Date.now()/1000+serverSkew)<state.transition_until;
  $('idle').hidden=!!state.current;$('up-next').hidden=!waiting;$('performance').hidden=!state.current||waiting;
  // Between songs, the ring drains over the breather; the card springs in as it starts.
  if(waiting){
   if(!breather)showUpNext();
   const left=Math.max(0,state.transition_until-(Date.now()/1000+serverSkew));breather=Math.max(breather,left);
   $('countdown-ring').style.strokeDashoffset=(100*(1-left/breather)).toFixed(2);const seconds=String(Math.ceil(left));if($('countdown').textContent!==seconds)$('countdown').textContent=seconds;if($('board-go'))$('board-go').textContent=seconds.padStart(2,'0');
  }else breather=0;
  if(!state.current||waiting)document.body.classList.remove('intro');
  document.body.classList.toggle('between',!!waiting);
  video.hidden=!showVideo()||!!waiting||!!video.error;
  $('video-shade').hidden=video.hidden;
  if(state.current&&!waiting&&enabled&&state.playing)start();
  if(showVideo()&&!waiting&&!buffering&&!music.paused&&state.playing&&video.readyState>=3){
   // karaoke-server: catch up gently. A small drift is fixed by playing the video up to 10% faster or slower,
   // which you can't see; only a big one (over 1.5 s) jumps. Every jump makes the browser re-decode from the
   // last keyframe, so on a slower TV jumping at 0.12 s kept stalling the video and drifting it again.
   const drift=music.currentTime-video.currentTime,speed=state.speed??1;
   if(Math.abs(drift)>1.5){video.currentTime=music.currentTime;video.playbackRate=speed;tvStats.jumps++;}
   else if(Math.abs(drift)>0.12)video.playbackRate=speed*(1+Math.max(-.1,Math.min(.1,drift*.25)));
   else if(video.playbackRate!==speed)video.playbackRate=speed;
   if(Math.abs(drift)<=1.5)tvStats.maxDrift=Math.max(tvStats.maxDrift,Math.abs(drift));  // song-start syncs aren't lag
   if(video.paused&&!video.ended&&!videoStarting){videoStarting=true;const token=generation;video.play().catch(()=>{if(token===generation)video.hidden=true;}).finally(()=>{if(token===generation)videoStarting=false;});}
  }else if(!video.paused)video.pause();
  if(state.current){
   if(!music.paused&&!voice.paused&&Math.abs(voice.currentTime-music.currentTime)>0.08)voice.currentTime=music.currentTime;
   const time=music.currentTime+state.offset;const lines=lyrics;let index=-1;
   // Lines switch on their sung words (start), so a stamp never cuts off the previous line's last word.
   for(let i=0;i<lines.length&&startOf(i)<=time;i++)index=i;
   // The breather hides this panel: wait for layout before measuring a line.
   // Each song starts at the beginning, without inheriting the previous scroll.
   // A backing-vocal line "(…)" is sung over the lead line, so the lead stays lit and centered under it.
   const leadLine=lines[index]?.over??index,echoing=leadLine!==index;
   if(!waiting&&(!lyricsPositioned||index!==activeLine)){
    const firstPosition=!lyricsPositioned;
    activeLine=index;
    const container=$('lyrics');
    [...container.children].forEach((line,i)=>{line.classList.toggle('active',i===index||i===leadLine);line.classList.toggle('past',i<leadLine);});
    // Before the first line is sung, keep it centered, ready for the count-in.
    const line=container.children[leadLine>=0?leadLine:firstLine];
    if(state.lyric_mode!=='two')container.scrollTo({
     top:line?Math.max(0,line.offsetTop-container.clientHeight/2+line.clientHeight/2):0,
     behavior:firstPosition?'instant':'smooth'
    });
    lyricsPositioned=true;
   }
   if(state.lyric_mode==='two'&&!waiting)twoLines(index,time);
   // Karaoke count-in: lyrics stay hidden through the intro, then dots count down to the first line.
   const remaining=firstLine>=0?startOf(firstLine)-time:0;
   if(classic()&&document.body.classList.contains('intro'))$('prelude').textContent=clock(Math.max(0,remaining));
   // Title card over the intro, like a karaoke machine: it fades before the count-in dots,
   // and a song whose lyrics start almost at once skips it.
   const cardEnd=firstLine>=0?Math.min(12,time+remaining-COUNT_IN-0.5):8;
   document.body.classList.toggle('intro',!waiting&&cardEnd>=3&&time<cardEnd);
   $('lyrics').classList.toggle('waiting',remaining>COUNT_IN);
   // Two-line mode counts in again after an instrumental break.
   const resume=state.lyric_mode==='two'&&breakAfter(index)?index+1:firstLine,left=startOf(resume)-time;
   const dots=left>0&&left<=COUNT_IN?'●'.repeat(Math.min(3,Math.ceil(left))):'';
   const dotted=$('lyrics').children[resume];
   if(counting&&counting!==dotted)counting.dataset.dots='';
   if(dotted&&dotted.dataset.dots!==dots)dotted.dataset.dots=dots;
   counting=dotted;
   // KTV wipe: each character/word fills left to right over its sung interval.
   for(const n of echoing?[leadLine,index]:[index]){
    const units=lines[n]?.units,spans=$('lyrics').children[n]?.children;
    if(units&&spans)units.forEach(([,from,to],i)=>spans[i]?.style.setProperty('--p',Math.max(0,Math.min(1,(time-from)/Math.max(.05,to-from)))));
   }
   $('elapsed').textContent=clock(music.currentTime);$('progress').parentElement.style.setProperty('--p',Math.min(1,music.currentTime/(state.current.duration||1)));
   if(classic()&&state.current.music_end&&!music.paused&&music.currentTime>=state.current.music_end)finish();
   if(now-lastReport>2000&&!music.paused){lastReport=now;send({action:'progress',key:currentKey,position:music.currentTime});}
  }
 }
 requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
// karaoke-server: the progress bar only shows progress; songs can be paused or skipped, not moved within.
const GUIDE_BADGES={off:['–','Guide off'],latin:['Aa','Romanization'],jyutping:['粵','Jyutping'],hangul:['가','한글']};
let badgeTimer;
function showGuideBadge(mode){const [icon,label]=GUIDE_BADGES[mode];showBadge(icon,'Pronunciation',label);}
// Any adjustment from a remote flashes on screen, so the room sees what changed.
// The meter shows where the value sits in its range; centred ones (key, timing) fill out from the middle.
const pct=v=>`${Math.round(v*100)}%`,signed=(v,digits=0)=>`${v>0?'+':v<0?'−':''}${Math.abs(v).toFixed(digits)}`,BADGES={
 music:v=>['♫','Music',pct(v),[0,v]],vocal:v=>['🎤','Vocals',pct(v),[0,v]],
 speed:v=>['⏱','Speed',pct(v),[0,v-0.5]],
 key:v=>[v>0?'♯':v<0?'♭':'♮','Key',signed(v),[.5,.5+v/12]],
 offset:v=>['⇆','Lyric timing',`${signed(v,1)}s`,[.5,.5+Math.max(-.5,Math.min(.5,v/10))]],
 lyric_scale:v=>['Aa','Lyric size',pct(v),[0,(v-0.6)/1.9]],
 video_mode:v=>['▣','Video',{show:'Shown',blur:'Blurred',hide:'Hidden'}[v]],
 lyric_mode:v=>['≡','Lyrics',{scroll:'Scrolling',two:'Two lines',off:'Hidden'}[v]]};
let shown=null;
function showChanges(s){
 const now=Object.fromEntries(Object.keys(BADGES).map(k=>[k,s[k]]));
 // A new song resets speed, key and timing on its own; only announce changes made mid-song.
 if(shown&&shown.song===(s.current?.key||null))for(const k in BADGES)if(now[k]!==undefined&&now[k]!==shown[k]){showBadge(...BADGES[k](now[k]));break;}
 shown={...now,song:s.current?.key||null};
}
// Queue changes from any remote get the same card: what was added, removed, moved, or became ready.
let lastQueue=null;
function showQueueChange(s){
 const queue=s.upcoming.map(i=>({key:i.key,id:i.id,title:i.title,singer:i.singer,status:i.status})),before=lastQueue;lastQueue=queue;
 if(!before)return;
 const old=new Map(before.map(i=>[i.key,i])),keys=new Set(queue.map(i=>i.key)),stage=s.current?.key;
 const added=queue.find(i=>!old.has(i.key)),removed=before.find(i=>!keys.has(i.key)&&i.key!==stage);
 if(added)return showBadge('+',added.singer?`Queued by ${added.singer}`:'Queued',added.title,null,added.id);
 if(removed)return showBadge('✕','Removed',removed.title,null,removed.id);
 const failed=queue.find(i=>i.status==='error'&&old.get(i.key).status!=='error');
 if(failed)return showBadge('!','Couldn’t prepare',failed.title,null,failed.id);
 const ready=queue.find(i=>i.status==='ready'&&old.get(i.key).status!=='ready');
 if(ready)return showBadge('✓','Ready to sing',ready.title,null,ready.id);
 const order=before.filter(i=>keys.has(i.key)).map(i=>i.key),now=queue.map(i=>i.key);
 if(order.join()!==now.join()){
  // Name the song that moved furthest, with its new place in line.
  let moved=0,best=-1;now.forEach((k,i)=>{const d=Math.abs(order.indexOf(k)-i);if(d>best){best=d;moved=i;}});
  const up=order.indexOf(now[moved])>moved;
  showBadge(up?'↑':'↓',`Moved to #${moved+1}`,queue[moved].title);
 }
}
// Classic names each notice the way the machine does.
const KO_NAMES={Music:'반주',Vocals:'음성',Speed:'템포',Key:'키','Lyric timing':'싱크','Lyric size':'가사 크기',Video:'영상',Lyrics:'가사',Pronunciation:'가이드',Queued:'예약',Removed:'취소','Couldn’t prepare':'오류','Ready to sing':'준비 완료'};
// Springs in, pulses the icon on each further change while visible, then shrinks away.
function showBadge(icon,name,value,range,art){
 const osd=$('guide-badge'),meter=$('osd-meter'),visible=!osd.hidden&&!osd.classList.contains('out');
 // karaoke-server: song notices show the song's artwork (as on the phone), with the icon as a badge on its corner.
 const pic=$('osd-art');osd.classList.toggle('has-art',!!art);pic.hidden=!art;if(art&&pic.dataset.id!==art){pic.dataset.id=art;pic.src=thumbnail(art);}
 $('guide-icon').textContent=icon;$('osd-name').textContent=classic()?KO_NAMES[name]??(name.startsWith('Moved')?'순서':name):name;$('guide-label').textContent=value;
 meter.hidden=!range;
 if(range){const [a,b]=range;meter.style.setProperty('--from',Math.min(a,b));meter.style.setProperty('--to',Math.max(a,b));}
 clearTimeout(badgeTimer);osd.classList.remove('out');osd.hidden=false;
 const glyph=$('guide-icon');glyph.classList.remove('pop');void glyph.offsetWidth;glyph.classList.add('pop');
 if(!visible){osd.classList.remove('in');void osd.offsetWidth;osd.classList.add('in');}
 badgeTimer=setTimeout(()=>{osd.classList.add('out');osd.addEventListener('animationend',()=>{if(osd.classList.contains('out')){osd.hidden=true;osd.classList.remove('out','in');}},{once:true});},1600);
}
function showUpNext(){
 if(REDUCED.matches)return;
 const spring='cubic-bezier(.34,1.4,.64,1)';
 document.querySelector('.next-card').animate([{opacity:0,transform:'translateY(60px) scale(.9)'},{opacity:1,transform:'none'}],{duration:700,easing:spring,fill:'backwards'});
 document.querySelector('.next-art').animate([{opacity:0,transform:'scale(.6) rotate(-8deg)'},{opacity:1,transform:'none'}],{duration:700,delay:120,easing:spring,fill:'backwards'});
 [1,2].forEach(n=>$(`peek-${n}`).animate([{opacity:0,transform:'none'}],{duration:600,delay:200+n*100,easing:spring,fill:'backwards'}));  // ends at the CSS offset
}
// Cheers from the remotes float up the right edge, clear of the lyrics. Each tap sends a few;
// a flood of taps is capped so the screen never fills up.
function cheer(emoji){
 // karaoke-server: one emoji per tap (was a burst of 3), so the TV shows exactly as many as were sent;
 // at most 30 on screen at once.
 const count=Math.min(1,30-document.querySelectorAll('.cheer').length);
 for(let i=0;i<count;i++){
  const node=el('span',emoji,'cheer');node.style.left=`${78+Math.random()*16}vw`;document.body.append(node);
  const drift=(Math.random()-.5)*12,rise=45+Math.random()*25,spin=(Math.random()-.5)*40;
  node.animate([{transform:'translateY(0) scale(.4)',opacity:0},{transform:`translate(${drift*.3}vw,-8vh) scale(1.2) rotate(${spin*.3}deg)`,opacity:1,offset:.15},{transform:`translate(${drift}vw,-${rise}vh) scale(.9) rotate(${spin}deg)`,opacity:0}],
   {duration:REDUCED.matches?600:2600+Math.random()*900,delay:i*160,easing:'cubic-bezier(.2,.7,.3,1)',fill:'both'}).onfinish=()=>node.remove();
 }
}
// "G" on a keyboard or remote also flips the guide.
addEventListener('keydown',e=>{if(e.key==='g'||e.key==='G')send({action:'guide',value:'cycle'});});
// Songs download and separate one at a time; show the one in progress so a wait never looks like a hang.
// On an empty stage the song being prepared is the headline. During a song, preparation shows
// only as small status text in the "Up next" list, so it never covers the lyrics.
function showPrep(queued){
 const item=state.current?null:queued.find(i=>i.status!=='ready'&&i.status!=='queued')||queued.find(i=>i.status==='queued');
 $('idle-prep').hidden=!item;$('idle-hero').hidden=!!item;
 if(!item)return;
 // karaoke-server: one percentage instead of the step name (same figure as the phone's queue)
 const percent=prepPercent(item,serverSkew);
 $('idle-prep-step').textContent=`Getting ready · ${percent}%`;$('idle-prep-title').textContent=item.title;
 const bar=$('idle-prep-bar');bar.parentElement.classList.remove('busy');bar.parentElement.style.setProperty('--p',percent/100);
}
// karaoke-server: no "Open remote" on the TV; the remote is each person's phone.

// Hide the TV's controls and pointer when nobody is using the mouse or keyboard.
let stillTimer;
function wake(){document.body.classList.remove('still');clearTimeout(stillTimer);stillTimer=setTimeout(()=>document.body.classList.add('still'),3000);}
for(const type of ['pointermove','pointerdown','keydown'])addEventListener(type,wake,{passive:true});
wake();
function scaleUi(){document.body.style.setProperty('--ui',Math.max(1,Math.min(innerWidth/1920,innerHeight/1080)));}
addEventListener('resize',scaleUi);scaleUi();

// karaoke-server: fun extras from the phones. Shout-outs slide in bottom-left; photos pop up top-right as a
// polaroid. Both carry the sender's name, leave on their own, and any phone can hide them early.
const SHOUT_COLORS=['linear-gradient(120deg,#d7fc70,#f6e05e 45%,#ff7ab6)','linear-gradient(120deg,#7af0ff,#b39cff 50%,#ff7ab6)','linear-gradient(120deg,#ffd166,#ff9f68 50%,#d7fc70)'];
const PHOTO_PILE=6;  // photos on screen at once; the oldest drifts off
const funStack=el('div',undefined,'fun-stack');document.body.append(funStack);
function leave(node,after){setTimeout(()=>{node.classList.add('out');setTimeout(()=>node.remove(),600);},after);}
function showShout(e){
 const node=el('div',undefined,'shout');node.dataset.id=e.id;node.append(el('b',`${e.name} says`),el('span',e.text));
 node.style.setProperty('--tilt',`${(Math.random()*6-3).toFixed(1)}deg`);
 node.style.setProperty('--shout-bg',SHOUT_COLORS[Math.floor(Math.random()*SHOUT_COLORS.length)]);
 funStack.append(node);while(funStack.children.length>3)funStack.firstChild.remove();
 leave(node,REDUCED.matches?5000:6500);
}
function showPhoto(e){
 // Thrown onto the screen like a polaroid on a table: a random spot in the upper part (the lyrics below stay
 // readable), a random tilt, flying in from below with a spin. Up to PHOTO_PILE stay up at once.
 const card=el('figure',undefined,'polaroid');card.dataset.id=e.id;
 const image=el('img');image.alt='';image.src=`/api/photo/${encodeURIComponent(e.photo)}`;
 // Between songs the join QR code fills the middle, so photos land in the side areas instead.
 const idle=!state?.current,side=Math.random()<.5;
 card.style.left=`${(idle?(side?2+Math.random()*14:66+Math.random()*14):3+Math.random()*76).toFixed(1)}vw`;
 card.style.top=`${(idle?8+Math.random()*50:5+Math.random()*26).toFixed(1)}vh`;
 card.style.setProperty('--tilt',`${(Math.random()*30-15).toFixed(1)}deg`);
 card.style.setProperty('--from-x',`${((Math.random()<.5?-1:1)*(25+Math.random()*35)).toFixed(0)}vw`);
 card.style.setProperty('--spin',`${(Math.random()*120-60).toFixed(0)}deg`);
 // karaoke-server: written on the strip like a real polaroid: the name, then the date it was taken.
 const caption=el('figcaption',undefined,'polaroid-note');caption.append(el('span',e.name,'pn-name'));
 if(e.date){const day=new Date(`${e.date}T12:00:00`);if(!isNaN(day))caption.append(el('span',day.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}),'pn-date'));}
 card.append(image,caption);
 image.onload=()=>{
  document.body.append(card);
  const pile=[...document.querySelectorAll('.polaroid:not(.out)')];
  for(const old of pile.slice(0,Math.max(0,pile.length-PHOTO_PILE))){old.classList.add('out');setTimeout(()=>old.remove(),600);}
  leave(card,12000);  // long enough for a burst to pile up
 };
}
function hideFun(id){for(const node of document.querySelectorAll('.shout,.polaroid'))if(node.dataset.id===id){node.classList.add('out');setTimeout(()=>node.remove(),600);}}

// karaoke-server: long titles scroll: the now-playing title/artist at the top and the Up next list.
const TV_MARQUEES='#song-title,#song-artist,#on-deck .deck-text';
setInterval(()=>marquees(TV_MARQUEES),1500);addEventListener('resize',()=>marquees(TV_MARQUEES));

// karaoke-server: tick the preparation percentages once a second (Up next rows in place, and the idle headline).
setInterval(()=>{if(!state)return;
 for(const small of document.querySelectorAll('#on-deck .deck-pct')){const item=state.upcoming.find(i=>i.key===small.dataset.key);if(item)small.textContent=`${prepPercent(item,serverSkew)}%`;}
 showPrep(state.upcoming.filter(i=>i.status!=='error'));},1000);

// karaoke-server: every 30 s while a video plays, tell the server how the TV is coping, so lag can be traced to
// decoding (dropped frames), the network (waiting for data) or sync (jumps, drift). One log line on the server.
const tvStats={jumps:0,waits:0,maxDrift:0};
video.addEventListener('waiting',()=>{if(!video.paused)tvStats.waits++;});
let lastFrames={dropped:0,total:0};
setInterval(()=>{
 if(!state?.current||video.paused||video.hidden||!video.videoHeight)return;
 const q=video.getVideoPlaybackQuality?.()||{droppedVideoFrames:0,totalVideoFrames:0};
 const dropped=q.droppedVideoFrames-lastFrames.dropped,total=q.totalVideoFrames-lastFrames.total;lastFrames={dropped:q.droppedVideoFrames,total:q.totalVideoFrames};
 send({action:'tv_stats',value:{song:state.current.id,height:video.videoHeight,dropped,frames:total,waits:tvStats.waits,jumps:tvStats.jumps,max_drift:Math.round(tvStats.maxDrift*1000),agent:navigator.userAgent.slice(0,160)}});
 tvStats.jumps=0;tvStats.waits=0;tvStats.maxDrift=0;
},30000);

// karaoke-server: on the between-songs countdown screen, the big card is the song about to start; below it,
// "After that" lists the following songs (artwork, title · artist, singer), as many as fit above the bottom of the
// screen, then "+N more". It replaces the two "peek" cards that used to show songs 2 and 3 under the big card.
const AFTER_MAX=12;let afterShown='';
function showAfter(s){
 const after=s.upcoming.filter(i=>i.status!=='error');
 $('next-after').hidden=!after.length;
 const key=JSON.stringify([after.length,innerHeight,...after.slice(0,AFTER_MAX).map(i=>[i.key,i.title,i.artist,i.singer])]);
 if(key===afterShown)return;afterShown=key;
 $('next-list').replaceChildren(...after.slice(0,AFTER_MAX).map(i=>{
  const row=el('li'),art=el('img');art.alt='';art.src=thumbnail(i.id);
  const text=el('span',undefined,'nl-text');text.append(el('b',i.title),el('span',` · ${i.artist}`));
  row.append(art,text,...(i.singer?[el('em',i.singer,'nl-singer')]:[]));return row;}));
 fitAfter(after.length);
}
function fitAfter(total){
 const list=$('next-list');list.querySelector('li.more')?.remove();
 if($('up-next').hidden)return;  // measured once the countdown screen shows (the 1 s tick below)
 const rows=[...list.querySelectorAll('li:not(.more)')],limit=innerHeight*.94,rowHeight=rows[0]?.getBoundingClientRect().height||0;
 if(rowHeight)while(rows.length>1&&list.getBoundingClientRect().bottom+(rows.length<total?rowHeight:0)>limit)rows.pop().remove();
 const shown=list.querySelectorAll('li:not(.more)').length;
 if(total>shown)list.append(el('li',`+${total-shown} more`,'more'));
}
setInterval(()=>{if(state&&!$('up-next').hidden){afterShown='';showAfter(state);}},1000);
addEventListener('resize',()=>{afterShown='';});

// karaoke-server: how many phones have the remote open right now (a phone that closes its browser drops off until
// it's back). Hidden when there are none.
function showPhones(n){const pill=$('phones');pill.hidden=!n;if(n)pill.textContent=`👥 ${n}`;pill.title=`${n} phone${n===1?'':'s'} connected`;}
