/* Seeded glandular-tissue illustration. All geometry is invented; no embedded bitmap. */
window.HP_EntryTissue = function (svg, compact) {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  let seed = 149336;
  const random = () => { seed = (Math.imul(1664525, seed) + 1013904223) >>> 0; return seed / 4294967296; };
  const between = (a,b) => a + random() * (b-a);
  const choose = a => a[Math.floor(random()*a.length)];
  const fmt = x => Math.round(x*100)/100;
  function node(tag, attrs, parent) {
    const el = document.createElementNS(NS,tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v));
    if(parent) parent.append(el);
    return el;
  }
  function loop(points) {
    const n = points.length;
    const mid = (a,b) => `${fmt((a[0]+b[0])/2)} ${fmt((a[1]+b[1])/2)}`;
    let d = 'M'+mid(points[n-1],points[0]);
    for(let i=0;i<n;i++) d+='Q'+points[i].map(fmt).join(' ')+' '+mid(points[i],points[(i+1)%n]);
    return d+'Z';
  }
  const defs = node('defs',{},svg);
  const wash = node('radialGradient',{id:compact?'hero-wash':'entry-wash',cx:'40%',cy:'30%',r:'80%'},defs);
  node('stop',{offset:'0','stop-color':'#e8b9cf'},wash);
  node('stop',{offset:'1','stop-color':'#ce8aad'},wash);
  const camera = node('g',{'data-camera':''},svg);
  node('rect',{x:-100,y:-100,width:1100,height:900,fill:compact?'url(#hero-wash)':'url(#entry-wash)'},camera);
  const stroma=node('g',{'data-stroma':''},camera);
  // Fine curved collagen and sparse spindle-shaped nuclei in the spaces between glands.
  for(let i=0;i<(compact?180:850);i++) {
    const x=between(-30,930),y=between(-30,710),a=between(0,180);
    node('path',{d:`M${fmt(x)} ${fmt(y)}q${fmt(between(-8,8))} ${fmt(between(-8,8))} ${fmt(between(10,25))} ${fmt(between(-10,10))}`,fill:'none',stroke:choose(['#fae5e7','#bd789e','#e8b2c7']), 'stroke-width':fmt(between(.4,1.1)),opacity:'.65'},stroma);
    if(i%3===0) node('ellipse',{cx:fmt(x),cy:fmt(y),rx:fmt(between(1,1.8)),ry:fmt(between(2.4,4)),fill:choose(['#975184','#864171','#ac608d']),transform:`rotate(${fmt(a)} ${fmt(x)} ${fmt(y)})`,opacity:'.7'},stroma);
  }
  const glands=node('g',{'data-glands':''},camera);
  const centers=[];
  // Pack unequal gland footprints organically; cell noise follows each gland's wall.
  const footprints=[{id:'region-a',x:260,y:235,r:49},{id:'region-b',x:650,y:445,r:47}];
  for(let trial=0;trial<14000&&footprints.length<90;trial++) {
    const r=trial<2200?between(38,58):between(23,38);
    const x=between(-18,918),y=between(-18,698);
    if(footprints.every(c=>Math.hypot(x-c.x,y-c.y)>r+c.r+between(1,5)))footprints.push({id:'gland-'+footprints.length,x,y,r});
  }
  for(const footprint of footprints) {
    const cx=footprint.x,cy=footprint.y;
    const rx=footprint.r*between(.91,1.04),ry=footprint.r*between(.8,1.02),rotation=between(-85,85);
    const id=footprint.id;
    const g=node('g',{id:(compact?'hero-':'demo-')+id,'data-gland':'',transform:`translate(${fmt(cx)} ${fmt(cy)}) rotate(${fmt(rotation)})`},glands);
    centers.push({id,x:cx,y:cy});
    const phase=between(0,6.28),waves=random()>.6?3:2;
    function shape(a,r) {
      const warp=1+.065*Math.sin(waves*a+phase)+.035*Math.sin(5*a-phase);
      return [rx*r*Math.cos(a)*warp,ry*r*Math.sin(a)*warp];
    }
    const outer=Array.from({length:24},(_,i)=>shape(i*Math.PI/12,1));
    node('path',{d:loop(outer),fill:choose(['#c980a2','#c579a0','#d18aa9','#bc7096']),stroke:'#fff0ec','stroke-width':2.1},g);
    node('path',{d:loop(outer),fill:'none',stroke:'#a45185','stroke-width':.8},g);
    const count=Math.floor(compact?between(15,20):between(25,34));
    const angles=Array.from({length:count},(_,i)=>2*Math.PI*(i+between(-.16,.16))/count);
    const inner=between(.23,.35);
    // Epithelial cells form a radial wall, with pale apical vacuoles and darker basal nuclei.
    for(let i=0;i<count;i++) {
      const a=angles[i], b=i===count-1?angles[0]+Math.PI*2:angles[i+1], m=(a+b)/2;
      const o1=shape(a,.96),o2=shape(b,.96),i1=shape(a,inner),i2=shape(b,inner);
      const cytoplasm=`M${o1.map(fmt).join(' ')}Q${shape(m,1.02).map(fmt).join(' ')} ${o2.map(fmt).join(' ')}L${i2.map(fmt).join(' ')}Q${shape(m,inner*.86).map(fmt).join(' ')} ${i1.map(fmt).join(' ')}Z`;
      node('path',{d:cytoplasm,fill:choose(['#c47da3','#ce8eae','#b96f99','#cb84a7','#b36594']),stroke:'#eabdd0','stroke-width':.48},g);
      const [nx,ny]=shape(m+between(-.025,.025),between(.73,.83));
      const nuclearAngle=m*180/Math.PI-90+between(-17,17);
      const nucleus=node('ellipse',{cx:fmt(nx),cy:fmt(ny),rx:fmt(between(1.65,2.55)),ry:fmt(between(3.6,5.7)),fill:choose(['#6c326b','#803e78','#8e457f','#74336e']),stroke:'#ad6393','stroke-width':.35,transform:`rotate(${fmt(nuclearAngle)} ${fmt(nx)} ${fmt(ny)})`,'data-nucleus':''},g);
      if(!compact&&random()>.4) node('ellipse',{cx:fmt(nx+.35),cy:fmt(ny-.6),rx:.65,ry:.9,fill:'#d899c2',opacity:'.6'},g);
      if(!compact&&random()>.16) {
        const [vx,vy]=shape(m,between(.43,.55));
        node('ellipse',{cx:fmt(vx),cy:fmt(vy),rx:fmt(between(2.5,4.2)),ry:fmt(between(5.1,8.3)),fill:choose(['#f5e5e8','#f7ecec','#edd2df','#f0dce4']),stroke:'#e8b5cf','stroke-width':.55,transform:`rotate(${fmt(m*180/Math.PI-90+between(-10,10))} ${fmt(vx)} ${fmt(vy)})`},g);
      }
    }
    const lumen=Array.from({length:16},(_,i)=>shape(i*Math.PI/8,inner*between(.78,1.14)));
    node('path',{d:loop(lumen),fill:'#fff3ef',stroke:'#efd0dc','stroke-width':1},g);
  }
  // These are illustration coordinates, not patient observations or physical measurements.
  const a=centers.find(c=>c.id==='region-a'),b=centers.find(c=>c.id==='region-b');
  const annotations=node('g',{'data-annotations':''},camera);
  for(const [key,c] of [['a',a],['b',b]]) {
    const group=node('g',{'data-mark':key,visibility:'hidden'},annotations);
    node('rect',{x:fmt(c.x-49),y:fmt(c.y-48),width:98,height:96,rx:5,fill:'none',stroke:'#007AFF','stroke-width':2,'vector-effect':'non-scaling-stroke'},group);

  }
  return {svg,camera,a,b};
};

