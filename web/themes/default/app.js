// ── Auth ──
// The login barrier answers every unauthorized request with 401. Instead of
// handling that in ~40 separate fetch() calls, fetch is wrapped once -
// so an expired session also reliably lands on the login page.
(function(){
  var _fetch=window.fetch.bind(window);
  window.fetch=function(){
    return _fetch.apply(null,arguments).then(function(r){
      if(r.status===401&&location.pathname!=='/login'){
        location.replace('/login?next='+encodeURIComponent(location.pathname+location.search));
      }
      return r;
    });
  };
})();

function logout(){
  fetch(_apiUrl('/api/logout'),{method:'POST'})
    .then(function(){location.replace('/login');})
    .catch(function(){location.replace('/login');});
}

// ── Estado ──
var S={nozzle_temp:0,nozzle_target:0,bed_temp:0,bed_target:0,
  print_state:'standby',filename:'',progress:0,print_duration:0,remain_time:0,
  curr_layer:0,total_layers:0,z_mm:0,printer_name:'Kobra X',firmware_version:'–',
  camera_url:'',fan_speed:0,print_speed_mode:2,light_on:false,light_brightness:80,
  ams_slots:[],filament_mode:'toolhead',ace_units:[],ace_dry_presets:null,ace_drying:{status:0,target_temp:0,duration:0,remain_time:0,humidity:null,current_temp:null,units:[]},web_upload_warning:1};
var tempHistory={n:[],b:[]};
var camOn=false;
var camUserStopped=false; // user stopped the camera manually - suppresses the auto-restart for this print
var _camPollInterval=null; // snapshot polling interval for Android (no MJPEG support)
var _lastLoadedFile=null;  // last loaded/printed file for the progress card actions (Issue #55)
var _idleCleared=false;    // user explicitly "cleared" the idle file -> do not reload s.filename (Issue #57)
var _fdDialogOpen=false;          // dialog is open right now
var _fdAutoOpenedFile=sessionStorage.getItem('fdAutoOpenedFile')||null;
var _fdUserCancelled=sessionStorage.getItem('fdUserCancelled')==='1';
var currentStep=1;
var currentPanel='dashboard';
var aceAutoRefillPrefs=(function(){
  try{return JSON.parse(localStorage.getItem('aceAutoRefillPrefs')||'{}')||{};}catch(_){return {};}
})();
var aceDryProfiles=(function(){
  try{return JSON.parse(localStorage.getItem('aceDryProfiles')||'{}')||{};}catch(_){return {};}
})();
var _aceDryDialogAceId=-1;
var _aceDryDialogPresetKey='';
var _aceDryDialogPresetOriginals={};
var ACE_DRY_PRESET_DEFAULTS={
  pla:{temp:45,duration_sec:4*3600},
  pla_plus:{temp:45,duration_sec:4*3600},
  petg:{temp:50,duration_sec:4*3600},
  tpu:{temp:55,duration_sec:4*3600},
  abs_asa:{temp:45,duration_sec:8*3600},
  pa_pc:{temp:55,duration_sec:12*3600}
};
var ACE_DRY_PRESETS={
  pla:{temp:45,duration_sec:4*3600},
  pla_plus:{temp:45,duration_sec:4*3600},
  petg:{temp:50,duration_sec:4*3600},
  tpu:{temp:55,duration_sec:4*3600},
  abs_asa:{temp:45,duration_sec:8*3600},
  pa_pc:{temp:55,duration_sec:12*3600},
  custom_1:{name:'Custom 1',temp:45,duration_sec:4*3600},
  custom_2:{name:'Custom 2',temp:45,duration_sec:4*3600},
  custom_3:{name:'Custom 3',temp:45,duration_sec:4*3600}
};

// Estado do Spoolman
var _spoolmanStatus={configured:false,reachable:false,server:'',sync_rate:0,slot_spools:{}};
var _spoolmanSpools=[];
var _slotSpoolMap={};  // {String(global_index): spoolman_spool_id} - last confirmed assignment

function _loadSpoolmanStatus(){
  fetch(_apiUrl('/kx/spoolman/status')).then(function(r){return r.json();}).then(function(d){
    _spoolmanStatus=d;
    _slotSpoolMap=d.slot_spools||{};
    _updateSpoolmanStatusDot();
    _buildSpoolmanSection();
    renderSpoolmanSlotCard();
    if(d.configured){
      fetch(_apiUrl('/kx/spoolman/spools')).then(function(r){return r.json();}).then(function(sd){
        _spoolmanSpools=sd.spools||[];
      });
    }
  }).catch(function(){});
}
function _updateSpoolmanStatusDot(){
  var dot=document.getElementById('spoolman-status-dot');
  var lbl=document.getElementById('spoolman-status-lbl');
  if(!dot||!lbl)return;
  if(!_spoolmanStatus.configured){
    dot.style.color='var(--txt2)';lbl.textContent=tr('spoolman_not_configured','not configured');
  } else if(_spoolmanStatus.reachable){
    dot.style.color='var(--ok)';lbl.textContent=_spoolmanStatus.server||tr('spoolman_connected','connected');
  } else {
    dot.style.color='var(--err)';lbl.textContent=(_spoolmanStatus.server||'')+tr('spoolman_unreachable_suffix',' (unreachable)');
  }
}

function _buildSpoolmanSection(){
  var sec=document.getElementById('fd-spoolman-section');
  var rows=document.getElementById('fd-spoolman-rows');
  var loading=document.getElementById('fd-spoolman-loading');
  if(!sec||!rows)return;
  if(!_spoolmanStatus.configured){sec.style.display='none';return;}
  sec.style.display='';
  rows.innerHTML='';
  if(loading)loading.style.display='';

  var usedSlots={};
  (_amsSlots||[]).forEach(function(slot){
    usedSlots[slot.slot_index]=slot;
  });

  fetch(_apiUrl('/kx/spoolman/spools')).then(function(r){return r.json();}).then(function(d){
    if(loading)loading.style.display='none';
    _spoolmanSpools=d.spools||[];
    var slotKeys=Object.keys(usedSlots).map(Number).sort(function(a,b){return a-b;});
    if(!slotKeys.length){rows.innerHTML='<span style="font-size:11px;color:var(--txt2)">–</span>';return;}
    rows.innerHTML=slotKeys.map(function(idx){
      var slot=usedSlots[idx];
      var col=(slot.color_hex||'#888');
      var currentSpool=_slotSpoolMap[String(idx)]||'';
      var opts='<option value="">–</option>'+_spoolmanSpools.map(function(sp){
        var rem=sp.remaining_weight!=null?' ('+sp.remaining_weight.toFixed(0)+'g)':'';
        var vendor=sp.filament&&sp.filament.vendor?sp.filament.vendor.name+' ':'';
        var name=sp.filament&&sp.filament.name?sp.filament.name:'Spool';
        return '<option value="'+sp.id+'"'+(sp.id==currentSpool?' selected':'')+'>'+
               escHtml('#'+sp.id+' '+vendor+name+rem)+'</option>';
      }).join('');
      return '<div style="display:flex;align-items:center;gap:8px;font-size:12px">'+
        '<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:'+col+';border:1px solid var(--border);flex-shrink:0"></span>'+
        '<span style="color:var(--txt2);min-width:46px">Slot '+(idx+1)+'</span>'+
        '<select data-spool-slot="'+idx+'" style="flex:1;padding:3px 6px;border-radius:6px;border:1px solid var(--border);background:var(--raised);color:var(--txt);font-size:12px">'+
        opts+'</select></div>';
    }).join('');
  }).catch(function(){if(loading)loading.style.display='none';});
}

function _aceAutoRefillGet(aceId){return !!aceAutoRefillPrefs[String(aceId)];}
function _aceAutoRefillSet(aceId,on){
  aceAutoRefillPrefs[String(aceId)]=!!on;
  localStorage.setItem('aceAutoRefillPrefs',JSON.stringify(aceAutoRefillPrefs));
}
function _aceDryProfileGet(aceId){
  var p=aceDryProfiles[String(aceId)]||{};
  var temp=parseInt(p.temp,10);
  var dur=parseInt(p.duration_sec,10);
  if(!Number.isFinite(temp))temp=45;
  if(!Number.isFinite(dur))dur=4*3600;
  temp=Math.max(30,Math.min(80,temp));
  dur=Math.max(10*60,Math.min(24*3600,dur));
  return {temp:temp,duration_sec:dur,preset:p.preset||''};
}
function _aceDryProfileSet(aceId,temp,durationSec,preset){
  aceDryProfiles[String(aceId)]={
    temp:Math.max(30,Math.min(80,parseInt(temp,10)||45)),
    duration_sec:Math.max(10*60,Math.min(24*3600,parseInt(durationSec,10)||4*3600)),
    preset:preset||''
  };
  localStorage.setItem('aceDryProfiles',JSON.stringify(aceDryProfiles));
}
function _aceDryDurationMinFromSec(sec){
  var minutes=Math.round((parseInt(sec,10)||0)/60);
  return Math.max(10,Math.min(1440,minutes));
}
function _syncAceDryPresetsFromServer(raw){
  if(!raw||typeof raw!=='object')return;
  Object.keys(ACE_DRY_PRESETS).forEach(function(k){
    var p=raw[k];
    if(!p||typeof p!=='object')return;
    var t=parseInt(p.temp,10);
    var d=parseInt(p.duration_sec,10);
    if(Number.isFinite(t))ACE_DRY_PRESETS[k].temp=Math.max(30,Math.min(80,t));
    if(Number.isFinite(d))ACE_DRY_PRESETS[k].duration_sec=Math.max(10*60,Math.min(24*3600,d));
    if(/^custom_[123]$/.test(k)&&typeof p.name==='string'){
      var n=p.name.trim();
      ACE_DRY_PRESETS[k].name=n||('Custom '+k.slice(-1));
    }
  });
}

// ── Display mode: Day (light), Night (dark) and Predawn (night, observatory
// red so it does not dazzle in the dark). Stored per browser.
var THEME_MODES=['light','dark','night'];
function setTheme(m){
  if(THEME_MODES.indexOf(m)<0)m='dark';
  document.documentElement.setAttribute('data-theme',m);
  try{localStorage.setItem('theme',m);}catch(e){}
  _syncThemeIcon();
  if(window.kxPreview)try{kxPreview.redraw();}catch(e){}
}
function toggleTheme(){
  var i=THEME_MODES.indexOf(document.documentElement.getAttribute('data-theme'));
  setTheme(THEME_MODES[(i+1)%THEME_MODES.length]);
}
function _syncThemeIcon(){
  var cur=document.documentElement.getAttribute('data-theme');
  document.querySelectorAll('[data-theme-mode]').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.themeMode===cur);});
}
(function(){try{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_syncThemeIcon);else _syncThemeIcon();})();

// ── i18n ──
var currentLang='de';
var T={};
var _langCache={};

function tr(key,fallback){
  var v=T&&T[key];
  return (typeof v==='string'&&v.length)?v:(fallback!==undefined?fallback:'');
}
// Backend messages come in English; the known ones are translated through the
// "srv:<message>" keys. "prefix: detail" messages translate only the prefix.
function srvMsg(m){
  if(typeof m!=='string'||!m)return m;
  var t=tr('srv:'+m,'');if(t)return t;
  var i=m.indexOf(': ');
  if(i>0){t=tr('srv:'+m.slice(0,i),'');if(t)return t+m.slice(i);}
  return m;
}

function _langToggleLabel(lang){
  if(lang==='de')return 'Deutsch';
  if(lang==='en')return 'English';
  if(lang==='fr')return 'Français';
  if(lang==='it')return 'Italiano';
  if(lang==='pt-br')return 'Português (BR)';
  if(lang==='zh-cn')return '简体中文';
  return 'Espanol';
}

function _mapSupportedLang(lang){
  if(!lang)return '';
  var l=String(lang).toLowerCase().replace(/_/g,'-').trim();
  if(l==='de'||l==='en'||l==='es'||l==='fr'||l==='it'||l==='pt-br'||l==='zh-cn')return l;

  var base=l.split('-')[0];
  if(base==='de'||base==='en'||base==='es'||base==='fr'||base==='it')return base;
  // pt-PT falls back to pt-BR - better than the German fallback.
  if(base==='pt')return 'pt-br';

  if(base==='zh'){
    if(l.indexOf('cn')>=0||l.indexOf('hans')>=0||l==='zh')return 'zh-cn';
  }
  return '';
}

function _normalizeLang(lang){
  return _mapSupportedLang(lang)||'de';
}

function _detectBrowserLanguage(){
  var prefs=[];
  if(Array.isArray(navigator.languages)&&navigator.languages.length)prefs=navigator.languages;
  else if(navigator.language)prefs=[navigator.language];
  for(var i=0;i<prefs.length;i++){
    var mapped=_mapSupportedLang(prefs[i]);
    if(mapped)return mapped;
  }
  return '';
}

function _resolveInitialLanguage(){
  var saved=localStorage.getItem('lang');
  var mappedSaved=_mapSupportedLang(saved);
  if(mappedSaved)return mappedSaved;
  return _detectBrowserLanguage()||'de';
}

async function _loadLanguage(lang){
  var l=_normalizeLang(lang);
  if(_langCache[l])return _langCache[l];
  var res=await fetch('/kx/ui/translations/'+l+'.json');
  if(!res.ok)throw new Error('failed to load translations: '+l);
  var data=await res.json();
  _langCache[l]=data||{};
  return _langCache[l];
}

async function setLanguage(lang){
  var l=_normalizeLang(lang);
  // Only pt-BR and English are kept complete; the other languages fill in what
  // is missing with English (before, they fell back to the German HTML text).
  var en={};
  try{ en=await _loadLanguage('en'); }catch(_){}
  try{
    T=Object.assign({},en,l==='en'?{}:await _loadLanguage(l));
  }catch(_){
    T=en; l='en';
  }
  currentLang=l;
  localStorage.setItem('lang',l);
  var sLangSel=document.getElementById('s-lang-select');
  if(sLangSel)sLangSel.value=l;
  document.documentElement.setAttribute('lang',l);
  applyLang();
}

// Multi-printer: BASE_URL from the pathname (/printer2 -> another bridge instance)
var _printers=[];
var _activePrinter=null;
(function(){
  var path=window.location.pathname.replace(/\/+$/,'');
  var m=path.match(/^\/printer(\d+)$/);
  var idx=m?parseInt(m[1]):1;
  window._printerIndex=idx;
})();
function _apiUrl(path){
  if(_activePrinter&&_activePrinter.bridge_url){
    return _activePrinter.bridge_url.replace(/\/+$/,'')+path;
  }
  return path;
}
function initPrinters(){
  fetch('/kx/printers').then(function(r){return r.json()}).then(function(d){  // always the local instance for the printer list
    _printers=d.result||[];
    var idx=window._printerIndex||1;
    _activePrinter=_printers.find(function(p){return String(p.id)===String(idx)})||_printers[0]||null;
    renderPrinterDropdown();
    if(!_printers.length)openSetup();else maybeShowOrcaGuide();
  }).catch(function(){});
}

// ── First run: no printer configured yet -> ask only for the IP and the language ──
function openSetup(){
  document.getElementById('setup-lang').value=currentLang||_resolveInitialLanguage();
  document.getElementById('setup-dialog').classList.add('open');
  setTimeout(function(){document.getElementById('setup-ip').focus();},50);
}
function confirmSetup(){
  var ip=document.getElementById('setup-ip').value.trim();
  var st=document.getElementById('setup-status'),btn=document.getElementById('setup-confirm');
  function say(key,fb,color){st.textContent=tr(key,fb);st.style.color=color||'var(--ink-2)';}
  if(!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)){say('setup_err_ip','Digite o IP no formato 192.168.1.100.','var(--err)');return;}
  btn.disabled=true;say('setup_wait','Procurando a impressora…');
  fetch('/kx/printers/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({printer_ip:ip})})
    .then(function(r){
      if(!r.ok)throw 0;
      say('setup_restart','Achei! Reiniciando o MoonKobra…','var(--ok)');
      // The bridge restarts to load the new printer: wait until it answers again
      var t0=Date.now();
      (function poll(){
        setTimeout(function(){
          fetch('/kx/printers',{cache:'no-store'}).then(function(r){return r.json();})
            .then(function(d){if((d.result||[]).length)location.href='/printer1';else throw 0;})
            .catch(function(){if(Date.now()-t0<90000)poll();else location.reload();});
        },1500);
      })();
    })
    .catch(function(){btn.disabled=false;say('setup_err','Não encontrei a impressora nesse IP. Confira o número e se o modo LAN está ligado.','var(--err)');});
}

// ── How to fill OrcaSlicer's "Physical Printer" window, until "don't show again" ──
function maybeShowOrcaGuide(force){
  var hidden=false;try{hidden=localStorage.getItem('orcaGuideHide')==='1';}catch(e){}
  if(hidden&&!force)return;
  fetch(_apiUrl('/api/settings')).then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(d){
    var rel=_apiUrl('');var base=/^https?:/.test(rel)?rel.replace(/\/+$/,''):location.origin;
    // OrcaSlicer may run on another PC: never hand it "localhost"
    if(d.lan_ip)base=base.replace(/\/\/(localhost|127\.0\.0\.1)(?=[:/]|$)/,'//'+d.lan_ip);
    document.getElementById('og-host').value=base;
    var key=d.auth_enabled?(d.auth_api_key||''):'';
    var ki=document.getElementById('og-key');
    ki.value=key;ki.placeholder=tr('og_key_missing','gere uma em Configurações → API');
    document.getElementById('og-key-row').style.display=d.auth_enabled?'':'none';
    document.getElementById('og-key-empty').style.display=d.auth_enabled?'none':'';
    document.getElementById('og-never').checked=hidden;
    document.getElementById('orca-guide').classList.add('open');
  }).catch(function(){});
}
function closeOrcaGuide(){
  try{localStorage.setItem('orcaGuideHide',document.getElementById('og-never').checked?'1':'0');}catch(e){}
  document.getElementById('orca-guide').classList.remove('open');
}

// ── Power button in the header (Issue: hard to find + barely visible on the
// Printers tab; mirrors togglePrinterPower()/_refreshPrinterPowerIcon() for
// the currently active printer, right next to the name/status in the header,
// so it works the same in single- and multi-printer setups - switching the
// active printer from the dropdown re-evaluates has_power_control for the
// nova ativa, em vez de um card fixo no dashboard preso a qualquer
// printer that happened to be active when the page loaded) ──
// Single button showing the action a click would take (not the current
// state): printer on → the button offers "Turn off", printer off → the button offers
// "Turn on". _headerPowerState keeps the last known real state ('on'/'off'/
// null=unknown) so toggleHeaderPower() knows which action to send.
var _headerPowerState=null;
function _initHeaderPower(){
  var btn=document.getElementById('h-power-btn');
  if(!btn)return;
  if(!_activePrinter||!_activePrinter.has_power_control){btn.style.display='none';return;}
  btn.style.display='';
  _refreshHeaderPower();
}
function _applyHeaderPowerState(state){
  _headerPowerState=state;
  var btn=document.getElementById('h-power-btn'), lbl=document.getElementById('h-power-lbl');
  if(!btn||!lbl)return;
  // Default: shows the action a click would take (printer on -> offers
  // "Turn off"). With power_status_inverted (Settings -> Power
  // switch), shows the current real state (printer on -> "On") -
  // some users expect the button to reflect reality, not the action.
  var inverted=!!(_activePrinter&&_activePrinter.power_status_inverted);
  var showOn=inverted?state==='on':state==='off';
  var showOff=inverted?state==='off':state==='on';
  if(showOff){
    btn.classList.remove('h-power-can-on');btn.classList.add('h-power-can-off');
    lbl.textContent=T.printers_power_off_short||'Off';
  }else if(showOn){
    btn.classList.remove('h-power-can-off');btn.classList.add('h-power-can-on');
    lbl.textContent=T.printers_power_on_short||'An';
  }else{
    btn.classList.remove('h-power-can-on','h-power-can-off');
    lbl.textContent=T.printers_power_on_short||'An';
  }
}
function _refreshHeaderPower(){
  if(!_activePrinter||!_activePrinter.has_power_control)return;
  var bridgeUrl=(_activePrinter.bridge_url||'').replace(/\/+$/,'');
  fetch(bridgeUrl+'/kx/printers/'+encodeURIComponent(_activePrinter.id)+'/power-status',{signal:AbortSignal.timeout(5000)})
    .then(function(r){return r.json()})
    .then(function(d){_applyHeaderPowerState(d.state==='on'||d.state==='off'?d.state:null);})
    .catch(function(){/* status endpoint is optional - the button just stays neutral */});
}
function toggleHeaderPower(){
  if(!_activePrinter)return;
  // Current state unknown: assume "turn on" - turning on an already-off switch
  // is harmless, whereas guessing "turn off" on a printer mid-print is not.
  var action=_headerPowerState==='on'?'off':'on';
  if(action==='off'&&!confirm(T.printers_power_off_confirm||'Turn off the printer? Make sure no print is in progress.'))return;
  var btn=document.getElementById('h-power-btn');
  if(btn)btn.style.opacity='0.5';
  post('/kx/printers/'+encodeURIComponent(_activePrinter.id)+'/power',{action:action}).then(function(){
    if(btn)btn.style.opacity='1';
    setTimeout(_refreshHeaderPower,1500);
  }).catch(function(e){
    if(btn)btn.style.opacity='1';
    clog('Erro de energia: '+e,'msg-err');
  });
}
function renderPrinterDropdown(){
  var wrap=document.getElementById('printer-dropdown-wrap');
  var single=document.getElementById('h-pname-single');
  var name=_printers.length===0?'–':(_activePrinter?(_activePrinter.name||'Kobra X'):'Kobra X');
  var pname=document.getElementById('h-pname');
  if(pname)pname.textContent=name;
  if(single)single.textContent=name;
  if(_printers.length>1){
    if(wrap)wrap.style.display='';
    if(single)single.style.display='none';
    var menu=document.getElementById('printer-dropdown-menu');
    if(menu){
      menu.innerHTML=_printers.map(function(p){
        var active=_activePrinter&&String(p.id)===String(_activePrinter.id);
        var num=p.id;
        return '<a href="'+((p.bridge_url||'').replace(/\/+$/,''))+'/printer'+num+'" style="display:block;padding:10px 14px;color:'+(active?'var(--accent)':'var(--txt)')+';text-decoration:none;font-size:13px;border-bottom:1px solid var(--border)" '+(active?'style="font-weight:600"':'')+'>'+
          (active?'<i data-icon="check" style="margin-right:6px"></i>':'')+escHtml(p.name)+'</a>';
      }).join('');
    }
  } else {
    if(wrap)wrap.style.display='none';
    if(single)single.style.display='';
  }
  _initHeaderPower();
}
function togglePrinterDropdown(){
  var menu=document.getElementById('printer-dropdown-menu');
  if(menu)menu.style.display=menu.style.display==='none'?'block':'none';
}
document.addEventListener('click',function(e){
  var wrap=document.getElementById('printer-dropdown-wrap');
  if(wrap&&!wrap.contains(e.target)){
    var menu=document.getElementById('printer-dropdown-menu');
    if(menu)menu.style.display='none';
  }
});
function applyLang(){
  ensureAceDryCards();
  applyDataT();
  // Navigation
  var nb=document.getElementById('nb-dashboard');if(nb)nb.querySelector('.nav-text').textContent=T.nav_dashboard;
  nb=document.getElementById('nb-console');if(nb)nb.querySelector('.nav-text').textContent=T.nav_console;
  nb=document.getElementById('nb-printers');if(nb)nb.querySelector('.nav-text').textContent=T.nav_printers;
  nb=document.getElementById('nb-store');if(nb)nb.querySelector('.nav-text').textContent=T.nav_browser;
  // Bottom navigation
  var bnb=document.getElementById('bnb-dashboard');if(bnb)bnb.lastChild.textContent=T.nav_dashboard;
  // the last child here is the count badge, not the label
  bnb=document.getElementById('bnb-console');if(bnb){var tn=Array.prototype.find.call(bnb.childNodes,function(n){return n.nodeType===3&&n.textContent.trim();});if(tn)tn.textContent=T.nav_console;}
  bnb=document.getElementById('bnb-printers');if(bnb)bnb.lastChild.textContent=T.nav_printers;
  bnb=document.getElementById('bnb-store');if(bnb)bnb.lastChild.textContent=T.nav_browser;
  // File browser panel
  setText('printers-panel-title',T.nav_printers);
  setText('add-printer-btn-label',T.add_printer);
  setText('apd-title',T.add_printer);
  setText('skip-title',T.skip_title);
  setText('skip-hint',T.skip_hint);
  setText('d-btn-skip-label',T.skip_btn_label);
  setText('fd-objects-toggle-lbl',T.fd_objects_toggle);
  setText('apd-lbl-ip',T.apd_lbl_ip);
  setText('apd-lbl-name',T.apd_lbl_name);
  var apn=document.getElementById('apd-name');if(apn)apn.setAttribute('placeholder',T.apd_placeholder_name);
  setText('apd-cancel',T.apd_cancel);
  setText('apd-confirm',T.apd_confirm);
  setText('fd-slots-hint',T.fd_slots_hint);
  setText('fd-cancel',T.fd_cancel);
  setText('fd-print',T.fd_print);
  setText('store-panel-title',T.panel_browser_title);
  var srb=document.getElementById('store-refresh-btn');if(srb)srb.textContent=T.store_refresh;
  var ssp=document.getElementById('store-search');if(ssp)ssp.setAttribute('placeholder',T.store_search_placeholder);
  setText('store-upload-label-prefix',T.store_upload_label_prefix);
  setText('store-upload-label-browse',T.store_upload_label_browse);
  setText('store-empty',T.store_empty);
  setText('sf-all',T.sf_all);setText('sf-ok',T.sf_ok);setText('sf-err',T.sf_err);setText('sf-new',T.sf_new);
  setText('ss-date',T.ss_date);setText('ss-name',T.ss_name);setText('ss-dur',T.ss_dur);
  setText('store-web-verify-title',T.store_web_verify_title);
  setText('store-web-verify-msg',T.store_web_verify_msg);
  setText('store-web-verify-confirm',T.store_web_verify_confirm);
  setText('store-web-verify-abort',T.store_web_verify_abort);
  setText('store-lbl-select-all',T.store_select_all||'Select All');
  setText('store-lbl-delete-selected',T.store_delete_selected||'Delete Selected');
  setText('store-lbl-exit-select',T.store_exit_select||'Cancel');
  setText('btab-lbl-uploaded',T.browser_tab_uploaded||'Uploaded');
  setText('btab-lbl-printer',T.browser_tab_printer||'On Printer');
  setText('printer-store-lbl-select-all',T.store_select_all||'Select All');
  setText('printer-store-lbl-delete-selected',T.store_delete_selected||'Delete Selected');
  setText('printer-store-lbl-exit-select',T.store_exit_select||'Cancel');
  // Dashboard card titles
  setText('d-card-temps',T.card_temps);
  setText('d-card-lightfan',T.card_light_fan);
  setText('d-card-speed',T.card_speed);
  setText('d-card-cam',T.card_cam);
  setText('d-card-ams',T.panel_ams_title);
  function _noColon(t){return String(t||'').replace(/\s*:\s*$/,'');}
  setText('d-lbl-elapsed',_noColon(T.lbl_elapsed));
  setText('d-lbl-remain',_noColon(T.lbl_remaining));
  setText('d-slicer-label',_noColon(T.lbl_slicer_time));
  setText('d-lbl-layers',_noColon(T.lbl_layers));
  setText('d-lbl-zpos',_noColon(T.lbl_zpos));
  setText('d-lbl-light',T.lbl_light);
  setText('d-lbl-nozzle',T.label_nozzle);
  setText('d-lbl-bed',T.label_bed);
  // Dashboard buttons - the Pause button becomes a toggle action; the Resume label
  // is set in updatePauseResumeBtn() according to the printer state.
  updatePauseResumeBtn();
  setText('cam-toggle-btn',camOn?T.btn_cam_stop:T.btn_cam_start);
  _camBtnIcon(camOn);
  setText('cam-placeholder-txt',T.cam_placeholder);
  // Temperature labels
  document.querySelectorAll('.lbl-set').forEach(e=>e.textContent=T.label_set);
  document.querySelectorAll('.lbl-off').forEach(e=>e.textContent=T.label_off);
  setText('d-chart-label',T.panel_temps_chart);
  // Axis labels
  setText('ptitle-motion-xy',T.panel_motion_xy);
  document.querySelectorAll('.lbl-home-z').forEach(e=>{e.title=T.btn_home_z;e.setAttribute('aria-label',T.btn_home_z);});
  document.querySelectorAll('.lbl-home-xy').forEach(e=>{e.title=T.btn_home_xy;e.setAttribute('aria-label',T.btn_home_xy);});
  document.querySelectorAll('.lbl-home-all').forEach(e=>e.textContent=T.btn_home_all);
  document.querySelectorAll('.lbl-disable-motors').forEach(e=>e.textContent=T.btn_disable_motors);
  document.querySelectorAll('.temp-input').forEach(e=>e.setAttribute('placeholder',T.label_target_c.replace(':','')));
  // Console
  setText('ptitle-console',T.panel_console_title);
  // Settings panel
  setText('modal-sec-connection',T.settings_connection);
  setText('modal-sec-print',T.settings_print);
  setText('modal-sec-version',T.settings_version);
  // Navigation + category labels (with fallback in case the i18n key is still missing)
  setText('nav-settings',T.nav_settings||'Settings');
  setText('setcat-lbl-connection',T.settings_connection||'Connection');
  setText('setcat-lbl-printer',T.settings_print||'Printer');
  setText('setcat-lbl-display',T.settings_cat_display||'Display');
  setText('setcat-lbl-display2',T.settings_cat_display||'Display');
  setText('setcat-lbl-filament',T.settings_cat_filament||'Filament');
  setText('setcat-lbl-integrations',T.settings_integrations||'Integrationen');
  setText('modal-sec-spoolman',T.modal_sec_spoolman||'Spoolman');
  setText('lbl-spoolman-url',T.lbl_spoolman_url||'Server-URL');
  setText('lbl-spoolman-sync-rate',T.lbl_spoolman_sync_rate||'Sync rate (s, 0=off)');
  setText('modal-sec-obico',T.modal_sec_obico||'Obico');
  setText('setcat-lbl-system',T.settings_version||'System');
  setText('lbl-set-lang',T.settings_cat_language||'Language');
  setText('lbl-set-theme',T.settings_cat_theme||'Hell / Dunkel umschalten');
  setText('lbl-poll-interval',T.settings_poll||'Poll-Intervall (Sekunden)');
  setText('lbl-verbose-http-log',T.settings_verbose_http_log||'Log every HTTP request (verbose)');
  ['job-log-keep','job-log-keep-hint','job-log-context','job-log-context-hint','log-buffer','log-buffer-hint'].forEach(function(k){
    var v=tr('settings_'+k.replace(/-/g,'_'));if(v)setText('lbl-'+k,v);
  });
  setText('lbl-filament-mapping',T.settings_filament_mapping||'Filament profile mapping (per slot)');
  setText('lbl-filament-mapping-save',T.settings_filament_mapping_save||'Save mapping');
  setText('lbl-visible-vendors',T.settings_visible_vendors||'Visible vendors (profile dropdown)');
  setText('visible-vendors-hint',T.settings_visible_vendors_hint||'Only these vendors appear in the slot profile dropdown. Nothing selected = show all. "Generic" and your own profiles are always visible.');
  setText('lbl-visible-vendors-save',T.settings_visible_vendors_save||'Save selection');
  // Custom profile import (Issue #41)
  setText('modal-sec-orca-profiles',T.orca_profile_section);
  setText('orca-profiles-hint',T.orca_profile_hint);
  setText('lbl-orca-profiles-import',T.orca_profile_import_btn);
  setText('lbl-slot-profile-import',T.orca_profile_import_link);
  setText('profile-import-title',T.orca_profile_import_title);
  setText('profile-import-dropmsg',T.orca_profile_dropmsg);
  setText('profile-import-list-label',T.orca_profile_list_label);
  // Help text with inline HTML - innerHTML instead of setText
  var helpEl=document.getElementById('profile-import-help');
  if(helpEl && T.orca_profile_help_html) helpEl.innerHTML=T.orca_profile_help_html;
  setText('btn-save-settings',T.settings_save);
  setText('lbl-printer-name',T.settings_printer_name);
  setText('lbl-printer-ip',T.settings_printer_ip);
  setText('lbl-mqtt-port',T.settings_mqtt_port);
  setText('lbl-username',T.settings_username);
  setText('lbl-password',T.settings_password);
  setText('lbl-device-id',T.settings_device_id);
  setText('lbl-mode-id',T.settings_mode_id);
  setText('modal-sec-power',T.settings_power||'Power Switch');
  setText('lbl-power-on-url',T.settings_power_on_url||'Power-On URL');
  setText('lbl-power-off-url',T.settings_power_off_url||'Power-Off URL');
  setText('lbl-power-status-url',T.settings_power_status_url||'Status URL');
  setText('lbl-power-hint',T.settings_power_hint||'Optional: plain HTTP GET URLs for a smart plug (e.g. Tasmota) controlling the printer\'s mains power. Leave empty to hide the power button.');
  setText('lbl-power-status-inverted',T.settings_power_status_inverted||'Show current status instead of action');
  setText('modal-sec-notify',tr('settings_notify_title'));
  setText('lbl-notify-url',tr('settings_notify_url'));
  setText('lbl-notify-hint',tr('settings_notify_hint'));
  setText('lbl-auth-title',tr('settings_auth_title'));
  setText('lbl-auth-hint',tr('settings_auth_hint'));
  setText('lbl-auth-enable',tr('settings_auth_enable'));
  setText('lbl-auth-user',tr('settings_auth_user'));
  setText('lbl-auth-password',tr('settings_auth_password'));
  setText('lbl-auth-api-key',tr('settings_auth_api_key'));
  setText('lbl-auth-generate',tr('settings_auth_generate'));
  setText('lbl-auth-save',tr('settings_auth_save'));
  // Now screen (mission, trajectory, Moko) and display modes
  ['lbl-lg-done','lbl-lg-now','lbl-lg-holo','lbl-traj','lbl-traj-prep','lbl-traj-heat','lbl-traj-level','lbl-traj-print','lbl-traj-done',
   'lbl-moko','lbl-k-target','d-btn-cancel-lbl','lbl-mode-light','lbl-mode-dark','lbl-mode-night','lbl-prev-progress','lbl-prev-full']
    .forEach(function(id){var t=tr(id.replace(/-/g,'_'));if(t)setText(id,t);});
  [['lbl-mode-light','lbl_mode_light'],['lbl-mode-dark','lbl_mode_dark'],['lbl-mode-night','lbl_mode_night'],
   ['lbl-k-nozzle','label_nozzle'],['lbl-k-bed','label_bed']].forEach(function(p){
    var t=tr(p[1]);if(t)document.querySelectorAll('.'+p[0]).forEach(function(e){e.textContent=t;});});
  for(var di=0;di<4;di++)setText('d-ace-dry-set-'+di,tr('ace_dry_set','Ajustar'));
  ['api-key-title','api-key-hint','api-save','api-guide-title','api-guide-hint','api-base','api-examples','api-endpoints',
   'api-ep-state','api-ep-query','api-ep-upload','api-ep-control','api-ep-files','api-ep-snapshot','api-ep-ws','api-more']
    .forEach(function(k){setText('lbl-'+k,tr('settings_'+k.replace(/-/g,'_')));});
  setText('lbl-default-slot',T.settings_default_slot);
  setText('opt-slot-auto',T.settings_slot_auto);
  setText('lbl-auto-leveling',T.settings_auto_leveling);
  setText('lbl-vibration-compensation',T.settings_vibration_compensation);
  setText('lbl-file-ready-mode',T.settings_file_ready_mode);
  setText('opt-file-ready-dialog',T.settings_file_ready_dialog);
  setText('opt-file-ready-banner',T.settings_file_ready_banner);
  setText('lbl-camera-on-print',T.settings_camera_on_print);
  setText('lbl-web-upload-warning',T.settings_web_upload_warning);
  setText('lbl-delete-printer-file-after-print',T.settings_delete_printer_file_after_print||'Delete file from printer after successful print');
  setText('lbl-delete-printer-file-after-print-hint',T.settings_delete_printer_file_after_print_hint||'Only applies to prints started through this bridge (files it uploaded itself) - prints started directly from the printer or Anycubic Slicer are never deleted, since no copy of those exists anywhere else.');
  setText('fd-options-title',T.fd_options_title);
  setText('fd-lbl-auto-leveling',T.print_auto_leveling);

  // Progress card actions for loaded/idle file (Issue #55)
  setText('d-idle-print-lbl',T.progress_action_print||'Drucken');
  setText('d-idle-slots-lbl',T.progress_action_slots||'Slots zuordnen');
  setText('d-idle-clear-lbl',T.progress_action_clear||'Leeren');
  // Speed buttons
  setText('d-spd-lbl-1',T.speed_silent.replace(/^\S+\s/,''));
  setText('d-spd-lbl-2',T.speed_normal.replace(/^\S+\s/,''));
  setText('d-spd-lbl-3',T.speed_sport.replace(/^\S+\s/,''));
  // Carregar/descarregar do AMS
  document.querySelectorAll('.lbl-feed').forEach(e=>e.textContent=T.lbl_feed);
  document.querySelectorAll('.lbl-unload').forEach(e=>e.textContent=T.lbl_unload);
  for(var i=0;i<4;i++){
      setText('d-card-ace-dry-'+i,'ACE '+(i+1)+' - '+tr('ace_dry_dryer'));
      setText('d-ace-auto-refill-label-'+i,tr('ace_dry_auto_refill'));
      setText('d-ace-drying-enable-label-'+i,tr('ace_dry_enable'));
      setText('d-ace-dry-humidity-label-'+i,tr('ace_dry_humidity'));
      setText('d-ace-dry-current-temp-label-'+i,tr('ace_dry_current_temp'));
      setText('d-ace-dry-target-label-'+i,tr('ace_dry_temp_line'));
      setText('d-ace-dry-time-label-'+i,tr('ace_dry_time_line'));
      setText('d-ace-dry-chart-label-'+i,tr('ace_dry_chart'));
    var adTemp=document.getElementById('ace-dry-temp-'+i);if(adTemp)adTemp.setAttribute('placeholder',T.ace_dry_temp);
    var adDur=document.getElementById('ace-dry-duration-'+i);if(adDur)adDur.setAttribute('placeholder',T.ace_dry_duration);
  }
  setText('ace-dry-dialog-title',tr('ace_dry_dialog_title'));
  setText('ace-dry-dialog-temp-label',tr('ace_dry_dialog_temp'));
  setText('ace-dry-dialog-time-label',tr('ace_dry_dialog_time'));
  setText('ace-dry-dialog-custom-name-label',tr('ace_dry_dialog_custom_name'));
  setText('ace-dry-dialog-cancel',tr('ace_dry_dialog_cancel'));
  setText('ace-dry-dialog-confirm',tr('ace_dry_dialog_confirm'));
  setText('ace-dry-dialog-reset-default',tr('ace_dry_dialog_reset_default'));
  setText('ace-dry-dialog-save-preset',tr('ace_dry_dialog_save_restart'));
  aceDryDialogSyncCustomButtonNames();
  // conn-btn text (only when not in a transition state)
  updateConnBtn();
  // Slot edit dialog
  setText('lbl-slot-color',T.slot_edit_color);
  setText('lbl-slot-material',T.slot_edit_material);
  setText('lbl-slot-profile',T.slot_edit_profile);
  setText('slot-profile-hint',T.slot_edit_profile_hint);
  var defOpt=document.getElementById('slot-profile-default-opt');
  if(defOpt) defOpt.textContent=T.slot_edit_profile_default;
  setText('btn-slot-edit-save',T.slot_edit_save);
  updateSlotEditFeedButton();
  var mi=document.getElementById('slot-edit-mat');if(mi)mi.setAttribute('placeholder',T.slot_edit_custom);
  setText('logdir-all',T.log_dir_all);
  setText('lbl-cam-urls-title',tr('cam_urls_title'));
  setText('lbl-cam-urls-hint',tr('cam_urls_hint'));
  setText('lbl-cam-url-stream',tr('cam_url_stream'));
  setText('lbl-cam-url-snapshot',tr('cam_url_snapshot'));
  setText('lbl-cam-token-new',tr('cam_token_new'));
  setText('btn-log-clear',tr('log_clear'));
  setText('btn-autoscroll',tr('log_auto'));
  setText('btn-log-dl',tr('log_download'));
  setText('ltab-lbl-live',tr('ltab_live'));
  setText('ltab-lbl-history',tr('ltab_history'));
  setText('hist-hint',tr('hist_hint'));
  setText('loglvl-all',T.log_dir_all);
  setText('log-lbl-level',T.log_lvl_label);
  setText('file-ready-btn',T.file_ready_btn);
  setText('file-slots-btn',T.file_slots_btn);
  setText('file-cancel-btn',T.file_cancel_btn);
  // GCode browser cards: the texts are embedded via innerHTML,
  // switching language re-renders everything.
  if(typeof renderStore==='function' && typeof storeFiles!=='undefined'){
    try{ renderStore(); }catch(e){}
  }
}
function setText(id,txt){var el=document.getElementById(id);if(el)el.textContent=txt;}

