const LOGO_B64_URL='/app/phantom-logo.b64?v=0.7.1';
let phantomLogoData='';

async function loadPhantomLogo(){
  if(phantomLogoData)return phantomLogoData;
  const response=await fetch(LOGO_B64_URL,{cache:'no-store'});
  if(!response.ok)throw new Error(`Logo HTTP ${response.status}`);
  const encoded=(await response.text()).replace(/\s+/g,'');
  if(!encoded.startsWith('/9j/')||encoded.length<1000)throw new Error('Invalid Phantom logo asset');
  phantomLogoData=`data:image/jpeg;base64,${encoded}`;
  await new Promise((resolve,reject)=>{
    const probe=new Image();
    probe.onload=resolve;
    probe.onerror=()=>reject(new Error('Phantom logo decode failed'));
    probe.src=phantomLogoData;
  });
  return phantomLogoData;
}

function logoImage(className='phantom-logo'){
  const img=document.createElement('img');
  img.className=className;
  img.src=phantomLogoData;
  img.alt='Phantom';
  img.decoding='async';
  img.draggable=false;
  return img;
}

function upgradeBrandLogo(){
  if(!phantomLogoData)return;
  document.querySelectorAll('.brand .logo').forEach(node=>{
    const img=logoImage();
    node.replaceWith(img);
  });
}

function upgradeBoot(){
  if(!phantomLogoData)return;
  document.querySelectorAll('.boot').forEach(boot=>{
    if(boot.querySelector('.phantom-assembly'))return;
    boot.querySelector('.logo')?.remove();
    const assembly=document.createElement('div');
    assembly.className='phantom-assembly';
    const pieces=[
      ['polygon(0 0,100% 0,100% 34%,0 48%)','-62px','-54px','-7deg','0ms'],
      ['polygon(0 35%,100% 25%,100% 61%,0 68%)','68px','-2px','6deg','120ms'],
      ['polygon(0 61%,100% 55%,100% 100%,0 100%)','-48px','66px','-5deg','240ms'],
      ['polygon(18% 23%,84% 24%,78% 77%,17% 74%)','0','-78px','8deg','360ms']
    ];
    for(const [clip,x,y,r,delay] of pieces){
      const img=logoImage('phantom-piece');
      img.style.setProperty('--clip',clip);
      img.style.setProperty('--x',x);
      img.style.setProperty('--y',y);
      img.style.setProperty('--r',r);
      img.style.setProperty('--delay',delay);
      assembly.appendChild(img);
    }
    boot.prepend(assembly);
  });
}

function refreshPhantomBrand(){upgradeBoot();upgradeBrandLogo()}

loadPhantomLogo().then(()=>{
  refreshPhantomBrand();
  const root=document.querySelector('#app');
  if(root)new MutationObserver(refreshPhantomBrand).observe(root,{childList:true,subtree:true});
}).catch(error=>{
  console.error('Phantom brand error',error);
  document.querySelectorAll('.boot .logo,.brand .logo').forEach(node=>node.remove());
});
