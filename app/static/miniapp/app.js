const tg=window.Telegram?.WebApp;
const app=document.querySelector('#app');
let token=null,refreshTimer=null,renderSeq=0,settings=null;
let state={screen:'home',id:null};
let mediaPlaying=false,firstDialogRender=true;

const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=v=>v?new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(v)):'—';

class ApiError extends Error{
  constructor(message,status=0,detail=null){super(message);this.name='ApiError';this.status=status;this.detail=detail;this.code=detail?.code||null;}
}

function initTelegram(){
  try{
    tg?.ready();tg?.expand();tg?.requestFullscreen?.();tg?.disableVerticalSwipes?.();
    const top=tg?.contentSafeAreaInset?.top||0;
    document.documentElement.style.setProperty('--tg-content-safe-area-inset-top',`${top}px`);
    tg?.setHeaderColor?.('#07050d');tg?.setBackgroundColor?.('#07050d');
  }catch{}
}

async function fetchWithTimeout(url,opt={},ms=12000){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),ms);
  try{return await fetch(url,{...opt,signal:controller.signal});}
  catch(e){if(e?.name==='AbortError')throw new Error('Сервер не ответил вовремя. Закройте Mini App и откройте снова.');throw e;}
  finally{clearTimeout(timer);}
}

async function api(path,opt={}){
  const r=await fetchWithTimeout(path,{...opt,headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json',...(opt.headers||{})},cache:'no-store'});
  if(!r.ok){
    const body=await r.json().catch(()=>null);
    const detail=body?.detail;
    const message=body?.error?.message||(typeof detail==='string'?detail:detail?.message)||`HTTP ${r.status}`;
    throw new ApiError(message,r.status,typeof detail==='object'?detail:null);
  }
  return r.json();
}

async function sendExport(format){
  const r=await fetchWithTimeout(`/api/intelligence/export/${format}/send`,{
    method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},cache:'no-store'
  },60000);
  if(!r.ok){
    const body=await r.json().catch(()=>null),detail=body?.detail;
    throw new ApiError(typeof detail==='string'?detail:detail?.message||'Не удалось отправить архив',r.status,typeof detail==='object'?detail:null);
  }
  return r.json();
}

function stopRefresh(){clearTimeout(refreshTimer);refreshTimer=null;}
function scheduleRefresh(){stopRefresh();if(state.screen!=='dialogs'||document.hidden||mediaPlaying)return;refreshTimer=setTimeout(()=>render(true),5000);}
function route(screen,id=null,push=true){
  stopRefresh();state={screen,id};firstDialogRender=true;
  const u=new URL(location.href);u.searchParams.set('screen',screen);id?u.searchParams.set('id',id):u.searchParams.delete('id');
  history[push?'pushState':'replaceState'](state,'',u);render();
}
function top(title,sub=''){return `<header class="topbar"><button class="back" data-back aria-label="Назад">‹</button><div><div class="title">${esc(title)}</div><div class="sub">${esc(sub)}</div></div></header>`;}
function avatar(x,size=''){
  const fallback=esc((x.peer_name||x.peer_username||'?')[0].toUpperCase());
  return x.avatar?`<span class="avatar ${size}"><img src="${esc(x.avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove();this.parentElement.textContent='${fallback}'"></span>`:`<span class="avatar ${size}">${fallback}</span>`;
}

async function home(){
  const me=await api('/api/me');
  if(me.funnel?.enabled&&!me.funnel?.channel_verified){
    return `<main class="page"><section class="brand"><div class="logo">P</div><h1>Phantom</h1><p>Архив Telegram Business</p></section><section class="settings-card"><h3>📢 Подпишитесь на канал</h3><p>${esc(me.funnel.subscription_text||'Подписка на информационный канал обязательна.')}</p>${me.funnel.channel_url?`<a class="retry" href="${esc(me.funnel.channel_url)}" target="_blank" rel="noopener">Открыть канал</a>`:''}<button class="retry" data-retry>Проверить подписку</button></section></main>`;
  }
  const locked=!me.access?.active;
  return `<main class="page"><section class="brand"><div class="logo">P</div><h1>Phantom</h1><p>Архив Telegram Business</p></section>${locked?`<section class="settings-card"><h3>Доступ не активен</h3><p class="muted">Пригласите друга или оплатите доступ.</p><div class="theme-buttons"><button class="theme-btn active" data-copy="${esc(me.monetization.referral_link)}">Скопировать приглашение</button><button class="theme-btn" data-go="subscription">Оплатить</button></div></section>`:''}<section class="grid"><button class="navcard" data-go="dialogs"><span class="navicon">💬</span><span><h2>Диалоги</h2><p>Сообщения, изменения, удаления и медиа</p></span><span class="arrow">›</span></button><button class="navcard" data-go="stats"><span class="navicon">📊</span><span><h2>Статистика</h2><p>Активность, лидеры общения и персональные выводы</p></span><span class="arrow">›</span></button><button class="navcard" data-go="profile"><span class="navicon">👤</span><span><h2>Профиль</h2><p>Уведомления, защищённые медиа и оформление</p></span><span class="arrow">›</span></button></section><div class="status"><b>Безопасность</b> <span class="dot">● активна</span><br><small>Приватный архив и защищённое соединение</small></div></main>`;
}

async function dialogs(){
  const d=await api('/api/dialogs');const list=d.items||[];
  return top('Диалоги','Автоматическое обновление')+`<main class="page"><input class="search" id="search" placeholder="Поиск"><div class="list">${list.length?list.map(x=>`<button class="dialog" data-dialog="${x.id}">${avatar(x)}<span><div class="name">${esc(x.peer_name||x.peer_username||'Без имени')}</div><div class="preview">${x.last_message_deleted?'Удалено: ':x.last_message_edited?'Изменено: ':''}${esc(x.last_message_text||'Нет сообщений')}</div><div class="dialog-count">${x.message_count||0} сообщений</div></span><span class="time">${fmt(x.last_message_at)}</span></button>`).join(''):`<div class="empty">Диалогов пока нет</div>`}</div></main>`;
}

function versions(m){
  if(!m.edited_at||!m.versions?.length)return '';
  const snapshots=m.versions.filter(v=>v.version!==undefined);
  const all=[...snapshots,{version:'текущая',text:m.text,caption:m.caption,created_at:m.edited_at}];
  return `<details class="history"><summary>История изменений · ${snapshots.length}</summary>${all.map(v=>`<div class="version"><div class="version-label">${v.version==='текущая'?'Текущая версия':`Версия ${esc(v.version)}`} · ${fmt(v.created_at)}</div>${esc(v.text||v.caption||'[медиа]')}</div>`).join('')}</details>`;
}
function mediaView(item){
  if(!item.url)return `<div class="media-wait">${esc(item.type)} · ${esc(item.status)}</div>`;
  const u=esc(item.url);
  if(item.type==='photo'||item.type==='sticker')return `<img class="media-image" src="${u}" alt="${esc(item.type)}" loading="lazy">`;
  if(item.type==='video'||item.type==='animation'||item.type==='video_note')return `<video class="media-video" src="${u}" controls playsinline preload="metadata"></video>`;
  if(item.type==='voice'||item.type==='audio')return `<audio class="media-audio" src="${u}" controls preload="metadata"></audio>`;
  return `<a class="media-file" href="${u}" target="_blank" rel="noopener">Скачать ${esc(item.filename||item.type)}</a>`;
}
async function dialog(){
  const d=await api(`/api/dialogs/${state.id}`);
  const messages=[...(d.messages||[])].sort((a,b)=>new Date(a.sent_at)-new Date(b.sent_at)||a.id-b.id);
  return top(d.dialog.peer_name||'Диалог',d.dialog.peer_username?`@${d.dialog.peer_username}`:'История сообщений')+`<main class="page dialog-page"><div class="dialog-head">${avatar(d.dialog,'large')}<div><b>${esc(d.dialog.peer_name||d.dialog.peer_username||'Диалог')}</b><div class="muted">${esc(d.dialog.peer_username?`@${d.dialog.peer_username}`:'Архив')}</div></div></div><div class="messages">${messages.length?messages.map(m=>`<article class="msg ${esc(m.direction)} ${m.is_deleted?'deleted':''}"><div>${esc(m.text||m.caption||(!m.media?.length?'Сообщение':''))}</div>${m.media?.map(mediaView).join('')||''}<div class="meta">${fmt(m.sent_at)}${m.edited_at?' · изменено':''}${m.is_deleted?' · удалено':''}${m.media?.some(x=>x.is_protected)?' · защищено':''}</div>${versions(m)}</article>`).join(''):`<div class="empty">Сообщений пока нет</div>`}</div></main>`;
}

function statMetric(icon,label,value){return `<section class="settings-card"><div class="row"><span>${icon} ${esc(label)}</span><b>${esc(value)}</b></div></section>`;}
function leaderCard(title,item,suffix=''){
  if(!item)return `<div class="row"><span>${esc(title)}</span><b>Недостаточно данных</b></div>`;
  const user=item.username?` · @${esc(item.username)}`:'';
  return `<div class="row"><span>${esc(title)}<small class="muted"><br>${esc(item.name)}${user}</small></span><b>${esc(item.value)}${esc(suffix)}</b></div>`;
}
async function stats(){
  const days=Number(state.id||30);const d=await api(`/api/intelligence?days=${days}`),t=d.totals||{},leaders=d.leaders||{};
  const peak=[...(d.hours||[])].sort((a,b)=>b.messages-a.messages).slice(0,6);const activity=(d.activity||[]).slice(-14);
  return top('Статистика',`Phantom Intelligence · ${days} дней`)+`<main class="page"><section class="settings-card"><h3>Период</h3><div class="theme-buttons"><button class="theme-btn ${days===7?'active':''}" data-stats-days="7">7 дней</button><button class="theme-btn ${days===30?'active':''}" data-stats-days="30">30 дней</button><button class="theme-btn ${days===90?'active':''}" data-stats-days="90">90 дней</button></div></section><section class="grid">${statMetric('💬','Диалогов',t.dialogs||0)}${statMetric('✉️','Сообщений',t.messages||0)}${statMetric('📸','Медиа',t.media||0)}${statMetric('🗑','Удалено',t.deleted||0)}${statMetric('✏️','Изменено',t.edited||0)}${statMetric('👻','Скрытых медиа',t.protected||0)}</section><section class="settings-card"><h3>Лидеры общения</h3>${leaderCard('🏆 Больше всего сообщений',leaders.active)}${leaderCard('📸 Больше всего медиа',leaders.media)}${leaderCard('🗑 Больше всего удалений',leaders.deleted)}${leaderCard('👻 Больше всего скрытых медиа',leaders.protected)}${leaders.longest?leaderCard('🔥 Самый длинный диалог',leaders.longest,' сообщений'):''}</section><section class="settings-card"><h3>Активность по времени</h3>${peak.length?peak.map(x=>`<div class="row"><span>${String(x.hour).padStart(2,'0')}:00</span><b>${x.messages}</b></div>`).join(''):'<p class="muted">Недостаточно данных</p>'}</section><section class="settings-card"><h3>Последние дни</h3>${activity.length?activity.map(x=>`<div class="row"><span>${esc(x.date)}</span><b>${x.messages} · 🗑 ${x.deleted} · ✏️ ${x.edited}</b></div>`).join(''):'<p class="muted">Недостаточно данных</p>'}</section><section class="settings-card"><h3>Интересные факты</h3>${(d.insights||[]).map(x=>`<p>💜 ${esc(x)}</p>`).join('')}</section>${d.locked?`<section class="settings-card"><h3>Подробная аналитика закрыта</h3><p class="muted">Оплатите доступ, чтобы открыть имена лидеров и экспорт.</p><button class="retry" data-go="subscription">Оплатить</button></section>`:`<section class="settings-card"><h3>Экспорт архива</h3><p class="muted">Выбранный файл придёт документом в чат с ботом.</p><div class="theme-buttons"><button class="theme-btn" data-export="csv">CSV</button><button class="theme-btn" data-export="json">JSON</button><button class="theme-btn" data-export="html">HTML</button></div></section>`}</main>`;
}

function settingRow(key,title,description,checked,disabled=false){return `<div class="row setting-row ${disabled?'disabled':''}" data-setting-row="${key}"><div><b>${esc(title)}</b><div class="muted">${esc(description)}</div></div><label class="switch"><input type="checkbox" data-setting="${key}" ${checked?'checked':''} ${disabled?'disabled':''}><span class="slider"></span></label></div>`;}
async function profile(){
  const me=await api('/api/me');const s=await api('/api/settings');settings=s;applyTheme(s.theme,false);
  const notificationsOn=Boolean(s.notifications_enabled);
  const trial=me.monetization.show_trial_in_profile?`<div class="row"><span>Доступ</span><b>${me.access.active?'Активен':'Не активен'}</b></div>${me.access.ends_at?`<div class="row"><span>До</span><b>${fmt(me.access.ends_at)}</b></div>`:''}`:'';
  const tariffs=me.monetization.show_tariffs?`<section class="settings-card"><h3>Доступ</h3><p class="muted">Пригласите друга или используйте оплату.</p><button class="retry" data-copy="${esc(me.monetization.referral_link)}">Скопировать ссылку приглашения</button><button class="retry" data-go="subscription">Тарифы</button></section>`:'';
  return top('Профиль','Настройки сохраняются без перезагрузки')+`<main class="page"><section class="profile-card"><div class="profile-head"><span class="avatar">${esc((me.first_name||'P')[0])}</span><div><div class="profile-name">${esc([me.first_name,me.last_name].filter(Boolean).join(' ')||'Пользователь')}</div><div class="muted">${me.username?'@'+esc(me.username):'Telegram ID '+esc(me.telegram_id)}</div></div></div><div class="row"><span>Telegram Business</span><b>${me.business_connected?'Подключён':'Не подключён'}</b></div>${trial}</section>${tariffs}<section class="settings-card"><h3>Основные функции</h3>${settingRow('save_protected_media','Сохранять скрытые медиа','При ответе сохранить одноразовое медиа в архиве и диалоге',s.save_protected_media)}${settingRow('notifications_enabled','Получать уведомления','Главный выключатель сообщений от бота',notificationsOn)}<div class="settings-nested">${settingRow('notify_edits','Изменения сообщений','Показывать предыдущую и новую версии',s.notify_edits,!notificationsOn)}${settingRow('notify_deletions','Удалённые сообщения','Присылать сохранённую копию',s.notify_deletions,!notificationsOn)}${settingRow('notify_protected_media','Копия защищённого медиа','Отправлять сохранённый файл в бот',s.notify_protected_media,!notificationsOn)}</div></section><section class="settings-card"><h3>Приватность и оформление</h3>${settingRow('hide_preview','Скрывать текст уведомлений','Показывать только тип события и кнопку Mini App',s.hide_preview)}${settingRow('notify_emoji','Эмодзи в уведомлениях','Использовать визуальные маркеры событий',s.notify_emoji)}<div class="row"><div><b>Тема интерфейса</b><div class="muted">Выберите оформление</div></div><div class="theme-buttons"><button class="theme-btn ${s.theme==='dark'?'active':''}" data-theme="dark">Тёмная</button><button class="theme-btn ${s.theme==='light'?'active':''}" data-theme="light">Светлая</button></div></div></section></main>`;
}
async function subscription(){
  const d=await api('/api/subscription');
  return top('Подписка','Продолжение доступа')+`<main class="page"><section class="settings-card"><h3>${d.access.active?'Доступ активен':'Выберите способ доступа'}</h3></section>${(d.plans||[]).map(p=>`<section class="settings-card"><h3>${esc(p.title)} · ${p.amount} ₽</h3><p>${esc(p.period)}</p><p class="muted">${esc(p.description)}</p><a class="retry" href="${esc(d.payment_url)}&plan=${esc(p.id)}" target="_blank" rel="noopener">Перейти к оплате</a></section>`).join('')}<section class="settings-card"><h3>Бесплатный доступ</h3><p class="muted">Пригласите друга по персональной ссылке.</p><button class="retry" data-copy="${esc(d.referral_link)}">Скопировать ссылку</button></section></main>`;
}

function applyTheme(t,notifyTelegram=true){
  const theme=t==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):t;
  document.documentElement.dataset.theme=theme;
  if(notifyTelegram){try{tg?.setHeaderColor?.(theme==='light'?'#f7f5fa':'#07050d');tg?.setBackgroundColor?.(theme==='light'?'#f7f5fa':'#07050d');}catch{}}
}
function syncNotificationDependants(enabled){for(const key of ['notify_edits','notify_deletions','notify_protected_media']){const input=document.querySelector(`[data-setting="${key}"]`);const row=document.querySelector(`[data-setting-row="${key}"]`);if(input)input.disabled=!enabled;row?.classList.toggle('disabled',!enabled);}}
function bindMediaState(){document.querySelectorAll('audio,video').forEach(el=>{el.addEventListener('play',()=>{mediaPlaying=true;stopRefresh();});const done=()=>{mediaPlaying=false;scheduleRefresh();};el.addEventListener('pause',done);el.addEventListener('ended',done);});}