function _slotSymbol(k){
  var p=['<circle cx="10" cy="10" r="7" stroke-dasharray="2 2.4"/>',
    '<circle cx="10" cy="10" r="7"/><path d="M10 3v14M3 10h14"/>',
    '<circle cx="10" cy="10" r="4.2"/><path d="M10 1.5v4M10 14.5v4M1.5 10h4M14.5 10h4"/>',
    '<ellipse cx="10" cy="10" rx="8" ry="4.2" transform="rotate(-25 10 10)"/>'][k%4];
  return '<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'+p+'</svg>';
}
function ensureAceDryCards(){
  var grid=document.getElementById('d-ace-dry-grid');
  if(!grid||grid.getAttribute('data-init')==='1')return;
  var html='';
  for(var i=0;i<4;i++){
    html+='<details class="dryer" id="d-ace-dry-card-'+i+'" style="display:none">'
      +'<summary class="dryer-head"><span class="plate" id="d-card-ace-dry-'+i+'">ACE '+(i+1)+' - Dryer</span>'
        +'<span class="dryer-sum"><span id="d-ace-dry-humidity-label-'+i+'">Humidity</span> <span class="num" id="d-ace-dry-humidity-'+i+'">-</span>'
        +'<span class="dryer-state" id="d-ace-dry-state-'+i+'"></span></span><i class="chev" data-icon="chevron-right"></i></summary>'
      +'<div class="dryer-body">'
      +'<div class="dryer-reads">'
        +'<span><span class="plate" id="d-ace-dry-current-temp-label-'+i+'">Temperature:</span><span class="num" id="d-ace-dry-current-temp-'+i+'">-</span></span>'
        +'<span><span class="plate" id="d-ace-dry-target-label-'+i+'">Drying Temperature:</span><span class="num" id="d-ace-dry-target-'+i+'">-</span></span>'
        +'<span><span class="plate" id="d-ace-dry-time-label-'+i+'">Drying Time:</span><span class="num" id="d-ace-dry-time-'+i+'">-</span></span>'
      +'</div>'
      +'<div class="dryer-toggles">'
        +'<label class="switch"><input type="checkbox" id="ace-dry-enable-toggle-'+i+'" onchange="aceDryToggle('+i+',this.checked)"><span id="d-ace-drying-enable-label-'+i+'">Enable Drying</span></label>'
        +'<label class="switch"><input type="checkbox" id="ace-auto-refill-toggle-'+i+'" onchange="aceAutoRefillToggle('+i+')"><span id="d-ace-auto-refill-label-'+i+'">Auto Refill</span></label>'
        +'<button class="btn btn-sm" onclick="openAceDryDialog('+i+')" data-icon="heater"><span id="d-ace-dry-set-'+i+'">'+escHtml(tr('ace_dry_set','Ajustar'))+'</span></button>'
      +'</div>'
      +'</div>'
      +'</details>';
  }
  grid.innerHTML=html;
  grid.setAttribute('data-init','1');
}
(function(){
  var l=_resolveInitialLanguage();
  currentLang=_normalizeLang(l);
  document.documentElement.setAttribute('lang',currentLang);
  // defer until the DOM is ready
  window.addEventListener('DOMContentLoaded',function(){
    setLanguage(currentLang).catch(function(){});
    _loadSpoolmanStatus();
    // No printer configured? -> go straight to the Printers tab (shows "+ Add printer")
    fetch('/kx/printers').then(function(r){return r.json()}).then(function(d){
      if(!d.result||!d.result.length){showPanel('printers');loadPrinterTab();}
    }).catch(function(){});
  });
})();

// ── Panel navigation ──
function showPanel(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  document.querySelectorAll('.nav-btn,.bnav-btn').forEach(b=>b.classList.remove('active'));
  var nb=document.getElementById('nb-'+id);if(nb)nb.classList.add('active');
  var bnb=document.getElementById('bnb-'+id);if(bnb)bnb.classList.add('active');
  currentPanel=id;
  if(id==='settings')openSettings();
}

// Console: live sub-tab vs. print history (per-print log).
function showLogTab(name){
  ['live','history'].forEach(function(n){
    var g=document.getElementById('log-group-'+n);if(g)g.style.display=n===name?'':'none';
    var t=document.getElementById('ltab-'+n);if(t)t.classList.toggle('active',n===name);
  });
  var acts=document.querySelector('#panel-console .page-head .actions');
  if(acts)acts.style.visibility=name==='live'?'':'hidden';
  if(name==='history')loadHistory();
}
function loadHistory(){
  var el=document.getElementById('hist-list');if(!el)return;
  fetch(_apiUrl('/kx/history?limit=100')).then(function(r){return r.json();}).then(function(d){
    var jobs=d.result||[];
    if(!jobs.length){el.innerHTML='<div class="empty-state">'+escHtml(tr('hist_empty')||'No prints logged yet.')+'</div>';return;}
    el.innerHTML=jobs.map(function(j){
      // started_at vem em UTC ("...Z") -> hora local
      var when=j.started_at?new Date(j.started_at).toLocaleString([], {dateStyle:'short',timeStyle:'short'}):'–';
      var st=j.status||'';
      var stLbl=tr('hist_status_'+st)||st;
      var dur=j.duration_sec?fmtTime(j.duration_sec):'–';
      var url=_apiUrl('/kx/history/'+encodeURIComponent(j.id)+'/log');
      var acts=j.has_log
        ?'<a class="btn btn-sm btn-ghost" data-icon="eye" target="_blank" rel="noopener" href="'+escHtml(url)+'">'+escHtml(tr('hist_open')||'Abrir')+'</a>'
         +'<a class="btn btn-sm btn-ghost" data-icon="download" href="'+escHtml(url+'?download=1')+'">'+escHtml(tr('hist_download')||'Baixar')+'</a>'
        :'<span class="muted">'+escHtml(tr('hist_no_log')||'no log')+'</span>';
      return '<div class="hist-row"><div class="hist-main"><div class="hist-name" title="'+escHtml(j.filename||'')+'">'+escHtml(j.filename||'?')+'</div>'
        +'<div class="hist-meta"><span>'+escHtml(when)+'</span><span class="hist-st '+escHtml(st)+'">'+escHtml(stLbl)+'</span><span>'+escHtml(dur)+'</span></div></div>'
        +'<div class="hist-acts">'+acts+'</div></div>';
    }).join('');
  }).catch(function(e){el.innerHTML='<div class="empty-state">'+escHtml(String(e))+'</div>';});
}

// Toggles the settings category (master-detail)
function showSettingsCat(name){
  document.querySelectorAll('.set-group').forEach(g=>g.classList.remove('active'));
  document.querySelectorAll('.set-cat').forEach(b=>b.classList.remove('active'));
  var g=document.getElementById('setgrp-'+name);if(g)g.classList.add('active');
  var c=document.getElementById('setcat-'+name);if(c)c.classList.add('active');
}

// Toggles the browser sub-tab: uploaded files (bridge store) vs. files
// on the printer itself (internal storage, via listLocal MQTT).
var _printerFilesLoaded=false;
function showBrowserTab(name){
  document.querySelectorAll('.browser-group').forEach(g=>g.classList.remove('active'));
  document.querySelectorAll('.browser-tab').forEach(b=>b.classList.remove('active'));
  var g=document.getElementById('browser-group-'+name);if(g)g.classList.add('active');
  var t=document.getElementById('btab-'+name);if(t)t.classList.add('active');
  if(name==='printer'&&!_printerFilesLoaded)loadPrinterFiles();
}

// ── Log do console ──
var consoleLogs=[];
var logAutoScroll=true;
var logBadgeCount=0;
var logDirFilter='all';   // 'all'|'rx'|'tx'
var logLevelFilter='all'; // 'all'|'err'|'warn'
var logTopicFilter='';    // '' = no topic filter

