/* Seeded glandular-tissue illustration. All geometry is invented; no embedded bitmap. */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.querySelector('#tissue');
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
  const wash = node('radialGradient',{id:'wash',cx:'40%',cy:'30%',r:'80%'},defs);
  node('stop',{offset:'0','stop-color':'#e8b9cf'},wash);
  node('stop',{offset:'1','stop-color':'#ce8aad'},wash);
  const camera = node('g',{'data-camera':''},svg);
  node('rect',{x:-100,y:-100,width:1100,height:900,fill:'url(#wash)'},camera);
  const stroma=node('g',{'data-stroma':''},camera);
  // Fine curved collagen and sparse spindle-shaped nuclei in the spaces between glands.
  for(let i=0;i<850;i++) {
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
    const g=node('g',{id,'data-gland':'',transform:`translate(${fmt(cx)} ${fmt(cy)}) rotate(${fmt(rotation)})`},glands);
    centers.push({id,x:cx,y:cy});
    const phase=between(0,6.28),waves=random()>.6?3:2;
    function shape(a,r) {
      const warp=1+.065*Math.sin(waves*a+phase)+.035*Math.sin(5*a-phase);
      return [rx*r*Math.cos(a)*warp,ry*r*Math.sin(a)*warp];
    }
    const outer=Array.from({length:24},(_,i)=>shape(i*Math.PI/12,1));
    node('path',{d:loop(outer),fill:choose(['#c980a2','#c579a0','#d18aa9','#bc7096']),stroke:'#fff0ec','stroke-width':2.1},g);
    node('path',{d:loop(outer),fill:'none',stroke:'#a45185','stroke-width':.8},g);
    const count=Math.floor(between(25,34));
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
      if(random()>.4) node('ellipse',{cx:fmt(nx+.35),cy:fmt(ny-.6),rx:.65,ry:.9,fill:'#d899c2',opacity:'.6'},g);
      if(random()>.16) {
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
  window.tissueConcept={svg,camera,a,b,seed:149336};
})();
