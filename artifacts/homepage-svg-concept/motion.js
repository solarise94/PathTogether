(function () {
  'use strict';
  const {svg,a,b}=window.tissueConcept;
  const $=id=>document.getElementById(id);
  const all={x:450,y:340,w:900};
  const local=c=>({x:c.x,y:c.y,w:340});
  const detail=c=>({x:c.x+3,y:c.y-2,w:140});
  const scenes=[
    {camera:all,travel:0,hold:1900,where:'全景',title:'先看看整体组织',body:'辨认腺体分布与腺体之间的间质，选择第一处观察区域。'},
    {camera:local(a),travel:1600,hold:1000,where:'局部 A',title:'选定一处腺体',body:'把视野移到区域 A，观察腺体轮廓、腔隙与相邻组织的关系。'},
    {camera:detail(a),travel:1600,hold:1500,where:'细节 A',title:'沿着腔隙看细胞',body:'继续放大，查看细胞围绕腔隙的排列，以及细胞核的形状与方向。'},
    {camera:detail(a),travel:0,hold:3200,where:'标注 A',title:'留下第一处观察',body:'标注固定在组织坐标上。缩小或移动视野后，仍能回到同一个位置。',note:'a'},
    {camera:local(b),travel:2800,hold:1100,where:'局部 B',title:'换到下一处继续观察',body:'先拉远，再移动到区域 B。保留第一处标注，继续查看另一组腺体。',via:{x:(a.x+b.x)/2,y:(a.y+b.y)/2,w:740}},
    {camera:detail(b),travel:1700,hold:1500,where:'细节 B',title:'再看一处细节',body:'观察这处腺体的形态与周围间质，比较不同位置的组织结构。'},
    {camera:detail(b),travel:0,hold:3200,where:'标注 B',title:'记录第二处位置',body:'为区域 B 添加观察标注，形成可以回看的观察路径。',note:'b'},
    {camera:all,travel:2200,hold:2600,where:'回看',title:'把观察放回整体',body:'回到全景，两处标注仍留在原来的组织位置。下一轮从整体重新开始。'}
  ];
  const extra=document.createElement('li');extra.textContent='回看';$('steps').append(extra);
  const total=scenes.reduce((sum,s)=>sum+s.travel+s.hold,0);
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
      $('phase').textContent=scene.title;$('status').textContent=scene.body;$('location').textContent=scene.where;
      $('steps').querySelectorAll('li').forEach((li,i)=>{li.classList.toggle('active',i===index);li.classList.toggle('done',i<index);});
      $('count').textContent=`${String(index+1).padStart(2,'0')} / 08`;
      $('previous').disabled=index===0;$('next').disabled=index===scenes.length-1;
      $('note').hidden=!scene.note;
      if(scene.note){$('note-id').textContent=scene.note==='a'?'01':'02';$('note-title').textContent=scene.note==='a'?'区域 A · 环绕腔隙的细胞排列':'区域 B · 腺体与周围间质';$('note-body').textContent='观察位置已标记，可缩回全景再次定位。';}
    }
    const spent=scenes.slice(0,index).reduce((sum,s)=>sum+s.travel+s.hold,0)+elapsed;
    $('progress').style.width=Math.min(100,spent/total*100)+'%';
    $('toggle').textContent=paused?'继续':'暂停';
  }
  function tick(now) {
    raf=0;
    if(paused||!visible||document.hidden)return;
    if(last)elapsed+=Math.min(now-last,100);
    last=now;
    if(elapsed>=scenes[index].travel+scenes[index].hold){index=(index+1)%scenes.length;elapsed=0;}
    paint();raf=requestAnimationFrame(tick);
  }
  function schedule() {
    cancelAnimationFrame(raf);raf=0;last=0;
    if(!paused&&visible&&!document.hidden)raf=requestAnimationFrame(tick);
  }
  function jump(to) {paused=true;manual=true;index=to;elapsed=scenes[index].travel;paint();schedule();}
  $('previous').onclick=()=>jump(Math.max(0,index-1));
  $('next').onclick=()=>jump(Math.min(scenes.length-1,index+1));
  $('toggle').onclick=()=>{paused=!paused;manual=true;paint();schedule();};
  $('replay').onclick=()=>{index=0;elapsed=0;paused=motion.matches;manual=false;paint();schedule();};
  document.addEventListener('visibilitychange',schedule);
  if('IntersectionObserver' in window)new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;schedule();},{threshold:0}).observe(svg);
  motion.addEventListener('change',()=>{if(motion.matches){paused=true;index=scenes.length-1;elapsed=scenes[index].travel;}else if(!manual)paused=false;paint();schedule();});
  if(motion.matches){index=scenes.length-1;elapsed=scenes[index].travel;}
  paint();schedule();
})();