function clog(msg,cls){
  cls=cls||'msg-info';
  var ts=new Date().toLocaleTimeString('de',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  _appendLog({ts:ts,lvl:'',name:'ui',msg:msg},cls);
}
function _lvlCls(lvl){
  if(lvl==='ERROR'||lvl==='CRITICAL')return'msg-err';
  if(lvl==='WARNING')return'msg-warn';
  return'msg-info';
}
function _appendLog(entry,forceCls){
  var cls=forceCls||_lvlCls(entry.lvl);
  var label=entry.name?'['+entry.name+'] ':'';
  var fullMsg=label+entry.msg;
  // Groups repeats into a counter (×N) instead of N identical lines.
  var last=consoleLogs[consoleLogs.length-1];
  if(last&&last.msg===fullMsg&&last.cls===cls){
    last.count=(last.count||1)+1;
    last.ts=entry.ts; // last occurrence
    renderLog();
    return;
  }
  consoleLogs.push({ts:entry.ts,msg:fullMsg,raw:String(entry.msg==null?'':entry.msg),src:entry.name||'',cls:cls,count:1});
  if(consoleLogs.length>500)consoleLogs.shift();
  // Badge + toast when the tab is not active and there are errors/warnings
  if(currentPanel!=='console'&&(cls==='msg-err'||cls==='msg-warn')){
    logBadgeCount++;
    var bc=logBadgeCount>99?'99+':logBadgeCount;
    ['log-badge','log-badge-bot'].forEach(function(id){var b=document.getElementById(id);if(b){b.style.display='inline';b.textContent=bc;}});
  }
  if(cls==='msg-err')showToast(entry.msg.split('\n')[0]);
  renderLog();
}
// Short red snackbar on errors (even with the Console tab closed).
var _toastTimer=null;
function showToast(msg){
  var t=document.getElementById('kx-toast');
  if(!t){
    t=document.createElement('div'); t.id='kx-toast';
    t.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--err);color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;z-index:9999;max-width:90vw;box-shadow:0 4px 16px rgba(0,0,0,.4);cursor:pointer';
    t.onclick=function(){showPanel('console');t.style.display='none';};
    document.body.appendChild(t);
  }
  t.textContent=msg;t.dataset.icon='triangle-alert';
  t.style.display='block';
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(function(){t.style.display='none';},6000);
}
function _setSegActive(sel,pred){
  document.querySelectorAll(sel).forEach(function(b){b.classList.toggle('active',!!pred(b));});
}
function setLogDir(dir){
  logDirFilter=dir;
  _setSegActive('.log-dir-btn',function(b){return b.id==='logdir-'+dir;});
  renderLog();
}
function setLogLevel(lvl){
  logLevelFilter=lvl;
  _setSegActive('.log-lvl-btn',function(b){return b.id==='loglvl-'+lvl;});
  renderLog();
}
function setLogTopic(topic){
  var inp=document.getElementById('log-filter');
  var active=inp.value===topic;
  inp.value=active?'':topic;
  _setSegActive('.log-topic-btn',function(b){return !active&&b.getAttribute('data-topic')===topic;});
  renderLog();
}
// Uma linha do registro: hora | etiqueta (TX/RX/erro/aviso) | mensagem.
// MQTT messages become "topic + key=value"; the long JSON is collapsed under
// "view data" (before, each printer response was a paragraph of raw JSON).
function _logRowHtml(l){
  var raw=l.raw!=null?l.raw:l.msg;
  var dir=(/^(RX|TX)\b/.exec(raw)||[])[1]||'';
  var tag,tcls;
  if(l.cls==='msg-err'){tag=tr('log_tag_err')||'Erro';tcls='err';}
  else if(l.cls==='msg-warn'){tag=tr('log_tag_warn')||'Aviso';tcls='warn';}
  else if(dir){tag=dir;tcls=dir.toLowerCase();}
  else if(l.src==='ui'){tag='UI';tcls='ok';}
  else {tag='Info';tcls='';}
  var head=raw, data='';
  var di=raw.search(/\s(data|payload)=[\[{]/);
  if(di===-1&&raw.length>160){var bi=raw.search(/[\[{]/);if(bi>20)di=bi-1;}
  if(di!==-1&&raw.length-di>60){head=raw.slice(0,di);data=raw.slice(di).replace(/^\s*(data|payload)=/,'');}
  var body=escHtml(head).replace(/(\s)([\w.]+)=([^\s]+)/g,'$1<span class="kv">$2=</span>$3');
  if(dir){
    body=body.replace(/^(RX|TX)(\s*\[[^\]]*\]|\(web\))?\s+(\S+)/,function(_,d,x,topic){return '<span class="topic">'+topic+'</span>';});
  }
  var pretty='';
  if(data){
    try{pretty=JSON.stringify(JSON.parse(data),null,2);}catch(e){pretty=data;}
  }
  var cnt=(l.count&&l.count>1)?'<span class="log-count">×'+l.count+'</span>':'';
  return '<div class="log-row '+l.cls+'" title="'+escHtml(l.src||'')+'"><span class="ts">'+escHtml(l.ts)+'</span>'+
    '<span class="log-tag '+tcls+'">'+escHtml(tag)+'</span>'+
    '<span class="log-msg">'+body+cnt+'</span>'+
    (pretty?'<details class="log-data"><summary>'+escHtml(tr('log_show_data')||'ver dados')+'</summary><pre>'+escHtml(pretty)+'</pre></details>':'')+
  '</div>';
}
// Rebuilding the innerHTML on every new line wiped the selection - you could not copy.
// With text selected (or the mouse held down) in the console, rendering
// espera; as linhas seguem entrando em consoleLogs e aparecem ao soltar.
var _logHold=false,_logPending=false;
function _logSelecting(el){
  if(_logHold)return true;
  var sel=window.getSelection&&window.getSelection();
  return !!(sel&&!sel.isCollapsed&&el.contains(sel.anchorNode));
}
document.addEventListener('selectionchange',function(){
  var el=document.getElementById('console-log');
  if(_logPending&&el&&!_logSelecting(el))renderLog();
});
window.addEventListener('DOMContentLoaded',function(){
  var el=document.getElementById('console-log');if(!el)return;
  el.addEventListener('pointerdown',function(){_logHold=true;});
  document.addEventListener('pointerup',function(){if(!_logHold)return;_logHold=false;if(_logPending&&!_logSelecting(el))renderLog();});
});
function renderLog(){
  var el=document.getElementById('console-log');
  if(!el)return;
  if(_logSelecting(el)){_logPending=true;return;}
  _logPending=false;
  var filter=(document.getElementById('log-filter')||{}).value||'';
  var fl=filter.toLowerCase();
  var rows=consoleLogs.filter(function(l){
    var m=l.msg;
    if(logDirFilter==='rx'&&!/ RX[ (]/.test(m))return false;
    if(logDirFilter==='tx'&&!/ TX[ (]/.test(m))return false;
    if(logLevelFilter==='err'&&l.cls!=='msg-err')return false;
    if(logLevelFilter==='warn'&&l.cls!=='msg-err'&&l.cls!=='msg-warn')return false;
    if(fl&&!m.toLowerCase().includes(fl))return false;
    return true;
  });
  // Open details stay open when a new line arrives.
  var open={};el.querySelectorAll('details[open]').forEach(function(d,i){open[d.parentNode.getAttribute('data-k')]=1;});
  var savedScroll=logAutoScroll?null:el.scrollTop;
  el.innerHTML=rows.map(function(l){
    return _logRowHtml(l).replace('<div class="log-row','<div data-k="'+escHtml(l.ts+l.msg.length)+'" class="log-row');
  }).join('')||'<div class="empty-state">'+escHtml(tr('log_empty')||'Nothing here yet.')+'</div>';
  if(Object.keys(open).length)el.querySelectorAll('.log-row').forEach(function(r){if(open[r.getAttribute('data-k')]){var d=r.querySelector('details');if(d)d.open=true;}});
  if(logAutoScroll)el.scrollTop=el.scrollHeight;
  else if(savedScroll!==null)el.scrollTop=savedScroll;
}
function onLogScroll(){
  var el=document.getElementById('console-log');
  if(!el)return;
  var atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<30;
  if(!atBottom&&logAutoScroll){setAutoScroll(false);}
}
function toggleAutoScroll(){
  setAutoScroll(!logAutoScroll);
  if(logAutoScroll){var el=document.getElementById('console-log');if(el)el.scrollTop=el.scrollHeight;}
}
function setAutoScroll(on){
  logAutoScroll=on;
  var btn=document.getElementById('btn-autoscroll');
  if(btn){btn.classList.toggle('btn-accent',on);btn.classList.toggle('btn-ghost',!on);}
}
function clearLogBadge(){
  logBadgeCount=0;
  ['log-badge','log-badge-bot'].forEach(function(id){var b=document.getElementById(id);if(b)b.style.display='none';});
}
function escHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
// JS string inside an onclick='...(\'X\')' attribute: escape for JS then for HTML.
// File names come from outside (upload, printer storage) - without this, a
// name with quotes becomes XSS.
function jsq(s){return escHtml(String(s==null?'':s).replace(/\\/g,'\\\\').replace(/'/g,"\\'"));}
// Stream SSE de log do servidor
(function(){
  function connect(){
    var es=new EventSource('/api/log/stream');
    es.onmessage=function(e){try{_appendLog(JSON.parse(e.data));}catch(_){}};
    es.onerror=function(){es.close();setTimeout(connect,3000);};
  }
  window.addEventListener('DOMContentLoaded',connect);
})();

// ── Helpers ──
function fmtTime(s){if(!s||s<0)return'–';var m=Math.floor(s/60),h=Math.floor(m/60);m%=60;return h>0?h+'h '+m+'m':m+'m'}
function fmtHmsFromSec(total){
  total=Math.max(0,parseInt(total||0,10));
  var h=Math.floor(total/3600);
  var mm=Math.floor((total%3600)/60);
  var ss=total%60;
  return h+':'+String(mm).padStart(2,'0')+':'+String(ss).padStart(2,'0');
}
function post(url,body){return fetch(_apiUrl(url),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
function clamp(v,lo,hi){return Math.min(hi,Math.max(lo,v))}

// ── Aplica o estado no DOM ──
function applyState(){
  _renderUploadStrip(S.upload);
  if(window.kxPreview)try{kxPreview.update(S);}catch(e){}
  _updateLogoutBtn();
  var s=S;
  _syncAceDryPresetsFromServer(s.ace_dry_presets);
  // connection error banner – only when at least one printer is configured
  var banner=document.getElementById('conn-error-banner');
  if(banner){if(s.connection_error&&_printers.length>0){banner.textContent=tr('lbl_conn_error')+' '+s.connection_error;banner.dataset.icon='triangle-alert';banner.style.display='block';}else{banner.style.display='none';}}
  var pauseBanner=document.getElementById('pause-msg-banner');
  if(pauseBanner){
    if(s.pause_msg && (s.print_state==='paused'||s.print_state==='error')){
      var code=parseInt(s.error_code,10)||0;
      var codePart = (code==0) ? ' ' :
        ' [<a href="https://wiki.anycubic.com/en/error-codes/'+code+'-code" target="_blank">'+code+'</a>] ';
      var failed=s.print_state==='error';
      pauseBanner.innerHTML='<i data-icon="'+(failed?'triangle-alert':'pause')+'" style="margin-right:6px"></i>'+escHtml(tr(failed?'lbl_error_reason':'lbl_pause_reason'))+codePart+escHtml(tr('err_'+code,s.pause_msg));
      pauseBanner.style.display='block';
    }else{
      pauseBanner.style.display='none';
    }
  }
  var bannerVisible=false;
  var frb=document.getElementById('file-ready-banner');
  if(frb){
    var shouldAutoOpen=(s.print_start_dialog===undefined?true:!!s.print_start_dialog);
    if(s.file_ready&&s.print_state==='standby'){
      document.getElementById('file-ready-name').textContent=s.file_ready;
      // New file → releases the cancel lock
      if(_fdAutoOpenedFile&&_fdAutoOpenedFile!==s.file_ready){
        _fdUserCancelled=false;
        sessionStorage.removeItem('fdUserCancelled');
        sessionStorage.removeItem('fdAutoOpenedFile');
      }
      if(shouldAutoOpen){
        // Dialog mode: never show the banner.
        frb.style.display='none';
        if(!_fdDialogOpen&&!_fdUserCancelled&&_fdAutoOpenedFile!==s.file_ready){
          _fdAutoOpenedFile=s.file_ready;
          startReadyFileWithSlots(s.file_ready,true,s.filament_mismatch||null);
        }
      } else {
        frb.style.display='flex';
        bannerVisible=true;
      }
    }else{frb.style.display='none';}
  }
  // skip button (during the print) – only visible while printing
  var printing=(s.print_state==='printing'||s.print_state==='paused');
  var skipBtn=document.getElementById('d-btn-skip');
  if(skipBtn) skipBtn.style.display=printing?'':'none';
  // Pause/Stop buttons only with an active print (otherwise it confuses when
  // the printer is idle). The Pause button re-renders according to the state.
  var ctrlBtns=document.getElementById('d-ctrl-btns');
  if(ctrlBtns) ctrlBtns.style.display=printing?'':'none';
  updatePauseResumeBtn();
  // Remembers the last loaded file (Issue #55): while it is still visible
  // by state. At print end/cancel the bridge clears file_ready+filename
  // (Issue #29) — the stored reference stays for the card actions.
  // A real ready file or an ongoing print undoes a previous "Clear".
  if(s.file_ready||printing) _idleCleared=false;
  if(s.file_ready) _lastLoadedFile=s.file_ready;
  else if(s.filename && !_idleCleared) _lastLoadedFile=s.filename;
  else if(_idleCleared) _lastLoadedFile=null;
  // Idle actions (Print/Slots/Clear) only when not printing, there is a
  // known file and the green banner does not already offer the same
  // action.
  var idleBtns=document.getElementById('d-idle-btns');
  if(idleBtns){
    var showIdle=(!printing && _lastLoadedFile && !bannerVisible);
    idleBtns.style.display=showIdle?'':'none';
    if(showIdle){
      var dfn=document.getElementById('d-fname');
      if(dfn && (!dfn.textContent || dfn.textContent==='–')){
        dfn.textContent=_lastLoadedFile;dfn.title=_lastLoadedFile;
      }
    }
  }

  // header
  var b=document.getElementById('h-badge');
  b.className='hbadge '+s.print_state;
  document.getElementById('h-state').textContent=T['kobra_'+s.kobra_state]||s.kobra_state||T.header_status_standby;
  var _pn=_printers.length===0?'–':((_activePrinter&&_activePrinter.name)||s.printer_name);
  var _el=document.getElementById('h-pname');if(_el)_el.textContent=_pn;
  var _el2=document.getElementById('h-pname-single');if(_el2)_el2.textContent=_pn;
  var hv=document.getElementById('h-version');if(hv&&s.version)hv.textContent='v'+s.version;


  // temperaturas
  var nt=document.getElementById('d-nt');if(nt)nt.textContent=s.nozzle_temp.toFixed(1);
  var ntt=document.getElementById('d-nt-t');if(ntt)ntt.textContent=s.nozzle_target.toFixed(0);
  var bt=document.getElementById('d-bt');if(bt)bt.textContent=s.bed_temp.toFixed(1);
  var btt=document.getElementById('d-bt-t');if(btt)btt.textContent=s.bed_target.toFixed(0);

  // barras de temperatura (dashboard)
  var nb=document.getElementById('d-ntbar');if(nb)nb.style.width=clamp(s.nozzle_temp/300*100,0,100)+'%';
  var bb=document.getElementById('d-btbar');if(bb)bb.style.width=clamp(s.bed_temp/120*100,0,100)+'%';

  // progresso
  var pct=Math.round(s.progress*100);
  // ≈ = printer stopped sending progress; value estimated from time (bridge)
  var est=!!s.progress_estimated;
  var dpct=document.getElementById('d-pct');if(dpct){dpct.textContent=(est?'≈':'')+pct;dpct.title=est?(tr('progress_estimated')||''):'';}
  var dpbar=document.getElementById('d-pbar');if(dpbar)dpbar.style.width=pct+'%';

  var layers=s.curr_layer&&s.total_layers?s.curr_layer+' / '+s.total_layers:'–';
  var dlayers=document.getElementById('d-layers');if(dlayers)dlayers.textContent=layers;
  var dzpos=document.getElementById('d-zpos');if(dzpos)dzpos.textContent=s.z_mm>0?s.z_mm.toFixed(2)+' mm':'–';

  var delapsed=document.getElementById('d-elapsed');if(delapsed)delapsed.textContent=fmtTime(s.print_duration);
  var dremain=document.getElementById('d-remain');if(dremain)dremain.textContent=s.remain_time>0?(est?'≈ ':'')+fmtTime(s.remain_time):'–';
  var dslrow=document.getElementById('d-slicer-row');
  var dsltime=document.getElementById('d-slicer-time');
  if(dslrow&&dsltime){
    if(s.slicer_time>0){dslrow.style.display='';dsltime.textContent=fmtTime(s.slicer_time);}
    else{dslrow.style.display='none';}
  }

  var fn=s.filename||'–';
  var dfname=document.getElementById('d-fname');if(dfname){dfname.textContent=fn;dfname.title=fn};
  var pfname=document.getElementById('p-fname');if(pfname){pfname.textContent=fn;pfname.title=fn};
  var cfo=document.getElementById('cam-fname');if(cfo)cfo.textContent=fn!=='–'?fn:'';

  // miniatura
  var thumb=document.getElementById('d-thumbnail');
  if(thumb){
    if(s.thumbnail){
      thumb.src='data:image/png;base64,'+s.thumbnail;
      thumb.style.display='block';
    } else {
      thumb.style.display='none';
      thumb.src='';
    }
  }

  // sincronia de luz/ventoinha
  document.getElementById('d-light-toggle').checked=s.light_on;
  var dfan=document.getElementById('d-fan');if(dfan)dfan.value=s.fan_speed;
  var dfanval=document.getElementById('d-fan-val');if(dfanval)dfanval.textContent=s.fan_speed;

  // speed mode buttons
  var spdWidths={1:25,2:55,3:90};
  [1,2,3].forEach(function(m){
    var b=document.getElementById('d-spd-'+m);
    if(b) b.classList.toggle('spd-active', s.print_speed_mode===m);
  });
  var spdBar=document.getElementById('d-spd-bar');
  if(spdBar) spdBar.style.width=(spdWidths[s.print_speed_mode]||55)+'%';

  var amsTitle=document.getElementById('d-card-ams');
  if(amsTitle){
    var baseTitle=tr('card_ams');
    var modeMap={toolhead:'Toolhead',ace_direct:'ACE Direct',ace_hub:'ACE Hub'};
    var modeTxt=modeMap[s.filament_mode]||'';
    amsTitle.textContent=modeTxt?(baseTitle+' - '+modeTxt):baseTitle;
  }

  ensureAceDryCards();
  var dry=s.ace_drying||{status:0,target_temp:0,duration:0,remain_time:0,humidity:null,current_temp:null,units:[]};
  var units=(dry.units||[]);
  var unitMap={};
  units.forEach(function(u){var id=Number(u.id);if(id>=0&&id<=3)unitMap[id]=u;});
  var aceMode=s.filament_mode==='ace_direct'||s.filament_mode==='ace_hub';
  var detected=(s.ace_units||[]).filter(function(id){return id>=0&&id<=3;});
  if(!detected.length){
    Object.keys(unitMap).forEach(function(k){detected.push(Number(k));});
  }
  if(!detected.length){
    (s.ams_slots||[]).forEach(function(sl){var id=Number(sl.box_id);if(id>=0&&id<=3&&detected.indexOf(id)<0)detected.push(id);});
  }
  detected.sort(function(a,b){return a-b;});
  var aceWrap=document.getElementById('d-ace-dry-wrap');
  if(aceWrap)aceWrap.style.display=(aceMode&&detected.length)?'contents':'none';
  for(var i=0;i<4;i++){
    var card=document.getElementById('d-ace-dry-card-'+i);
    if(!card)continue;
    var show=aceMode&&detected.indexOf(i)>=0;
    card.style.display=show?'':'none';
    if(!show)continue;
    var ud=unitMap[i]||dry;
    var refillToggle=document.getElementById('ace-auto-refill-toggle-'+i);
    var autoFeedMap=s.ace_auto_feed||{};
    if(refillToggle&&!_aceAutoFeedPending[i]){
      var afVal=autoFeedMap.hasOwnProperty(String(i))?Number(autoFeedMap[String(i)]):(_aceAutoRefillGet(i)?1:0);
      refillToggle.checked=afVal===1;
    }
    var dryToggle=document.getElementById('ace-dry-enable-toggle-'+i);
    if(dryToggle)dryToggle.checked=Number(ud.status||0)>0;
    var dst=document.getElementById('d-ace-dry-state-'+i);
    if(dst){var on=Number(ud.status||0)>0;dst.textContent=on?tr('ace_dry_state_on','secando'):tr('ace_dry_state_off','desligado');dst.classList.toggle('on',on);}
    var hh=document.getElementById('d-ace-dry-humidity-'+i);
    if(hh){
      var hv=(ud.humidity===null||ud.humidity===undefined||ud.humidity==='')?null:Number(ud.humidity);
      hh.textContent=(hv===null||Number.isNaN(hv))?'-':(Math.round(hv)+'%');
    }
    var ht=document.getElementById('d-ace-dry-current-temp-'+i);
    if(ht){
      var ct=(ud.current_temp===null||ud.current_temp===undefined||ud.current_temp==='')?null:Number(ud.current_temp);
      ht.textContent=(ct===null||Number.isNaN(ct))?'-':(ct.toFixed(1)+'°C');
    }
    var prof=_aceDryProfileGet(i);
    var useSec=(Number(ud.status||0)>0&&Number(ud.remain_time)>0)
      ?Number(ud.remain_time||0)*60
      :prof.duration_sec;
    var showTemp=(Number(ud.status||0)>0&&Number(ud.target_temp)>0)?Number(ud.target_temp):prof.temp;
    var dryTempEl=document.getElementById('d-ace-dry-target-'+i);
    if(dryTempEl)dryTempEl.textContent=showTemp+'°C';
    var dryTimeEl=document.getElementById('d-ace-dry-time-'+i);
    if(dryTimeEl)dryTimeEl.textContent=fmtHmsFromSec(useSec);
  }

  // AMS
  if(s.ams_slots&&s.ams_slots.length){
    window._amsSlots=s.ams_slots;
    // Agrupa por box_id (-1=Toolhead, 0=ACE 1, 1=ACE 2, ...)
    var boxMap={};
    s.ams_slots.forEach(function(slot,i){
      var bid=slot.box_id!=null?slot.box_id:-1;
      if(!boxMap[bid])boxMap[bid]=[];
      boxMap[bid].push({slot:slot,arrIdx:i});
    });
    var boxIds=Object.keys(boxMap).map(Number).sort(function(a,b){return a-b});
    var acePresent=boxIds.some(function(b){return b>=0;});
    var html='';
    boxIds.forEach(function(bid){
      var entries=boxMap[bid];
      var label=bid===-1
        ?(acePresent?'Toolhead (Slots 1–3)':'Toolhead')
        :('ACE '+(bid+1));
      html+='<div class="ams-box-group">'
        +'<div class="ams-box-label">'+label+'</div>'
        +'<ul class="ams-box-slots">';
      entries.forEach(function(e,k){
        var slot=e.slot;var i=e.arrIdx;
        var empty=slot.status!==5;
        var rgb=empty?null:(Array.isArray(slot.color)?slot.color:[128,128,128]);
        var col=rgb?'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')':'transparent';
        var globalIdx=slot.global_index!=null?slot.global_index:i;
        var active=slot.status===1||slot.active;
        var loaded=(s.ams_loaded_slot!=null&&s.ams_loaded_slot>=0&&globalIdx===s.ams_loaded_slot);
        var activity=(slot.activity||'');
        var pct=(!empty&&slot.consumables_percent!=null)?slot.consumables_percent:null;
        // Uses the mapped profile only for occupied slots — otherwise an
        // orphan mapping (slot was emptied) shows a "ghost" profile (Issue #57).
        var profile=empty?null:(window._slotProfileMap||{})[globalIdx];
        var genericType=(slot.type||slot.material_type||'–');
        // Material label: on an occupied slot with a mapping, shows the concrete profile name
        // (e.g. "eSUN PLA+") instead of just the generic type (Issue #57 point 4).
        var materialLabel=empty?T.ams_empty:((profile&&profile.name)?profile.name:genericType);
        var sub=empty?tr('ams_insert_spool','insert a spool')
          :(loaded?tr('ams_in_use','em uso agora'):((profile&&profile.vendor)?profile.vendor:genericType));
        if(sub===materialLabel)sub='';
        if(activity==='loading')sub=tr('ams_loading','carregando…');
        else if(activity==='unloading')sub=tr('ams_unloading','descarregando…');
        var spoolSel='',spoolRem=null;
        if(_spoolmanStatus.configured&&!empty&&_spoolmanSpools.length){
          var curSpool=_slotSpoolMap[String(globalIdx)]||'';
          var spoolOpts='<option value="">Spoolman –</option>'+_spoolmanSpools.map(function(sp){
            var vendor=sp.filament&&sp.filament.vendor?sp.filament.vendor.name+' ':'';
            var name=sp.filament?sp.filament.name:'#'+sp.id;
            var rem=sp.remaining_weight!=null?' '+sp.remaining_weight.toFixed(0)+'g':'';
            if(String(sp.id)==String(curSpool)&&sp.remaining_weight!=null)spoolRem=sp.remaining_weight;
            return '<option value="'+sp.id+'"'+(String(sp.id)==String(curSpool)?' selected':'')+'>'+
                   escHtml(vendor+name+rem)+'</option>';
          }).join('');
          spoolSel='<select class="slot-spool" data-spool-slot="'+globalIdx+'" onclick="event.stopPropagation()" onchange="onAmsSpoolChange(this)" aria-label="Spoolman">'+spoolOpts+'</select>';
        }
        var amt=spoolRem!=null?'<span class="num">'+spoolRem.toFixed(0)+' g</span><small>Spoolman</small>'
          :(pct!=null?'<span class="num">'+pct+'%</span><small>'+escHtml(tr('ams_remaining','restante'))+'</small>':'');
        html+='<li class="ams-slot'+(active?' active':'')+(loaded?' loaded':'')+(activity?' '+activity:'')+(empty?' empty':'')
          +'" style="--slot-color:'+col+'" tabindex="0" role="button" onclick="openSlotEdit('+i+')" onkeydown="if(event.key===\'Enter\')openSlotEdit('+i+')"'
          +' title="'+escHtml(empty?'':genericType)+'">'
          +'<span class="slot-sym">'+_slotSymbol(k)+'<b>'+(globalIdx+1)+'</b></span>'
          +'<span class="slot-circle"></span>'
          +'<span class="slot-name"><span class="slot-material">'+escHtml(materialLabel)+'</span><span class="slot-label">'+escHtml(sub)+'</span>'+spoolSel+'</span>'
          +'<span class="slot-amt">'+amt+'</span>'
          +'</li>';
      });
      if(bid===-1&&acePresent){
        html+='<li class="ams-slot ams-slot-bridge"><span class="slot-sym">'+_slotSymbol(3)+'<b>4</b></span>'
          +'<span class="bridge-chip">ACE</span><span class="slot-label">'+escHtml(tr('ams_bridge','o slot 4 vem do ACE'))+'</span></li>';
      }
      html+='</ul></div>';
    });
  // Does not render if a spool dropdown is open (avoids closing it on poll)
  var activeEl=document.activeElement;
  var spoolOpen=activeEl&&activeEl.tagName==='SELECT'&&activeEl.dataset.spoolSlot!=null;
  if(!spoolOpen) document.getElementById('ams-slots').innerHTML=html;
  }

  // camera overlay
  var co=document.getElementById('cam-overlay');
  if(co)co.style.display=(s.print_state==='printing'&&camOn)?'block':'none';

  // turns the camera on automatically during the print (unless the user stopped it)
  if(s.print_state==='printing'&&!camOn&&s.camera_url&&!camUserStopped&&s.camera_on_print){
    camStart();
  }
  // clears the user-stopped flag when the print ends, so the next one turns on by itself again
  if(s.print_state!=='printing'){
    camUserStopped=false;
  }

  _renderMission(s);
  updateConnBtn();
}

// ── Mission: end, phase trajectory and Moko bulletin ──
// Phases the Kobra X actually reports (kobra_state). Sub-steps mid-print
// (heating the nozzle during a color change) do not reset the trajectory: with
// progress > 0 the phase is "printing".
var MISSION_PHASES=['prep','heat','level','print','done'];
function _missionPhase(s){
  var k=s.kobra_state||'';
  if(s.print_state==='complete'||k==='finished')return 'done';
  if(s.print_state!=='printing'&&s.print_state!=='paused')return null;
  if((s.progress||0)>0||(s.curr_layer||0)>0)return 'print';
  if(k==='preheating'||k==='nozzle_heating'||k==='hotbed_heating')return 'heat';
  if(k==='auto_leveling'||k==='frequency_sweeping')return 'level';
  if(k==='checking'||k==='init'||k==='updated'||k==='busy')return 'prep';
  return 'print';
}
function _clock(d){return d.toLocaleTimeString(currentLang==='pt-br'?'pt-BR':currentLang,{hour:'2-digit',minute:'2-digit'});}
// Start of each phase, as seen by this browser (the printer does not send the time).
var _phaseSince=(function(){try{return JSON.parse(sessionStorage.getItem('phaseSince'))||{};}catch(e){return {};}})();
function _renderMission(s){
  var phase=_missionPhase(s),file=s.filename||'';
  if(phase&&(_phaseSince.file!==file)){_phaseSince={file:file};}
  if(phase&&!_phaseSince[phase]){_phaseSince[phase]=Date.now();try{sessionStorage.setItem('phaseSince',JSON.stringify(_phaseSince));}catch(e){}}
  var eta=s.remain_time>0?new Date(Date.now()+s.remain_time*1000):null;
  var etaTxt=eta?_clock(eta):'';
  var el=document.getElementById('d-eta');
  if(el)el.textContent=(eta&&phase&&phase!=='done')?tr('mission_ends','termina {t}').replace('{t}',etaTxt):'';
  // trajectory
  var wrap=document.getElementById('d-traj-wrap');
  if(wrap){
    wrap.style.display=phase?'':'none';
    var idx=MISSION_PHASES.indexOf(phase);
    MISSION_PHASES.forEach(function(p,i){
      var li=document.getElementById('d-traj-'+p);if(!li)return;
      li.className=i<idx||(phase==='done')?'done':(i===idx?'now':'');
      var sm=li.querySelector('small');if(!sm)return;
      if(i===idx&&phase!=='done'&&_phaseSince[p])sm.textContent=tr('mission_since','desde {t}').replace('{t}',_clock(new Date(_phaseSince[p])));
      else if(p==='done'&&eta&&phase!=='done')sm.textContent='≈ '+etaTxt;
      else if(p==='done'&&phase==='done'&&_phaseSince.done)sm.textContent=_clock(new Date(_phaseSince.done));
      else sm.textContent='';
    });
    var run=document.getElementById('d-traj-run');
    if(run)run.style.width=(phase==='done'?100:Math.max(0,idx)/(MISSION_PHASES.length-1)*100)+'%';
  }
  _renderMoko(s,phase,etaTxt);
}
// Moko comments on the state with a pose from the character sheet (lib/moko/*.png).
function _renderMoko(s,phase,etaTxt){
  var img=document.getElementById('moko-img'),txt=document.getElementById('moko-txt');
  if(!img||!txt)return;
  var pose='ok',key='moko_idle';
  var up=_browserUpload||s.upload;
  var uploading=up&&['browser','receiving','processing','sending'].indexOf(up.phase)!==-1;
  if(s.kobra_state==='offline'){pose='sleep';key='moko_offline';}
  else if(s.print_state==='error'){pose='err';key='moko_error';}
  else if(uploading&&phase!=='print'){pose='upload';key='moko_upload';}
  else if(phase==='done'){pose='done';key='moko_done';}
  else if(s.print_state==='paused'){
    var runout=/filament|material|runout|feed/i.test(s.pause_msg||'');
    pose=runout?'empty':'wait';key=runout?'moko_runout':'moko_paused';
  }
  else if(phase==='level'){pose='level';key='moko_leveling';}
  else if(phase==='heat'||phase==='prep'){pose='heat';key='moko_heating';}
  else if(phase==='print'){pose='print';key=(s.total_layers>0&&s.curr_layer>0)?'moko_printing':'moko_printing_pct';}
  var code=parseInt(s.error_code,10)||0;
  var vars={pct:Math.round((s.progress||0)*100),upfile:(up&&up.name)||'',layer:s.curr_layer||0,total:s.total_layers||0,eta:etaTxt||'–',file:s.filename||'',
    nozzle:Math.round(s.nozzle_temp||0),nt:Math.round(s.nozzle_target||0),bed:Math.round(s.bed_temp||0),bt:Math.round(s.bed_target||0),
    msg:code?tr('err_'+code,s.pause_msg||''):(s.pause_msg||''),dur:fmtTime(s.print_duration)};
  // the printer message comes without a final period; the Moko text continues after it
  if(vars.msg&&!/[.!?…]$/.test(vars.msg))vars.msg+='.';
  function fill(t){return t.replace(/\{(\w+)\}/g,function(m,k){return vars[k]!=null?vars[k]:m;});}
  // Two masks per pose: outline (theme ink) and details (action color).
  if(img.dataset.pose!==pose){
    var base='/kx/ui/lib/moko/'+pose,v='.png?v='+(document.documentElement.dataset.assets||'');
    img.style.setProperty('--moko-l','url('+base+'-l'+v+')');
    img.style.setProperty('--moko-a','url('+base+'-a'+v+')');
    img.dataset.pose=pose;
  }
  txt.innerHTML='<b>'+escHtml(fill(tr(key+'_title','')))+'</b> '+escHtml(fill(tr(key+'_body','')));
}

// Trend of a reading: change per minute over the last ~60 s of history.
function _trend(arr,times,target){
  var n=arr.length;if(n<2)return {cls:'',icon:'',txt:''};
  var now=times[n-1],j=n-1;while(j>0&&now-times[j-1]<=60000)j--;
  var dt=(now-times[j])/60000,v=arr[n-1];
  var rate=dt>0.1?(v-arr[j])/dt:0;
  if(!(target>0)&&v<45)return {cls:'',icon:'',txt:tr('trend_off','desligado')};
  if(Math.abs(rate)<0.5)return {cls:'',icon:'',txt:tr('trend_stable','stable')};
  var up=rate>0;
  return {cls:up?'warm':'cool',icon:up?'arrow-up':'arrow-down',
    txt:tr(up?'trend_rising':'trend_falling',up?'subindo {r} °C/min':'descendo {r} °C/min').replace('{r}',Math.abs(rate).toFixed(1).replace('.',currentLang==='en'?'.':','))};
}
function _paintTrend(id,t){
  var el=document.getElementById(id);if(!el)return;
  el.textContent=t.txt;el.className='trend'+(t.cls?' '+t.cls:'');
  if(t.icon)el.dataset.icon=t.icon;else delete el.dataset.icon;
}

// "Hold to stop": stopping the print requires holding 1.4 s (an accidental tap
// on the phone cancels nothing). Keyboard: hold Space/Enter.
(function(){
  var b=document.getElementById('d-btn-cancel');if(!b)return;
  var t=null;
  function start(e){if(e.type==='keydown'){if((e.key!==' '&&e.key!=='Enter')||e.repeat)return;}
    e.preventDefault();b.classList.add('holding');t=setTimeout(function(){b.classList.remove('holding');t=null;printAction('cancel');},1400);}
  function end(){if(t){clearTimeout(t);t=null;}b.classList.remove('holding');}
  b.addEventListener('pointerdown',start);b.addEventListener('keydown',start);
  ['pointerup','pointerleave','pointercancel','keyup','blur'].forEach(function(ev){b.addEventListener(ev,end);});
})();

function _updateLogoutBtn(){
  var b=document.getElementById('logout-btn');
  if(!b)return;
  b.style.display=S.auth_enabled?'':'none';
  b.title=tr('nav_logout','Log out');
}

function _camBtnIcon(on){
  var b=document.getElementById('cam-toggle-btn');if(b)b.dataset.icon=on?'square':'play';
}
function updateConnBtn(){
  var btn=document.getElementById('conn-btn');
  if(!btn)return;
  var offline=S.kobra_state==='offline';
  if(offline){
    btn.className='conn-btn disconnected';
    btn.textContent=tr('btn_connect');
    btn.dataset.icon='plug';
  } else {
    btn.className='conn-btn connected';
    btn.textContent=tr('btn_disconnect');
    btn.dataset.icon='unplug';
  }
}

function toggleConnection(){
  var btn=document.getElementById('conn-btn');
  var offline=S.kobra_state==='offline';
  btn.disabled=true;
  btn.textContent='…';
  var url=offline?'/api/connect':'/api/disconnect';
  post(url,{}).then(function(r){return r.json()}).then(function(r){
    btn.disabled=false;
    if(r.error)addLog('Erro: '+srvMsg(r.error));
  }).catch(function(){btn.disabled=false;});
}

// ── History + temperature chart ──
function updateHistory(){
  tempHistory.n.push(S.nozzle_temp);
  tempHistory.b.push(S.bed_temp);
  (tempHistory.t=tempHistory.t||[]).push(Date.now());
  if(tempHistory.n.length>60){tempHistory.n.shift();tempHistory.b.shift();tempHistory.t.shift();}
  _paintTrend('d-nt-trend',_trend(tempHistory.n,tempHistory.t,S.nozzle_target));
  _paintTrend('d-bt-trend',_trend(tempHistory.b,tempHistory.t,S.bed_target));
  var cs=getComputedStyle(document.documentElement);
  function c(n,f){return cs.getPropertyValue(n).trim()||f;}
  drawChart('d-chart',tempHistory,[
    {data:tempHistory.n,color:c('--heat','#F08A4B'),max:300,target:S.nozzle_target},
    {data:tempHistory.b,color:c('--bed','#6FA3C8'),max:300,target:S.bed_target}],c('--rule','#2E3438'),c('--ink-3','#6E746F'));
}
function drawChart(id,_,series,gridColor,targetColor){
  var canvas=document.getElementById(id);if(!canvas)return;
  var ctx=canvas.getContext('2d'),dpr=window.devicePixelRatio||1;
  var W=canvas.offsetWidth*dpr||canvas.width;
  var H=canvas.offsetHeight*dpr||canvas.height;
  canvas.width=W;canvas.height=H;
  ctx.clearRect(0,0,W,H);
  // grid every 60 °C and a dashed line for each series target
  var max=(series[0]&&series[0].max)||300;
  if(gridColor){ctx.strokeStyle=gridColor;ctx.lineWidth=1;
    for(var g=0;g<=max;g+=60){var gy=Math.round(H-4-(g/max)*(H-8))+.5;ctx.beginPath();ctx.moveTo(0,gy);ctx.lineTo(W,gy);ctx.stroke();}}
  if(targetColor){ctx.save();ctx.setLineDash([4*dpr,4*dpr]);ctx.strokeStyle=targetColor;
    series.forEach(function(s){if(!(s.target>0))return;var ty=Math.round(H-4-(s.target/s.max)*(H-8))+.5;ctx.beginPath();ctx.moveTo(0,ty);ctx.lineTo(W,ty);ctx.stroke();});
    ctx.restore();}
  series.forEach(function(s){
    var data=s.data;if(!data.length)return;
    var max=s.max;
    ctx.beginPath();ctx.strokeStyle=s.color;ctx.lineWidth=1.8*dpr;ctx.lineJoin='round';
    data.forEach(function(v,i){
      var x=i/(Math.max(data.length-1,1))*(W-4)+2;
      var y=H-4-(v/max)*(H-8);
      if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    });
    ctx.stroke();
  });
}

// ── Settings modal ──
function openSettings(){
  fetch(_apiUrl('/api/settings')).then(function(r){return r.json()}).then(function(d){
    document.getElementById('s-printer-name').value=d.printer_name||'';
    document.getElementById('s-printer-ip').value=d.printer_ip||'';
    document.getElementById('s-mqtt-port').value=d.mqtt_port||9883;
    document.getElementById('s-username').value=d.username||'';
    document.getElementById('s-password').value=d.password||'';
    document.getElementById('s-device-id').value=d.device_id||'';
    document.getElementById('s-mode-id').value=d.mode_id||'';
    var pon=document.getElementById('s-power-on-url');if(pon)pon.value=d.power_on_url||'';
    var poff=document.getElementById('s-power-off-url');if(poff)poff.value=d.power_off_url||'';
    var pstat=document.getElementById('s-power-status-url');if(pstat)pstat.value=d.power_status_url||'';
    var pinv=document.getElementById('s-power-status-inverted');if(pinv)pinv.checked=!!d.power_status_inverted;
    var nurl=document.getElementById('s-notify-url');if(nurl)nurl.value=d.notify_url||'';
    _authFill(d);
    document.getElementById('s-default-slot').value=d.default_ams_slot||'auto';
    document.getElementById('s-auto-leveling').checked=(d.auto_leveling===undefined?true:!!d.auto_leveling);
    var vc=document.getElementById('s-vibration-compensation');if(vc)vc.checked=!!d.vibration_compensation;
    var cop=document.getElementById('s-camera-on-print');if(cop)cop.checked=!!d.camera_on_print;
    var frm=document.getElementById('s-file-ready-mode');if(frm)frm.value=(d.print_start_dialog===undefined?'1':String(d.print_start_dialog?1:0));
    var wuw=document.getElementById('s-web-upload-warning');if(wuw)wuw.checked=(d.web_upload_warning===undefined?true:!!d.web_upload_warning);
    var dpfap=document.getElementById('s-delete-printer-file-after-print');if(dpfap)dpfap.checked=!!d.delete_printer_file_after_print;
    [['s-job-log-keep','job_log_keep'],['s-job-log-context','job_log_context_lines'],['s-log-buffer','log_buffer_lines']].forEach(function(p){
      var el=document.getElementById(p[0]);if(el&&d[p[1]]!==undefined)el.value=d[p[1]];
    });
    // Polling interval (seconds) — the backend takes precedence over localStorage
    var pi=document.getElementById('s-poll-interval');
    if(pi){
      var sec=d.poll_interval||Math.round((parseInt(localStorage.getItem('pollInterval')||'2000'))/1000)||3;
      pi.value=sec;
    }
    var vhl=document.getElementById('s-verbose-http-log');if(vhl)vhl.checked=!!d.verbose_http_log;
    renderFilamentMapping(d.filament_profiles||{});
    renderSpoolmanSlotCard();
    // Spoolman
    var su=document.getElementById('s-spoolman-url');if(su)su.value=d.spoolman_server||'';
    var sr=document.getElementById('s-spoolman-sync-rate');if(sr)sr.value=(d.spoolman_sync_rate!==undefined?d.spoolman_sync_rate:30);
    _updateSpoolmanStatusDot();
  });
  // Mirrors the language selection in the settings panel with the current language
  var ls=document.getElementById('s-lang-select');
  if(ls)ls.value=(localStorage.getItem('lang')||document.documentElement.lang||'de');
  document.getElementById('s-version-label').textContent='v'+('__VERSION__'||'?');
  // Checks GitHub for a newer release (cached by the backend); shows a link when there is one.
  fetch('/api/update').then(function(r){return r.json();}).then(function(u){
    var el=document.getElementById('s-update');if(!el)return;
    if(u&&u.available){
      el.textContent=tr('update_available','New version available: ')+u.latest;
      el.href=u.url||'https://github.com/clevim/MoonKobra/releases';
      el.style.display='block';
    }else{el.style.display='none';}
  }).catch(function(){});
  // Loads the list of custom profiles (Issue #41)
  refreshUserProfileList();
  // Vendor visibility filter (Issue #41 option A)
  loadVendorChecklist();
}
function closeSettings(){
  // Panel variant: back to the dashboard
  showPanel('dashboard');
}

// Polling interval field → applies to live polling right away (persists only on save)
function onPollIntervalInput(){
  var pi=document.getElementById('s-poll-interval');
  if(!pi)return;
  var sec=parseInt(pi.value,10);
  if(sec>=1&&sec<=60)setPoll(sec*1000);
}

// ── Per-slot filament profile mapping ([filament_profiles]) ──
// A single profile dropdown per slot (vendor+name together, keyed by
// _profileKey). No free text → the (vendor,name)→id matching can no longer
// break from manual typing (Issue #57 point 1). The options come from
// /kx/filament/profiles, grouped by vendor, user profiles first,
// with the same vendor visibility filter as the slot edit dropdown.
function renderFilamentMapping(map){
  var el=document.getElementById('filament-mapping-list');
  if(!el)return;
  var rows='';
  for(var i=0;i<4;i++){
    var m=map[i]||map[String(i)]||{};
    var idHint=m.id?' <span style="color:var(--txt2);font-size:11px">('+m.id+')</span>':'';
    rows+='<div class="modal-field" style="margin-bottom:8px">'
      +'<label>Slot '+(i+1)+idHint+'</label>'
      +'<select id="fmap-'+i+'" data-vendor="'+(m.vendor||'')+'" data-name="'+(m.name||'')+'" style="width:100%"></select>'
      +'</div>';
  }
  el.innerHTML=rows;
  // Fills the dropdowns (async, shared profile cache + vendor filter)
  for(var j=0;j<4;j++){ _fillMappingDropdown(j); }
}
function _fillMappingDropdown(slot){
  var sel=document.getElementById('fmap-'+slot);
  if(!sel) return;
  var wantKey=_profileKey(sel.dataset.vendor, sel.dataset.name);
  _loadOrcaFilaments(function(profiles){
    sel.innerHTML='<option value="">'+(tr('slot_edit_profile_default')||'Generic (Standard)')+'</option>';
    var userProfs=profiles.filter(function(p){return p.is_user;});
    var systemProfs=profiles.filter(function(p){return !p.is_user;});
    function _opt(g,p){
      var o=document.createElement('option');
      o.value=_profileKey(p.vendor,p.name);
      o.dataset.vendor=p.vendor; o.dataset.name=p.name; o.dataset.id=p.id||'';
      o.textContent=p.name+(p.vendor?' — '+p.vendor:'');
      if(o.value===wantKey)o.selected=true;
      g.appendChild(o);
    }
    if(userProfs.length){
      var gUser=document.createElement('optgroup');
      gUser.label=(tr('orca_profile_user_label')||'My profiles');
      userProfs.forEach(function(p){_opt(gUser,p);});
      sel.appendChild(gUser);
    }
    _loadVisibleVendors(function(vis){
      var filtered=systemProfs;
      if(vis&&vis.length){
        var allow={};vis.forEach(function(v){allow[v]=1;});allow['Generic']=1;
        filtered=systemProfs.filter(function(p){return allow[p.vendor];});
      }
      var byVendor={};
      filtered.forEach(function(p){(byVendor[p.vendor]=byVendor[p.vendor]||[]).push(p);});
      Object.keys(byVendor).sort().forEach(function(v){
        var g=document.createElement('optgroup');g.label=v;
        byVendor[v].forEach(function(p){_opt(g,p);});
        sel.appendChild(g);
      });
    });
  });
}
function saveFilamentMapping(){
  // Usa o endpoint por slot (vendor,name → busca do ID no backend).
  // Empty selection ("") = removes the mapping.
  var chain=Promise.resolve();
  for(var i=0;i<4;i++){
    (function(slot){
      var sel=document.getElementById('fmap-'+slot);
      var opt=sel?sel.options[sel.selectedIndex]:null;
      var vendor=(opt&&opt.dataset.vendor)||'';
      var name=(opt&&opt.dataset.name)||'';
      chain=chain.then(function(){
        return fetch(_apiUrl('/kx/filament/slots/'+slot+'/profile'),
          {method:'POST',headers:{'Content-Type':'application/json'},
           body:JSON.stringify({vendor:vendor,name:name})});
      });
    })(i);
  }
  chain.then(function(){
    clog(tr('log_filament_mapping_saved')||'Filament mapping saved','msg-ok');
    openSettings(); // recarrega → atualiza as dicas de ID
  }).catch(function(e){clog('Erro no mapeamento: '+e,'msg-err');});
}

// ── Vendor visibility filter (Issue #41 option A) ──
var _vendorChecklistSel={};  // {vendor:true} — selection in progress in the UI
function loadVendorChecklist(){
  // current backend selection, then renders all available vendors
  _visibleVendors=null; // invalida o cache
  _loadVisibleVendors(function(vis){
    _vendorChecklistSel={};
    (vis||[]).forEach(function(v){_vendorChecklistSel[v]=true;});
    renderVendorChecklist();
  });
}
function renderVendorChecklist(){
  var el=document.getElementById('visible-vendors-list');
  if(!el)return;
  _loadOrcaFilaments(function(profiles){
    // gathers all system vendors (without Generic — that one is always visible)
    var set={};
    profiles.forEach(function(p){ if(!p.is_user && p.vendor && p.vendor!=='Generic') set[p.vendor]=1; });
    var vendors=Object.keys(set).sort();
    var q=((document.getElementById('vendor-filter-search')||{}).value||'').toLowerCase();
    if(q)vendors=vendors.filter(function(v){return v.toLowerCase().indexOf(q)>=0;});
    el.innerHTML=vendors.map(function(v){
      var ck=_vendorChecklistSel[v]?'checked':'';
      var safe=v.replace(/"/g,'&quot;');
      return '<label style="display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer;font-size:13px">'
        +'<input type="checkbox" data-vendor="'+safe+'" '+ck+' onchange="_vendorCheck(this)" style="width:auto;margin:0"> '+v+'</label>';
    }).join('')||'<i style="color:var(--txt2)">–</i>';
  });
}
function _vendorCheck(cb){
  var v=cb.getAttribute('data-vendor');
  if(cb.checked)_vendorChecklistSel[v]=true; else delete _vendorChecklistSel[v];
}
function renderSpoolmanSlotCard(){
  var card=document.getElementById('spoolman-slot-card');
  var rows=document.getElementById('spoolman-slot-rows');
  if(!card||!rows)return;
  if(!_spoolmanStatus.configured){card.style.display='none';return;}
  card.style.display='';
  Promise.all([
    fetch(_apiUrl('/kx/spoolman/spools')).then(function(r){return r.json();}),
    fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json();})
  ]).then(function(res){
    var spools=res[0].spools||[];
    var slots=(res[1].result||[]).sort(function(a,b){return a.slot_index-b.slot_index;});
    if(!slots.length){rows.innerHTML='<span style="font-size:11px;color:var(--txt2)">'+tr('ams_no_slots','No AMS slots known.')+'</span>';return;}
    rows.innerHTML=slots.map(function(slot){
      var idx=parseInt(slot.slot_index);
      var col=slot.color_hex||'#888';
      var mat=slot.material||'';
      var current=_slotSpoolMap[String(idx)]||'';
      var opts='<option value="">–</option>'+spools.map(function(sp){
        var rem=sp.remaining_weight!=null?' ('+sp.remaining_weight.toFixed(0)+'g)':'';
        var vendor=sp.filament&&sp.filament.vendor?sp.filament.vendor.name+' ':'';
        var name=sp.filament?sp.filament.name:'Spool #'+sp.id;
        var mat2=sp.filament&&sp.filament.material?' · '+sp.filament.material:'';
        return '<option value="'+sp.id+'"'+(String(sp.id)==String(current)?' selected':'')+'>'+
               escHtml('#'+sp.id+' '+vendor+name+mat2+rem)+'</option>';
      }).join('');
      return '<div style="display:flex;align-items:center;gap:8px;font-size:12px">'+
        '<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:'+col+';border:1px solid var(--border);flex-shrink:0"></span>'+
        '<span style="color:var(--txt2);min-width:60px">Slot '+(idx+1)+' <span style="color:var(--txt2);font-size:10px">'+escHtml(mat)+'</span></span>'+
        '<select data-spool-slot="'+idx+'" style="flex:1;padding:3px 6px;border-radius:6px;border:1px solid var(--border);background:var(--raised);color:var(--txt);font-size:12px">'+opts+'</select></div>';
    }).join('');
  }).catch(function(){rows.innerHTML='<span style="font-size:11px;color:var(--err)">'+tr('spoolman_unreachable','Spoolman unreachable')+'</span>';});
}

function onAmsSpoolChange(sel){
  var idx=sel.getAttribute('data-spool-slot');
  var val=sel.value;
  if(val) _slotSpoolMap[String(idx)]=parseInt(val);
  else delete _slotSpoolMap[String(idx)];
  var mapping={};
  Object.keys(_slotSpoolMap).forEach(function(k){mapping[k]=_slotSpoolMap[k];});
  fetch(_apiUrl('/kx/spoolman/active-spool'),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({slot_spools:mapping})});
}

function saveSpoolmanSlots(){
  var mapping={};
  document.querySelectorAll('#spoolman-slot-rows select[data-spool-slot]').forEach(function(sel){
    var idx=sel.getAttribute('data-spool-slot');
    var val=sel.value;
    if(val)mapping[idx]=parseInt(val);
  });
  fetch(_apiUrl('/kx/spoolman/active-spool'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slot_spools:mapping})}).then(function(r){return r.json();}).then(function(d){
    _slotSpoolMap=d.slot_spools||{};
  });
}

function saveVisibleVendors(){
  var vendors=Object.keys(_vendorChecklistSel);
  fetch(_apiUrl('/kx/filament/visible_vendors'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({vendors:vendors})}).then(function(r){return r.json();}).then(function(){
    _visibleVendors=vendors.slice(); // atualiza o cache do dropdown
    clog(tr('log_visible_vendors_saved')||'Vendor selection saved','msg-ok');
  }).catch(function(e){clog('Erro no filtro de fabricantes: '+e,'msg-err');});
}

// ── Custom filament profile import (Issue #41) ──
function refreshUserProfileList(){
  var listEl=document.getElementById('orca-profiles-list');
  if(!listEl) return;
  fetch(_apiUrl('/kx/filament/profiles/user')).then(function(r){return r.json();}).then(function(d){
    var profs=(d && d.result)||[];
    if(!profs.length){
      listEl.innerHTML='<i style="color:var(--txt2)">'+(tr('orca_profile_user_empty')||'– none –')+'</i>';
      return;
    }
    listEl.innerHTML=profs.map(function(p){
      var label=escHtml(p.vendor+' / '+p.name+' ('+p.type+')');
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid var(--border)">'
        +'<span data-icon="star">'+label+'</span>'
        +'<button class="icon-btn danger" data-icon="trash-2" onclick="deleteUserProfile(\''+encodeURIComponent(p.vendor)+'\',\''+encodeURIComponent(p.name)+'\')" '
        +'title="'+escHtml(T.store_delete||'')+'"></button>'
        +'</div>';
    }).join('');
  }).catch(function(){});
}
function deleteUserProfile(vendor, name){
  fetch(_apiUrl('/kx/filament/profiles/user?vendor='+vendor+'&name='+name), {method:'DELETE'})
    .then(function(r){return r.json();})
    .then(function(){
      _orcaFilamentCache=null;
      refreshUserProfileList();
      // If the import dialog is open, refresh it there too
      refreshImportDialogList();
    });
}
function openProfileImport(){
  document.getElementById('profile-import-status').textContent='';
  refreshImportDialogList();
  document.getElementById('profile-import-modal').classList.add('open');
}
function closeProfileImport(){
  document.getElementById('profile-import-modal').classList.remove('open');
}
function refreshImportDialogList(){
  var el=document.getElementById('profile-import-list');
  if(!el) return;
  fetch(_apiUrl('/kx/filament/profiles/user')).then(function(r){return r.json();}).then(function(d){
    var profs=(d && d.result)||[];
    if(!profs.length){
      el.innerHTML='<i style="color:var(--txt2)">'+(tr('orca_profile_user_empty')||'– none –')+'</i>';
      return;
    }
    el.innerHTML=profs.map(function(p){
      var label=escHtml(p.vendor+' / '+p.name+' ('+p.type+')');
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 6px;border-bottom:1px solid var(--border)">'
        +'<span data-icon="star">'+label+'</span>'
        +'<button class="icon-btn danger" data-icon="trash-2" onclick="deleteUserProfile(\''+encodeURIComponent(p.vendor)+'\',\''+encodeURIComponent(p.name)+'\')" '
        +'title="'+escHtml(T.store_delete||'')+'"></button>'
        +'</div>';
    }).join('');
  }).catch(function(){});
}
function doProfileImportUpload(files){
  if(!files || !files.length) return;
  var status=document.getElementById('profile-import-status');
  status.textContent=(tr('orca_profile_uploading')||'Lade hoch…');
  status.style.color='var(--txt2)';
  var done=0, totalAdded=0, totalSkipped=0;
  function _one(idx){
    if(idx>=files.length){
      status.textContent=(tr('orca_profile_done')||'Importiert')+': '+totalAdded
        +(totalSkipped?' / '+totalSkipped+' '+(tr('orca_profile_skipped')||'skipped'):'');
      status.style.color='var(--ok)';
      _orcaFilamentCache=null;
      refreshImportDialogList();
      refreshUserProfileList();
      // Rebuilds the vendor checklist — an import can bring in a
      // system vendor unknown until then (Issue #41).
      if(document.getElementById('visible-vendors-list')) renderVendorChecklist();
      // If slot editing is open, fill the dropdown again right away
      var mat=document.getElementById('slot-edit-mat');
      if(mat && document.getElementById('slot-edit-modal').classList.contains('open')){
        _fillSlotProfileDropdown(mat.value, '', '');
      }
      return;
    }
    var fd=new FormData();
    fd.append('file', files[idx]);
    fetch(_apiUrl('/kx/filament/profiles/user'), {method:'POST', body:fd})
      .then(function(r){return r.json();})
      .then(function(d){
        totalAdded += (d.added||0);
        totalSkipped += (d.skipped||0);
        done++;
        _one(idx+1);
      })
      .catch(function(e){
        status.textContent=tr('error_prefix','Error: ')+e;
        status.style.color='var(--err)';
      });
  }
  _one(0);
}

// ── AMS slot editing ──
var _slotEditIndex=-1;
var _slotEditLoaded=false;
var _MAT_PRESETS=['PLA','PLA+','PLA SILK','PLA MATTE','PETG','ABS','ASA','TPU','PA','PC','HIPS'];
function _normalizeMat(m){
  var s=m.toUpperCase().trim().replace(/-/g,' ').replace(/_/g,' ');
  var aliases={'PLAPLUS':'PLA+','PLA PLUS':'PLA+','SILK PLA':'PLA SILK','PLASILK':'PLA SILK',
    'PLA MATTE':'PLA','PLA MARBLE':'PLA','PLA WOOD':'PLA','TPE':'TPU',
    'PETG PLUS':'PETG+','PA6':'PA','PA12':'PA','PA66':'PA'};
  return aliases[s]||s;
}
var _BASE_MATERIAL_TYPES=['PLA','PETG','ABS','ASA','TPU','TPE','PA','PC','HIPS','PEI','PEEK'];
function updateSlotEditFeedButton(){
  var btn=document.getElementById('btn-slot-edit-feed');
  if(!btn)return;
  if(_slotEditIndex<0){
    btn.style.display='none';
    return;
  }
  btn.style.display='';
  btn.textContent=_slotEditLoaded?tr('slot_edit_unload'):tr('slot_edit_load');
}
var _orcaFilamentCache=null;  // [{id,name,vendor,type,color}, …]
var _visibleVendors=null;     // filtro de visibilidade de fabricantes (Issue #41); [] = todos
function _loadVisibleVendors(cb){
  if(_visibleVendors!==null){ cb(_visibleVendors); return; }
  fetch(_apiUrl('/kx/filament/visible_vendors')).then(function(r){return r.json();}).then(function(d){
    _visibleVendors=d.result||[];
    cb(_visibleVendors);
  }).catch(function(){ _visibleVendors=[]; cb([]); });
}
function _loadOrcaFilaments(cb){
  if(_orcaFilamentCache){ cb(_orcaFilamentCache); return; }
  fetch(_apiUrl('/kx/filament/profiles')).then(function(r){return r.json();}).then(function(d){
    _orcaFilamentCache=d.result||[];
    cb(_orcaFilamentCache);
  }).catch(function(){ cb([]); });
}
function _profileKey(vendor, name){
  // Single selector: (vendor, name). The ids in orca_filaments.json are NOT
  // unique (e.g. 136 profiles with OGFL99). We encode both in the
  // <option> value string with | as the separator.
  return (vendor||'')+'|'+(name||'');
}
function _fillSlotProfileDropdown(material, currentVendor, currentName){
  var sel=document.getElementById('slot-edit-profile');
  if(!sel) return;
  var wantKey=_profileKey(currentVendor, currentName);
  _loadOrcaFilaments(function(profiles){
    // Type filter: only shows profiles of the right material (e.g. PLA → all PLA variants)
    var matU=(material||'').toUpperCase().trim();
    // PLA variants: the printer reports "PLA SILK"/"PLA+"/"PLA MATTE", OrcaSlicer
    // guarda tudo sob type=PLA — palavra-chave do nome como filtro extra.
    var _PLA_VARIANT_KW={'PLA SILK':'silk','PLA+':'pla+','PLA MATTE':'matte',
      'PLA MARBLE':'marble','PLA WOOD':'wood'};
    var variantKw=_PLA_VARIANT_KW[matU]||null;
    var baseMat=variantKw?'PLA':matU;
    var matched=profiles.filter(function(p){
      var pt=(p.type||'').toUpperCase();
      var nameL=(p.name||'').toLowerCase();
      // O tipo base precisa bater
      var typeOk=baseMat===''||pt===baseMat||pt.startsWith(baseMat+'-')||pt.startsWith(baseMat+' ');
      if(!typeOk) return false;
      // Em variante: o nome precisa conter a palavra-chave (ex.: "silk", "matte", "pla+")
      if(variantKw) return nameL.indexOf(variantKw)!==-1;
      return true;
    });
    _slotProfileList=matched;
    sel.innerHTML='<option value="">'+tr('slot_edit_profile_default')+'</option>';
    // User profiles (is_user) first — their own optgroup '★ Mine' in first place.
    var userProfs=matched.filter(function(p){return p.is_user;});
    var systemProfs=matched.filter(function(p){return !p.is_user;});
    function _appendOption(g, p){
      var o=document.createElement('option');
      o.value=_profileKey(p.vendor, p.name);
      o.dataset.vendor=p.vendor;
      o.dataset.name=p.name;
      o.dataset.id=p.id || '';
      o.dataset.color=p.color || '';
      o.textContent=p.name;
      if(o.value===wantKey) o.selected=true;
      g.appendChild(o);
    }
    if(userProfs.length){
      var gUser=document.createElement('optgroup');
      gUser.label=(tr('orca_profile_user_label')||'My profiles');
      userProfs.forEach(function(p){ _appendOption(gUser, p); });
      sel.appendChild(gUser);
    }
    // Vendor visibility filter (Issue #41 option A): only the chosen vendors +
    // Generic. Empty list = all (compatible with old versions). Own profiles (is_user)
    // are already included above unconditionally.
    _loadVisibleVendors(function(vis){
      var filtered=systemProfs;
      if(vis&&vis.length){
        var allow={};vis.forEach(function(v){allow[v]=1;});
        allow['Generic']=1;
        filtered=systemProfs.filter(function(p){return allow[p.vendor];});
      }
      var byVendor={};
      filtered.forEach(function(p){ (byVendor[p.vendor]=byVendor[p.vendor]||[]).push(p); });
      Object.keys(byVendor).sort().forEach(function(v){
        var g=document.createElement('optgroup'); g.label=v;
        byVendor[v].forEach(function(p){ _appendOption(g, p); });
        sel.appendChild(g);
      });
      _renderProfilePalette();
    });
  });
}

// ── Paleta de cores do fabricante ──
// Per-color profiles follow "<Brand> <Line> - <Color>" (e.g. profiles/brasil). With a
// line or a color chosen in the dropdown, it shows the sibling colors of the same line;
// clicking picks the profile of that color and applies the color to the slot.
var _slotProfileList=[];
function _profileLine(name){ return (name||'').split(' - ')[0]; }
function _renderProfilePalette(){
  var el=document.getElementById('slot-profile-palette');
  var sel=document.getElementById('slot-edit-profile');
  if(!el||!sel) return;
  var o=sel.options[sel.selectedIndex];
  var vendor=o&&o.dataset.vendor, line=o&&_profileLine(o.dataset.name);
  var irmas=vendor?_slotProfileList.filter(function(p){
    return p.vendor===vendor&&p.color&&p.name.indexOf(' - ')!==-1&&_profileLine(p.name)===line;
  }):[];
  if(!irmas.length){ el.style.display='none'; el.innerHTML=''; return; }
  el.style.display='flex';
  el.innerHTML='';
  irmas.forEach(function(p){
    var d=document.createElement('div');
    var ativo=o.dataset.name===p.name;
    d.title=p.name.split(' - ').slice(1).join(' - ')+' '+p.color;
    d.style.cssText='width:22px;height:22px;border-radius:4px;cursor:pointer;flex-shrink:0;background:'+p.color+
      ';border:2px solid '+(ativo?'var(--accent)':'rgba(128,128,128,.35)');
    d.onclick=function(){ sel.value=_profileKey(p.vendor,p.name); onSlotProfileChange(); };
    el.appendChild(d);
  });
}
function onSlotProfileChange(){
  var sel=document.getElementById('slot-edit-profile');
  var o=sel&&sel.options[sel.selectedIndex];
  if(o&&o.dataset.color) slotPickSwatch(o.dataset.color);
  _renderProfilePalette();
}
// ── Seletor de cor Pickr ────────────────────────────────────────────────────
var _pickr=null;

function _initPickr(hex){
  // destroys the previous instance, if any
  if(_pickr){ try{ _pickr.destroyAndRemove(); }catch(e){} _pickr=null; }
  var anchor=document.getElementById('slot-pickr-anchor');
  if(!anchor||typeof Pickr==='undefined') return;
  // new button element so Pickr can mount
  anchor.innerHTML='<div id="slot-pickr-btn"></div>';
  _pickr=Pickr.create({
    el:'#slot-pickr-btn',
    theme:'nano',
    default: hex||'#808080',
    inline: true,
    showAlways: true,
    components:{
      preview:true, opacity:false, hue:true,
      interaction:{ hex:true, rgba:false, input:true, save:false, clear:false }
    }
  });
  _pickr.on('change',function(color){
    var h=color.toHEXA().toString().slice(0,7);
    document.getElementById('slot-edit-color').value=h;
    document.getElementById('slot-edit-preview').style.background=h;
  });
  // Adjusts the theme: Pickr uses its own CSS variables, we override them via style
  requestAnimationFrame(function(){
    var el=anchor.querySelector('.pickr');
    if(el) el.style.cssText='width:100%';
    var app=anchor.querySelector('.pcr-app');
    if(app){
      app.style.cssText='position:relative;width:100%;box-shadow:none;background:transparent';
      var btn=app.querySelector('.pcr-result');
      if(btn) btn.style.cssText='background:var(--raised);border:1px solid var(--border);color:var(--txt);border-radius:6px;font-size:12px';
    }
  });
}

// ── Color swatches (localStorage, max. 16) ─────────────────────────────────
var _SWATCH_KEY='kxb_color_swatches';
var _SWATCH_MAX=16;

function _loadSwatches(){
  try{ return JSON.parse(localStorage.getItem(_SWATCH_KEY)||'[]'); }catch(e){ return []; }
}
function _saveSwatches(arr){ try{ localStorage.setItem(_SWATCH_KEY, JSON.stringify(arr)); }catch(e){} }

function _addSwatch(hex){
  var arr=_loadSwatches().filter(function(c){ return c.toLowerCase()!==hex.toLowerCase(); });
  arr.unshift(hex);
  if(arr.length>_SWATCH_MAX) arr=arr.slice(0,_SWATCH_MAX);
  _saveSwatches(arr);
}

function _renderSwatches(){
  var el=document.getElementById('slot-color-swatches');
  if(!el) return;
  var arr=_loadSwatches();
  if(!arr.length){ el.style.display='none'; return; }
  el.style.display='flex';
  el.innerHTML=arr.map(function(c){
    return '<div title="'+c+'" onclick="slotPickSwatch(\''+c+'\')" style="width:22px;height:22px;border-radius:4px;background:'+c+
      ';border:2px solid rgba(255,255,255,.2);cursor:pointer;flex-shrink:0"></div>';
  }).join('');
}

function slotPickSwatch(hex){
  if(_pickr){ _pickr.setColor(hex); }
  var ci=document.getElementById('slot-edit-color');
  if(ci) ci.value=hex;
  document.getElementById('slot-edit-preview').style.background=hex;
}

// ── Copiar cor de outro slot ────────────────────────────────────────────────
function _renderCopyFromSlot(currentGlobalIdx){
  var slots=(window._amsSlots||[]).filter(function(s){
    return s.global_index!==currentGlobalIdx && s.status==5 && Array.isArray(s.color);
  });
  var row=document.getElementById('slot-copy-row');
  var sel=document.getElementById('slot-copy-select');
  if(!row||!sel) return;
  if(!slots.length){ row.style.display='none'; return; }
  row.style.display='';
  var ph=document.getElementById('lbl-slot-copy-from');
  var phTxt=ph?ph.textContent:(T.slot_copy_from||'Copy color from slot…');
  sel.innerHTML='<option value="">'+phTxt+'</option>'+slots.map(function(s){
    var rgb=s.color;
    var hex='#'+rgb.map(function(v){return('0'+Math.min(255,v).toString(16)).slice(-2)}).join('');
    return '<option value="'+hex+'">Slot '+(s.global_index+1)+' — '+(s.type||'?')+' '+hex+'</option>';
  }).join('');
}

function slotCopyColor(sel){
  if(!sel.value) return;
  var ci=document.getElementById('slot-edit-color');
  if(!ci) return;
  ci.value=sel.value;
  document.getElementById('slot-edit-preview').style.background=sel.value;
  sel.selectedIndex=0;
}

// ───────────────────────────────────────────────────────────────────────────

function openSlotEdit(i){
  var slot=(window._amsSlots||[])[i]||{};
  var globalIdx=slot.global_index!=null?slot.global_index:(slot.index!=null?slot.index:i);
  _slotEditIndex=globalIdx;
  _slotEditLoaded=(S.ams_loaded_slot!=null&&S.ams_loaded_slot===globalIdx);
  document.getElementById('slot-edit-title').textContent=T.slot_edit_title+' '+(globalIdx+1);
  var rgb=Array.isArray(slot.color)?slot.color:[128,128,128];
  var hex='#'+rgb.map(function(v){return('0'+Math.min(255,v).toString(16)).slice(-2)}).join('');
  var ci=document.getElementById('slot-edit-color');
  ci.value=hex;
  document.getElementById('slot-edit-preview').style.background=hex;
  _initPickr(hex);
  _renderSwatches();
  _renderCopyFromSlot(globalIdx);
  var mat=(slot.type||'PLA').toUpperCase();
  document.getElementById('slot-edit-mat').value=mat;
  // Normalizes to highlight buttons: maps PLA variants to the closest preset
  var matNorm=_normalizeMat(mat);
  var btns=document.getElementById('slot-mat-btns');
  btns.innerHTML=_MAT_PRESETS.map(function(m){
    var active=m===mat||m===matNorm;
    return '<button class="mat-preset-btn" data-mat="'+m+'" onclick="selectMatPreset(\''+m+'\')" '
      +'style="padding:4px 10px;border-radius:6px;border:1px solid var(--border);cursor:pointer;font-size:12px;'
      +(active?'background:var(--accent);color:#fff':'background:var(--raised);color:var(--txt2)')+'">'+m+'</button>';
  }).join('');
  // OrcaSlicer profile dropdown: fetches the user's current override for this slot
  // from /kx/filament/slots (contains vendor+name+id).
  fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json();}).then(function(d){
    var arr=d.result||[];
    var entry=arr.find(function(x){return x.slot_index===globalIdx;})||{};
    window._slotProfileMap=window._slotProfileMap||{};
    window._slotProfileMap[globalIdx]={
      id:    entry.filament_id||'',
      vendor:entry.filament_vendor||'',
      name:  entry.filament_name||'',
    };
    _fillSlotProfileDropdown(mat, entry.filament_vendor||'', entry.filament_name||'');
  }).catch(function(){ _fillSlotProfileDropdown(mat,'',''); });
  updateSlotEditFeedButton();
  document.getElementById('slot-edit-modal').classList.add('open');
}
function closeSlotEdit(){
  _slotEditIndex=-1;
  document.getElementById('slot-edit-modal').classList.remove('open');
}
function slotEditFeed(){
  if(_slotEditIndex<0)return;
  var type=_slotEditLoaded?2:1;
  amsFeed(type,_slotEditIndex)
    .then(function(){
      _slotEditLoaded=!_slotEditLoaded;
      updateSlotEditFeedButton();
      poll();
    })
    .catch(function(){});
}
function startReadyFile(filename){
  var fn=filename||S.file_ready;
  function _doStartReadyFile(){
    var btn=document.getElementById('file-ready-btn');
    if(btn){btn.disabled=true;btn.textContent='…';}
    post('/printer/print/start',{filename:fn})
      .then(function(r){return r.json();})
      .then(function(){
        document.getElementById('file-ready-banner').style.display='none';
        if(btn){btn.disabled=false;setText('file-ready-btn',T.file_ready_btn);}
      })
      .catch(function(e){
        clog(tr('log_error')+' '+e,'msg-err');
        if(btn){btn.disabled=false;setText('file-ready-btn',T.file_ready_btn);}
      });
  }
  function _gateAndStart(fileObj){
    if(fileObj && fileObj.web_unverified && webUploadWarningEnabled()){
      maybeGateWebUpload(fileObj, function(){ startReadyFile(fn); });
      return;
    }
    _doStartReadyFile();
  }
  var currentFile=(storeFiles||[]).find(function(f){return f.filename===fn;});
  if(currentFile){
    _gateAndStart(currentFile);
    return;
  }
  fetch(_apiUrl('/kx/files')).then(function(r){return r.json();}).then(function(d){
    storeFiles=d.result||[];
    var refreshed=(storeFiles||[]).find(function(f){return f.filename===fn;})||null;
    _gateAndStart(refreshed);
  }).catch(function(){
    _doStartReadyFile();
  });
}
function cancelReadyFile(){
  post('/api/file_ready/clear',{})
    .then(function(){document.getElementById('file-ready-banner').style.display='none';});
}

// ── Actions for loaded/idle file in the progress card (Issue #55) ──
function startIdleFile(){
  if(_lastLoadedFile) startReadyFile(_lastLoadedFile);
}
function startIdleFileWithSlots(){
  if(_lastLoadedFile) startReadyFileWithSlots(_lastLoadedFile);
}
function clearIdleFile(){
  _lastLoadedFile=null;
  _idleCleared=true;  // prevents reloading s.filename on the next poll() (Issue #57)
  _fdAutoOpenedFile=null;  // the next upload of the same file should open the dialog again
  _fdUserCancelled=false;
  _fdDialogOpen=false;
  sessionStorage.removeItem('fdAutoOpenedFile');
  sessionStorage.removeItem('fdUserCancelled');
  sessionStorage.removeItem('webVerifyCancelledFileId');
  S.file_ready=''; S.filename=''; S.thumbnail='';  // clears locally right away, without waiting for the next poll
  var ib=document.getElementById('d-idle-btns');if(ib)ib.style.display='none';
  var fn=document.getElementById('d-fname');if(fn){fn.textContent='–';fn.title='';}
  var thumb=document.getElementById('d-thumbnail');if(thumb){thumb.style.display='none';thumb.src='';}
  post('/api/file_ready/clear',{}).catch(function(){});
}
function selectMatPreset(m){
  document.getElementById('slot-edit-mat').value=m;
  highlightMatBtn(m);
  // Adjusts the filament profile dropdown to the new material
  // (resets the previous selection — profiles of another material do not fit)
  _fillSlotProfileDropdown(m, '', '');
}
function highlightMatBtn(val){
  document.querySelectorAll('.mat-preset-btn').forEach(function(b){
    var on=b.getAttribute('data-mat')===val.toUpperCase();
    b.style.background=on?'var(--accent)':'var(--raised)';
    b.style.color=on?'#fff':'var(--txt2)';
  });
  // Also on manual typing in the material field: updates the dropdown.
  if(val) _fillSlotProfileDropdown(val, '', '');
}
function hexToRgb(hex){
  var r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return[r,g,b];
}
function saveSlotEdit(){
  var hex=document.getElementById('slot-edit-color').value;
  _addSwatch(hex);
  var mat=document.getElementById('slot-edit-mat').value.trim().toUpperCase()||'PLA';
  var color=hexToRgb(hex);
  var slotIdx=_slotEditIndex;
  var profSel=document.getElementById('slot-edit-profile');
  var sel=profSel && profSel.selectedOptions && profSel.selectedOptions[0];
  // Main selector: (vendor, name). The id is only a hint (comes from the data-attr of the
  // JSON — the backend looks it up again on its own to correct
  // dicas desatualizadas).
  var newProfVendor=sel?(sel.dataset.vendor||''):'';
  var newProfName  =sel?(sel.dataset.name  ||''):'';
  var newProfId    =sel?(sel.dataset.id    ||''):'';
  // Saves in sequence: first the profile override (config.ini), then material/color
  // (MQTT to the printer). Otherwise the two paths can trip over each other and the slot state
  // fica inconsistente ao reabrir.
  fetch(_apiUrl('/kx/filament/slots/'+slotIdx+'/profile'),{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({vendor:newProfVendor, name:newProfName, id:newProfId})
  })
  .then(function(r){return r.json();})
  .then(function(){
    window._slotProfileMap=window._slotProfileMap||{};
    if(newProfVendor && newProfName){
      window._slotProfileMap[slotIdx]={id:newProfId, vendor:newProfVendor, name:newProfName};
    } else {
      delete window._slotProfileMap[slotIdx];
    }
    return post('/api/ams/set_slot',{index:slotIdx,type:mat,color:color});
  })
  .then(function(r){return r?r.json():null;})
  .then(function(){
    // Updates the slot map so the card shows the vendor right away.
    return fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json();});
  })
  .then(function(d){
    var arr=(d && d.result)||[];
    window._slotProfileMap={};
    arr.forEach(function(e){
      if(e.filament_vendor && e.filament_name){
        window._slotProfileMap[e.slot_index]={
          id:    e.filament_id||'',
          vendor:e.filament_vendor,
          name:  e.filament_name,
        };
      }
    });
    closeSlotEdit();
    var profSuffix=newProfName?(' ['+newProfVendor+' '+newProfName+']'):'';
    clog(tr('slot_edit_ok')+' '+(slotIdx+1)+': '+mat+' '+hex+profSuffix,'msg-ok');
    // Immediate re-render with the current _slotProfileMap (poll() is async
    // and re-renders on the next tick — but we want the vendor
    // badge to show NOW).
    if(typeof applyState==='function') applyState();
    if(typeof poll==='function') poll();
  })
  .catch(function(e){clog('Erro: '+e,'msg-err');});
}
document.addEventListener('DOMContentLoaded',function(){
  document.getElementById('s-printer-ip').addEventListener('input',function(){
    var hint=document.getElementById('lbl-ip-hint');
    if(this.value.includes(':')){hint.textContent=T.hint_ip_no_port;hint.style.display='block';}
    else{hint.style.display='none';}
  });
});
function setPoll(ms){
  localStorage.setItem('pollInterval',ms);
  clearInterval(pollTimer);
  pollTimer=setInterval(poll,ms);
}
function saveSettings(){
  var btn=document.getElementById('btn-save-settings');
  btn.disabled=true;btn.textContent='…';
  var webUploadWarning=(document.getElementById('s-web-upload-warning')||{}).checked?1:0;
  S.web_upload_warning=webUploadWarning;
  // Switching the print-start behavior could block the auto-open forever
  // (old _fdUserCancelled with the same file_ready) → resets the dialog state (Issue #57).
  _fdUserCancelled=false;_fdAutoOpenedFile=null;
  sessionStorage.removeItem('fdUserCancelled');sessionStorage.removeItem('fdAutoOpenedFile');
  sessionStorage.removeItem('webVerifyCancelledFileId');
  post('/api/settings',{
    printer_name:     document.getElementById('s-printer-name').value,
    printer_ip:       document.getElementById('s-printer-ip').value,
    mqtt_port:        parseInt(document.getElementById('s-mqtt-port').value)||9883,
    username:         document.getElementById('s-username').value,
    password:         document.getElementById('s-password').value,
    device_id:        document.getElementById('s-device-id').value,
    mode_id:          document.getElementById('s-mode-id').value,
    power_on_url:     (document.getElementById('s-power-on-url')||{}).value||'',
    power_off_url:    (document.getElementById('s-power-off-url')||{}).value||'',
    power_status_url: (document.getElementById('s-power-status-url')||{}).value||'',
    power_status_inverted: (document.getElementById('s-power-status-inverted')||{}).checked?1:0,
    notify_url:       (document.getElementById('s-notify-url')||{}).value||'',
    notify_lang:      currentLang,
    default_ams_slot: document.getElementById('s-default-slot').value,
    auto_leveling:           document.getElementById('s-auto-leveling').checked?1:0,
    vibration_compensation:  (document.getElementById('s-vibration-compensation')||{}).checked?1:0,
    camera_on_print:         (document.getElementById('s-camera-on-print')||{}).checked?1:0,
    print_start_dialog: parseInt((document.getElementById('s-file-ready-mode')||{}).value||'1',10),
    web_upload_warning:webUploadWarning,
    delete_printer_file_after_print: (document.getElementById('s-delete-printer-file-after-print')||{}).checked?1:0,
    poll_interval:    Math.min(60,Math.max(1,parseInt((document.getElementById('s-poll-interval')||{}).value,10)||3)),
    verbose_http_log: (document.getElementById('s-verbose-http-log')||{}).checked?1:0,
    job_log_keep:          (document.getElementById('s-job-log-keep')||{}).value,
    job_log_context_lines: (document.getElementById('s-job-log-context')||{}).value,
    log_buffer_lines:      (document.getElementById('s-log-buffer')||{}).value,
    spoolman_server:  (document.getElementById('s-spoolman-url')||{}).value||'',
    spoolman_sync_rate: Math.max(0,parseInt((document.getElementById('s-spoolman-sync-rate')||{}).value||'30',10)),
  }).then(function(){
    btn.textContent=T.update_restarting;
    setTimeout(function(){
      btn.disabled=false;
      setText('btn-save-settings',T.settings_save);
      closeSettings();
      _loadSpoolmanStatus();
      poll();
    },4000);
  }).catch(function(e){
    btn.disabled=false;setText('btn-save-settings',T.settings_save);
    clog('Erro nas configurações: '+e,'msg-err');
  });
}
// ── Acesso / Login ──
// Own endpoint (/api/auth): the password does not travel with the rest of the
// settings and goes to config.ini only as a hash.
function _authFill(d){
  var en=document.getElementById('s-auth-enabled'); if(!en) return;
  en.checked=!!d.auth_enabled;
  document.getElementById('s-auth-user').value=d.auth_user||'';
  document.getElementById('s-auth-password').value='';
  document.getElementById('s-auth-password').placeholder=d.auth_enabled?tr('settings_auth_password_keep'):'';
  document.getElementById('s-auth-api-key').value=d.auth_api_key||'';
  document.getElementById('auth-status').textContent='';
  _authToggleFields();
  _fillCamUrls(d);
  _fillApiGuide();
}
// ── API tab: base address + examples with the real key ──
function _fillApiGuide(){
  var ex=document.getElementById('api-examples');if(!ex)return;
  var rel=_apiUrl('');var base=/^https?:/.test(rel)?rel.replace(/\/+$/,''):location.origin;
  var key=document.getElementById('s-auth-api-key').value.trim();
  var h=key&&document.getElementById('s-auth-enabled').checked?' -H "X-Api-Key: '+key+'"':'';
  document.getElementById('api-base-url').value=base;
  ex.textContent='curl'+h+' '+base+'/api/state\n'+
    'curl'+h+' "'+base+'/printer/objects/query?print_stats&display_status"\n'+
    'curl'+h+' -F "file=@peca.gcode" '+base+'/server/files/upload\n'+
    'curl'+h+' -X POST '+base+'/printer/print/pause';
}
// ── Camera for OBS: camera-only URLs, in OctoPrint format ──
function _fillCamUrls(d){
  // Absolute URL of the active printer (multi-printer = another port).
  var rel=_apiUrl('');var base=/^https?:/.test(rel)?rel.replace(/\/+$/,''):location.origin;
  var tok=(d.auth_enabled&&d.auth_camera_token)?'&token='+encodeURIComponent(d.auth_camera_token):'';
  var st=document.getElementById('cam-url-stream'),sn=document.getElementById('cam-url-snapshot');
  if(st)st.value=base+'/webcam/?action=stream'+tok;
  if(sn)sn.value=base+'/webcam/?action=snapshot'+tok;
  var nb=document.getElementById('btn-cam-token-new');if(nb)nb.style.display=d.auth_enabled?'':'none';
}
function copyCamUrl(id){
  var i=document.getElementById(id);if(!i)return;
  i.select();
  (navigator.clipboard?navigator.clipboard.writeText(i.value):Promise.reject()).catch(function(){try{document.execCommand('copy');}catch(e){}})
    .then(function(){clog((tr('copied')||'Copiado')+(i.readOnly?': '+i.value.split('?')[0]:''),'msg-ok');});
}
function newCameraToken(){
  if(!confirm(tr('cam_token_confirm')||'Generate a new token? The old camera links (OBS etc.) will stop working.'))return;
  post('/api/auth/camera-token',{}).then(function(){setTimeout(function(){location.reload();},4000);});
}
function _authToggleFields(){
  var on=document.getElementById('s-auth-enabled').checked;
  document.getElementById('auth-fields').style.display=on?'':'none';
  // The key only works with login enabled (without login the API is already open).
  document.getElementById('btn-api-save').style.display=on?'':'none';
  document.getElementById('api-login-off').textContent=on?'':tr('settings_api_login_off');
}
function _authGenerateKey(){
  var b=new Uint8Array(24); crypto.getRandomValues(b);
  document.getElementById('s-auth-api-key').value=Array.from(b,function(x){return ('0'+x.toString(16)).slice(-2);}).join('');
  _fillApiGuide();
}
function saveAuth(statusId){
  var on=document.getElementById('s-auth-enabled').checked;
  var st=document.getElementById(statusId||'auth-status');
  if(!on&&!confirm(tr('settings_auth_confirm_off')))return;
  st.style.color='var(--txt2)'; st.textContent='…';
  post('/api/auth',{enabled:on,user:document.getElementById('s-auth-user').value,
    password:document.getElementById('s-auth-password').value,
    api_key:document.getElementById('s-auth-api-key').value})
  .then(function(r){
    if(r&&r.error){st.style.color='var(--err)';st.textContent=srvMsg(r.error);return;}
    st.style.color='var(--ok)';st.textContent=tr('settings_auth_saved');
    setTimeout(function(){location.reload();},4000);
  }).catch(function(e){st.style.color='var(--err)';st.textContent=tr('log_error')+' '+e;});
}

// ── Poll ──
async function poll(){
  try{
    var r=await fetch(_apiUrl('/api/state'));
    if(!r.ok)return;
    var d=await r.json();
    Object.assign(S,d);
    applyState();
    updateHistory();
  }catch(e){clog(T.log_poll_error+' '+e,'msg-err')}
}
var pollTimer;
(function(){
  var ms=parseInt(localStorage.getItem('pollInterval')||'2000');
  initPrinters();
  // Loads the slot profile map at startup, otherwise the cards do not show
  // the vendor badge on the first render even when there is an override in config.ini.
  fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json();}).then(function(d){
    var arr=(d && d.result)||[];
    window._slotProfileMap={};
    arr.forEach(function(e){
      if(e.filament_vendor && e.filament_name){
        window._slotProfileMap[e.slot_index]={
          id:    e.filament_id||'',
          vendor:e.filament_vendor,
          name:  e.filament_name,
        };
      }
    });
  }).catch(function(){});
  poll();pollTimer=setInterval(poll,ms);
  setInterval(_loadSpoolmanStatus,30000);
  setInterval(_refreshHeaderPower,30000);
})();