function accessErrorHtml(error){
  const detail=error?.detail||{};
  if(error?.code==='CHANNEL_SUBSCRIPTION_REQUIRED'){
    return `${state.screen!=='home'?top('Подписка обязательна'):''}<main class="page"><section class="settings-card"><h3>📢 Подпишитесь на канал</h3><p>${esc(error.message)}</p>${detail.channel_url?`<a class="retry" href="${esc(detail.channel_url)}" target="_blank" rel="noopener">Открыть канал</a>`:''}<button class="retry" data-retry>Проверить подписку</button><button class="retry" data-go="home">На главную</button></section></main>`;
  }
  if(error?.code==='PAYMENT_REQUIRED'){
    return `${state.screen!=='home'?top('Доступ завершён'):''}<main class="page"><section class="settings-card"><h3>🔒 Требуется доступ</h3><p>${esc(error.message)}</p>${detail.payment_url?`<a class="retry" href="${esc(detail.payment_url)}" target="_blank" rel="noopener">${esc(detail.payment_button_text||'Оплатить')}</a>`:''}<button class="retry" data-go="home">На главную</button></section></main>`;
  }
  return `${state.screen!=='home'?top('Ошибка загрузки'):''}<main class="page"><div class="error">${esc(error?.message||'Не удалось загрузить данные')}<br><button class="retry" data-retry>Повторить</button><button class="retry" data-go="home">На главную</button></div></main>`;
}