function initEntryTissue(root) {
  'use strict';
  const {svg,a,b}=window.HP_EntryTissue(root.querySelector('#tissue'),false);
  const $=id=>root.querySelector('#'+id);
  const t=key=>window.HP_I18N.t('entry.svg.'+key);
  const all={x:450,y:340,w:900};
  const local=c=>({x:c.x,y:c.y,w:340});
  const detail=c=>({x:c.x+3,y:c.y-2,w:140});
  const scenes=[
    {camera:all,travel:0,hold:1900,key:0},
    {camera:local(a),travel:1600,hold:1000,key:1},
    {camera:detail(a),travel:1600,hold:1500,key:2},
    {camera:detail(a),travel:0,hold:6200,key:3,note:'a'},
    {camera:local(b),travel:2800,hold:1100,key:4,via:{x:(a.x+b.x)/2,y:(a.y+b.y)/2,w:740}},
    {camera:detail(b),travel:1700,hold:1500,key:5},
    {camera:detail(b),travel:0,hold:6200,key:6,note:'b'},
    {camera:all,travel:2200,hold:4200,key:7}
  ];
  $('steps').replaceChildren(...scenes.map(()=>document.createElement('li')));

  function placeReview(frame,h) {
    const bounds=svg.getBoundingClientRect();
    const scale=Math.max(bounds.width/frame.w,bounds.height/h);
    for(const [key,point] of [['a',a],['b',b]]) {
      const pin=$('review-'+key);
      pin.hidden=index!==7;
      if(pin.hidden)continue;
      const x=bounds.width/2+(point.x-frame.x)*scale;
      const y=bounds.height/2+(point.y+48-frame.y)*scale+7;
      pin.style.left=Math.max(8,Math.min(bounds.width-pin.offsetWidth-8,x-pin.offsetWidth/2))+'px';
      pin.style.top=Math.max(8,Math.min(bounds.height-pin.offsetHeight-8,y))+'px';
    }
  }
  function duration(scene){return scene.travel+(scene.note?Math.max(scene.hold,350+60*(t('note.'+scene.note).length+t('observation.'+scene.note).length)+1600):scene.hold);}
  const total=()=>scenes.reduce((sum,s)=>sum+duration(s),0);
  let index=0,elapsed=0,last=0,raf=0,manual=false;
  const motion=matchMedia('(prefers-reduced-motion: reduce)');
  let paused=motion.matches,visible=true;
  const smooth=k=>k*k*(3-2*k);
  function mix(from,to,k) {
    k=smooth(Math.max(0,Math.min(1,k)));
    return {x:from.x+(to.x-from.x)*k,y:from.y+(to.y-from.y)*k,w:from.w*Math.pow(to.w/from.w,k)};
  }
  function paint() {
    const scene=scenes[index],from=index?scenes[index-1].camera:all;
    const fraction=scene.travel?Math.min(elapsed/scene.travel,1):1;
    let frame=scene.via&&fraction<.5?mix(from,scene.via,fraction*2):scene.via?mix(scene.via,scene.camera,(fraction-.5)*2):mix(from,scene.camera,fraction);
    const h=frame.w*680/900;
    svg.setAttribute('viewBox',`${frame.x-frame.w/2} ${frame.y-h/2} ${frame.w} ${h}`);
    svg.dataset.scene=String(index);
    svg.querySelector('[data-mark="a"]').setAttribute('visibility',index>=3?'visible':'hidden');
    svg.querySelector('[data-mark="b"]').setAttribute('visibility',index>=6?'visible':'hidden');
    // Only the camera changes each frame. Thousands of cell nodes stay untouched.
    if($('phase').dataset.scene!==String(index)) {
      $('phase').dataset.scene=String(index);
      $('phase').textContent=t('scene.'+index+'.title');$('status').textContent=t('scene.'+index+'.body');$('location').textContent=t('scene.'+index+'.where');
      $('steps').querySelectorAll('li').forEach((li,i)=>{li.textContent=t('scene.'+i+'.where');li.classList.toggle('active',i===index);li.classList.toggle('done',i<index);});
      $('count').textContent=`${String(index+1).padStart(2,'0')} / 08`;
      $('previous').disabled=index===0;$('next').disabled=index===scenes.length-1;
      $('note').hidden=!scene.note;
      if(scene.note){$('note-id').textContent=scene.note==='a'?'01':'02';$('note-title').textContent='';$('note-body').textContent='';}
    }
    if(scene.note) {
      const full=t('observation.'+scene.note);
      const title=t('note.'+scene.note);
      const count=motion.matches&&!manual?title.length+full.length:Math.max(0,Math.floor((elapsed-350)/60));
      $('note-title').textContent=title.slice(0,count);
      $('note-body').textContent=full.slice(0,Math.max(0,count-title.length));
      $('note-title').classList.toggle('streaming',count<title.length);
      $('note-body').classList.toggle('streaming',count>=title.length&&count<title.length+full.length);
    } else $('note-body').classList.remove('streaming');
    placeReview(frame,h);
    const spent=scenes.slice(0,index).reduce((sum,s)=>sum+duration(s),0)+elapsed;
    $('progress').style.width=Math.min(100,spent/total()*100)+'%';
    $('toggle').textContent=t(paused?'resume':'pause');
  }
  function tick(now) {
    raf=0;
    if(paused||!visible||document.hidden)return;
    if(last)elapsed+=Math.min(now-last,100);
    last=now;
    if(elapsed>=duration(scenes[index])){
      if(manual)paused=true;
      else {index=(index+1)%scenes.length;elapsed=0;}
    }
    paint();raf=requestAnimationFrame(tick);
  }
  function schedule() {
    cancelAnimationFrame(raf);raf=0;last=0;
    if(!paused&&visible&&!document.hidden)raf=requestAnimationFrame(tick);
  }
  function jump(to) {manual=true;index=to;elapsed=scenes[index].travel;paused=!scenes[index].note;paint();schedule();}
  $('previous').onclick=()=>jump(Math.max(0,index-1));
  $('next').onclick=()=>jump(Math.min(scenes.length-1,index+1));
  $('toggle').onclick=()=>{paused=!paused;if(!paused)manual=false;paint();schedule();};
  $('replay').onclick=()=>{index=0;elapsed=0;paused=motion.matches;manual=false;paint();schedule();};
  document.addEventListener('visibilitychange',schedule);
  document.addEventListener('hp-lang-change',()=>{$('phase').dataset.scene='';paint();});
  new ResizeObserver(()=>paint()).observe(svg);
  if('IntersectionObserver' in window)new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;schedule();},{threshold:0}).observe(svg);
  motion.addEventListener('change',()=>{if(motion.matches){paused=true;index=scenes.length-1;elapsed=scenes[index].travel;}else if(!manual)paused=false;paint();schedule();});
  if(motion.matches){index=scenes.length-1;elapsed=scenes[index].travel;}
  paint();schedule();
 }
(function boot(){
  if(document.documentElement.dataset.page!=='entry')return;
  const hero=document.querySelector('#hero-tissue');
  if(hero)window.HP_EntryTissue(hero,true);
  const root=document.querySelector('[data-hp-stage]');
  if(!root)return;
  if('IntersectionObserver' in window){
    const observer=new IntersectionObserver(entries=>{if(entries[0].isIntersecting){observer.disconnect();initEntryTissue(root);}}, {rootMargin:'160px'});
    observer.observe(root);
  }else initEntryTissue(root);
})();