// ── Print actions ──
function printAction(a){
  post('/printer/print/'+a,{}).then(function(){clog('Impressão: '+a,'msg-ok');poll()})
    .catch(function(e){clog('Erro: '+e,'msg-err')});
}
function togglePauseResume(){
  // Printing → pause; paused → resume. The status comes from the last print_state
  // queried in S; when in doubt (no state) Pause is the default.
  var state=(S && S.print_state)||'';
  if(state==='paused') printAction('resume');
  else                 printAction('pause');
}
function updatePauseResumeBtn(){
  var btn=document.getElementById('d-btn-pause');
  if(!btn) return;
  var state=(S && S.print_state)||'';
  if(state==='paused'){
    btn.textContent=T.btn_resume||'Retomar';
    btn.dataset.icon='play';
    btn.classList.add('btn-resume');
    btn.classList.remove('btn-pause');
  } else {
    btn.textContent=T.btn_pause||'Pausar';
    btn.dataset.icon='pause';
    btn.classList.add('btn-pause');
    btn.classList.remove('btn-resume');
  }
}
function confirmCancel(){if(confirm(T.confirm_cancel||'Really cancel the print?'))printAction('cancel')}

// ── Movimento dos eixos ──
// axis codes: 0=X, 1=Y, 2=Z
// move_type 1=relative, positive/negative distance
function getStep(){return currentStep}
function setStep(btn,v){
  currentStep=v;
  document.querySelectorAll('.step-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  var ci=document.getElementById('step-custom');
  if(ci)ci.value='';
}
function setStepCustom(inp){
  var v=parseFloat(inp.value);
  if(!isNaN(v)&&v>0){
    currentStep=v;
    document.querySelectorAll('.step-btn').forEach(b=>b.classList.remove('active'));
  }
}
function move(axis,dir,dist){
  // axis: 0=X,1=Y,2=Z → printer axis codes: 1=X,2=Y,3=Z
  var axisMap={0:1,1:2,2:3};
  post('/api/axis',{axis:axisMap[axis],move_type:1,distance:dir*dist})
    .then(function(){clog('Eixo '+(axis===0?'X':axis===1?'Y':'Z')+' '+(dir>0?'+':'')+dir*dist+'mm','msg-ok')})
    .catch(function(e){clog('Erro no eixo: '+e,'msg-err')});
}
function homeAll(){
  post('/api/axis',{axis:5,move_type:2,distance:0})
    .then(function(){clog('Home em todos','msg-ok')})
    .catch(function(e){clog('Erro no home: '+e,'msg-err')});
}
function homeXY(){
  post('/api/axis',{axis:4,move_type:2,distance:0})
    .then(function(){clog('Home XY','msg-ok')})
    .catch(function(e){clog('Erro no home: '+e,'msg-err')});
}
function homeZ(){
  post('/api/axis',{axis:3,move_type:2,distance:0})
    .then(function(){clog('Home Z','msg-ok')})
    .catch(function(e){clog('Erro no home: '+e,'msg-err')});
}
function disableMotors(){
  post('/api/axis',{action:'turnOff'})
    .then(function(){clog('Motores desligados','msg-ok')})
    .catch(function(e){clog('Erro nos motores: '+e,'msg-err')});
}

// ── Temperatura ──
function setNozzle(){
  var v=parseFloat(document.getElementById('p-nozzle-inp').value||0);
  post('/api/temperature',{nozzle:v,bed:S.bed_target})
    .then(function(){clog('Bico → '+v+'°C','msg-ok')})
    .catch(function(e){clog('Erro de temperatura: '+e,'msg-err')});
}
function setBed(){
  var v=parseFloat(document.getElementById('p-bed-inp').value||0);
  post('/api/temperature',{nozzle:S.nozzle_target,bed:v})
    .then(function(){clog(T.label_bed+' → '+v+'°C','msg-ok')})
    .catch(function(e){clog('Erro de temperatura: '+e,'msg-err')});
}

// ── Luz ──
function setLight(){
  var on=document.getElementById('d-light-toggle').checked;
  post('/api/light',{on:on,brightness:80})
    .then(function(){clog('Luz '+(on?'ligada, 80%':'desligada'),'msg-ok')})

    .catch(function(e){clog('Erro na luz: '+e,'msg-err')});
}

// ── Print speed ──
function setSpeed(mode){
  S.print_speed_mode=mode;
  [1,2,3].forEach(function(m){
    var b=document.getElementById('d-spd-'+m);
    if(b) b.classList.toggle('spd-active',m===mode);
  });
  post('/api/speed',{mode:mode})
    .catch(function(e){clog('Erro de velocidade: '+e,'msg-err')});
}

// ── Ventoinha ──
function setFan(){
  var v=parseInt(document.getElementById('d-fan').value);
  document.getElementById('d-fan-val').textContent=v;
  post('/api/fan',{speed:v})
    .then(function(){clog('Ventoinha → '+v+'%','msg-ok')})
    .catch(function(e){clog('Erro na ventoinha: '+e,'msg-err')});
}
function quickFan(v){
  document.getElementById('d-fan').value=v;
  document.getElementById('d-fan-val').textContent=v;
  post('/api/fan',{speed:v})
    .then(function(){clog('Ventoinha → '+v+'%','msg-ok')})
    .catch(function(e){clog('Erro na ventoinha: '+e,'msg-err')});
}

// ── AMS ──
function amsFeed(type,slotIndex){
  if(typeof slotIndex!=='number'||slotIndex<0)return Promise.reject(new Error('slot inválido'));
  var globalIdx=slotIndex;
  return post('/api/ams/feed',{slot_index:globalIdx,type:type})
    .then(function(){clog((type===1?T.lbl_feed:T.lbl_unload)+' Slot '+(globalIdx+1),'msg-ok')})
    .catch(function(e){clog('Erro no AMS: '+e,'msg-err');throw e;});
}

// ── Camera ──
function camStart(){
  var img=document.getElementById('cam-img');
  var ph=document.getElementById('cam-placeholder');
  var sp=document.getElementById('cam-spinner');
  ph.style.display='none';
  img.style.display='none';
  sp.style.display='block';
  post('/api/camera/start',{}).then(function(){
    camOn=true;
    document.getElementById('cam-toggle-btn').textContent=tr('btn_cam_stop');_camBtnIcon(true);
    var rb=document.getElementById('cam-reset-btn');if(rb)rb.style.display='';
    clog(tr('log_cam_start'),'msg-ok');
    setTimeout(function(){ sp.style.display='none'; img.style.display='block'; },1200);
    if(/Android/i.test(navigator.userAgent)){
      // Android browsers do not support multipart/x-mixed-replace (MJPEG) — polls snapshots
      _camPollInterval=setInterval(function(){ img.src='/api/camera/snapshot?t='+Date.now(); },200);
    } else {
      img.onerror=function(){
        sp.style.display='none';
        img.style.display='none';
        ph.style.display='flex';
        camOn=false;
        document.getElementById('cam-toggle-btn').textContent=tr('btn_cam_start');_camBtnIcon(false);
        clog(tr('log_error')+' '+tr('cam_stream_unavailable'),'msg-err');
      };
      img.src='/api/camera/stream?t='+Date.now();
    }
  }).catch(function(e){
    sp.style.display='none';
    ph.style.display='flex';
    clog(tr('log_error')+' '+e,'msg-err');
  });
}

function camStop(){
  var img=document.getElementById('cam-img');
  if(_camPollInterval){clearInterval(_camPollInterval);_camPollInterval=null;}
  img.onerror=null; // removes the error handler before clearing the src to avoid a false error toast
  post('/api/camera/stop',{}).catch(function(){});
  img.src='';
  img.style.display='none';
  document.getElementById('cam-placeholder').style.display='flex';
  camOn=false;
  camUserStopped=true; // suppresses the auto-restart for the rest of this print
  document.getElementById('cam-toggle-btn').textContent=tr('btn_cam_start');_camBtnIcon(false);
  var rb=document.getElementById('cam-reset-btn');if(rb)rb.style.display='none';
  clog(tr('log_cam_stop'),'msg-ok');
}

function aceDryStart(aceId){
  aceId=(typeof aceId==='number'&&aceId>=0)?aceId:0;
  var prof=_aceDryProfileGet(aceId);
  var t=parseInt(prof.temp,10);
  var d=_aceDryDurationMinFromSec(prof.duration_sec);
  t=Math.max(30,Math.min(80,t));
  d=Math.max(10,Math.min(1440,d));
  return post('/api/ace/dry',{action:'start',target_temp:t,duration:d,ace_id:aceId})
    .then(function(r){return r.json();})
    .then(function(r){
      if(r.error){throw new Error(srvMsg(r.error));}
      clog('ACE '+(aceId+1)+' - '+tr('ace_dry_dryer')+': '+tr('ace_dry_start')+' ('+t+'°C, '+d+' min)','msg-ok');
      poll();
    })
    .catch(function(e){clog('Erro no ACE: '+e,'msg-err');});
}

var _aceAutoFeedPending={};
function aceAutoRefillToggle(aceId){
  aceId=(typeof aceId==='number'&&aceId>=0)?aceId:0;
  var on=!!((document.getElementById('ace-auto-refill-toggle-'+aceId)||{}).checked);
  _aceAutoFeedPending[aceId]=true;
  fetch('/api/ace/auto_feed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ace_id:aceId,on:on?1:0})})
    .then(function(r){return r.json();})
    .then(function(d){
      delete _aceAutoFeedPending[aceId];
      if(d.error){clog('Erro no Auto Refill: '+srvMsg(d.error),'msg-err');var t=document.getElementById('ace-auto-refill-toggle-'+aceId);if(t)t.checked=!on;return;}
      clog('ACE '+(aceId+1)+' - '+tr('ace_dry_auto_refill')+': '+(on?'LIGADO':'DESLIGADO'),'msg-ok');
    })
    .catch(function(e){delete _aceAutoFeedPending[aceId];clog('Erro no Auto Refill: '+e,'msg-err');var t=document.getElementById('ace-auto-refill-toggle-'+aceId);if(t)t.checked=!on;});
}

function openAceDryDialog(aceId){
  aceId=(typeof aceId==='number'&&aceId>=0)?aceId:0;
  _aceDryDialogAceId=aceId;
  _syncAceDryPresetsFromServer(S.ace_dry_presets);
  _aceDryDialogPresetOriginals=JSON.parse(JSON.stringify(ACE_DRY_PRESETS));
  aceDryDialogSyncCustomButtonNames();
  var hasStored=Object.prototype.hasOwnProperty.call(aceDryProfiles,String(aceId));
  var prof=_aceDryProfileGet(aceId);
  if(hasStored&&prof.preset&&ACE_DRY_PRESETS[prof.preset]){
    aceDryDialogPreset(prof.preset);
  }else if(hasStored){
    var sec=prof.duration_sec;
    document.getElementById('ace-dry-dialog-temp').value=prof.temp;
    document.getElementById('ace-dry-dialog-h').value=Math.floor(sec/3600);
    document.getElementById('ace-dry-dialog-m').value=Math.floor((sec%3600)/60);
    document.getElementById('ace-dry-dialog-s').value=sec%60;
    aceDryDialogHighlightPreset('');
  }else{
    aceDryDialogPreset('pla');
  }
  aceDryDialogUpdateSaveButton();
  aceDryDialogUpdateResetButton();
  var sb=document.getElementById('ace-dry-dialog-save-preset');
  if(sb){sb.disabled=false;sb.textContent=tr('ace_dry_dialog_save_restart');}
  document.getElementById('ace-dry-dialog').classList.add('open');
}

function closeAceDryDialog(){
  _aceDryDialogAceId=-1;
  _aceDryDialogPresetOriginals={};
  var sb=document.getElementById('ace-dry-dialog-save-preset');
  if(sb)sb.style.display='none';
  var rb=document.getElementById('ace-dry-dialog-reset-default');
  if(rb)rb.style.display='none';
  document.getElementById('ace-dry-dialog').classList.remove('open');
}

function aceDryDialogIsCustomPreset(key){
  return /^custom_[123]$/.test(String(key||''));
}

function aceDryDialogSyncCustomButtonNames(){
  ['custom_1','custom_2','custom_3'].forEach(function(k){
    var b=document.querySelector('.ace-dry-preset-btn[data-preset="'+k+'"]');
    if(b)b.textContent=(ACE_DRY_PRESETS[k]&&ACE_DRY_PRESETS[k].name)||('Custom '+k.slice(-1));
  });
}

function aceDryDialogUpdateCustomNameUi(){
  var row=document.getElementById('ace-dry-dialog-custom-name-row');
  var input=document.getElementById('ace-dry-dialog-custom-name');
  if(!row||!input)return;
  if(!aceDryDialogIsCustomPreset(_aceDryDialogPresetKey)){
    row.style.display='none';
    return;
  }
  row.style.display='flex';
  input.value=(ACE_DRY_PRESETS[_aceDryDialogPresetKey]&&ACE_DRY_PRESETS[_aceDryDialogPresetKey].name)||'';
}

function aceDryDialogCurrentValues(){
  var t=parseInt(document.getElementById('ace-dry-dialog-temp').value||45,10);
  var h=parseInt(document.getElementById('ace-dry-dialog-h').value||0,10);
  var m=parseInt(document.getElementById('ace-dry-dialog-m').value||0,10);
  var s=parseInt(document.getElementById('ace-dry-dialog-s').value||0,10);
  t=Math.max(30,Math.min(80,t));
  h=Math.max(0,Math.min(24,h));
  m=Math.max(0,Math.min(59,m));
  s=Math.max(0,Math.min(59,s));
  var totalSec=(h*3600)+(m*60)+s;
  totalSec=Math.max(10*60,Math.min(24*3600,totalSec));
  return {temp:t,duration_sec:totalSec};
}

function aceDryDialogUpdateSaveButton(){
  var btn=document.getElementById('ace-dry-dialog-save-preset');
  if(!btn)return;
  var key=_aceDryDialogPresetKey||'';
  if(!key||!ACE_DRY_PRESETS[key]){btn.style.display='none';return;}
  var p=_aceDryDialogPresetOriginals[key]||ACE_DRY_PRESETS[key];
  var cur=aceDryDialogCurrentValues();
  var changed=(cur.temp!==Number(p.temp)||cur.duration_sec!==Number(p.duration_sec));
  if(aceDryDialogIsCustomPreset(key)){
    var nameInp=document.getElementById('ace-dry-dialog-custom-name');
    var n=((nameInp&&nameInp.value)||'').trim();
    var old=(p&&p.name?String(p.name):('Custom '+key.slice(-1))).trim();
    if((n||old)!==old)changed=true;
  }
  btn.style.display=changed?'':'none';
}

function aceDryDialogUpdateResetButton(){
  var btn=document.getElementById('ace-dry-dialog-reset-default');
  if(!btn)return;
  var key=_aceDryDialogPresetKey||'';
  var d=ACE_DRY_PRESET_DEFAULTS[key];
  if(!key||!d){btn.style.display='none';return;}
  var cur=aceDryDialogCurrentValues();
  var changed=(cur.temp!==Number(d.temp)||cur.duration_sec!==Number(d.duration_sec));
  btn.style.display=changed?'':'none';
}

function aceDryDialogInputsChanged(){
  if(aceDryDialogIsCustomPreset(_aceDryDialogPresetKey)){
    var b=document.querySelector('.ace-dry-preset-btn[data-preset="'+_aceDryDialogPresetKey+'"]');
    var i=document.getElementById('ace-dry-dialog-custom-name');
    if(b&&i){
      var t=(i.value||'').trim();
      b.textContent=t||((ACE_DRY_PRESETS[_aceDryDialogPresetKey]&&ACE_DRY_PRESETS[_aceDryDialogPresetKey].name)||('Custom '+_aceDryDialogPresetKey.slice(-1)));
    }
  }
  aceDryDialogUpdateSaveButton();
  aceDryDialogUpdateResetButton();
}

function aceDryDialogHighlightPreset(presetKey){
  _aceDryDialogPresetKey=presetKey||'';
  document.querySelectorAll('.ace-dry-preset-btn').forEach(function(btn){
    var on=(btn.getAttribute('data-preset')===presetKey);
    btn.style.background=on?'var(--accent)':'var(--raised)';
    btn.style.color=on?'#fff':'var(--txt2)';
    btn.style.borderColor=on?'var(--accent)':'var(--border)';
  });
  aceDryDialogUpdateCustomNameUi();
}

function aceDryDialogPreset(presetKey){
  var p=ACE_DRY_PRESETS[presetKey];
  if(!p)return;
  var sec=p.duration_sec;
  document.getElementById('ace-dry-dialog-temp').value=p.temp;
  document.getElementById('ace-dry-dialog-h').value=Math.floor(sec/3600);
  document.getElementById('ace-dry-dialog-m').value=Math.floor((sec%3600)/60);
  document.getElementById('ace-dry-dialog-s').value=sec%60;
  aceDryDialogHighlightPreset(presetKey);
  aceDryDialogSyncCustomButtonNames();
  aceDryDialogUpdateSaveButton();
  aceDryDialogUpdateResetButton();
}

function resetAceDryPresetToDefault(){
  var key=_aceDryDialogPresetKey||'';
  var d=ACE_DRY_PRESET_DEFAULTS[key];
  if(!key||!d)return;
  var sec=Number(d.duration_sec)||0;
  document.getElementById('ace-dry-dialog-temp').value=Number(d.temp)||45;
  document.getElementById('ace-dry-dialog-h').value=Math.floor(sec/3600);
  document.getElementById('ace-dry-dialog-m').value=Math.floor((sec%3600)/60);
  document.getElementById('ace-dry-dialog-s').value=sec%60;
  aceDryDialogInputsChanged();
}

function saveAceDryPresetAndRestart(){
  var key=_aceDryDialogPresetKey||'';
  var btn=document.getElementById('ace-dry-dialog-save-preset');
  if(!key||!ACE_DRY_PRESETS[key]||!btn)return;
  var cur=aceDryDialogCurrentValues();
  if(!ACE_DRY_PRESETS[key])ACE_DRY_PRESETS[key]={};
  ACE_DRY_PRESETS[key].temp=cur.temp;
  ACE_DRY_PRESETS[key].duration_sec=cur.duration_sec;
  if(aceDryDialogIsCustomPreset(key)){
    var nameInp=document.getElementById('ace-dry-dialog-custom-name');
    var nm=((nameInp&&nameInp.value)||'').trim();
    ACE_DRY_PRESETS[key].name=nm||('Custom '+key.slice(-1));
  }
  btn.disabled=true;
  btn.textContent='…';
  fetch(_apiUrl('/api/settings')).then(function(r){return r.json();}).then(function(d){
    d.ace_dry_presets={
      pla:{temp:ACE_DRY_PRESETS.pla.temp,duration_sec:ACE_DRY_PRESETS.pla.duration_sec},
      pla_plus:{temp:ACE_DRY_PRESETS.pla_plus.temp,duration_sec:ACE_DRY_PRESETS.pla_plus.duration_sec},
      petg:{temp:ACE_DRY_PRESETS.petg.temp,duration_sec:ACE_DRY_PRESETS.petg.duration_sec},
      tpu:{temp:ACE_DRY_PRESETS.tpu.temp,duration_sec:ACE_DRY_PRESETS.tpu.duration_sec},
      abs_asa:{temp:ACE_DRY_PRESETS.abs_asa.temp,duration_sec:ACE_DRY_PRESETS.abs_asa.duration_sec},
      pa_pc:{temp:ACE_DRY_PRESETS.pa_pc.temp,duration_sec:ACE_DRY_PRESETS.pa_pc.duration_sec},
      custom_1:{name:ACE_DRY_PRESETS.custom_1.name,temp:ACE_DRY_PRESETS.custom_1.temp,duration_sec:ACE_DRY_PRESETS.custom_1.duration_sec},
      custom_2:{name:ACE_DRY_PRESETS.custom_2.name,temp:ACE_DRY_PRESETS.custom_2.temp,duration_sec:ACE_DRY_PRESETS.custom_2.duration_sec},
      custom_3:{name:ACE_DRY_PRESETS.custom_3.name,temp:ACE_DRY_PRESETS.custom_3.temp,duration_sec:ACE_DRY_PRESETS.custom_3.duration_sec}
    };
    return post('/api/settings',d);
  }).then(function(){
    clog('ACE preset '+key+' '+tr('settings_save'),'msg-ok');
    closeAceDryDialog();
  }).catch(function(e){
    btn.disabled=false;
    btn.textContent=tr('ace_dry_dialog_save_restart');
    clog('Erro no preset do ACE: '+e,'msg-err');
  });
}

function confirmAceDryDialog(){
  if(_aceDryDialogAceId<0)return;
  var t=parseInt(document.getElementById('ace-dry-dialog-temp').value||45,10);
  var h=parseInt(document.getElementById('ace-dry-dialog-h').value||0,10);
  var m=parseInt(document.getElementById('ace-dry-dialog-m').value||0,10);
  var s=parseInt(document.getElementById('ace-dry-dialog-s').value||0,10);
  t=Math.max(30,Math.min(80,t));
  h=Math.max(0,Math.min(24,h));
  m=Math.max(0,Math.min(59,m));
  s=Math.max(0,Math.min(59,s));
  var totalSec=(h*3600)+(m*60)+s;
  totalSec=Math.max(10*60,Math.min(24*3600,totalSec));
  var preset=_aceDryDialogPresetKey||'';
  _aceDryProfileSet(_aceDryDialogAceId,t,totalSec,preset);
  closeAceDryDialog();
  applyState();
}

function aceDryToggle(aceId,on){
  if(on)return aceDryStart(aceId);
  return aceDryStop(aceId);
}

function toggleCam(){if(camOn)camStop();else camStart()}

function resetCamera(){
  post('/api/camera/reset',{}).then(function(){
    var btn=document.getElementById('cam-reset-btn');
    if(btn){btn.dataset.icon='check';setTimeout(function(){btn.dataset.icon='refresh-cw';},1000);}
  }).catch(function(){});
}

function aceDryStop(aceId){
  aceId=(typeof aceId==='number'&&aceId>=0)?aceId:0;
  return post('/api/ace/dry',{action:'stop',ace_id:aceId})
    .then(function(r){return r.json();})
    .then(function(r){
      if(r.error){throw new Error(srvMsg(r.error));}
      clog('ACE '+(aceId+1)+' - '+tr('ace_dry_dryer')+': '+tr('ace_dry_stop'),'msg-ok');
      poll();
    })
    .catch(function(e){clog('Erro no ACE: '+e,'msg-err');});
}

function loadStore(){
  fetch(_apiUrl('/kx/files')).then(function(r){return r.json()}).then(function(d){
    storeFiles=d.result||[];
    // Resets any pending selection - old ids from a deleted/renamed file
    // must never carry over to a new file list.
    storeExitSelectMode();
    renderStore();
  }).catch(function(e){clog('Erro no store: '+e,'msg-err')});
}

// Applies the store's search/filter/sort controls to storeFiles. Shared by
// renderStore() and storeToggleSelectAll() so that "Select all" only selects what
// the user is seeing, not the whole (possibly filtered) list.
function _storeFilteredFiles(){
  var q=(document.getElementById('store-search')||{value:''}).value.toLowerCase().trim();
  var filter=(document.getElementById('store-filter')||{value:'all'}).value;
  var sort=(document.getElementById('store-sort')||{value:'date_desc'}).value;

  var files=storeFiles.filter(function(f){
    if(q&&f.filename.toLowerCase().indexOf(q)===-1) return false;
    if(filter==='completed'&&f.last_print_status!=='completed') return false;
    if(filter==='failed'&&(f.last_print_status!=='cancelled'&&f.last_print_status!=='failed')) return false;
    if(filter==='never'&&f.last_print_status) return false;
    return true;
  });

  files.sort(function(a,b){
    if(sort==='name_asc') return a.filename.localeCompare(b.filename);
    if(sort==='duration_asc'){
      var da=a.last_print_duration||a.est_print_time_sec||0;
      var db=b.last_print_duration||b.est_print_time_sec||0;
      return da-db;
    }
    // date_desc (default)
    return (b.uploaded_at||'').localeCompare(a.uploaded_at||'');
  });
  return files;
}

// ── Progresso de upload ──
// Steps: browser (browser → bridge, measured here) and the server ones, coming from
// /api/state.upload: receiving (Orca/navegador → bridge), processing, sending
// (bridge → printer), done, error. Applies to upload via the UI and via OrcaSlicer.
var _browserUpload=null, _uploadWatchTimer=null, _uploadHideTimer=null;
function _fmtBytes(n){if(!n)return'0';var u=['B','KB','MB','GB'],i=0;while(n>=1024&&i<3){n/=1024;i++;}return (i?n.toFixed(n<10?1:0):n)+' '+u[i];}
function _renderUploadStrip(u){
  var el=document.getElementById('upload-strip'); if(!el)return;
  if(_browserUpload)u=_browserUpload;
  var now=Date.now()/1000;
  var show=u&&(['done','error'].indexOf(u.phase)===-1||(now-(u.ts||now))<(u.phase==='error'?20:6));
  if(!show){el.style.display='none';return;}
  var pct=u.total?Math.min(100,Math.round(u.sent*100/u.total)):(u.phase==='done'?100:0);
  var labels={browser:'upload_phase_browser',receiving:'upload_phase_receiving',processing:'upload_phase_processing',
    sending:'upload_phase_sending',done:'upload_phase_done',error:'upload_phase_error'};
  var icons={browser:'upload',receiving:'download',processing:'cpu',sending:'printer',done:'circle-check',error:'circle-alert'};
  el.style.display='flex';
  el.className='upload-strip '+u.phase;
  document.getElementById('upload-strip-icon').dataset.icon=icons[u.phase]||'upload';
  document.getElementById('upload-strip-name').textContent=u.name||'';
  var ph=tr(labels[u.phase])||u.phase;
  if(u.phase==='error'&&u.error)ph+=': '+srvMsg(u.error);
  else if(u.total&&(u.phase==='browser'||u.phase==='receiving'||u.phase==='sending'))ph+=' · '+_fmtBytes(u.sent)+' / '+_fmtBytes(u.total);
  document.getElementById('upload-strip-phase').textContent=ph;
  document.getElementById('upload-strip-pct').textContent=u.phase==='error'?'':pct+'%';
  document.getElementById('upload-strip-bar').style.width=(u.phase==='processing'?100:pct)+'%';
  // During the server steps, it queries the state more often so the bar moves.
  if(!_browserUpload&&['receiving','processing','sending'].indexOf(u.phase)!==-1)_watchUpload();
}
function _watchUpload(){
  if(_uploadWatchTimer)return;
  _uploadWatchTimer=setInterval(function(){
    fetch(_apiUrl('/api/state')).then(function(r){return r.json();}).then(function(d){
      _renderUploadStrip(d.upload);
      if(!d.upload||['done','error'].indexOf(d.upload.phase)!==-1){clearInterval(_uploadWatchTimer);_uploadWatchTimer=null;}
    }).catch(function(){});
  },700);
}
function uploadGcode(file){
  if(!file) return;
  var zone=document.getElementById('store-upload-zone');
  var status=document.getElementById('store-upload-status');
  var label=document.getElementById('store-upload-label');
  // Only allows printable files (Issue #59) — drag and drop ignores the
  // accept attribute, so it checks explicitly here.
  var _fn=(file.name||'').toLowerCase();
  if(!/\.(gcode|gcode\.3mf|3mf|bgcode)$/.test(_fn)){
    if(status){ status.textContent=T.store_upload_only_gcode||'Only GCode files allowed'; status.style.display=''; status.className='upload-status-err'; }
    clog('Upload rejeitado (não é GCode): '+file.name,'msg-err');
    return;
  }
  if(status) { status.textContent=T.store_upload_busy; status.style.display=''; status.className='upload-status-busy'; }
  if(label) label.style.display='none';
  if(zone) zone.style.pointerEvents='none';
  var fd=new FormData();
  fd.append('file', file);
  fd.append('web_upload', 'true');
  function done(){ if(label) label.style.display=''; if(zone) zone.style.pointerEvents=''; }
  // XHR instead of fetch: it is the only way to measure the browser → bridge upload.
  var xhr=new XMLHttpRequest();
  xhr.open('POST',_apiUrl('/api/files/local'));
  _browserUpload={name:file.name,phase:'browser',sent:0,total:file.size,ts:Date.now()/1000};
  _renderUploadStrip();
  xhr.upload.onprogress=function(e){
    if(!_browserUpload)return;
    _browserUpload.sent=e.loaded;_browserUpload.total=e.total||file.size;_renderUploadStrip();
  };
  xhr.upload.onload=function(){ _browserUpload=null; _watchUpload(); };
  xhr.onload=function(){
    _browserUpload=null;
    if(xhr.status>=200&&xhr.status<300){
      if(status){ status.textContent=T.store_upload_success.replace('{file}',file.name); status.className='upload-status-ok'; }
      loadStore();
      setTimeout(function(){ if(status){status.style.display='none'; status.className='';} done(); }, 3000);
    } else {
      var msg=xhr.status+': '+xhr.responseText;
      try{msg=srvMsg(JSON.parse(xhr.responseText).error)||msg;}catch(e){}
      if(status){ status.textContent=T.store_upload_error.replace('{error}',msg); status.className='upload-status-err'; }
      done(); clog('Erro no upload: '+msg,'msg-err');
    }
    _watchUpload();
  };
  xhr.onerror=function(){
    _browserUpload=null;
    if(status){ status.textContent=T.store_upload_error.replace('{error}','rede'); status.className='upload-status-err'; }
    done(); clog('Erro no upload: falha de rede','msg-err');
  };
  xhr.send(fd);
}

function renderStore(){
  var grid=document.getElementById('store-grid');
  var empty=document.getElementById('store-empty');
  var files=_storeFilteredFiles();

  if(!storeFiles.length){
    empty.textContent=T.store_empty;
    grid.innerHTML='';
    empty.style.display='block';
    return;
  }
  if(!files.length){
    empty.textContent=T.store_no_results;
    grid.innerHTML='';
    empty.style.display='block';
    return;
  }
  empty.style.display='none';
  grid.innerHTML=files.map(function(f){
    var thumb=f.thumbnail_b64
      ? '<img class="fcard-thumb" src="data:image/png;base64,'+f.thumbnail_b64+'" alt="">'
      : '<div class="fcard-thumb empty"><i data-icon="box"></i></div>';
    var date=f.uploaded_at?f.uploaded_at.replace('T',' ').slice(0,16):'';
    var est=f.est_print_time_sec?formatDur(f.est_print_time_sec):'–';
    // State as a mark: green tape (done) / red (failed) / none (new).
    var tape='';
    if(f.last_print_status==='completed'){
      tape='<span class="dymo green fcard-tape" data-icon="check">'+escHtml(f.last_print_duration?formatDur(f.last_print_duration):'OK')+'</span>';
    } else if(f.last_print_status==='cancelled'||f.last_print_status==='failed'){
      tape='<span class="dymo red fcard-tape" data-icon="x">'+escHtml(tr('store_status_'+f.last_print_status)||f.last_print_status)+'</span>';
    } else if(!f.last_print_status){
      tape='<span class="fcard-new">'+escHtml(T.store_never||'')+'</span>';
    }
    var isSelected=!!_storeSelected[f.id];
    var checkbox='<label class="fcard-check" onclick="event.stopPropagation()"><input type="checkbox" class="store-card-cb" '+(isSelected?'checked':'')+
      ' onclick="event.stopPropagation();storeToggleSelect(\''+f.id+'\')"></label>';
    var click=_storeSelectMode?' onclick="storeToggleSelect(\''+f.id+'\')" style="cursor:pointer"':'';
    return '<div class="fcard'+(isSelected?' selected':'')+'"'+click+'>'+
      checkbox+thumb+tape+
      '<div class="fcard-name" title="'+escHtml(f.filename)+'">'+escHtml(f.filename)+'</div>'+
      '<div class="fcard-meta"><span data-icon="timer">'+escHtml(est)+'</span><span data-icon="clock">'+escHtml(date)+'</span></div>'+
      '<div class="fcard-actions">'+
        '<button class="btn btn-sm btn-accent" data-icon="play" style="flex:1" onclick="event.stopPropagation();storePrint(\''+f.id+'\',\''+jsq(f.filename)+'\')">'+escHtml(T.store_print||'')+'</button>'+
        '<button class="icon-btn" data-icon="calculator" title="'+escHtml(tr('pr_quote_this','Quote this part'))+'" onclick="event.stopPropagation();prFromFile(\''+jsq(f.id)+'\')"></button>'+
        '<button class="icon-btn" data-icon="download" title="'+escHtml(T.store_download||'')+'" onclick="event.stopPropagation();storeDownload(\''+f.id+'\')"></button>'+
        '<button class="icon-btn danger" data-icon="trash-2" title="'+escHtml(T.store_delete||'')+'" onclick="event.stopPropagation();storeDelete(\''+f.id+'\')"></button>'+
      '</div>'+
    '</div>';
  }).join('');

  _storeUpdateSelectBar(files);
}

// Reflects the current selection in the select-all checkbox (incl. the
// indeterminate state), in the selected-count label and in the disabled
// state of the delete button. `files` is the currently filtered/visible list.
function _storeUpdateSelectBar(files){
  var selectAll=document.getElementById('store-select-all');
  var countEl=document.getElementById('store-selected-count');
  var delBtn=document.getElementById('store-delete-selected-btn');
  if(!selectAll||!countEl||!delBtn)return;
  var visibleIds=files.map(function(f){return f.id;});
  var selectedVisible=visibleIds.filter(function(id){return _storeSelected[id];});
  var n=selectedVisible.length;
  selectAll.checked=n>0&&n===visibleIds.length;
  selectAll.indeterminate=n>0&&n<visibleIds.length;
  countEl.textContent=n>0?(T.store_selected_count||'{n} selected').replace('{n}',n):'';
  delBtn.disabled=n===0;
}

function formatDur(sec){
  var h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);
  return h?h+'h '+m+'m':m+'m';
}

var _storeFileId=null;
var _storeFilename=null;
var _filamentDialogMode='store'; // 'store' ou 'banner'
var _pendingWebVerifyFileId=null;
var _pendingWebVerifyFilename='';
var _pendingWebVerifyAction=null;
var _pendingWebVerifyAutoOpen=false;
// GCode store file list. MUST be declared – otherwise ReferenceError when
// "Choose slots" in the banner is clicked before the browser tab has ever been
// loaded (Issue #29 / theme separation PR #27).
var storeFiles=[];

// Multi-select state of the GCode browser (Issue #94).
var _storeSelectMode=false;
var _storeSelected={};  // file.id -> true

function storeToggleSelect(id){
  if(!_storeSelectMode){
    _storeSelectMode=true;
    var bar=document.getElementById('store-select-bar');
    if(bar)bar.style.display='flex';
  }
  if(_storeSelected[id])delete _storeSelected[id];
  else _storeSelected[id]=true;
  renderStore();
}

function storeToggleSelectAll(checked){
  // Only the currently filtered/visible files are affected, so search/filter
  // never silently makes "Select all" grab hidden files.
  var visible=_storeFilteredFiles();
  _storeSelected={};
  if(checked)visible.forEach(function(f){_storeSelected[f.id]=true;});
  renderStore();
}

function storeExitSelectMode(){
  _storeSelectMode=false;
  _storeSelected={};
  var bar=document.getElementById('store-select-bar');
  if(bar)bar.style.display='none';
  renderStore();
}

function storeDeleteSelected(){
  var ids=Object.keys(_storeSelected);
  if(!ids.length)return;
  if(!confirm((T.store_delete_selected_confirm||'Delete {n} selected files?').replace('{n}',ids.length)))return;
  Promise.all(ids.map(function(id){
    return fetch(_apiUrl('/kx/files/'+id),{method:'DELETE'}).then(function(r){return{id:id,ok:r.ok};});
  })).then(function(results){
    var failed=results.filter(function(r){return !r.ok;});
    if(failed.length)clog((T.log_delete_failed||'Falha ao excluir')+': '+failed.length,'msg-err');
    storeExitSelectMode();
    loadStore();
  });
}

// ── Files in the printer's own internal storage (Issue: 2nd browser tab) ──
// Uses the file name as identity key (the printer has no numeric file id like
// the bridge's GCodeStore) and a single batch delete call, since the
// printer's file/deleteBatch MQTT action accepts a list of names natively.
var printerFiles=[];
var _printerFilesSelectMode=false;
var _printerFilesSelected={};  // file name -> true

function loadPrinterFiles(){
  var errEl=document.getElementById('printer-store-error');
  if(errEl)errEl.style.display='none';
  fetch(_apiUrl('/kx/printer-files')).then(function(r){return r.json()}).then(function(d){
    _printerFilesLoaded=true;
    if(d.error){
      printerFiles=[];
      if(errEl){errEl.textContent=T.printer_store_unreachable||srvMsg(d.error);errEl.style.display='block';}
      renderPrinterFiles();
      return;
    }
    printerFiles=d.result||[];
    _printerFilesSelected={};
    _printerFilesSelectMode=false;
    var bar=document.getElementById('printer-store-select-bar');
    if(bar)bar.style.display='none';
    renderPrinterFiles();
  }).catch(function(e){
    _printerFilesLoaded=true;
    if(errEl){errEl.textContent=T.printer_store_unreachable||String(e);errEl.style.display='block';}
  });
}

function renderPrinterFiles(){
  var grid=document.getElementById('printer-store-grid');
  var empty=document.getElementById('printer-store-empty');
  if(!grid||!empty)return;
  if(!printerFiles.length){
    empty.textContent=T.printer_store_empty||'No files on the printer.';
    grid.innerHTML='';
    empty.style.display='block';
    return;
  }
  empty.style.display='none';
  grid.innerHTML=printerFiles.map(function(f,idx){
    var sizeKb=f.size?(f.size/1024).toFixed(0)+' KB':'–';
    var date=f.timestamp?new Date(f.timestamp).toISOString().replace('T',' ').slice(0,16):'';
    var isSelected=!!_printerFilesSelected[f.filename];
    var checkbox='<label class="fcard-check" onclick="event.stopPropagation()"><input type="checkbox" class="store-card-cb" '+(isSelected?'checked':'')+
      ' onclick="event.stopPropagation();printerFileToggleSelect(\''+jsq(f.filename)+'\')"></label>';
    var click=_printerFilesSelectMode?' onclick="printerFileToggleSelect(\''+jsq(f.filename)+'\')" style="cursor:pointer"':'';
    var thumbId='pft-'+idx;
    var cachedThumb=_printerThumbCache[f.filename];
    var thumbHtml=cachedThumb
      ? '<img class="fcard-thumb" id="'+thumbId+'" src="data:image/png;base64,'+cachedThumb+'" alt="">'
      : '<div class="fcard-thumb empty" id="'+thumbId+'" data-filename="'+encodeURIComponent(f.filename)+'"><i data-icon="box"></i></div>';
    return '<div class="fcard'+(isSelected?' selected':'')+'"'+click+'>'+
      checkbox+thumbHtml+
      '<div class="fcard-name" title="'+escHtml(f.filename)+'">'+escHtml(f.filename)+'</div>'+
      '<div class="fcard-meta"><span data-icon="save">'+escHtml(sizeKb)+'</span><span data-icon="clock">'+escHtml(date)+'</span></div>'+
      '<div class="fcard-actions">'+
        '<button class="btn btn-sm btn-danger" data-icon="trash-2" style="flex:1" onclick="event.stopPropagation();printerFileDelete(\''+jsq(f.filename)+'\')">'+escHtml(T.store_delete||'')+'</button>'+
      '</div>'+
    '</div>';
  }).join('');
  _printerFilesUpdateSelectBar();
  _loadVisiblePrinterThumbnails();
}

// Thumbnails are fetched on demand, one at a time, only for cards not yet
// cached - fetching them all upfront would mean one MQTT roundtrip per
// file (145+ files is common), overloading the single MQTT connection.
var _printerThumbCache={};  // file name -> base64 PNG string ("" = no thumbnail)
var _printerThumbQueue=[];
var _printerThumbLoading=false;

function _loadVisiblePrinterThumbnails(){
  var placeholders=document.querySelectorAll('#printer-store-grid [data-filename]');
  _printerThumbQueue=Array.prototype.slice.call(placeholders);
  _pumpPrinterThumbQueue();
}

function _pumpPrinterThumbQueue(){
  if(_printerThumbLoading||!_printerThumbQueue.length)return;
  var el=_printerThumbQueue.shift();
  if(!el||!el.isConnected){_pumpPrinterThumbQueue();return;}
  var encodedName=el.getAttribute('data-filename');
  _printerThumbLoading=true;
  fetch(_apiUrl('/kx/printer-files/'+encodedName+'/thumbnail'))
    .then(function(r){return r.json()})
    .then(function(d){
      var thumb=(d.result&&d.result.thumbnail)||'';
      _printerThumbCache[decodeURIComponent(encodedName)]=thumb;
      if(thumb&&el.isConnected){
        var img=document.createElement('img');
        img.src='data:image/png;base64,'+thumb;
        img.className='fcard-thumb';
        img.id=el.id;
        el.replaceWith(img);
      }
    })
    .catch(function(){/* keeps the placeholder icon on failure */})
    .finally(function(){
      _printerThumbLoading=false;
      _pumpPrinterThumbQueue();
    });
}

function _printerFilesUpdateSelectBar(){
  var selectAll=document.getElementById('printer-store-select-all');
  var countEl=document.getElementById('printer-store-selected-count');
  var delBtn=document.getElementById('printer-store-delete-selected-btn');
  if(!selectAll||!countEl||!delBtn)return;
  var allNames=printerFiles.map(function(f){return f.filename;});
  var selected=allNames.filter(function(n){return _printerFilesSelected[n];});
  var n=selected.length;
  selectAll.checked=n>0&&n===allNames.length;
  selectAll.indeterminate=n>0&&n<allNames.length;
  countEl.textContent=n>0?(T.store_selected_count||'{n} selected').replace('{n}',n):'';
  delBtn.disabled=n===0;
}

function printerFileToggleSelect(filename){
  if(!_printerFilesSelectMode){
    _printerFilesSelectMode=true;
    var bar=document.getElementById('printer-store-select-bar');
    if(bar)bar.style.display='flex';
  }
  if(_printerFilesSelected[filename])delete _printerFilesSelected[filename];
  else _printerFilesSelected[filename]=true;
  renderPrinterFiles();
}

function printerFileToggleSelectAll(checked){
  _printerFilesSelected={};
  if(checked)printerFiles.forEach(function(f){_printerFilesSelected[f.filename]=true;});
  renderPrinterFiles();
}

function printerFileExitSelectMode(){
  _printerFilesSelectMode=false;
  _printerFilesSelected={};
  var bar=document.getElementById('printer-store-select-bar');
  if(bar)bar.style.display='none';
  renderPrinterFiles();
}

function _printerFilesDeleteRequest(filenames){
  return fetch(_apiUrl('/kx/printer-files/delete'),{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({filenames:filenames}),
  }).then(function(r){return r.json().then(function(d){return{ok:r.ok,body:d};});});
}

function printerFileDelete(filename){
  if(!confirm(T.printer_store_delete_confirm||'Delete file?'))return;
  _printerFilesDeleteRequest([filename]).then(function(res){
    if(!res.ok){clog((T.log_delete_failed||'Falha ao excluir')+': '+(srvMsg(res.body.error)||''),'msg-err');return;}
    loadPrinterFiles();
  });
}

function printerFileDeleteSelected(){
  var names=Object.keys(_printerFilesSelected);
  if(!names.length)return;
  if(!confirm((T.printer_store_delete_selected_confirm||'Delete {n} selected files?').replace('{n}',names.length)))return;
  _printerFilesDeleteRequest(names).then(function(res){
    if(!res.ok){clog((T.log_delete_failed||'Falha ao excluir')+': '+(srvMsg(res.body.error)||''),'msg-err');return;}
    printerFileExitSelectMode();
    loadPrinterFiles();
  });
}

var _gcodeFilaments=[];

function _setGcodeFilamentsFromFileObj(fileObj){
  try{
    if(fileObj&&Array.isArray(fileObj.gcode_filaments)){
      _gcodeFilaments=fileObj.gcode_filaments;
    }else if(fileObj&&typeof fileObj.gcode_filaments==='string'&&fileObj.gcode_filaments){
      _gcodeFilaments=JSON.parse(fileObj.gcode_filaments);
    }else{
      _gcodeFilaments=[];
    }
  }catch(e){
    _gcodeFilaments=[];
  }
}

function storePrint(fileId, filename){
  _storeFileId=fileId;
  _storeFilename=filename;
  _filamentDialogMode='store';
  var fileObj=storeFiles.find(function(f){return f.id===fileId;});
  openStorePrintDialog(fileId, filename, fileObj);
}

function openStorePrintDialog(fileId, filename, fileObj){
  _storeFileId=fileId;
  _storeFilename=filename;
  _filamentDialogMode='store';
  maybeGateWebUpload(fileObj, function(){
    // Fetches the GCode filaments from the store file (for the preview in the dialog)
    _setGcodeFilamentsFromFileObj(fileObj);
    fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json()}).then(function(d){
      openFilamentDialog(d.result||[]);
    }).catch(function(){openFilamentDialog([]);});
  });
}