async function render(silent=false){
  const seq=++renderSeq,oldScroll=window.scrollY;if(!silent)app.innerHTML='<div class="boot"><div class="spinner"></div></div>';
  try{
    const html=state.screen==='home'?await home():state.screen==='dialogs'?await dialogs():state.screen==='dialog'?await dialog():state.screen==='stats'?await stats():state.screen==='profile'?await profile():state.screen==='subscription'?await subscription():await home();
    if(seq!==renderSeq)return;app.innerHTML=html;tg?.BackButton?.[state.screen==='home'?'hide':'show']?.();bindMediaState();
    if(state.screen==='dialog'&&firstDialogRender){firstDialogRender=false;requestAnimationFrame(()=>window.scrollTo({top:document.documentElement.scrollHeight,behavior:'auto'}));}
    else if(silent)requestAnimationFrame(()=>window.scrollTo(0,oldScroll));
    scheduleRefresh();
  }catch(e){if(seq!==renderSeq)return;app.innerHTML=accessErrorHtml(e);}
}
function back(){if(state.screen==='dialog')route('dialogs');else if(state.screen!=='home')route('home');else tg?.close?.();}

app.addEventListener('click',async e=>{
  const copy=e.target.closest('[data-copy]');if(copy){try{await navigator.clipboard.writeText(copy.dataset.copy);tg?.showAlert?.('Ссылка скопирована');}catch{tg?.showAlert?.(copy.dataset.copy);}return;}
  const go=e.target.closest('[data-go]');if(go)return route(go.dataset.go);
  const d=e.target.closest('[data-dialog]');if(d)return route('dialog',d.dataset.dialog);
  if(e.target.closest('[data-back]'))return back();
  if(e.target.closest('[data-retry]'))return render();
  const sd=e.target.closest('[data-stats-days]');if(sd)return route('stats',sd.dataset.statsDays);
  const ex=e.target.closest('[data-export]');if(ex){ex.disabled=true;const old=ex.textContent;ex.textContent='Отправляем…';try{await sendExport(ex.dataset.export);tg?.showAlert?.('Архив отправлен документом в чат с ботом.');}catch(err){tg?.showAlert?.(err.message||'Не удалось отправить архив');}finally{ex.disabled=false;ex.textContent=old;}return;}
  const th=e.target.closest('[data-theme]');if(th){document.querySelectorAll('[data-theme]').forEach(x=>x.classList.remove('active'));th.classList.add('active');const previous=settings?.theme;applyTheme(th.dataset.theme);try{const r=await api('/api/settings',{method:'PATCH',body:JSON.stringify({theme:th.dataset.theme})});settings=r.settings;}catch(err){applyTheme(previous);tg?.showAlert?.(err.message||'Не удалось сохранить тему');}}
});
app.addEventListener('change',async e=>{
  const key=e.target.dataset.setting;if(!key)return;const input=e.target,previous=!input.checked;input.disabled=true;if(key==='notifications_enabled')syncNotificationDependants(input.checked);
  try{const result=await api('/api/settings',{method:'PATCH',body:JSON.stringify({[key]:input.checked})});settings=result.settings;input.checked=Boolean(settings[key]);if(key==='notifications_enabled')syncNotificationDependants(Boolean(settings.notifications_enabled));}
  catch(err){input.checked=previous;if(key==='notifications_enabled')syncNotificationDependants(previous);tg?.showAlert?.(err.message||'Не удалось сохранить настройку');}
  finally{input.disabled=false;}
});
app.addEventListener('input',e=>{if(e.target.id==='search'){const q=e.target.value.toLowerCase();document.querySelectorAll('.dialog').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(q));}});
window.addEventListener('popstate',e=>{stopRefresh();state=e.state||{screen:'home',id:null};firstDialogRender=true;render();});
document.addEventListener('visibilitychange',()=>document.hidden?stopRefresh():scheduleRefresh());
tg?.BackButton?.onClick(back);

(async()=>{
  initTelegram();
  if(!tg?.initData){app.innerHTML='<main class="page"><div class="error">Telegram не передал данные авторизации. Откройте приложение кнопкой внутри бота.</div></main>';return;}
  try{
    const r=await fetchWithTimeout('/api/auth/telegram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:tg.initData}),cache:'no-store'},12000);
    if(!r.ok){const body=await r.json().catch(()=>null);throw new Error(body?.detail||'Ошибка безопасного входа');}
    token=(await r.json()).access_token;
    const u=new URL(location.href),requested=u.searchParams.get('screen');
    state={screen:['home','dialogs','dialog','stats','profile','subscription'].includes(requested)?requested:'home',id:u.searchParams.get('id')};
    history.replaceState(state,'',u);await render();
  }catch(e){app.innerHTML=`<main class="page"><div class="error">${esc(e.message||'Не удалось войти')}<br><button class="retry" onclick="location.reload()">Повторить</button></div></main>`;}
})();
