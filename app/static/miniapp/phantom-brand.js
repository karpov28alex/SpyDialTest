const LOGO_B64_URL='/app/phantom-logo.b64?v=0.7.0';
let phantomLogoData='';

async function loadPhantomLogo(){
  if(phantomLogoData)return phantomLogoData;
  const response=await fetch(LOGO_B64_URL,{cache:'no-store'});
  if(!response.ok)throw new Error(`Logo HTTP ${response.status}`);
  phantomLogoData=`data:image/jpeg;base64,${(await response.text()).trim()}`;
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
      ['polygon(0 0,100% 0,100% 35%,0 50%)','-70px','-58px','-8deg','0ms'],
      ['polygon(0 43%,100% 29%,100% 67%,0 70%)','75px','-4px','7deg','130ms'],
      ['polygon(0 66%,100% 60%,100% 100%,0 100%)','-55px','72px','-6deg','260ms'],
      ['polygon(20% 24%,82% 26%,76% 76%,18% 73%)','0','-90px','10deg','390ms']
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
  new MutationObserver(refreshPhantomBrand).observe(document.querySelector('#app'),{childList:true,subtree:true});
}).catch(console.error);