function webUploadWarningEnabled(){
  return S.web_upload_warning===undefined ? true : !!S.web_upload_warning;
}

function clearWebUploadWarningFlag(fileId, onDone){
  if(!fileId){
    if(onDone) onDone();
    return;
  }
  fetch(_apiUrl('/kx/files/'+encodeURIComponent(fileId)+'/verify'), {method:'POST'})
    .then(function(r){
      if(!r.ok) return r.text().then(function(t){throw new Error(r.status+': '+t);});
      return r.json();
    })
    .then(function(){
      var fileObj=(storeFiles||[]).find(function(f){return f.id===fileId;});
      if(fileObj){fileObj.web_unverified=false;}
      if(onDone) onDone();
      loadStore();
    })
    .catch(function(e){
      clog('Erro de verificação: '+e,'msg-err');
    });
}

function maybeGateWebUpload(fileObj, onContinue, opts){
  opts=opts||{};
  if(!fileObj || !fileObj.web_unverified){
    if(onContinue) onContinue();
    return;
  }
  if(!webUploadWarningEnabled()){
    if(onContinue) onContinue();
    return;
  }
  var cancelledId=sessionStorage.getItem('webVerifyCancelledFileId')||'';
  if(opts.autoOpen && cancelledId && cancelledId===String(fileObj.id||'')){
    return;
  }
  openWebVerifyDialog(fileObj.id, fileObj.filename, function(){
    clearWebUploadWarningFlag(fileObj.id, onContinue);
  }, !!opts.autoOpen);
}

function openWebVerifyDialog(fileId, filename, onConfirm, autoOpen){
  _pendingWebVerifyFileId=fileId;
  _pendingWebVerifyFilename=filename;
  _pendingWebVerifyAction=onConfirm||null;
  _pendingWebVerifyAutoOpen=!!autoOpen;
  var status=document.getElementById('store-web-verify-status');
  if(status){status.textContent='';}
  openStoreWebVerifyDialog();
}

function openStoreWebVerifyDialog(){
  var modal=document.getElementById('store-web-verify-dialog');
  if(modal){modal.classList.add('open');}
}

function closeStoreWebVerifyDialog(){
  var modal=document.getElementById('store-web-verify-dialog');
  if(modal){modal.classList.remove('open');}
  if(_pendingWebVerifyAutoOpen && _pendingWebVerifyFileId){
    sessionStorage.setItem('webVerifyCancelledFileId', String(_pendingWebVerifyFileId));
  }
  _pendingWebVerifyFileId=null;
  _pendingWebVerifyFilename='';
  _pendingWebVerifyAction=null;
  _pendingWebVerifyAutoOpen=false;
}

function confirmStoreWebVerify(){
  if(!_pendingWebVerifyFileId||!_pendingWebVerifyFilename){
    closeStoreWebVerifyDialog();
    return;
  }
  var fileId=_pendingWebVerifyFileId;
  var action=_pendingWebVerifyAction;
  var status=document.getElementById('store-web-verify-status');
  if(status){status.textContent='…';}
  fetch(_apiUrl('/kx/files/'+encodeURIComponent(fileId)+'/verify'), {method:'POST'})
    .then(function(r){
      if(!r.ok) return r.text().then(function(t){throw new Error(r.status+': '+t);});
      return r.json();
    })
    .then(function(){
      var fileObj=(storeFiles||[]).find(function(f){return f.id===fileId;});
      if(fileObj){fileObj.web_unverified=false;}
      sessionStorage.removeItem('webVerifyCancelledFileId');
      _pendingWebVerifyFileId=null;
      _pendingWebVerifyFilename='';
      _pendingWebVerifyAction=null;
      _pendingWebVerifyAutoOpen=false;
      closeStoreWebVerifyDialog();
      loadStore();
      if(typeof action==='function') action();
    })
    .catch(function(e){
      if(status){status.textContent=e.message;}
      clog('Erro de verificação: '+e,'msg-err');
    });
}

function _showMismatchWarn(mismatches){
  var el=document.getElementById('fd-mismatch-warn');
  if(!el)return;
  if(!mismatches||!mismatches.length){el.style.display='none';el.innerHTML='';return;}
  var lines=mismatches.map(function(m){
    var slot='Slot '+(m.slot_index+1);
    if(m.reason==='empty')
      return slot+': GCode needs '+m.gcode_material+' — slot is empty';
    return slot+': GCode needs '+m.gcode_material+', loaded: '+(m.ams_material||'?');
  });
  el.innerHTML='<strong>Filament mismatch detected</strong><br>'+lines.join('<br>');
  el.style.display='';
}
function startReadyFileWithSlots(filename,_autoOpen,_mismatch){
  if(!_autoOpen) _fdAutoOpenedFile=null; // chamada manual → libera a trava de auto-open
  var fn=filename||S.file_ready;
  var currentFile=(storeFiles||[]).find(function(f){return f.filename===fn;});
  if(currentFile && currentFile.web_unverified && webUploadWarningEnabled()){
    maybeGateWebUpload(currentFile, function(){ startReadyFileWithSlots(fn,_autoOpen); }, {autoOpen:!!_autoOpen});
    return;
  }
  _filamentDialogMode='banner';
  _storeFilename=fn||'';
  // The banner must never reuse old store file context.
  _storeFileId=null;
  _gcodeFilaments=[];

  var _autoOpenFile=_autoOpen?fn:null;
  if(_autoOpen) _fdDialogOpen=true; // already locks during the fetch
  function openWithSlots(){
    fetch(_apiUrl('/kx/filament/slots')).then(function(r){return r.json()}).then(function(d){
      if(_autoOpenFile && _fdUserCancelled){_fdDialogOpen=false;return;}
      openFilamentDialog(d.result||[]);
      _showMismatchWarn(_mismatch||null);
    }).catch(function(){
      if(_autoOpenFile && _fdUserCancelled){_fdDialogOpen=false;return;}
      openFilamentDialog([]);
      _showMismatchWarn(_mismatch||null);
    });
  }

  function _proceedWithFileObj(fileObj){
    if(fileObj && fileObj.web_unverified && webUploadWarningEnabled()){
      // The verification barrier was not active yet on the first fetch (storeFiles empty) — checks now.
      if(_autoOpen){_fdDialogOpen=false;}
      maybeGateWebUpload(fileObj, function(){ startReadyFileWithSlots(fn,_autoOpen); }, {autoOpen:!!_autoOpen});
      return;
    }
    if(fileObj){
      _storeFileId=fileObj.id;
      _setGcodeFilamentsFromFileObj(fileObj);
    }
    openWithSlots();
  }

  var fileObj=(storeFiles||[]).find(function(f){return f.filename===_storeFilename;});
  if(fileObj){
    _proceedWithFileObj(fileObj);
    return;
  }

  // Fallback: refreshes the file list and resolves the current file by name.
  fetch(_apiUrl('/kx/files')).then(function(r){return r.json()}).then(function(d){
    storeFiles=d.result||[];
    var refreshed=(storeFiles||[]).find(function(f){return f.filename===_storeFilename;})||null;
    _proceedWithFileObj(refreshed);
  }).catch(function(){
    openWithSlots();
  });
}

var _amsSlots=[];
var _printObjects=[]; // [{name, skip}] for the currently open dialog
var _printObjectsSvg=''; // base64 SVG from the DB for preview

// Helper functions for colored channel/slot markers (Issue #23)
function _contrastText(hex){
  // Cor clara → texto escuro, cor escura → texto claro
  var c=(hex||'').replace('#','');
  if(c.length===3)c=c[0]+c[0]+c[1]+c[1]+c[2]+c[2];
  if(c.length<6)return '#fff';
  var r=parseInt(c.slice(0,2),16),g=parseInt(c.slice(2,4),16),b=parseInt(c.slice(4,6),16);
  // Luminosidade YIQ
  var y=(r*299 + g*587 + b*114)/1000;
  return y>=140?'#111':'#fff';
}
function _normalizeMaterialKey(material){
  var key=(material||'').toUpperCase().replace(/[^A-Z0-9+]/g,'');
  // Orca often uses PLA for PLA+, whereas the AMS may report PLA+.
  if(key==='PLA+'||key==='PLAPLUS') return 'PLA';
  // Handles modifier+base patterns in any order: "Matte PLA", "Silk PETG",
  // "PLA Silk", "PLA Matte". O OrcaSlicer sempre escreve o tipo base no GCode
  // (filament_type = PLA), but users name the slots with the full product name.
  var trimmed=(material||'').trim();
  if(trimmed.indexOf(' ')>=0){
    var words=trimmed.toUpperCase().split(/\s+/);
    for(var i=0;i<words.length;i++){
      var w=words[i].replace(/[^A-Z0-9+]/g,'');
      if(_BASE_MATERIAL_TYPES.indexOf(w)>=0) return w;
    }
  }
  return key;
}
function _materialsCompatible(a,b){
  return _normalizeMaterialKey(a)===_normalizeMaterialKey(b);
}
// Issue #57 point 4: concrete profile name (user override) instead of the generic type.
// Falls back to the material type when there is no mapped profile.
function _slotProfileLabel(slot){
  if(!slot)return '';
  if(slot.filament_name){
    return slot.filament_name+(slot.filament_vendor?' — '+slot.filament_vendor:'');
  }
  return slot.material||'';
}
function _escAttr(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function _updateSlotMarker(sel){
  var opt=sel.options[sel.selectedIndex];
  var color=opt&&opt.dataset.color?opt.dataset.color:'#888';
  var slotIdx=parseInt(opt.value);
  var paintIdx=sel.dataset.paint;
  var marker=document.querySelector('.fd-slot-marker[data-for-paint="'+paintIdx+'"]');
  if(marker){
    marker.style.background=color;
    marker.style.color=_contrastText(color);
    marker.textContent=(slotIdx+1);
    marker.title=(opt&&opt.dataset.profile)?opt.dataset.profile:'';
  }
}

function openFilamentDialog(slots){
  _amsSlots=slots
    .filter(function(s){return s.status==='loaded';})
    .sort(function(a,b){return (a.slot_index||0)-(b.slot_index||0);});
  _loadSpoolmanStatus();
  var dlg=document.getElementById('filament-dialog');
  var title=document.getElementById('fd-title');
  var body=document.getElementById('fd-slots');
  if(title)title.textContent=_storeFilename;
  // Pre-fills the auto-leveling checkbox with the global default
  var fdAl=document.getElementById('fd-auto-leveling');
  if(fdAl) fdAl.checked=(S.auto_leveling===undefined?true:!!S.auto_leveling);
  // Loads the object list — as soon as a file ID can be resolved (Issue #57 point 3:
  // skip-part parity also in banner/upload mode, not only in store mode).
  // startReadyFileWithSlots() sets _storeFileId also in banner mode via the
  // filename→fileObj lookup, so here it is enough to check _storeFileId.
  _printObjects=[];
  _printObjectsSvg='';
  var objSection=document.getElementById('fd-objects-section');
  var objBody=document.getElementById('fd-objects-body');
  var objArrow=document.getElementById('fd-objects-arrow');
  if(objSection)objSection.style.display='none';
  if(objBody)objBody.style.display='none';      // always starts collapsed
  if(objArrow)objArrow.style.transform='';
  if(_storeFileId){
    // On a fresh upload via Orca/web the printer only delivers the object list
    // (objects_skip_parts) depois via fileDetails → fica vazia no store por um instante.
    // So it asks several times until the objects arrive (Issue #57 skip-part parity).
    var _objFid=_storeFileId;
    var _objTries=0;
    (function _loadObjects(){
      if(_objFid!==_storeFileId) return; // the dialog switched file → aborts
      fetch(_apiUrl('/kx/files/'+encodeURIComponent(_objFid)+'/objects'))
        .then(function(r){return r.json()})
        .then(function(d){
          var names=(d.result&&d.result.names)||[];
          var svg=(d.result&&d.result.svg_b64)||'';
          if(names.length>=2){
            _printObjectsSvg=svg;
            _printObjects=names.map(function(n){return {name:n,skip:false};});
            renderObjectChecklist(); renderObjectSvg();
            var cnt=document.getElementById('fd-objects-count');
            if(cnt)cnt.textContent='('+names.length+')';
            if(objSection)objSection.style.display='block';
          } else if(_objTries++ < 6){
            setTimeout(_loadObjects, 1000); // waits up to ~6s for fileDetails
          }
        }).catch(function(){});
    })();
  }

  // GCode channels: preferably from _gcodeFilaments, otherwise derived from the occupied AMS slots
  var channels=_gcodeFilaments.length?_gcodeFilaments:_amsSlots.map(function(s,i){
    return {slot_index:i,color_hex:s.color_hex,material:s.material};
  });

  // Default mapping strategy:
  // 1) keep the order when possible (line i -> nearest compatible slot i)
  // 2) keep the unique defaults while there are compatible slots
  // 3) usa a proximidade de cor como desempate
  function _hexToRgb(hex){
    var c=(hex||'').replace('#','');
    if(c.length===3)c=c[0]+c[0]+c[1]+c[1]+c[2]+c[2];
    if(c.length<6)return [255,255,255];
    return [parseInt(c.slice(0,2),16),parseInt(c.slice(2,4),16),parseInt(c.slice(4,6),16)];
  }
  function _colorDist(a,b){
    var ar=_hexToRgb(a), br=_hexToRgb(b);
    var dr=ar[0]-br[0], dg=ar[1]-br[1], db=ar[2]-br[2];
    return (dr*dr + dg*dg + db*db);
  }
  var defaultSlotByPaint={};
  var usedDefaultSlot={};
  channels.forEach(function(gc,i){
    var compatible=_amsSlots.filter(function(s){
      return _materialsCompatible(gc.material, s.material);
    });
    if(!compatible.length){
      defaultSlotByPaint[i]=-1;
      return;
    }

    var ranked=compatible.slice().sort(function(a,b){
      var da=Math.abs((a.slot_index||0)-i), db=Math.abs((b.slot_index||0)-i);
      if(da!==db)return da-db;
      var ca=_colorDist(gc.color_hex, a.color_hex), cb=_colorDist(gc.color_hex, b.color_hex);
      if(ca!==cb)return ca-cb;
      return (a.slot_index||0)-(b.slot_index||0);
    });

    var chosen=ranked.find(function(s){return !usedDefaultSlot[s.slot_index];}) || ranked[0];
    defaultSlotByPaint[i]=chosen?chosen.slot_index:-1;
    if(chosen) usedDefaultSlot[chosen.slot_index]=1;
  });

  if(!_amsSlots.length){
    body.innerHTML='<p style="color:var(--txt2);font-size:13px;text-align:center;padding:16px 0">'+T.fd_no_slots_msg.replace('{br}','<br>')+'</p>';
  } else {
    body.innerHTML=channels.map(function(gc,i){
      var isUsed=(gc&&gc.is_used!==false);
      // Only allows slots with a compatible material.
      var compatible=_amsSlots.filter(function(s){
        return _materialsCompatible(gc.material, s.material);
      });

      var defaultSlotIndex=(defaultSlotByPaint.hasOwnProperty(i)?defaultSlotByPaint[i]:-1);
      var defaultSlot=compatible.find(function(s){return s.slot_index===defaultSlotIndex;})||null;
      var opts=compatible.map(function(s){
        var sel=(defaultSlot&&s.slot_index===defaultSlot.slot_index)?'selected':'';
        return '<option value="'+s.slot_index+'" data-color="'+s.color_hex+'" data-material="'+s.material+'" data-profile="'+_escAttr(_slotProfileLabel(s))+'" '+sel+'>'+
          T.fd_slot+' '+(s.slot_index+1)+' · '+_slotProfileLabel(s)+'</option>';
      }).join('');
      if(!compatible.length){
        opts='<option value="-1" data-color="#888888" data-material="" selected>'+T.fd_no_matching_material+'</option>';
      }
      // Channel box (left): colored box with number + auto-contrast text
      var txt=_contrastText(gc.color_hex);
      var slotColor=defaultSlot?defaultSlot.color_hex:'#888';
      var slotTxt=_contrastText(slotColor);
      var usedBadge=isUsed
        ? '<span style="font-size:10px;color:var(--ok);font-weight:700;min-width:32px">'+T.fd_used+'</span>'
        : '<span style="font-size:10px;color:var(--txt2);font-weight:700;min-width:32px;opacity:.75">'+T.fd_used+'</span>';
      return '<div style="display:flex;align-items:center;gap:8px;padding:8px;border-radius:6px;background:var(--raised);border:1px solid var(--border)">'+
        '<span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:'+gc.color_hex+';color:'+txt+';font-weight:700;font-size:13px;border:1px solid var(--border);flex-shrink:0">'+(i+1)+'</span>'+
        '<span style="font-size:11px;color:var(--txt2);min-width:36px">'+gc.material+'</span>'+
        usedBadge+
        '<i data-icon="arrow-right" style="color:var(--txt2)"></i>'+
        '<span class="fd-slot-marker" data-for-paint="'+i+'" title="'+_escAttr(defaultSlot?_slotProfileLabel(defaultSlot):'')+'" style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:5px;background:'+slotColor+';color:'+slotTxt+';font-weight:700;font-size:12px;border:1px solid var(--border);flex-shrink:0">'+(defaultSlot?defaultSlot.slot_index+1:'?')+'</span>'+
        '<select data-paint="'+i+'" data-paint-color="'+gc.color_hex+'" data-is-used="'+(isUsed?'1':'0')+'" data-has-compatible="'+(compatible.length?'1':'0')+'" '+(compatible.length?'':'disabled')+' onchange="_updateSlotMarker(this)" style="flex:1;min-width:0;padding:4px 6px;border-radius:6px;border:1px solid var(--border);background:var(--raised);color:var(--txt);font-size:12px">'+
        opts+'</select>'+
      '</div>';
    }).join('');
  }
  if(dlg)dlg.classList.add('open');
  // Builds the Spoolman section after rendering the slots (needs the selects in the DOM)
  setTimeout(_buildSpoolmanSection, 0);
}

function closeFilamentDialog(){
  var dlg=document.getElementById('filament-dialog');
  if(dlg)dlg.classList.remove('open');
  _showMismatchWarn(null);
  _fdDialogOpen=false;
  if(_fdAutoOpenedFile){
    _fdUserCancelled=true;
    sessionStorage.setItem('fdUserCancelled','1');
    sessionStorage.setItem('fdAutoOpenedFile',_fdAutoOpenedFile);
  }
}

function confirmFilamentPrint(){
  var selects=document.querySelectorAll('#fd-slots select');
  var assignments=[];
  var missingCompatible=0;
  selects.forEach(function(sel){
    var paintIdx=parseInt(sel.dataset.paint);
    var paintColor=sel.dataset.paintColor;
    var isUsed=(sel.dataset.isUsed==='1');
    var hasCompatible=(sel.dataset.hasCompatible==='1');
    var opt=sel.options[sel.selectedIndex];
    var amsIdx=parseInt(opt&&opt.value);
    if(!hasCompatible || Number.isNaN(amsIdx) || amsIdx < 0){
      if(isUsed) missingCompatible += 1;
      amsIdx = -1;
    }
    var amsSlot=_amsSlots.find(function(s){return s.slot_index===amsIdx;})||{};
    // Cor como [R,G,B,255]
    function hexToRgba(h){
      var c=h.replace('#','');
      if(c.length===3)c=c[0]+c[0]+c[1]+c[1]+c[2]+c[2];
      return [parseInt(c.slice(0,2),16),parseInt(c.slice(2,4),16),parseInt(c.slice(4,6),16),255];
    }
    assignments.push({
      paint_index:  paintIdx,
      is_used:      isUsed,
      slot_index:   amsIdx,
      material:     opt.dataset.material||'PLA',
      paint_color:  hexToRgba(paintColor||'#ffffff'),
      ams_color:    hexToRgba(amsSlot.color_hex||'#ffffff'),
    });
  });
  if(missingCompatible>0){
    clog('Não é possível iniciar a impressão: '+missingCompatible+' pintura(s) usada(s) sem slot de material correspondente','msg-err');
    return;
  }
  // Pular antes de imprimir: junta os nomes dos objetos marcados
  var excludedObjects=_printObjects.filter(function(o){return o.skip;}).map(function(o){return o.name;});
  var fdAlEl=document.getElementById('fd-auto-leveling');
  var fdAutoLeveling=fdAlEl?( fdAlEl.checked?1:0):(S.auto_leveling===undefined?1:S.auto_leveling?1:0);
  // Spoolman: gathers the slot→spool mapping from the dialog and sends it
  if(_spoolmanStatus.configured){
    var slotMap={};
    document.querySelectorAll('[data-spool-slot]').forEach(function(sel){
      var idx=sel.dataset.spoolSlot;
      var val=parseInt(sel.value);
      if(val>0)slotMap[idx]=val;
    });
    _slotSpoolMap=slotMap;
    post('/kx/spoolman/active-spool',{slot_map:slotMap}).catch(function(){});
  }
  closeFilamentDialog();
  if(_filamentDialogMode==='banner'){
    // Banner mode: prefers /kx/print when _storeFileId is known (same path as the file browser).
    var btn=document.getElementById('file-ready-btn');
    if(btn){btn.disabled=true;btn.textContent='…';}
    var startPromise;
    if(_storeFileId){
      startPromise=fetch(_apiUrl('/kx/print'),{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          file_id:_storeFileId,
          filament_assignments:assignments,
          excluded_objects:excludedObjects,
          auto_leveling:fdAutoLeveling
        })
      });
    }else{
      startPromise=post('/printer/print/start',{
        filename:S.file_ready||_storeFilename,
        filament_assignments:assignments,
        excluded_objects:excludedObjects,
        auto_leveling:fdAutoLeveling
      });
    }
    startPromise.then(function(r){
      if(!r.ok){return r.text().then(function(t){throw new Error(t||('HTTP '+r.status));});}
      return r.json();
    }).then(function(d){
      if(d&&d.error) throw new Error(srvMsg(d.error));
      if(d&&d.result&&d.result!=='ok') throw new Error(String(d.result));
      document.getElementById('file-ready-banner').style.display='none';
      if(btn){btn.disabled=false;setText('file-ready-btn',T.file_ready_btn);}
    }).catch(function(e){
      clog(tr('log_error')+' '+e,'msg-err');
      if(btn){btn.disabled=false;setText('file-ready-btn',T.file_ready_btn);}
    });
  } else {
    // Modo store: POST /kx/print
    fetch(_apiUrl('/kx/print'),{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file_id:_storeFileId,filament_assignments:assignments,excluded_objects:excludedObjects,auto_leveling:fdAutoLeveling})
    }).then(function(r){return r.json()}).then(function(d){
      if(d.result==='ok'){clog('Impressão iniciada: '+_storeFilename,'msg-ok');showPanel('dashboard');}
      else{clog('Erro na impressão: '+(srvMsg(d.error)||'?'),'msg-err');}
    }).catch(function(e){clog('Erro na impressão: '+e,'msg-err');});
  }
}

function renderObjectChecklist(){
  var box=document.getElementById('fd-objects');
  if(!box)return;
  box.innerHTML=_printObjects.map(function(o,i){
    var label=o.name;
    // Klipper names are often "File.stl_id_N_copy_M" → shows them more nicely
    var m=label.match(/^(.+)\.stl_id_(\d+)_copy_(\d+)$/);
    if(m)label=m[1]+' #'+(parseInt(m[2])+1)+(m[3]!=='0'?' ('+(parseInt(m[3])+1)+')':'');
    return '<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;background:var(--raised);border:1px solid var(--border);cursor:pointer;font-size:12px">'+
      '<input type="checkbox" data-idx="'+i+'" '+(o.skip?'checked':'')+' onchange="_toggleObjectSkip('+i+',this.checked)">'+
      '<span style="word-break:break-all">'+escHtml(label)+'</span>'+
      '</label>';
  }).join('');
}
function _toggleObjectSkip(idx,val){
  if(_printObjects[idx])_printObjects[idx].skip=!!val;
  renderObjectSvg();
}
// Issue #57 point 3: collapses/expands the skip-part area
function toggleFdObjects(){
  var body=document.getElementById('fd-objects-body');
  var arrow=document.getElementById('fd-objects-arrow');
  if(!body)return;
  var open=body.style.display!=='none';
  body.style.display=open?'none':'block';
  if(arrow)arrow.style.transform=open?'':'rotate(90deg)';
}
function renderObjectSvg(){
  var box=document.getElementById('fd-objects-svg');
  if(!box)return;
  if(!_printObjectsSvg||!_printObjects.length){box.style.display='none';box.innerHTML='';return;}
  box.style.display='block';
  var svg=''; try{ svg=atob(_printObjectsSvg);}catch(e){ box.style.display='none'; return; }
  box.innerHTML=svg;
  var svgEl=box.querySelector('svg');
  if(!svgEl)return;
  svgEl.style.width='100%'; svgEl.style.maxHeight='200px'; svgEl.style.height='auto';
  _printObjects.forEach(function(o,i){
    var g=svgEl.querySelector('g[id="'+CSS.escape(o.name)+'"]');
    if(!g)return;
    var path=g.querySelector('path');
    g.style.cursor='pointer';
    g.setAttribute('opacity', o.skip?'0.8':'0.35');
    if(path){
      path.setAttribute('fill', o.skip?'#ff5e5b':'#5fa7ff');
      path.setAttribute('fill-opacity', o.skip?'0.4':'0.18');
    }
    g.onclick=function(){
      _printObjects[i].skip=!_printObjects[i].skip;
      renderObjectChecklist(); renderObjectSvg();
    };
  });
}

// ── Skip during the print ──
var _skipObjects=[]; // [{name, skipped, willSkip}]
var _skipSvg='';
var _skipPollTimer=null;
function _applySkipDialogState(s){
  s=s||{};
  _skipSvg=s.svg_b64||'';
  var skipped=s.skipped||[];
  // Keeps the pending selection (willSkip) on refresh.
  var prevWillSkip={};
  (_skipObjects||[]).forEach(function(o){if(o&&o.name)prevWillSkip[o.name]=!!o.willSkip;});
  _skipObjects=(s.objects||[]).map(function(n){
    var isSkipped=(skipped.indexOf(n)>=0);
    return {name:n, skipped:isSkipped, willSkip:isSkipped?false:!!prevWillSkip[n]};
  });
  renderSkipList(); renderSkipSvg();
}
function openSkipDialog(){
  document.getElementById('skip-status').textContent='';
  document.getElementById('skip-confirm').disabled=false;
  _refreshSkipDialog();
  if(_skipPollTimer)clearInterval(_skipPollTimer);
  _skipPollTimer=setInterval(function(){
    var dlg=document.getElementById('skip-dialog');
    if(!(dlg&&dlg.classList.contains('open')))return;
    _refreshSkipDialog();
  },2000);
  document.getElementById('skip-dialog').classList.add('open');
}
function _refreshSkipDialog(){
  // The query endpoint internally waits for a new skip/report (up to 1.5s).
  fetch(_apiUrl('/kx/skip/query'),{method:'POST'})
    .then(function(r){return r.json();})
    .then(function(d){_applySkipDialogState(d.result||{});})
    .catch(function(){
      fetch(_apiUrl('/kx/skip/state')).then(function(r){return r.json();}).then(function(d){
        _applySkipDialogState(d.result||{});
      }).catch(function(){});
    });
}
function closeSkipDialog(){
  if(_skipPollTimer){clearInterval(_skipPollTimer);_skipPollTimer=null;}
  document.getElementById('skip-dialog').classList.remove('open');
}
function _shortLabel(name){
  var m=name.match(/^(.+)\.[sS][tT][lL]_id_(\d+)_copy_(\d+)$/);
  if(!m)return name;
  return m[1]+' #'+(parseInt(m[2])+1)+(m[3]!=='0'?' ('+(parseInt(m[3])+1)+')':'');
}
function renderSkipList(){
  var box=document.getElementById('skip-list');
  if(!box)return;
  if(!_skipObjects.length){
    box.innerHTML='<div style="color:var(--txt2);font-size:12px;padding:12px;text-align:center">'+tr('skip_no_objects')+'</div>';
    return;
  }
  box.innerHTML=_skipObjects.map(function(o,i){
    var label=_shortLabel(o.name);
    var dis=o.skipped?'disabled':'';
    var note=o.skipped?'<span style="font-size:11px;color:var(--warn);margin-left:auto">'+tr('skip_already')+'</span>':'';
    return '<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;background:var(--raised);border:1px solid var(--border);font-size:12px;'+(o.skipped?'opacity:0.5':'')+'">'+
      '<input type="checkbox" data-idx="'+i+'" '+(o.willSkip?'checked':'')+' '+dis+' onchange="_toggleWillSkip('+i+',this.checked)">'+
      '<span style="word-break:break-all">'+escHtml(label)+'</span>'+note+
      '</label>';
  }).join('');
}
function renderSkipSvg(){
  var box=document.getElementById('skip-svg');
  if(!box)return;
  if(!_skipSvg||!_skipObjects.length){box.style.display='none';box.innerHTML='';return;}
  box.style.display='block';
  // Decodifica o SVG do base64
  var svg='';
  try{ svg=atob(_skipSvg); }catch(e){ box.style.display='none'; return; }
  box.innerHTML=svg;
  // Makes the polygons interactive: each <g id="..."> matches an object
  var svgEl=box.querySelector('svg');
  if(!svgEl)return;
  svgEl.style.width='100%'; svgEl.style.maxHeight='280px'; svgEl.style.height='auto';
  _skipObjects.forEach(function(o,i){
    var g=svgEl.querySelector('g[id="'+CSS.escape(o.name)+'"]');
    if(!g)return;
    var path=g.querySelector('path');
    if(o.skipped){
      // already skipped → grayed out, no click
      g.setAttribute('opacity','0.25');
      if(path){path.setAttribute('fill','#888');path.setAttribute('fill-opacity','0.3');}
      g.style.cursor='not-allowed';
    } else {
      g.style.cursor='pointer';
      g.setAttribute('opacity', o.willSkip?'0.8':'0.35');
      if(path){
        path.setAttribute('fill', o.willSkip?'#ff5e5b':'#5fa7ff');
        path.setAttribute('fill-opacity', o.willSkip?'0.4':'0.18');
      }
      g.onclick=function(){
        _skipObjects[i].willSkip=!_skipObjects[i].willSkip;
        renderSkipList(); renderSkipSvg();
      };
    }
  });
}
function _toggleWillSkip(idx,val){
  if(_skipObjects[idx])_skipObjects[idx].willSkip=!!val;
  renderSkipSvg();
}
function confirmSkip(){
  var names=_skipObjects.filter(function(o){return o.willSkip;}).map(function(o){return o.name;});
  var st=document.getElementById('skip-status');
  var btn=document.getElementById('skip-confirm');
  if(!names.length){st.textContent=tr('skip_select_at_least_one');st.style.color='var(--warn)';return;}
  btn.disabled=true; st.textContent=tr('skip_sending'); st.style.color='var(--txt2)';
  fetch(_apiUrl('/kx/skip'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:names})})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(res){
      if(!res.ok){st.textContent=(res.j&&srvMsg(res.j.error))||tr('error','Error');st.style.color='var(--err)';btn.disabled=false;return;}
      st.textContent=tr('skip_success');st.style.color='var(--ok)';
      // Keeps the dialog open + reloads so the "skipped" status appears
      setTimeout(function(){ _refreshSkipDialog(); btn.disabled=false; st.textContent=''; }, 1500);
    })
    .catch(function(e){st.textContent=''+e;st.style.color='var(--err)';btn.disabled=false;});
}

function storeDelete(fileId){
  if(!confirm(T.store_delete_confirm)) return;
  fetch(_apiUrl('/kx/files/'+fileId),{method:'DELETE'}).then(function(r){
    if(r.ok){loadStore();}
    else{clog('Falha ao excluir','msg-err');}
  });
}

function storeDownload(fileId){
  var a=document.createElement('a');
  a.href=_apiUrl('/kx/files/'+encodeURIComponent(fileId)+'/download');
  a.style.display='none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ── Add printer ──
function openAddPrinterDialog(){
  document.getElementById('apd-ip').value='';
  document.getElementById('apd-name').value='';
  var st=document.getElementById('apd-status');st.textContent='';st.style.color='var(--txt2)';
  document.getElementById('apd-confirm').disabled=false;
  document.getElementById('add-printer-dialog').classList.add('open');
}
function closeAddPrinterDialog(){
  document.getElementById('add-printer-dialog').classList.remove('open');
}
function confirmAddPrinter(){
  var ip=document.getElementById('apd-ip').value.trim();
  var name=document.getElementById('apd-name').value.trim();
  var st=document.getElementById('apd-status'),btn=document.getElementById('apd-confirm');
  if(!ip){st.textContent=T.apd_err_ip;st.style.color='var(--err)';return;}
  st.textContent=T.apd_fetching;st.style.color='var(--txt2)';btn.disabled=true;
  fetch('/kx/printers/add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({printer_ip:ip,name:name})})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(res){
      if(!res.ok){st.textContent=(res.j&&srvMsg(res.j.error))||tr('error','Error');st.style.color='var(--err)';btn.disabled=false;return;}
      st.textContent=T.apd_success;st.style.color='var(--ok)';
      setTimeout(function(){location.reload();},2500);
    })
    .catch(function(e){st.textContent=''+e;st.style.color='var(--err)';btn.disabled=false;});
}
function removePrinter(id,name){
  if(!confirm(T.printers_remove_confirm.replace('{name}',name)))return;
  fetch('/kx/printers/'+encodeURIComponent(id),{method:'DELETE'})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(res){
      if(!res.ok){alert((res.j&&srvMsg(res.j.error))||tr('error','Error'));return;}
      setTimeout(function(){location.href='/printer1';},2000);
    })
    .catch(function(e){alert(''+e);});
}

// ── Aba Impressoras ──
// ── Who is connected to each printer (OrcaSlicer, Obico, browser...) ──
function _renderClients(list){
  var rows=list.map(function(c){
    var icon=c.app==='Browser'?'globe':(/slicer|studio|cura/i.test(c.app)?'monitor':'smartphone');
    // Generic names come from the backend in English; the UI translates them.
    var app={'Browser':tr('client_browser','Navegador'),'Python script':tr('client_python','Script Python'),'Unknown':tr('client_unknown','Desconhecido')}[c.app]||c.app;
    var nome=app+(c.version?' '+c.version:'');
    var onde=(c.host?c.host+' · ':'')+c.ip;
    var quando=c.live?tr('printers_client_live'):tr('printers_client_ago').replace('{s}',c.idle_s);
    return '<div class="client-row"><i data-icon="'+icon+'"></i>'+
      '<span class="client-name"><b>'+escHtml(nome)+'</b> <span class="muted">'+escHtml(onde)+'</span></span>'+
      '<span class="client-when'+(c.live?' live':'')+'">'+escHtml(quando)+'</span></div>';
  }).join('');
  return '<div class="pcard-clients"><div class="field-label">'+escHtml(tr('printers_clients'))+'</div>'+
    (rows||'<div class="muted" style="font-size:13px">'+escHtml(tr('printers_clients_none'))+'</div>')+'</div>';
}
// Refreshes the Printers tab every 10 s while it is open, so the list
// de conectados acompanhar quem entra e sai.
setInterval(function(){
  var pn=document.getElementById('panel-printers');
  if(pn&&pn.classList.contains('active')&&!document.hidden) loadPrinterTab(true);
},10000);
function loadPrinterTab(silent){
  var grid=document.getElementById('printers-grid');
  if(grid&&!silent)grid.innerHTML='<div style="color:var(--txt2);font-size:13px;padding:20px">'+T.printers_loading+'</div>';
  // Fetches the printer list on the local instance
  fetch('/kx/printers').then(function(r){return r.json()}).then(function(d){
    var printers=d.result||[];
    if(!printers.length){
      if(grid)grid.innerHTML='<div class="empty-state" style="grid-column:1/-1">'+
        '<div data-icon="printer" style="font-size:34px;margin-bottom:10px"></div>'+
        '<div style="margin-bottom:14px">'+escHtml(T.printers_empty_hint||'')+'</div>'+
        '<button class="btn btn-accent" data-icon="plus" onclick="openAddPrinterDialog()">'+escHtml(T.add_printer||'')+'</button>'+
      '</div>';
      return;
    }
    // Fetches each printer's status in parallel
    var fetches=printers.map(function(p){
      var url=(p.bridge_url||'').replace(/\/+$/,'');
      return fetch(url+'/api/state',{signal:AbortSignal.timeout(3000)})
        .then(function(r){return r.json()})
        .then(function(s){return {printer:p,state:s,online:true};})
        .catch(function(){return {printer:p,state:{},online:false};});
    });
    Promise.all(fetches).then(function(results){
      var activeId=_activePrinter?String(_activePrinter.id):null;
      if(grid)grid.innerHTML=results.map(function(res){
        var p=res.printer,s=res.state,online=res.online;
        var isActive=String(p.id)===activeId;
        var url=(p.bridge_url||'').replace(/\/+$/,'');
        var printerNum=p.id;
        var ks=online?(s.kobra_state||'free'):'offline';
        var stateKey='kobra_'+ks;
        var stateLabel=T[stateKey]||ks;
        // State = tape (as in the top rail), not just a colored dot.
        var tapeCls=ks==='free'?'green':ks==='printing'?'yellow':(ks==='offline'||ks==='error')?'red':'';
        var progress=online&&s.progress?Math.round(s.progress*100):null;
        var filename=online&&s.filename?s.filename:'';
        var nt=online&&s.nozzle_temp?s.nozzle_temp.toFixed(1):'–';
        var bt=online&&s.bed_temp?s.bed_temp.toFixed(1):'–';
        var nameEsc=jsq(p.name);
        return '<div class="card pcard'+(isActive?' active':'')+'">'+
          '<div class="pcard-head">'+
            '<span class="pcard-name" data-icon="printer">'+escHtml(p.name)+'</span>'+
            (p.has_power_control?'<button class="icon-btn" id="power-btn-'+printerNum+'" data-icon="power" onclick="togglePrinterPower(\''+printerNum+'\')" title="'+escHtml(T.printers_power||'')+'"></button>':'')+
            '<button class="icon-btn" data-icon="x" onclick="removePrinter(\''+printerNum+'\',\''+nameEsc+'\')" title="'+escHtml(T.printers_remove||'')+'"></button>'+
          '</div>'+
          '<div class="pcard-state"><span class="dymo '+tapeCls+'">'+escHtml(stateLabel)+'</span>'+
            (isActive?'<span class="muted" style="font-size:13px">'+escHtml(T.printers_active||'')+'</span>':'')+'</div>'+
          '<div class="pcard-lines">'+
            (p.printer_ip?'<span data-icon="wifi">'+escHtml(p.printer_ip)+'</span>':'')+
            (filename?'<span data-icon="file-code" title="'+escHtml(filename)+'">'+escHtml(filename)+'</span>':'')+
          '</div>'+
          (progress!==null?'<div class="progress-bar"><div class="progress-fill" style="width:'+progress+'%"></div></div>':'')+
          '<div class="pcard-temps"><span data-icon="flame">'+nt+'°C</span><span data-icon="layers">'+bt+'°C</span></div>'+
          (online?_renderClients(s.clients||[]):'')+
          (!isActive?'<a class="btn btn-accent" data-icon="arrow-right" href="'+url+'/printer'+printerNum+'" style="text-decoration:none">'+escHtml(T.printers_switch||'')+'</a>':'')+
        '</div>';
      }).join('');
      results.forEach(function(res){
        if(res.printer.has_power_control)_refreshPrinterPowerIcon(res.printer.id,(res.printer.bridge_url||'').replace(/\/+$/,''));
      });
    });
  }).catch(function(e){
    if(grid)grid.innerHTML='<div style="color:var(--err);font-size:13px;padding:20px">'+tr('error_prefix','Error: ')+escHtml(e)+'</div>';
  });
}

function _refreshPrinterPowerIcon(pid,bridgeUrl){
  fetch((bridgeUrl||'')+'/kx/printers/'+encodeURIComponent(pid)+'/power-status',{signal:AbortSignal.timeout(5000)})
    .then(function(r){return r.json()})
    .then(function(d){
      var btn=document.getElementById('power-btn-'+pid);
      if(!btn)return;
      if(d.state==='on'){btn.style.color='var(--ok)';btn.title=T.printers_power_on||'Power: On';}
      else if(d.state==='off'){btn.style.color='var(--txt2)';btn.title=T.printers_power_off||'Power: Off';}
    })
    .catch(function(){/* status endpoint is optional - the icon just stays neutral */});
}

function togglePrinterPower(pid){
  var btn=document.getElementById('power-btn-'+pid);
  var currentlyOn=btn&&btn.style.color&&btn.style.color.indexOf('var(--ok)')!==-1;
  // Without a known current state, assume "turn on" - turning on an already
  // off switch is harmless, whereas guessing "turn off" on a printer mid-print is not.
  var action=currentlyOn?'off':'on';
  if(action==='off'&&!confirm(T.printers_power_off_confirm||'Turn off the printer? Make sure no print is in progress.'))return;
  if(btn)btn.style.opacity='0.5';
  post('/kx/printers/'+encodeURIComponent(pid)+'/power',{action:action}).then(function(){
    if(btn)btn.style.opacity='1';
    setTimeout(function(){loadPrinterTab();},1500);
  }).catch(function(e){
    if(btn)btn.style.opacity='1';
    clog('Erro de energia: '+e,'msg-err');
  });
}

// ── 3D part ──────────────────────────────────────────────────────────────
// The G-code of the printing (or loaded) file comes from the bridge store once,
// is parsed here and drawn in pure WebGL: finished layers in full color, the
// current layer in yellow tape, the rest as a ghost. No animation loop: it only
// redraws when the layer changes or when you rotate/zoom - zero cost when idle,
// e nada disso roda no bridge. ponytail: WebGL1 + gl.LINES em vez de Three.js
// (o webview do Orca exigiria embutir ~600 KB); se um dia precisar de sombreado
// for real (tubes, light), then it is worth pulling in the library.
// 3D G-code preview. pfx swaps the id prefix ("preview-canvas" → pfx+"-canvas"):
// the dashboard uses "preview"; the Quote creates another with {full, slicerColors}.
function makeGcodePreview(pfx,opts){
  opts=opts||{};
  var MAX_BYTES=80*1024*1024;      // G-code larger than this: does not download (phones struggle)
  var canvas,gl,prog,buf,nVerts=0,layerOf=null,nLayers=0,bounds=null;
  var yaw=-0.7,pitch=0.9,dist=1,panX=0,panY=0,curLayer=-1,loadedName='',loading='';
  var uMvp,uCur,uColDone,uColCur,uColGhost,aPos,aLayer,aDir,uLight,uShade,aTool,uPal,uUsePal,bedBuf,bedN=0;
  var palette=null,palSig='',fileRec=null,toolsUsed={},look={colours:{},glow:{}},glowArr=new Float32Array(16),anyGlow=false,uGlw,uGp,uOff;
  var fileIdByName={},layerZs=[],estLayer=false,fullView=opts.full?true:(function(){try{return localStorage.getItem('previewFull')==='1';}catch(e){return false;}})();
  if(!opts.full)window.addEventListener('DOMContentLoaded',function(){setFull(fullView);});

  function el(id){return document.getElementById(id.replace(/^preview/,pfx));}
  function setEmpty(msg,icon){
    var e=el('preview-empty');if(!e)return;
    if(msg){e.style.display='flex';e.dataset.icon=icon||'box';el('preview-empty-txt').textContent=msg;}
    else e.style.display='none';
  }
  function init(){
    canvas=el('preview-canvas');if(!canvas||gl)return !!gl;
    try{gl=canvas.getContext('webgl',{antialias:true,alpha:true});}catch(e){gl=null;}
    if(!gl){setEmpty(tr('preview_no_webgl')||'WebGL unavailable in this browser','triangle-alert');return false;}
    // Shading without a mesh: the wall formed by a G-code segment has a normal
    // horizontal perpendicular to its direction. Light that follows the camera + a light/dark
    // band per layer + darkening with distance = readable volume.
    // Color of each segment = color of the ACE slot of the tool (T0..T15) that extruded it.
    var vs='attribute vec3 p;attribute float l;attribute vec2 t;attribute float tl;uniform mat4 m;uniform float c,sh,usePal,gp;uniform vec2 off;uniform vec3 lt;uniform vec4 cd,cc,cg;uniform vec3 pal[16];uniform float glw[16];varying vec4 v;'+
      'void main(){gl_Position=m*vec4(p,1.0);vec3 fc=usePal>0.5?pal[int(tl)]:cd.rgb;float gw=usePal>0.5&&l<c+0.5?glw[int(tl)]:0.0;'+
      // Glow-in-the-dark filament halo: the same stroke offset by a few px, added (additive blend).
      'if(gp>0.0){gl_Position.xy+=off*gl_Position.w;float ga=gw*gp;v=vec4(fc*ga,ga);return;}'+
      'vec4 base=l<c-0.5?vec4(fc,1.0):(l<c+0.5?vec4(cc.rgb,1.0):vec4(cg.rgb,cg.a));'+
      'vec3 n=normalize(vec3(-t.y,t.x,0.0)+vec3(0.0,0.0,0.0001));float d=abs(dot(n,lt));'+
      'float band=mod(l,2.0)<1.0?0.0:0.07;float fog=clamp(gl_Position.w/(gl_Position.w+sh),0.0,1.0);'+
      'float k=mix(1.0,(0.32+0.78*d-band)*(1.15-0.45*fog),sh>0.0&&l<c+0.5?1.0:0.0);'+
      'if(gw>0.5&&l<c-0.5){k=0.45+0.62*k;}'+
      'v=vec4(base.rgb*k,base.a);}';
    var fs='precision mediump float;varying vec4 v;void main(){if(v.a<0.003)discard;gl_FragColor=v;}';
    function sh(t,src){var s=gl.createShader(t);gl.shaderSource(s,src);gl.compileShader(s);return s;}
    prog=gl.createProgram();gl.attachShader(prog,sh(gl.VERTEX_SHADER,vs));gl.attachShader(prog,sh(gl.FRAGMENT_SHADER,fs));gl.linkProgram(prog);
    uMvp=gl.getUniformLocation(prog,'m');uCur=gl.getUniformLocation(prog,'c');
    uColDone=gl.getUniformLocation(prog,'cd');uColCur=gl.getUniformLocation(prog,'cc');uColGhost=gl.getUniformLocation(prog,'cg');
    aPos=gl.getAttribLocation(prog,'p');aLayer=gl.getAttribLocation(prog,'l');aDir=gl.getAttribLocation(prog,'t');
    uLight=gl.getUniformLocation(prog,'lt');uShade=gl.getUniformLocation(prog,'sh');
    aTool=gl.getAttribLocation(prog,'tl');uPal=gl.getUniformLocation(prog,'pal');uUsePal=gl.getUniformLocation(prog,'usePal');
    uGlw=gl.getUniformLocation(prog,'glw');uGp=gl.getUniformLocation(prog,'gp');uOff=gl.getUniformLocation(prog,'off');
    bindInput();
    if(window.ResizeObserver)new ResizeObserver(function(){draw();}).observe(canvas.parentNode);
    return true;
  }
  // Parses G-code: only extruding moves become segments.
  function parse(text){
    var zs=[],pos=[],lay=[],dir=[],tools=[],tool=-1,used={},x=0,y=0,z=0,e=0,relE=false,relXYZ=false,layer=-1,layerZ=-1;
    var mn=[1e9,1e9,1e9],mx=[-1e9,-1e9,-1e9];
    var lines=text.split('\n');
    for(var i=0;i<lines.length;i++){
      var ln=lines[i];var c0=ln.charCodeAt(0);
      if(c0===77){ // M
        if(ln.indexOf('M83')===0)relE=true;else if(ln.indexOf('M82')===0)relE=false;continue;}
      if(c0===84){var tm=/^T(\d+)/.exec(ln);if(tm)tool=Math.min(15,parseInt(tm[1],10));continue;} // T<n> = troca de ferramenta
      if(c0!==71)continue; // G
      var cmd=ln.substr(0,3);
      if(cmd==='G90'){relXYZ=false;continue;} if(cmd==='G91'){relXYZ=true;continue;}
      if(cmd==='G92'){var m92=/E(-?[\d.]+)/.exec(ln);if(m92)e=parseFloat(m92[1]);continue;}
      if(!(cmd==='G1 '||cmd==='G0 '||cmd==='G2 '||cmd==='G3 '))continue;
      var sc=ln.indexOf(';');if(sc>=0)ln=ln.slice(0,sc);
      var nx=x,ny=y,nz=z,ne=null,mt,re=/([XYZE])(-?[\d.]+)/g;
      while((mt=re.exec(ln))){var v=parseFloat(mt[2]);
        if(mt[1]==='X')nx=relXYZ?x+v:v;else if(mt[1]==='Y')ny=relXYZ?y+v:v;else if(mt[1]==='Z')nz=relXYZ?z+v:v;else ne=v;}
      var extr=ne!==null&&(relE?ne>0:ne>e+1e-5);
      if(ne!==null&&!relE)e=ne;
      if(extr&&(nx!==x||ny!==y)){
        if(nz!==layerZ){layer++;layerZ=nz;zs.push(nz);}
        pos.push(x,y,z,nx,ny,nz);lay.push(layer,layer);
        var dl=Math.hypot(nx-x,ny-y)||1;dir.push((nx-x)/dl,(ny-y)/dl,(nx-x)/dl,(ny-y)/dl);
        tools.push(tool,tool);used[tool]=1;
        if(nx<mn[0])mn[0]=nx;if(ny<mn[1])mn[1]=ny;if(nz<mn[2])mn[2]=nz;
        if(nx>mx[0])mx[0]=nx;if(ny>mx[1])mx[1]=ny;if(nz>mx[2])mx[2]=nz;
      }
      x=nx;y=ny;z=nz;
    }
    return {pos:new Float32Array(pos),lay:new Float32Array(lay),dir:new Float32Array(dir),tools:new Float32Array(tools),used:used,layers:layer+1,zs:zs,mn:mn,mx:mx};
  }
  function upload(r){
    buf=buf||{p:gl.createBuffer(),l:gl.createBuffer(),t:gl.createBuffer(),k:gl.createBuffer()};
    // Without any T<n> in the file (single color): uses the channel marked as used by the slicer.
    var t0=0;(fileRec&&fileRec.gcode_filaments||[]).some(function(g,i){if(g.is_used){t0=i;return true;}});
    for(var q=0;q<r.tools.length;q++)if(r.tools[q]<0)r.tools[q]=t0;
    toolsUsed=r.used;
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.k);gl.bufferData(gl.ARRAY_BUFFER,r.tools,gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.t);gl.bufferData(gl.ARRAY_BUFFER,r.dir,gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.p);gl.bufferData(gl.ARRAY_BUFFER,r.pos,gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.l);gl.bufferData(gl.ARRAY_BUFFER,r.lay,gl.STATIC_DRAW);
    nVerts=r.pos.length/3;nLayers=r.layers;layerZs=r.zs||[];bounds={mn:r.mn,mx:r.mx};
    // Bed: outline + 10 mm grid around the part
    var b=[],pad=20,x0=Math.floor((r.mn[0]-pad)/10)*10,x1=Math.ceil((r.mx[0]+pad)/10)*10,y0=Math.floor((r.mn[1]-pad)/10)*10,y1=Math.ceil((r.mx[1]+pad)/10)*10;
    for(var gx=x0;gx<=x1;gx+=10)b.push(gx,y0,0,gx,y1,0);
    for(var gy=y0;gy<=y1;gy+=10)b.push(x0,gy,0,x1,gy,0);
    bedBuf=bedBuf||gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,bedBuf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(b),gl.STATIC_DRAW);bedN=b.length/3;
    resetView();
  }
  function resetView(){yaw=-0.7;pitch=0.55;panX=panY=0;
    if(bounds){var dx=bounds.mx[0]-bounds.mn[0],dy=bounds.mx[1]-bounds.mn[1],dz=bounds.mx[2]-bounds.mn[2];dist=Math.max(dx,dy,dz,20)*2.3;}
    draw();}
  function mat(){
    var w=canvas.clientWidth||1,h=canvas.clientHeight||1,a=w/h,f=1/Math.tan(0.4),n=1,fa=dist*10;
    var cx=(bounds.mn[0]+bounds.mx[0])/2,cy=(bounds.mn[1]+bounds.mx[1])/2,cz=(bounds.mx[2]-bounds.mn[2])/3;
    var ex=cx+dist*Math.cos(pitch)*Math.cos(yaw),ey=cy+dist*Math.cos(pitch)*Math.sin(yaw),ez=cz+dist*Math.sin(pitch);
    var zx=ex-cx,zy=ey-cy,zz=ez-cz,zl=Math.hypot(zx,zy,zz);zx/=zl;zy/=zl;zz/=zl;
    var xx=-zy,xy=zx,xz=0,xl=Math.hypot(xx,xy)||1;xx/=xl;xy/=xl;
    var yx=zy*xz-zz*xy,yy=zz*xx-zx*xz,yz=zx*xy-zy*xx;
    var v=[xx,yx,zx,0, xy,yy,zy,0, xz,yz,zz,0,
      -(xx*ex+xy*ey+xz*ez)+panX,-(yx*ex+yy*ey+yz*ez)+panY,-(zx*ex+zy*ey+zz*ez),1];
    var p=[f/a,0,0,0, 0,f,0,0, 0,0,(fa+n)/(n-fa),-1, 0,0,2*fa*n/(n-fa),0];
    var o=new Float32Array(16);
    for(var i=0;i<4;i++)for(var j=0;j<4;j++){var s=0;for(var k=0;k<4;k++)s+=p[k*4+j]*v[i*4+k];o[i*4+j]=s;}
    return o;
  }
  function col(name,alpha){var c=getComputedStyle(document.documentElement).getPropertyValue(name).trim()||'#888';
    var m=/^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})/i.exec(c);return m?[parseInt(m[1],16)/255,parseInt(m[2],16)/255,parseInt(m[3],16)/255,alpha]:[.6,.6,.6,alpha];}
  function draw(){
    if(!gl||!nVerts)return;
    var dpr=Math.min(window.devicePixelRatio||1,2),w=Math.round(canvas.clientWidth*dpr),h=Math.round(canvas.clientHeight*dpr);
    if(!w||!h)return;
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
    gl.viewport(0,0,w,h);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(prog);gl.uniformMatrix4fv(uMvp,false,mat());
    // bed (fixed "ghost" layer)
    gl.uniform1f(uCur,-10);gl.uniform1f(uShade,0);gl.uniform4fv(uColGhost,col('--rule-2',0.8));
    gl.disableVertexAttribArray(aDir);gl.vertexAttrib2f(aDir,1,0);gl.disableVertexAttribArray(aTool);gl.vertexAttrib1f(aTool,0);gl.uniform1f(uUsePal,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,bedBuf);gl.enableVertexAttribArray(aPos);gl.vertexAttribPointer(aPos,3,gl.FLOAT,false,0,0);
    gl.disableVertexAttribArray(aLayer);gl.vertexAttrib1f(aLayer,1e6);gl.drawArrays(gl.LINES,0,bedN);
    // part
    var c=curLayer<0?nLayers:curLayer;
    gl.uniform1f(uCur,c);
    // light at ~50° to the left of the camera and slightly above
    var la=yaw+0.9,lv=[Math.cos(la),Math.sin(la),0.5],ll=Math.hypot(lv[0],lv[1],lv[2]);
    gl.uniform3f(uLight,lv[0]/ll,lv[1]/ll,lv[2]/ll);gl.uniform1f(uShade,dist*0.9);
    gl.uniform4fv(uColDone,col('--ink',1));gl.uniform4fv(uColCur,col('--orange',1));gl.uniform4fv(uColGhost,col('--holo',curLayer<0?0.7:0.14));
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.p);gl.vertexAttribPointer(aPos,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.l);gl.enableVertexAttribArray(aLayer);gl.vertexAttribPointer(aLayer,1,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.t);gl.enableVertexAttribArray(aDir);gl.vertexAttribPointer(aDir,2,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,buf.k);gl.enableVertexAttribArray(aTool);gl.vertexAttribPointer(aTool,1,gl.FLOAT,false,0,0);
    gl.uniform1f(uUsePal,palette?1:0);if(palette)gl.uniform3fv(uPal,palette);
    gl.uniform1fv(uGlw,glowArr);gl.uniform1f(uGp,0);
    // Glow in the dark: halo drawn BEFORE the part (it covers the center, leaving only the outline) and
    // with MAX blend - it does not add up the overlapping layers, so it stays subtle and constant.
    var mm=anyGlow&&palette&&(gl.getExtension('EXT_blend_minmax'));
    if(mm){
      gl.disable(gl.DEPTH_TEST);gl.depthMask(false);gl.blendEquation(mm.MAX_EXT);
      // three fading rings: 1.5 px (faint), 3 px (fainter), 5 px (almost nothing)
      var RINGS=[[1.5,0.22],[3,0.12],[5,0.05]];
      for(var gi=0;gi<18;gi++){var rg=RINGS[gi%3],ga=Math.floor(gi/3)*Math.PI/3+(gi%3)*0.35;
        gl.uniform1f(uGp,rg[1]);gl.uniform2f(uOff,Math.cos(ga)*rg[0]*dpr*2/w,Math.sin(ga)*rg[0]*dpr*2/h);gl.drawArrays(gl.LINES,0,nVerts);}
      gl.uniform1f(uGp,0);gl.blendEquation(gl.FUNC_ADD);gl.depthMask(true);gl.enable(gl.DEPTH_TEST);
    }
    gl.drawArrays(gl.LINES,0,nVerts);
    gl.disableVertexAttribArray(aTool);
    var meta=el('preview-meta');
    if(meta)meta.textContent=nLayers?(curLayer>=0?(tr('preview_layer')||'Layer')+' '+(estLayer?'≈':'')+(curLayer+1)+' / '+nLayers:nLayers+' '+(tr('preview_layers')||'layers')):'';
  }
  function bindInput(){
    var drag=null,pinch=0;
    canvas.addEventListener('pointerdown',function(e){drag={x:e.clientX,y:e.clientY,pan:e.button===2||e.shiftKey};canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',function(e){if(!drag)return;var dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;
      if(drag.pan){panX+=dx*dist/600;panY-=dy*dist/600;}else{yaw-=dx*0.008;pitch=Math.max(0.05,Math.min(1.5,pitch+dy*0.008));}draw();});
    canvas.addEventListener('pointerup',function(){drag=null;});
    canvas.addEventListener('contextmenu',function(e){e.preventDefault();});
    canvas.addEventListener('wheel',function(e){e.preventDefault();dist*=e.deltaY>0?1.12:0.89;draw();},{passive:false});
    canvas.addEventListener('touchmove',function(e){if(e.touches.length===2){var d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
      if(pinch){dist*=pinch/d;draw();}pinch=d;}},{passive:true});
    canvas.addEventListener('touchend',function(){pinch=0;});
  }
  function fileId(name,cb){
    if(fileIdByName[name])return cb(fileIdByName[name]);
    fetch(_apiUrl('/kx/files')).then(function(r){return r.json();}).then(function(d){
      // The list comes newest to oldest: with repeated names (re-uploads)
      // the first stays, the newest - before, the oldest overwrote (wrong color).
      (d.result||d||[]).forEach(function(f){if(f&&f.filename&&!fileIdByName[f.filename])fileIdByName[f.filename]=f;});
      cb(fileIdByName[name]||null);
    }).catch(function(){cb(null);});
  }
  // rec: record of the already-known file (the Quote picks by id; names can repeat).
  function load(name,rec){
    var key=rec?rec.id:name;
    if(!name||key===loadedName||key===loading)return;
    if(!init())return;
    loading=key;setEmpty(tr('preview_loading')||'Loading the part…','hourglass');
    (rec?function(n,cb){cb(rec);}:fileId)(name,function(f){
      if(loading!==key)return;
      // gcode_filaments vem do banco como texto JSON
      var gf=f&&f.gcode_filaments;if(typeof gf==='string'){try{gf=JSON.parse(gf);}catch(e){gf=null;}}
      fileRec={gcode_filaments:Array.isArray(gf)?gf:[]};
      if(!f){loading='';loadedName=key;nVerts=0;draw();setEmpty(tr('preview_not_in_store')||'This part did not go through the bridge (no G-code to draw)','box');return;}
      if(f.size_bytes>MAX_BYTES){loading='';loadedName=key;setEmpty(tr('preview_too_big')||'G-code too large to draw here','box');return;}
      fetch(_apiUrl('/kx/files/'+encodeURIComponent(f.id)+'/download')).then(function(r){if(!r.ok)throw new Error(r.status);return r.text();})
        .then(function(t){
          if(loading!==key)return;
          var res=parse(t);loading='';loadedName=key;
          if(!res.pos.length){setEmpty(tr('preview_empty')||'Nothing to draw','box');return;}
          setEmpty('');palSig='';buildPalette(opts.slicerColors?{}:(S||{}));upload(res);
        }).catch(function(e){console.warn('kxPreview:',e);loading='';setEmpty(tr('preview_error')||'Could not load the part','triangle-alert');});
    });
  }
  // G-code channel N → ACE slot N (same rule as the bridge's print start).
  // Color of the configured slot; empty slot → color stored by the slicer; nothing → ink.
  function buildPalette(s){
    var slots={};(s.ams_slots||[]).forEach(function(x){slots[x.global_index]=x;});
    var gf=(fileRec&&fileRec.gcode_filaments)||[];
    var pal=new Float32Array(48),sig='';
    for(var i=0;i<16;i++){
      var rgb=null,sl=slots[i],lc=look.colours[i];
      if(lc&&/^#[\da-f]{6}$/i.test(lc))rgb=[parseInt(lc.slice(1,3),16)/255,parseInt(lc.slice(3,5),16)/255,parseInt(lc.slice(5,7),16)/255];
      else if(sl&&sl.status===5&&Array.isArray(sl.color))rgb=[sl.color[0]/255,sl.color[1]/255,sl.color[2]/255];
      else if(gf[i]&&/^#?[\da-f]{6}/i.test(gf[i].color_hex||'')){var h=gf[i].color_hex.replace('#','');rgb=[parseInt(h.slice(0,2),16)/255,parseInt(h.slice(2,4),16)/255,parseInt(h.slice(4,6),16)/255];}
      else rgb=col('--ink',1).slice(0,3);
      // Very dark filament disappears into the dark background: a slight luminosity floor.
      var lum=0.3*rgb[0]+0.59*rgb[1]+0.11*rgb[2];if(lum<0.18){var k=0.18-lum;rgb=[rgb[0]+k,rgb[1]+k,rgb[2]+k];}
      // Predawn: the part also turns observatory red (no real color lit)
      if(document.documentElement.getAttribute('data-theme')==='night'){var ln=0.3*rgb[0]+0.59*rgb[1]+0.11*rgb[2];rgb=[0.25+0.6*ln,0.06+0.12*ln,0.05+0.1*ln];}
      pal[i*3]=rgb[0];pal[i*3+1]=rgb[1];pal[i*3+2]=rgb[2];sig+=rgb.map(function(v){return v.toFixed(2);}).join(',')+(look.glow[i]?'*':'')+';';
      glowArr[i]=look.glow[i]?1:0;
    }
    anyGlow=false;for(var gq=0;gq<16;gq++)if(glowArr[gq])anyGlow=true;
    if(sig!==palSig){palSig=sig;palette=pal;return true;}
    return false;
  }
  // Called on every state update.
  function update(s){
    if(nVerts&&buildPalette(s))draw();
    var printing=['printing','paused','preheating','auto_leveling','checking'].indexOf(s.print_state)!==-1;
    var name=printing?(s.filename||''):'';
    if(!name){
      // Only shows the part while something is actually printing; stopped = empty card.
      if(loadedName||loading||nVerts){loadedName=loading='';nVerts=0;curLayer=-1;
        if(gl){gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);}
        var meta=el('preview-meta');if(meta)meta.textContent='';}
      setEmpty(tr('preview_idle')||'No part loaded','box');
      return;
    }
    load(name);
    var nl=-1;estLayer=false;
    // The printer does not always send the layer (in sub-steps, like reheating the
    // nozzle, it sends 0 of 0). Then the position comes from the Z height and, as a last resort, from the %.
    if(printing&&nLayers&&!fullView){
      if(s.total_layers>0&&s.curr_layer>0)nl=Math.round(s.curr_layer/s.total_layers*nLayers)-1;
      else if(s.z_mm>0&&layerZs.length){nl=0;while(nl<layerZs.length-1&&layerZs[nl]<s.z_mm-0.01)nl++;estLayer=true;}
      else if(s.progress>0){nl=Math.round(s.progress*nLayers)-1;estLayer=true;}
      if(nl>=0)nl=Math.max(0,Math.min(nLayers-1,nl));
    }
    if(nl!==curLayer){curLayer=nl;draw();}
  }
  // "Progress" (solid up to the current layer + hologram) or "Whole part"; remembered per browser.
  function setFull(on){
    fullView=!!on;
    try{localStorage.setItem('previewFull',fullView?'1':'0');}catch(e){}
    var w=el('preview-wrap');if(w)w.classList.toggle('full',fullView);
    document.querySelectorAll('[data-preview-mode]').forEach(function(b){b.setAttribute('aria-pressed',(b.dataset.previewMode==='full')===fullView);});
    if(window.S)update(S);else draw();
  }
  // Photo of the part for the receipt: read right after drawing (the WebGL buffer is still there).
  function snapshot(){if(!gl||!nVerts)return '';draw();try{return canvas.toDataURL('image/png');}catch(e){return '';}}
  // Appearance only for this screen (Quote): swapped color and per-tool glow {colours:{t:'#hex'},glow:{t:true}}.
  function setLook(l){look={colours:(l&&l.colours)||{},glow:(l&&l.glow)||{}};palSig='';if(nVerts){buildPalette(opts.slicerColors?{}:(window.S||{}));draw();}}
  return {update:update,setFull:setFull,resetView:function(){if(gl)resetView();},redraw:draw,load:load,snapshot:snapshot,setLook:setLook,
    ready:function(){return !!nVerts&&!loading;}};
}
var kxPreview=makeGcodePreview('preview');

// ═══════════════════════════════════════════════════════════════════════════
// QUOTE - print price from the G-code (calculation in pricing.py).
// Part → Order → Price ("stack of stages": cost at the bottom, profit on top).
// The receipt is a paper "mission manifest": HTML for printing/PDF and the
// same drawing on canvas to become an image (WhatsApp).
// ═══════════════════════════════════════════════════════════════════════════
var prPreview=null,prCfg=null,prFiles=[],prFileId='',prLast=null,prTimer=0,prSeq=0;
var prToolMat={},prToolPrice={},prRc=null,prExcluded={},prToolColour={},prToolGlow={},prTotalOv='';

// Texts with data-t="key": the HTML text is the pt-BR fallback.
function applyDataT(root){
  (root||document).querySelectorAll('[data-t]').forEach(function(e){
    if(e.dataset.tDefault===undefined)e.dataset.tDefault=e.textContent;
    e.textContent=tr(e.dataset.t,e.dataset.tDefault);
  });
}
function prLocale(){return /^pt/.test(currentLang)?'pt-BR':(currentLang||undefined);}
function prMoney(v){
  return ((prCfg&&prCfg.currency)||'R$')+' '+Number(v||0).toLocaleString(prLocale(),{minimumFractionDigits:2,maximumFractionDigits:2});
}
function prNum(v,d){return Number(v||0).toLocaleString(prLocale(),{maximumFractionDigits:d==null?1:d});}
function prDate(d){return d.toLocaleDateString(prLocale(),{day:'2-digit',month:'2-digit',year:'numeric'});}
function prJSON(url,body){
  return fetch(_apiUrl(url),body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().then(function(d){if(!r.ok||d.error)throw new Error(srvMsg(d.error)||r.status);return d.result;});});
}

function prOpen(){
  if(!prPreview)prPreview=makeGcodePreview('pp',{full:true,slicerColors:true});
  applyDataT(document.getElementById('panel-pricing'));
  var cfgP=prCfg?Promise.resolve(prCfg):prJSON('/kx/pricing/config').then(function(c){prCfg=c;return c;});
  return cfgP.then(function(){prFillSelects();return prLoadFiles();}).catch(function(e){prStatus(String(e.message||e),true);});
}
// File browser → Quote already with this part.
function prFromFile(id){
  showPanel('pricing');prTab('calc');
  prOpen().then(function(){var sel=document.getElementById('pr-file');if(sel)sel.value=id;prPickFile(id);});
}
function prTab(name){
  ['calc','quotes','config'].forEach(function(n){
    var g=document.getElementById('pr-group-'+n);if(g)g.style.display=n===name?'':'none';
    var t=document.getElementById('prtab-'+n);if(t)t.classList.toggle('active',n===name);
  });
  if(name==='quotes')prLoadQuotes();
  if(name==='config')prRenderConfig();
}
function prStatus(msg,bad){
  var w=document.getElementById('pr-warn');if(!w)return;
  w.textContent=msg||'';w.classList.toggle('bad',!!bad);
}

// ── Part ──
function prLoadFiles(){
  return prJSON('/kx/files').then(function(list){
    prFiles=(list||[]).filter(function(f){return /\.gcode$/i.test(f.filename||'');});
    var sel=document.getElementById('pr-file');if(!sel)return;
    sel.innerHTML='<option value="">'+escHtml(tr('pr_pick_file','Escolha um G-code da lista'))+'</option>'+
      prFiles.map(function(f){return '<option value="'+escHtml(f.id)+'">'+escHtml(f.filename.replace(/\.gcode$/i,''))+'</option>';}).join('');
    sel.value=prFileId;
  });
}
function prPickFile(id,keep){
  prFileId=id||'';
  if(!keep){prToolMat={};prToolPrice={};prToolColour={};prToolGlow={};prTotalOv='';var h=document.getElementById('pr-hours');if(h)h.value='';}
  prLook();
  var f=prFiles.find(function(x){return x.id===prFileId;});
  if(f&&prPreview)prPreview.load(f.filename,f);
  prCalc();
}
function prFillSelects(){
  var keep=function(id,items,label){
    var el=document.getElementById(id);if(!el)return;
    var prev=el.value!==''?el.value:(function(){try{return localStorage.getItem('pr-'+id)||'0';}catch(e){return '0';}})();
    el.innerHTML=items.map(function(x,i){return '<option value="'+i+'">'+escHtml(label(x))+'</option>';}).join('');
    el.value=prev<items.length?prev:'0';
  };
  keep('pr-channel',prCfg.channels,function(c){return c.name;});
  keep('pr-tax',prCfg.taxes,function(t){return t.name+' · '+prNum(t.pct,2)+'%';});
}

// ── Pedido ──
function prVal(id){var e=document.getElementById(id);return e?e.value:'';}
function prOpt(){
  ['pr-channel','pr-tax'].forEach(function(id){try{localStorage.setItem('pr-'+id,prVal(id));}catch(e){}});
  return {qty:prVal('pr-qty'),per_plate:prVal('pr-per-plate'),channel:prVal('pr-channel'),tax:prVal('pr-tax'),coupon:prVal('pr-coupon'),
    shipping:prVal('pr-shipping'),extras:prVal('pr-extras'),design_fee:prVal('pr-design'),hours:prVal('pr-hours'),
    discount_pct:prVal('pr-dpct'),discount_value:prVal('pr-dval'),
    urgent:!!(document.getElementById('pr-urgent')||{}).checked,materials:prToolMat,price_kg:prToolPrice,
    colours:prToolColour,glow:prToolGlow,total_override:prTotalOv,no_validity:!(document.getElementById('pr-valid')||{checked:true}).checked,
    exclude:Object.keys(prExcluded).filter(function(k){return prExcluded[k];})};
}
function prSetOpt(o){
  var map={qty:'pr-qty',per_plate:'pr-per-plate',channel:'pr-channel',tax:'pr-tax',coupon:'pr-coupon',shipping:'pr-shipping',extras:'pr-extras',
    design_fee:'pr-design',hours:'pr-hours',discount_pct:'pr-dpct',discount_value:'pr-dval'};
  Object.keys(map).forEach(function(k){var e=document.getElementById(map[k]);if(e)e.value=o[k]==null?(k==='per_plate'?1:''):o[k];});
  var u=document.getElementById('pr-urgent');if(u)u.checked=!!o.urgent;
  prToolMat=o.materials||{};prToolPrice=o.price_kg||{};prToolColour=o.colours||{};prToolGlow=o.glow||{};prTotalOv=o.total_override||'';
  var vd=document.getElementById('pr-valid');if(vd)vd.checked=!o.no_validity;
  prExcluded={};(o.exclude||[]).forEach(function(k){prExcluded[k]=true;});
}
// Real spool color and special effect (glow in the dark): visual only - preview and receipt.
function prLook(){if(prPreview&&prPreview.setLook)prPreview.setLook({colours:prToolColour,glow:prToolGlow});}
function prSetColour(t,hex){prToolColour[t]=hex;prLook();prCalcSoon();}
function prSetGlow(t,on){if(on)prToolGlow[t]=true;else delete prToolGlow[t];prLook();prCalcSoon();}
// Total typed by hand: the costs stay, fees and profit are recomputed over it (pricing.py).
function prEditTotal(){
  if(!prLast)return;var el=document.getElementById('pr-total');if(el.querySelector('input'))return;
  var r=prLast.result,done=false;
  el.innerHTML='<input type="number" class="pr-total-in num" min="0" step="0.01" inputmode="decimal" value="'+r.total.toFixed(2)+'" aria-label="'+escHtml(tr('pr_total_edit','Editar o total'))+'">';
  var i=el.querySelector('input');i.focus();i.select();
  var fim=function(ok){if(done)return;done=true;var v=parseFloat(String(i.value).replace(',','.'));
    if(ok&&v>0&&Math.abs(v-r.auto_total)>=0.005)prTotalOv=v.toFixed(2);else if(ok&&v>0)prTotalOv='';
    prCalc();};
  i.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();fim(true);}else if(e.key==='Escape'){fim(false);}});
  i.addEventListener('blur',function(){fim(true);});
}
function prResetTotal(){prTotalOv='';prCalc();}
// Toggles a single cost just for this order (e.g. no packaging when the customer picks up).
function prToggle(k,on){prExcluded[k]=!on;prCalc();}
function prCalcSoon(){clearTimeout(prTimer);prTimer=setTimeout(prCalc,250);}
function prCalc(){
  if(!prFileId){prRender(null);return;}
  var seq=++prSeq;
  prJSON('/kx/pricing/calc',{file_id:prFileId,opt:prOpt()}).then(function(out){
    if(seq!==prSeq)return;prLast=out;prRender(out);
  }).catch(function(e){if(seq===prSeq)prStatus(String(e.message||e),true);});
}

// ── Price ──
// Stages from bottom to top; profit is the "payload" on top.
function prStages(r){
  var c=r.costs;
  return [
    {k:'material',v:c.material,cls:'s-mat'},
    {k:'flush',v:c.flush,cls:'s-mat',x:1},
    {k:'energy',v:c.energy,cls:'s-energy',x:1},
    {k:'machine',v:c.machine,cls:'s-machine',x:1},
    {k:'labor',v:c.labor,cls:'s-labor',x:1},
    {k:'design',v:c.design,cls:'s-labor'},
    {k:'failure',v:c.failure,cls:'s-fail',x:1},
    {k:'packaging',v:c.packaging,cls:'s-pack',x:1},
    {k:'extras',v:c.extras,cls:'s-pack'},
    {k:'shipping',v:r.shipping,cls:'s-ship'},
    {k:'fees',v:r.fees_channel,cls:'s-fees'},
    {k:'tax',v:r.fees_tax,cls:'s-tax'},
    {k:'profit',v:Math.max(r.profit,0),cls:'s-profit'}
  ];
}
var PR_STAGE_LBL={material:'Material',flush:'ACE purge',energy:'Energy',machine:'Machine',labor:'Labor',design:'Design',
  failure:'Failure reserve',packaging:'Packaging',extras:'Extras',shipping:'Shipping',fees:'Channel fee',tax:'Tax',profit:'Your profit'};
function prRender(out){
  var $=function(id){return document.getElementById(id);};
  if(!out){['pr-specs','pr-tools','pr-rocket','pr-manifest','pr-sub','pr-margin','pr-coupon-msg','pr-hours-msg'].forEach(function(id){var e=$(id);if(e)e.innerHTML='';});
    $('pr-total').textContent='–';$('pr-profit').textContent='–';prStatus('');return;}
  var r=out.result,p=out.parsed;
  // Part readings
  // Time and weight of the whole order (scale with the quantity); per part below when qty > 1.
  var spec=function(lbl,val,sub){return '<div class="pr-spec"><div class="plate">'+escHtml(lbl)+'</div><div class="num">'+escHtml(val)+'</div>'+(sub?'<small class="pr-spec-sub">'+escHtml(sub)+'</small>':'')+'</div>';};
  var pp=r.qty>1?tr('pr_per_part','/part'):'';
  $('pr-specs').innerHTML=spec(tr('pr_spec_time','Tempo'),fmtTime(r.hours*r.qty*3600),pp&&fmtTime(r.hours*3600)+pp)+spec(tr('pr_spec_weight','Peso'),prNum(r.grams_total)+' g',pp&&prNum(r.grams_total/r.qty)+' g'+pp)+
    spec(tr('pr_spec_layers','Layers'),p.layers||'–')+spec(tr('pr_spec_changes','Trocas'),p.tool_changes);
  // Tools used: color, material, price/kg, grams (+ purge)
  var mats=prCfg.materials;
  $('pr-tools').innerHTML=r.tools.filter(function(t){return t.grams>0||t.flush_g>0;}).map(function(t){
    var opts='<option value="">'+escHtml(tr('pr_auto','Automatic'))+' ('+escHtml(t.from_profile?t.material:(t.type||'?'))+')</option>'+
      mats.map(function(m,i){return '<option value="'+i+'"'+(String(prToolMat[t.tool])===String(i)?' selected':'')+'>'+escHtml(m.name)+'</option>';}).join('');
    var hex=/^#[\da-f]{6}/i.test(t.colour)?t.colour.slice(0,7):'#888888',glow=!!prToolGlow[t.tool];
    return '<div class="pr-tool'+(glow?' glow':'')+'"><input type="color" class="pr-swatch" value="'+escHtml(hex.toLowerCase())+'" style="--sw:'+escHtml(hex)+'" title="'+escHtml(tr('pr_colour','Filament color'))+'" aria-label="'+escHtml(tr('pr_colour','Filament color'))+' T'+t.tool+'" oninput="this.style.setProperty(\'--sw\',this.value);prSetColour('+t.tool+',this.value)">'+
      '<b class="pr-tnum">T'+t.tool+'</b>'+
      '<select onchange="prToolMat['+t.tool+']=this.value;prCalcSoon()">'+opts+'</select>'+
      '<label class="pr-kg"><input type="number" min="0" step="1" placeholder="'+prNum(t.price_kg,2)+'" value="'+escHtml(prToolPrice[t.tool]||'')+'" oninput="prToolPrice['+t.tool+']=this.value;prCalcSoon()"><span>/kg</span></label>'+
      '<span class="pr-g num">'+prNum(t.grams)+' g'+(t.flush_g>0?'<small> +'+prNum(t.flush_g)+' '+escHtml(tr('pr_flush','purga'))+'</small>':'')+'</span>'+
      '<button type="button" class="pr-glow" aria-pressed="'+glow+'" title="'+escHtml(tr('pr_glow','Brilha no escuro'))+'" aria-label="'+escHtml(tr('pr_glow','Brilha no escuro'))+' T'+t.tool+'" onclick="prSetGlow('+t.tool+',this.getAttribute(\'aria-pressed\')!==\'true\')">✦</button></div>';
  }).join('')||'<p class="hint">'+escHtml(tr('pr_no_grams','This G-code does not report the filament used.'))+'</p>';
  // Cupom
  var cm=$('pr-coupon-msg'),cd=r.discounts.find(function(d){return d.kind==='coupon';});
  cm.className='pr-coupon-msg '+(cd?'ok':(r.coupon_error?'bad':''));
  cm.textContent=cd?('✓ −'+prMoney(cd.value)):(r.coupon_error?tr('pr_coupon_'+r.coupon_error,{not_found:'Coupon does not exist',expired:'Coupon expired',inactive:'Coupon disabled',min_total:'Order below the coupon minimum'}[r.coupon_error]):'');
  // Real time of the last print of this file
  var hm=$('pr-hours-msg');
  hm.innerHTML=out.last_job_s?escHtml(tr('pr_real_time','Last real print:'))+' <a href="#" onclick="document.getElementById(\'pr-hours\').value='+(out.last_job_s/3600).toFixed(2)+';prCalc();return false">'+escHtml(fmtTime(out.last_job_s))+'</a>':
    escHtml(tr('pr_slicer_time','Estimativa do fatiador:'))+' '+escHtml(fmtTime(p.time_s));
  // Price readings
  $('pr-total').textContent=prMoney(r.total);$('pr-total').classList.toggle('manual',!!r.total_override);
  var sub=r.qty>1?r.qty+' × '+prMoney(r.unit_total):'';
  if(r.discounts.length)sub+=(sub?' · ':'')+'<s>'+prMoney(r.list_total)+'</s>';
  if(r.total_override)sub+=(sub?'<br>':'')+'<span class="pr-manual">'+escHtml(tr('pr_total_manual','adjusted by hand'))+' · '+escHtml(tr('pr_total_calc','calculado'))+' '+prMoney(r.auto_total)+' · <a href="#" onclick="prResetTotal();return false">'+escHtml(tr('pr_total_reset','voltar'))+'</a></span>';
  $('pr-sub').innerHTML=sub;
  var pe=$('pr-profit');pe.textContent=prMoney(r.profit);pe.classList.toggle('bad',r.profit<0);
  $('pr-margin').textContent=prNum(r.margin_real_pct)+'% '+tr('pr_of_price','of the price');
  // Stage stack + manifest
  var st=prStages(r),sum=st.reduce(function(a,s){return a+Math.max(s.v,0);},0)||1;
  $('pr-rocket').innerHTML='<i class="pr-nose'+(r.profit<=0?' bad':'')+'"></i>'+st.slice().reverse().map(function(s){
    return s.v>0?'<i class="pr-stage '+s.cls+'" style="flex-grow:'+(s.v/sum).toFixed(4)+'" title="'+escHtml(tr('pr_st_'+s.k,PR_STAGE_LBL[s.k]))+'"></i>':'';}).join('')+'<i class="pr-fins"></i>';
  // Optional costs: the legend square is the switch (stays visible even when off).
  $('pr-manifest').innerHTML=st.slice().reverse().filter(function(s){return s.v>0||s.k==='profit'||(s.x&&prExcluded[s.k]);}).map(function(s){
    var lbl=escHtml(tr('pr_st_'+s.k,PR_STAGE_LBL[s.k])),off=s.x&&prExcluded[s.k];
    var key=s.x?'<input type="checkbox" class="pr-key pr-tog"'+(off?'':' checked')+' onchange="prToggle(\''+s.k+'\',this.checked)" title="'+escHtml(tr('pr_toggle','Include in the price'))+'" aria-label="'+lbl+'">':'<span class="pr-key"></span>';
    return '<label class="pr-row '+s.cls+(off?' off':'')+'">'+key+'<span class="pr-lbl">'+lbl+'</span>'+
      '<span class="pr-lead"></span><span class="pr-val num">'+prMoney(s.v)+'</span><span class="pr-pct">'+(off?'':prNum(s.v/sum*100,0)+'%')+'</span></label>';
  }).join('')+r.discounts.map(function(d){
    var lbl=d.kind==='coupon'?tr('pr_disc_coupon','Cupom')+' '+d.label:(d.kind==='qty'?tr('pr_disc_qty','Desconto')+' '+d.label:tr('pr_disc_manual','Desconto'));
    return '<div class="pr-row pr-disc"><span class="pr-key"></span><span class="pr-lbl">'+escHtml(lbl)+'</span><span class="pr-lead"></span><span class="pr-val num">−'+prMoney(d.value)+'</span><span class="pr-pct"></span></div>';
  }).join('');
  prStatus(r.profit<0?tr('pr_loss','Loss: the final price does not cover the costs (check the coupon and discounts).'):'',r.profit<0);
}

// ── Saved quotes ──
function prQuoteNo(id,created){return 'MK-'+String(created||'').slice(0,4)+'-'+String(id).padStart(4,'0');}
function prSnapshot(){
  // Reduces to at most 900 px wide: the receipt needs no more than that and the database is grateful.
  var src=prPreview&&prPreview.snapshot();if(!src)return Promise.resolve('');
  return new Promise(function(ok){
    var im=new Image();im.onload=function(){
      var k=Math.min(1,900/im.width),c=document.createElement('canvas');c.width=Math.round(im.width*k);c.height=Math.round(im.height*k);
      c.getContext('2d').drawImage(im,0,0,c.width,c.height);ok(c.toDataURL('image/png'));
    };im.onerror=function(){ok('');};im.src=src;
  });
}
// An order equal to the last saved one (e.g. Save then Receipt) reuses the same number.
var prSaved={key:'',id:0};
function prSaveQuote(){
  if(!prFileId){prStatus(tr('pr_pick_file','Escolha um G-code da lista'),true);return Promise.reject();}
  var body={file_id:prFileId,opt:prOpt(),client:prVal('pr-client'),notes:prVal('pr-notes')},key=JSON.stringify(body);
  if(key===prSaved.key)return Promise.resolve(prSaved.id);
  return prSnapshot().then(function(snap){
    body.snapshot=snap;return prJSON('/kx/pricing/quotes',body);
  }).then(function(res){prSaved={key:key,id:res.id};prStatus(tr('pr_saved','Quote saved')+' · '+prQuoteNo(res.id,new Date().toISOString()));return res.id;})
    .catch(function(e){if(e)prStatus(String(e.message||e),true);throw e;});
}
function prLoadQuotes(){
  var el=document.getElementById('pr-quotes');if(!el)return;
  prJSON('/kx/pricing/quotes').then(function(list){
    if(!list.length){el.innerHTML='<div class="empty-state">'+escHtml(tr('pr_no_quotes','No quotes saved yet.'))+'</div>';return;}
    el.innerHTML=list.map(function(q){
      var when=new Date(q.created_at).toLocaleString(prLocale(),{dateStyle:'short',timeStyle:'short'});
      return '<div class="hist-row"><div class="hist-main"><div class="hist-name"><span class="pr-qno">'+prQuoteNo(q.id,q.created_at)+'</span> '+escHtml(q.client||'—')+'</div>'+
        '<div class="hist-meta"><span>'+escHtml(when)+'</span><span>'+escHtml((q.filename||'').replace(/\.gcode$/i,''))+'</span><b class="num">'+prMoney(q.total)+'</b></div></div>'+
        '<div class="hist-acts"><button class="btn btn-sm btn-ghost" data-icon="pencil" onclick="prOpenQuote('+q.id+')">'+escHtml(tr('pr_open','Abrir'))+'</button>'+
        '<button class="btn btn-sm btn-ghost" data-icon="receipt" onclick="prQuoteReceipt('+q.id+')">'+escHtml(tr('pr_receipt','Recibo'))+'</button>'+
        '<button class="btn btn-sm btn-ghost" data-icon="trash-2" onclick="prDeleteQuote('+q.id+')" aria-label="'+escHtml(tr('pr_delete','Apagar'))+'"></button></div></div>';
    }).join('');
  }).catch(function(e){el.innerHTML='<div class="empty-state">'+escHtml(String(e.message||e))+'</div>';});
}
function prOpenQuote(id){
  prJSON('/kx/pricing/quotes/'+id).then(function(q){
    prTab('calc');prSetOpt(q.data.opt||{});
    document.getElementById('pr-client').value=q.client||'';document.getElementById('pr-notes').value=q.data.notes||'';
    var sel=document.getElementById('pr-file');if(sel)sel.value=q.gcode_file_id||'';
    prPickFile(q.gcode_file_id||'',true);
  }).catch(function(e){prStatus(String(e.message||e),true);});
}
function prDeleteQuote(id){
  if(!confirm(tr('pr_confirm_delete','Delete this quote?')))return;
  if(prSaved.id===id)prSaved={key:'',id:0};
  prJSON('/kx/pricing/quotes/'+id+'/delete',{}).then(prLoadQuotes);
}

// ── Recibo ("manifesto") ──
var PR_LOGO='/kx/ui/lib/icon/icon-192.png';
function prBuildRc(q){
  var d=q.data,r=d.result,cfg=d.config||prCfg||{},created=new Date(q.created_at||Date.now());
  var valid=(d.opt||{}).no_validity?null:new Date(created.getTime()+((cfg.quote_valid_days||7)*86400000));
  return {no:prQuoteNo(q.id,q.created_at||created.toISOString()),date:created,valid:valid,client:d.client||q.client||'',
    notes:d.notes||'',file:(q.filename||'').replace(/\.gcode$/i,'').replace(/_plate\(\d+\)/,'').replace(/_(PLA|PETG|ABS|ASA|TPU)_[\d.]+_.*$/i,''),
    snapshot:q.snapshot||'',tools:r.tools.filter(function(t){return t.grams>0;}).map(function(t){return {colour:t.colour,material:t.material,grams:(t.grams+t.flush_g)*r.qty,glow:!!t.glow};}),
    hours:r.hours*r.qty,qty:r.qty,unit:r.unit_list,list_total:r.list_total,discounts:r.discounts,shipping:r.shipping,total:r.total,
    currency:cfg.currency||'R$',business:cfg.business||{}};
}
function prReceiptFromCalc(){prSaveQuote().then(prQuoteReceipt).catch(function(){});}
function prQuoteReceipt(id){
  prRcMsg('');
  prJSON('/kx/pricing/quotes/'+id).then(function(q){prRc=prBuildRc(q);prRenderReceipt(prRc);
    document.getElementById('pr-receipt-dialog').classList.add('open');}).catch(function(e){prStatus(String(e.message||e),true);});
}
function prCloseReceipt(){document.getElementById('pr-receipt-dialog').classList.remove('open');}
function prRcLines(rc){
  var L=[{l:(rc.qty>1?rc.qty+' × ':'')+tr('pr_rc_unit','Price per part'),v:rc.qty>1?prMoney(rc.unit):prMoney(rc.list_total)}];
  if(rc.qty>1)L.push({l:tr('pr_rc_subtotal','Subtotal'),v:prMoney(rc.list_total)});
  rc.discounts.forEach(function(d){L.push({l:d.kind==='coupon'?tr('pr_disc_coupon','Cupom')+' '+d.label:tr('pr_disc_manual','Desconto')+(d.label?' '+d.label:''),v:'−'+prMoney(d.value),disc:true});});
  if(rc.shipping)L.push({l:tr('pr_rc_shipping','Frete (incluso)'),v:prMoney(rc.shipping)});
  return L;
}
function prRenderReceipt(rc){
  var b=rc.business||{},name=b.name||'MoonKobra';
  var patch='<svg class="rc-patch" viewBox="0 0 120 120" aria-hidden="true"><defs><path id="rc-arc" d="M60 60 m-45 0 a45 45 0 1 1 90 0 a45 45 0 1 1 -90 0"/></defs>'+
    '<circle cx="60" cy="60" r="57" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="60" cy="60" r="36" fill="none" stroke="currentColor" stroke-width="1"/>'+
    '<text font-size="10.5" letter-spacing="2.2" font-weight="700" fill="currentColor"><textPath href="#rc-arc">'+escHtml((name+' · '+tr('pr_rc_title','QUOTE')).toUpperCase())+'</textPath></text>'+
    '<clipPath id="rc-clip"><circle cx="60" cy="60" r="31"/></clipPath><image href="'+PR_LOGO+'" x="29" y="29" width="62" height="62" clip-path="url(#rc-clip)"/></svg>';
  var tools=rc.tools.map(function(t){return '<span class="rc-mat"><i style="background:'+escHtml(/^#[\da-f]{6}/i.test(t.colour)?t.colour.slice(0,7):'#999')+'"></i>'+escHtml(t.material)+(t.glow?' <em class="rc-glow">✦ '+escHtml(tr('pr_glow_short','brilha no escuro'))+'</em>':'')+' · '+prNum(t.grams)+' g'+'</span>';}).join('');
  document.getElementById('pr-receipt').innerHTML='<article class="rc">'+
    '<header class="rc-top">'+patch+'<div class="rc-id"><div class="rc-biz">'+escHtml(name)+'</div>'+(b.contact?'<div class="rc-contact">'+escHtml(b.contact)+'</div>':'')+'</div>'+
      '<div class="rc-no"><span>'+escHtml(tr('pr_rc_no','Nº'))+'</span><b>'+escHtml(rc.no)+'</b><span>'+escHtml(prDate(rc.date))+'</span></div></header>'+
    (rc.client?'<div class="rc-client"><span>'+escHtml(tr('pr_rc_for','To'))+'</span> '+escHtml(rc.client)+'</div>':'')+
    '<figure class="rc-cargo">'+(rc.snapshot?'<img src="'+escHtml(rc.snapshot)+'" alt="">':'')+'<figcaption>'+escHtml(tr('pr_rc_cargo','PAYLOAD'))+'</figcaption></figure>'+
    '<h3 class="rc-file">'+escHtml(rc.file)+'</h3>'+
    '<div class="rc-specs">'+tools+'<span>'+escHtml(tr('pr_spec_time','Tempo'))+' ≈ '+escHtml(fmtTime(rc.hours*3600))+'</span><span>'+escHtml(tr('pr_qty','Quantidade'))+': '+rc.qty+'</span></div>'+
    '<div class="rc-lines">'+prRcLines(rc).map(function(x){return '<div class="rc-line'+(x.disc?' disc':'')+'"><span>'+escHtml(x.l)+'</span><i></i><b>'+escHtml(x.v)+'</b></div>';}).join('')+'</div>'+
    '<div class="rc-total"><span>'+escHtml(tr('pr_total','Total'))+'</span><b>'+escHtml(prMoney(rc.total))+'</b></div>'+
    (rc.valid?'<div class="rc-stamp">'+escHtml(tr('pr_rc_valid','Valid until'))+'<br><b>'+escHtml(prDate(rc.valid))+'</b></div>':'')+
    (rc.notes?'<p class="rc-notes">'+escHtml(rc.notes)+'</p>':'')+
    (b.footer?'<footer class="rc-foot">'+escHtml(b.footer)+'</footer>':'')+'</article>';
}
function prRcText(rc){
  var b=rc.business||{},t=['*'+tr('pr_rc_title_txt','Quote')+' '+rc.no+'*'+(b.name?' · '+b.name:'')];
  if(rc.client)t.push(tr('pr_rc_for','To')+': '+rc.client);
  t.push(tr('pr_part','Part')+': '+rc.file);
  t.push(rc.tools.map(function(x){return x.material+(x.glow?' ✦ '+tr('pr_glow_short','brilha no escuro'):'')+' ('+prNum(x.grams)+' g)';}).join(' + ')+' · '+tr('pr_spec_time','Tempo')+' ≈ '+fmtTime(rc.hours*3600));
  prRcLines(rc).forEach(function(x){t.push(x.l+': '+x.v);});
  t.push('*'+tr('pr_total','Total')+': '+prMoney(rc.total)+'*');
  if(rc.valid)t.push(tr('pr_rc_valid','Valid until')+' '+prDate(rc.valid));
  if(rc.notes)t.push(rc.notes);
  if(b.contact)t.push(b.contact);
  return t.join('\n');
}
function prReceiptCopy(){
  if(!prRc)return;var txt=prRcText(prRc);
  (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).catch(function(){
    var ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}ta.remove();
  });
}
function prReceiptPrint(){
  document.body.classList.add('print-receipt');
  window.addEventListener('afterprint',function f(){document.body.classList.remove('print-receipt');window.removeEventListener('afterprint',f);});
  window.print();
}
// Same manifest drawn on a 2D canvas (1080 px, good for WhatsApp). Fixed paper colors.
function prRcCanvas(){
  var rc=prRc,W=1080,P=64;
  var INK='#16191B',INK2='#4E5450',RULE='#CFC7B3',PAPER='#F4EEDF',ACC='#D9500A',WELL='#E9E2D1';
  var fonts=['700 40px Archivo','400 24px Archivo','400 22px "JetBrains Mono"'];
  return Promise.all(fonts.map(function(f){return document.fonts?document.fonts.load(f):null;})).catch(function(){}).then(function(){
    var ld=function(src){return new Promise(function(ok){if(!src)return ok(null);var im=new Image();im.onload=function(){ok(im);};im.onerror=function(){ok(null);};im.src=src;});};
    return Promise.all([ld(rc.snapshot),ld(PR_LOGO)]);
  }).then(function(imgs){
    var img=imgs[0],logo=imgs[1];
    // Draws on a tall canvas and crops at the end of the content (exact height without summing everything first).
    var lines=prRcLines(rc),c=document.createElement('canvas');c.width=W;c.height=3000;var g=c.getContext('2d');
    g.fillStyle=PAPER;g.fillRect(0,0,W,c.height);
    var y=P;
    // badge with the moon phase
    var cx=P+62,cy=y+62,name=(rc.business&&rc.business.name)||'MoonKobra';
    g.strokeStyle=INK;g.lineWidth=2.5;g.beginPath();g.arc(cx,cy,60,0,Math.PI*2);g.stroke();g.lineWidth=1.2;g.beginPath();g.arc(cx,cy,38,0,Math.PI*2);g.stroke();
    if(logo){g.save();g.beginPath();g.arc(cx,cy,32,0,Math.PI*2);g.clip();g.drawImage(logo,cx-32,cy-32,64,64);g.restore();}
    g.fillStyle=INK;
    var ring=(name+' · '+tr('pr_rc_title','QUOTE')).toUpperCase()+' · ';g.font='700 13px Archivo';
    var ang=-Math.PI/2-0.9,step=(Math.PI*2-0.2)/Math.max(ring.length,24);
    for(var i=0;i<ring.length&&i*step<Math.PI*2-0.3;i++){g.save();g.translate(cx+49*Math.cos(ang),cy+49*Math.sin(ang));g.rotate(ang+Math.PI/2);g.fillText(ring[i],-4,4);g.restore();ang+=step;}
    // identification
    g.fillStyle=INK;g.font='700 40px Archivo';g.fillText(name,P+150,y+52);
    g.fillStyle=INK2;g.font='400 22px Archivo';if(rc.business&&rc.business.contact)g.fillText(rc.business.contact,P+150,y+88);
    g.textAlign='right';g.font='400 18px "JetBrains Mono"';g.fillText(tr('pr_rc_no','Nº'),W-P,y+22);
    g.fillStyle=INK;g.font='700 26px "JetBrains Mono"';g.fillText(rc.no,W-P,y+56);
    g.fillStyle=INK2;g.font='400 20px "JetBrains Mono"';g.fillText(prDate(rc.date),W-P,y+86);g.textAlign='left';
    y+=150;g.strokeStyle=RULE;g.lineWidth=1.5;g.beginPath();g.moveTo(P,y);g.lineTo(W-P,y);g.stroke();y+=20;
    if(rc.client){g.fillStyle=INK2;g.font='400 24px Archivo';g.fillText(tr('pr_rc_for','To')+'  ',P,y+26);g.fillStyle=INK;g.font='700 26px Archivo';g.fillText(rc.client,P+70,y+26);y+=50;}
    if(img){
      var bw=W-2*P,bh=520;g.fillStyle=WELL;g.fillRect(P,y,bw,bh);g.strokeStyle=RULE;g.strokeRect(P+.5,y+.5,bw-1,bh-1);
      var k=Math.min(bw/img.width,bh/img.height),iw=img.width*k,ih=img.height*k;g.drawImage(img,P+(bw-iw)/2,y+(bh-ih)/2,iw,ih);
      g.fillStyle=INK2;g.font='700 14px Archivo';g.fillText(tr('pr_rc_cargo','PAYLOAD'),P+14,y+26);y+=bh+40;
    }
    g.fillStyle=INK;g.font='700 34px Archivo';g.fillText(rc.file,P,y+10);y+=50;
    var sx=P;g.font='400 22px Archivo';
    rc.tools.forEach(function(t){g.fillStyle=/^#[\da-f]{6}/i.test(t.colour)?t.colour.slice(0,7):'#999';g.fillRect(sx,y-16,18,18);g.strokeStyle=INK2;g.strokeRect(sx+.5,y-15.5,17,17);
      g.fillStyle=INK2;var s=t.material+(t.glow?' ✦ '+tr('pr_glow_short','brilha no escuro'):'')+' · '+prNum(t.grams)+' g';g.fillText(s,sx+26,y);sx+=g.measureText(s).width+56;});
    g.fillText(tr('pr_spec_time','Tempo')+' ≈ '+fmtTime(rc.hours*3600)+'   ·   '+tr('pr_qty','Quantidade')+': '+rc.qty,sx,y);y+=40;
    lines.forEach(function(l){
      g.fillStyle=l.disc?ACC:INK2;g.font='400 24px Archivo';g.fillText(l.l,P,y+30);
      g.textAlign='right';g.font='400 24px "JetBrains Mono"';g.fillStyle=l.disc?ACC:INK;g.fillText(l.v,W-P,y+30);g.textAlign='left';
      g.strokeStyle=RULE;g.setLineDash([2,6]);g.beginPath();g.moveTo(P,y+44);g.lineTo(W-P,y+44);g.stroke();g.setLineDash([]);y+=52;
    });
    y+=20;g.fillStyle=INK;g.fillRect(P,y,W-2*P,3);y+=16;
    g.font='700 20px Archivo';g.fillText(tr('pr_total','Total').toUpperCase(),P,y+60);
    g.textAlign='right';g.font='700 76px Archivo';g.fillText(prMoney(rc.total),W-P,y+80);g.textAlign='left';
    // carimbo de validade
    if(rc.valid){g.save();g.translate(P+250,y+50);g.rotate(-0.1);g.strokeStyle=ACC;g.fillStyle=ACC;g.lineWidth=3;g.strokeRect(-10,-38,230,74);
    g.font='700 16px Archivo';g.fillText(tr('pr_rc_valid','Valid until').toUpperCase(),4,-12);g.font='700 28px "JetBrains Mono"';g.fillText(prDate(rc.valid),4,24);g.restore();}
    y+=130;
    if(rc.notes){g.fillStyle=INK2;g.font='400 22px Archivo';y+=prWrap(g,rc.notes,P,y,W-2*P,30,3)*30+20;}
    if(rc.business&&rc.business.footer){g.fillStyle=INK2;g.font='400 20px Archivo';g.textAlign='center';g.fillText(rc.business.footer,W/2,y+20);g.textAlign='left';y+=40;}
    var H=y+P,out=document.createElement('canvas');out.width=W;out.height=H;var o=out.getContext('2d');o.drawImage(c,0,0);
    // No transparency at all: WhatsApp converts to JPEG and transparent becomes black.
    // O picote vira linha tracejada em vez de furos.
    o.strokeStyle=RULE;o.lineWidth=2;o.setLineDash([10,8]);[14,H-14].forEach(function(py){o.beginPath();o.moveTo(0,py);o.lineTo(W,py);o.stroke();});o.setLineDash([]);
    return out;
  });
}
function prRcBlob(){return prRcCanvas().then(function(c){return new Promise(function(ok){c.toBlob(ok,'image/png');});});}
function prRcMsg(t){var e=document.getElementById('pr-rc-status');if(e)e.textContent=t||'';}
function prReceiptImage(){
  if(!prRc)return;var rc=prRc;
  prRcBlob().then(function(blob){
    var fname=rc.no+'.png',file=new File([blob],fname,{type:'image/png'});
    if(navigator.canShare&&navigator.canShare({files:[file]})&&/Mobi|Android|iPhone/i.test(navigator.userAgent)){
      navigator.share({files:[file],text:prRcText(rc)}).catch(function(){});return;
    }
    var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fname;document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},1000);
  });
}
// Copy image: the image API only exists on a secure page (HTTPS/localhost). Over
// http://IP it falls back to copying a selected <img>, which Chrome/Edge accept pasting into WhatsApp Web.
function prReceiptCopyImage(){
  if(!prRc)return;
  if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){
    // Safari requires the ClipboardItem created right on the click, with the blob promise inside.
    navigator.clipboard.write([new ClipboardItem({'image/png':prRcBlob()})])
      .then(function(){prRcMsg(tr('pr_img_copied','Imagem copiada'));})
      .catch(function(){prRcCopyFallback();});
    return;
  }
  prRcCopyFallback();
}
// Without a secure page (http://IP) the browser does not let a site put an image on the clipboard
// (and Firefox's "select and copy" carries HTML, not the image). So the receipt becomes the image
// itself inside the window: right-click → Copy image (PC) or hold → copy (phone).
function prRcCopyFallback(){
  prRcCanvas().then(function(c){
    var box=document.getElementById('pr-receipt');if(!box)return;
    box.innerHTML='<img class="pr-rc-img" alt="" src="'+c.toDataURL('image/png')+'">';
    var mob=/Mobi|Android|iPhone/i.test(navigator.userAgent);
    prRcMsg(mob?tr('pr_img_copy_hold','Segure em cima da imagem e escolha copiar ou compartilhar'):tr('pr_img_copy_right','Right-click the image → Copy image'));
  });
}
function prWrap(g,text,x,y,maxW,lh,maxLines){
  var words=String(text).split(/\s+/),line='',n=0;
  for(var i=0;i<words.length&&n<maxLines;i++){
    var t=line?line+' '+words[i]:words[i];
    if(g.measureText(t).width>maxW&&line){g.fillText(line,x,y+n*lh);n++;line=words[i];}else line=t;
  }
  if(n<maxLines&&line){g.fillText(line,x,y+n*lh);n++;}
  return n;
}

// ── Configuration ──
var PR_CFG_FIELDS={
  machine:[['printer.name','text','Printer name'],['printer.watts','num','Average power draw (W)'],['printer.price','num','Printer price (R$)'],
    ['printer.life_hours','num','Service life (hours)'],['printer.maint_per_hour','num','Maintenance per hour (R$)'],['kwh_price','num','Energy rate (R$/kWh)']],
  labor:[['labor_rate','num','Your hourly rate (R$/h)'],['prep_minutes','num','Prep per order (min)'],['post_minutes','num','Finishing per part (min)'],
    ['failure_pct','num','Failure reserve (%)'],['include_flush','check','Add the ACE purge on color changes']],
  price:[['margin_pct','num','Markup over cost (%)'],['urgency_pct','num','Rush surcharge (%)'],['min_price','num','Minimum price per part (R$)'],
    ['packaging','num','Packaging per part (R$)'],['rounding','round','Round the price'],['quote_valid_days','num','Quote validity (days)'],
    ['currency','text','Currency'],['updated_at','date','Taxas revisadas em']],
  business:[['business.name','text','Business name'],['business.contact','text','Contact (WhatsApp, Instagram…)'],['business.footer','text','Receipt footer']]
};
var PR_TABLES={
  materials:{cols:[['name','text','Name'],['type','text','Type in the G-code'],['price_kg','num','R$/kg'],['density','num','g/cm³']],blank:{name:'',type:'',price_kg:0,density:1.24}},
  channels:{cols:[['name','text','Channel'],['threshold','num','Threshold (R$)'],['pct_low','num','% below'],['fixed_low','num','R$ below'],['pct','num','% above'],['fixed','num','R$ above']],
    blank:{name:'',threshold:0,pct_low:0,fixed_low:0,pct:0,fixed:0}},
  taxes:{cols:[['name','text','Tax regime'],['pct','num','%']],blank:{name:'',pct:0}},
  qty_discounts:{cols:[['min_qty','num','From (units)'],['pct','num','Discount (%)']],blank:{min_qty:0,pct:0}}
};
var PR_TABLE_EL={materials:'pr-cfg-materials',channels:'pr-cfg-channels',taxes:'pr-cfg-taxes',qty_discounts:'pr-cfg-qty'};
function prGet(o,path){return path.split('.').reduce(function(a,k){return a==null?a:a[k];},o);}
function prSetPath(o,path,v){var ks=path.split('.'),last=ks.pop();ks.reduce(function(a,k){return a[k]=a[k]||{};},o)[last]=v;}
function prRenderConfig(){
  if(!prCfg)return;
  document.getElementById('pr-cfg-updated').textContent=prCfg.updated_at||'–';
  Object.keys(PR_CFG_FIELDS).forEach(function(sec){
    document.getElementById('pr-cfg-'+sec).innerHTML=PR_CFG_FIELDS[sec].map(function(f){
      var key='pr_cfg_'+f[0].replace('.','_'),lbl=escHtml(tr(key,f[2])),v=prGet(prCfg,f[0]);
      if(f[1]==='check')return '<label class="check-row pr-wide"><input type="checkbox" data-path="'+f[0]+'"'+(v?' checked':'')+'> '+lbl+'</label>';
      if(f[1]==='round')return '<label class="pr-f"><span>'+lbl+'</span><select data-path="'+f[0]+'">'+
        [['none','R$ 12,34'],['90','R$ 12,90'],['1','R$ 13'],['5','R$ 15']].map(function(o){return '<option value="'+o[0]+'"'+(v===o[0]?' selected':'')+'>'+o[1]+'</option>';}).join('')+'</select></label>';
      return '<label class="pr-f'+(f[1]==='text'&&sec==='business'?' pr-wide':'')+'"><span>'+lbl+'</span><input type="'+(f[1]==='num'?'number':f[1])+'"'+(f[1]==='num'?' step="any" min="0"':'')+
        ' data-path="'+f[0]+'" value="'+escHtml(v==null?'':v)+'"></label>';
    }).join('');
  });
  Object.keys(PR_TABLES).forEach(prRenderTable);
  prRenderCoupons();
}
function prRenderTable(key){
  var t=PR_TABLES[key],rows=prCfg[key]||[];
  document.getElementById(PR_TABLE_EL[key]).innerHTML='<div class="pr-table" data-table="'+key+'" style="--cols:'+t.cols.length+'">'+
    '<div class="pr-tr pr-th">'+t.cols.map(function(c){return '<span>'+escHtml(tr('pr_col_'+key+'_'+c[0],c[2]))+'</span>';}).join('')+'<span></span></div>'+
    rows.map(function(r,i){return '<div class="pr-tr">'+t.cols.map(function(c){
      return '<input type="'+(c[1]==='num'?'number':'text')+'"'+(c[1]==='num'?' step="any" min="0"':'')+' data-col="'+c[0]+'" value="'+escHtml(r[c[0]]==null?'':r[c[0]])+'" aria-label="'+escHtml(c[2])+'">';
    }).join('')+'<button class="icon-btn" data-icon="trash-2" onclick="prReadConfig();prCfg.'+key+'.splice('+i+',1);prRenderTable(\''+key+'\')" aria-label="'+escHtml(tr('pr_delete','Apagar'))+'"></button></div>';}).join('')+
    '</div><button class="btn btn-sm btn-ghost" data-icon="plus" onclick="prReadConfig();prCfg.'+key+'.push(JSON.parse(JSON.stringify(PR_TABLES.'+key+'.blank)));prRenderTable(\''+key+'\')">'+escHtml(tr('pr_add','Adicionar'))+'</button>';
}
// Coupons as "patches": badge card with a big code, value and validity.
function prCouponState(c){
  if(!c.active)return ['off',tr('pr_cp_off','desativado')];
  if(c.valid_until&&new Date(c.valid_until+'T23:59:59')<new Date())return ['expired',tr('pr_cp_expired','vencido')];
  return ['on',tr('pr_cp_on','ativo')];
}
function prRenderCoupons(){
  var el=document.getElementById('pr-cfg-coupons');
  el.innerHTML=(prCfg.coupons||[]).map(function(c,i){
    var st=prCouponState(c);
    return '<div class="pr-patch '+st[0]+'" data-coupon="'+i+'">'+
      '<div class="pr-patch-top"><input class="pr-code" data-f="code" value="'+escHtml(c.code)+'" placeholder="CODIGO" oninput="this.value=this.value.toUpperCase()" aria-label="'+escHtml(tr('pr_cp_code','Code'))+'">'+
      '<span class="dymo '+(st[0]==='on'?'green':(st[0]==='expired'?'red':''))+'">'+escHtml(st[1])+'</span></div>'+
      '<div class="pr-patch-val"><input type="number" step="any" min="0" data-f="value" value="'+escHtml(c.value)+'" aria-label="'+escHtml(tr('pr_cp_value','Valor'))+'">'+
      '<select data-f="kind"><option value="pct"'+(c.kind==='pct'?' selected':'')+'>%</option><option value="value"'+(c.kind==='value'?' selected':'')+'>R$</option></select>'+
      '<span class="pr-patch-off">OFF</span></div>'+
      '<div class="pr-patch-perf"></div>'+
      '<label class="pr-f"><span>'+escHtml(tr('pr_cp_min','Minimum order (R$)'))+'</span><input type="number" step="any" min="0" data-f="min_total" value="'+escHtml(c.min_total)+'"></label>'+
      '<label class="pr-f"><span>'+escHtml(tr('pr_cp_until','Valid until (empty = always)'))+'</span><input type="date" data-f="valid_until" value="'+escHtml(c.valid_until||'')+'"></label>'+
      '<label class="pr-f"><span>'+escHtml(tr('pr_cp_note','Note'))+'</span><input type="text" data-f="note" value="'+escHtml(c.note||'')+'"></label>'+
      '<div class="pr-patch-foot"><label class="check-row"><input type="checkbox" data-f="active"'+(c.active?' checked':'')+'> '+escHtml(tr('pr_cp_active','Ativo'))+'</label>'+
      '<button class="icon-btn" data-icon="trash-2" onclick="prReadConfig();prCfg.coupons.splice('+i+',1);prRenderCoupons()" aria-label="'+escHtml(tr('pr_delete','Apagar'))+'"></button></div></div>';
  }).join('')+'<button class="pr-patch pr-patch-add" data-icon="plus" onclick="prReadConfig();prCfg.coupons.push({code:\'\',kind:\'pct\',value:10,min_total:0,valid_until:\'\',active:true,note:\'\'});prRenderCoupons()">'+escHtml(tr('pr_cp_new','Novo cupom'))+'</button>';
}
function prReadConfig(){
  var root=document.getElementById('pr-group-config');if(!root||!prCfg)return;
  root.querySelectorAll('[data-path]').forEach(function(e){
    var v=e.type==='checkbox'?e.checked:(e.type==='number'?parseFloat(e.value||'0'):e.value);prSetPath(prCfg,e.dataset.path,v);
  });
  root.querySelectorAll('[data-table]').forEach(function(tb){
    var key=tb.dataset.table;
    prCfg[key]=Array.prototype.map.call(tb.querySelectorAll('.pr-tr:not(.pr-th)'),function(row){
      var o={};row.querySelectorAll('[data-col]').forEach(function(e){o[e.dataset.col]=e.type==='number'?parseFloat(e.value||'0'):e.value;});return o;
    });
  });
  var cps=root.querySelectorAll('[data-coupon]');
  if(cps.length||(prCfg.coupons||[]).length)prCfg.coupons=Array.prototype.map.call(cps,function(card){
    var o={};card.querySelectorAll('[data-f]').forEach(function(e){o[e.dataset.f]=e.type==='checkbox'?e.checked:(e.type==='number'?parseFloat(e.value||'0'):e.value);});return o;
  });
}
function prSaveConfig(){
  prReadConfig();var st=document.getElementById('pr-cfg-status');
  prJSON('/kx/pricing/config',prCfg).then(function(c){
    prCfg=c;prRenderConfig();prFillSelects();prCalc();if(st)st.textContent=tr('pr_cfg_saved','Configuration saved');
  }).catch(function(e){if(st)st.textContent=String(e.message||e);});
}

// ── Import/export configuration ──
function prExportConfig(){
  prReadConfig();
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(prCfg,null,2)],{type:'application/json'}));
  a.download='moonkobra-orcamento.json';document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},1000);
}
function prImportConfig(file){
  if(!file)return;var st=document.getElementById('pr-cfg-status');
  file.text().then(function(t){return prJSON('/kx/pricing/config',JSON.parse(t));}).then(function(c){
    prCfg=c;prRenderConfig();prFillSelects();prCalc();if(st)st.textContent=tr('pr_cfg_imported','Configuration imported');
  }).catch(function(e){if(st)st.textContent=tr('pr_cfg_import_fail','Invalid file')+': '+(e.message||e);});
}
// Backup do projeto inteiro: o backend valida tudo antes de gravar e reinicia o bridge.
function bkImport(file){
  if(!file)return;var st=document.getElementById('bk-status');
  if(!confirm(tr('bk_confirm','Replace the current configuration with this backup and restart MoonKobra?')))return;
  fetch(_apiUrl('/kx/backup/import'),{method:'POST',headers:{'Content-Type':'application/zip'},body:file})
    .then(function(r){return r.json();}).then(function(d){
      if(d.error)throw new Error(srvMsg(d.error));
      if(st)st.textContent=tr('bk_done','Restaurado:')+' '+d.result.join(', ')+' - '+tr('bk_restarting','reiniciando…');
      setTimeout(function(){location.reload();},6000);
    }).catch(function(e){if(st)st.textContent=String(e.message||e);});
}
