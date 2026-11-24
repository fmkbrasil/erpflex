(function(){
  const map = window.UI_LABEL_OVERRIDES || {};
  const canCustomize = !!window.UI_CAN_CUSTOMIZE;
  const hiddenControls = window.UI_HIDDEN_CONTROLS || {};
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','TEXTAREA','CODE','PRE']);
  const UI_TEXT_SELECTOR = [
    'h1','h2','h3','h4','h5','h6','thead th','button','.btn','.topbar a','.topbar summary','.commercial-brand b','.commercial-brand small',
    '.module-nav a','.module-nav-title span','.module-nav-group>b','label','.hint','.hint-block',
    '.hero p','.panel-title p','.panel-title span','.panel>p','.bi-card>p','.bi-executive-note p',
    '.bi-note','.bi-period','.bi-kicker','.bi-sync','.bi-data-warning','.bi-unavailable','.bi-empty',
    '.alert','.status-tabs a','.mobile-menu-divider','.mobile-bottom-nav a span','.mobile-home-card b','.mobile-home-card small','.mobile-home-feature b','.mobile-home-feature small','.mobile-home-feature em','.mobile-home-bi-shortcuts a','.bi-kpi>span','.bi-kpi>small','.metric>span',
    '.info-card>span','.theme-color-field span','select option[value=""]','select[name="situation"] option',
    // BI rápido: rótulos/títulos de interface. Valores, documentos e dados operacionais ficam fora.
    '.bi-fast-eyebrow','.bi-fast-head p','.bi-fast-kpi small','.bi-fast-kpi em','.bi-fast-kpi [data-ui-label]','.bi-fast-panel-head span','.bi-fast-panel-head h2','.bi-fast-panel-head>small',
    '.bi-fast-commercial-card small','.bi-fast-commercial-card strong','.bi-fast-commercial-card em','.bi-fast-commercial-card>b',
    '.bi-fast-summary-title b','.bi-fast-summary-metrics small','.bi-fast-legend span',
    '.bi-fast-period-buttons button','.bi-fast-custom-trigger','.bi-fast-sync-button b','.bi-fast-sync-status','.bi-fast-sync-link',
    '.bi-fast-detail-select-wrap>span','.bi-fast-detail-tabs button','.bi-fast-breakdown-total>span',
    '.bi-fast-seller-summary>h4','.bi-fast-product-summary>h4','.bi-fast-group-documents>h4',
    '.bi-fast-audit-summary small','.bi-fast-audit-diagnostic>div>b','.bi-fast-audit-diagnostic>div>small','.audit-diag-grid small','.bi-fast-audit-rule>b','.bi-fast-audit-price-grid small',
    '.bi-fast-subsection-title>span','.bi-fast-drawer header>div>small','.bi-fast-empty','.bi-fast-loading','.bi-fast-error',
    '[data-ui-label]'
  ].join(',');
  const SKIP_CLOSEST = 'td,.raw-json,.json-view,.key,.message,[data-ui-data],[data-no-label-edit],.bi-branch>summary,.sales-drill>summary,.sales-subbranch>summary';

  function isUiTextNode(node){
    if(!node || node.nodeType!==Node.TEXT_NODE || !node.nodeValue.trim()) return false;
    const p=node.parentElement;
    if(!p || SKIP_TAGS.has(p.tagName) || p.closest(SKIP_CLOSEST)) return false;
    return !!p.closest(UI_TEXT_SELECTOR);
  }
  function rawText(node){ return (node.nodeValue||'').trim(); }
  function replaceNode(node, custom){
    const pre=(node.nodeValue.match(/^\s*/)||[''])[0];
    const post=(node.nodeValue.match(/\s*$/)||[''])[0];
    node.nodeValue=pre+custom+post;
  }
  function applyTextOverrides(root=document.body){
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:n=>isUiTextNode(n)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT});
    const nodes=[];while(walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(n=>{const raw=rawText(n);if(Object.prototype.hasOwnProperty.call(map,raw)){if(n.parentElement && !n.parentElement.dataset.uiOriginalText)n.parentElement.dataset.uiOriginalText=raw;replaceNode(n,map[raw]);}});
    document.querySelectorAll('[placeholder],[title],[aria-label],input[type="submit"][value],input[type="button"][value]').forEach(el=>{
      ['placeholder','title','aria-label','value'].forEach(attr=>{
        if(!el.hasAttribute(attr))return;const raw=(el.getAttribute(attr)||'').trim();if(raw && Object.prototype.hasOwnProperty.call(map,raw)){el.dataset['uiOriginal'+attr.replace(/(^|-)([a-z])/g,(_,a,b)=>b.toUpperCase())]=raw;el.setAttribute(attr,map[raw]);}
      });
    });
    if(document.title){Object.entries(map).forEach(([from,to])=>{if(from && document.title.includes(from))document.title=document.title.split(from).join(to);});}
  }
  applyTextOverrides();

  function normalizeControlText(v){return (v||'').replace(/\s+/g,' ').trim();}
  function controlLabel(el){
    const own=el.dataset.uiOriginalText;
    const nested=el.querySelector && el.querySelector('[data-ui-original-text]');
    return normalizeControlText(own || (nested && nested.dataset.uiOriginalText) || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || el.innerText || el.textContent || 'Botão');
  }
  function hashString(value){let h=2166136261;for(let i=0;i<value.length;i++){h^=value.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0).toString(36);}
  function assignControlKeys(){
    const selector='button:not(.label-editor-toggle),a.btn,input[type="submit"],input[type="button"]';
    const counts={};
    document.querySelectorAll(selector).forEach(el=>{
      if(el.closest('.label-edit-popover') || el.closest('form[action="/aparencia/controle"]') || el.dataset.noUiHide!==undefined)return;
      const label=controlLabel(el);const sig=(el.tagName||'CTRL')+'|'+hashString(label);const idx=counts[sig]||0;counts[sig]=idx+1;
      el.dataset.uiControlKey=location.pathname+'|'+sig+'|'+idx;
      el.dataset.uiControlLabel=label;
      el.dataset.uiControlKind='button';
    });
  }
  function surfaceLabel(el,index){
    const marked=el.querySelector('[data-ui-original-text]');
    const heading=el.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > span,:scope > .sales-card-title h3,h1,h2,h3,h4');
    const raw=(marked&&marked.dataset.uiOriginalText)||(heading&&heading.textContent)||el.getAttribute('aria-label')||('Card '+(index+1));
    return normalizeControlText(raw);
  }
  function assignSurfaceKeys(){
    if(!document.body.classList.contains('page-bi'))return;
    const selectors=['.bi-kpi','.bi-card','.bi-executive-note','.bi-data-quality'];
    const used={};
    selectors.forEach(selector=>{
      document.querySelectorAll(selector).forEach((el,index)=>{
        if(el.dataset.noUiHide!==undefined || el.dataset.uiControlKey)return;
        const kind=selector.replace(/^\./,'').replace(/[^a-z0-9_-]/gi,'_');
        const label=surfaceLabel(el,index);
        const base=location.pathname+'|CARD|'+kind+'|'+hashString(label.toLowerCase());
        const n=used[base]||0;used[base]=n+1;
        el.dataset.uiControlKey=base+(n?'|'+n:'');
        el.dataset.uiControlLabel=map[label]||label;
        el.dataset.uiOriginalCardLabel=label;
        el.dataset.uiControlKind='card';
      });
    });
  }
  function applyHiddenControls(){
    document.querySelectorAll('[data-ui-control-key]').forEach(el=>{
      const key=el.dataset.uiControlKey;
      if(Object.prototype.hasOwnProperty.call(hiddenControls,key)){el.dataset.uiHidden='1';el.hidden=true;el.classList.add('ui-control-hidden');}
    });
  }
  assignControlKeys();assignSurfaceKeys();applyHiddenControls();

  function directTextNodes(el){return [...el.childNodes].filter(isUiTextNode);}
  function closestEditable(start){
    let el=start instanceof Element?start:null;
    while(el && el!==document.body){
      if(el.matches(SKIP_CLOSEST) || el.dataset.noLabelEdit!==undefined)return null;
      if(el.matches(UI_TEXT_SELECTOR) && directTextNodes(el).length)return el;
      if(el.matches('input[placeholder],input[title],select[aria-label],input[aria-label],button[title],a[title]'))return el;
      el=el.parentElement;
    }
    return null;
  }
  function markEditable(on){
    document.querySelectorAll('.ui-editable-text,.ui-hideable-control,.ui-hidden-preview').forEach(el=>el.classList.remove('ui-editable-text','ui-hideable-control','ui-hidden-preview'));
    if(!on){
      document.querySelectorAll('[data-ui-hidden="1"]').forEach(el=>{el.hidden=true;});
      return;
    }
    document.querySelectorAll('[data-ui-control-key]').forEach(el=>{
      el.classList.add('ui-hideable-control');
      if(el.dataset.uiHidden==='1'){el.hidden=false;el.classList.add('ui-hidden-preview');}
    });
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode:n=>isUiTextNode(n)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT});
    while(walker.nextNode()){
      const el=walker.currentNode.parentElement;
      if(el && !el.closest('.label-editor-toggle,.label-edit-popover') && !el.matches('option'))el.classList.add('ui-editable-text');
    }
    document.querySelectorAll('[placeholder],[title],[aria-label]').forEach(el=>{if(!el.closest(SKIP_CLOSEST))el.classList.add('ui-editable-text');});
  }
  async function saveLabel(original,custom){
    const body=new URLSearchParams({original_text:original,custom_text:custom});
    const r=await fetch('/aparencia/legenda',{method:'POST',headers:{'X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'},body});
    if(!r.ok)throw new Error('Falha ao salvar');
    map[original]=custom;
  }
  function promptTextEdit(el){
    const nodes=directTextNodes(el);
    if(nodes.length){
      const node=nodes[0];const visible=rawText(node);const original=el.dataset.uiOriginalText||Object.keys(map).find(k=>map[k]===visible)||visible;
      const custom=prompt('Novo texto para “'+visible+'”:',visible);
      if(!custom || custom.trim()===visible)return;
      saveLabel(original,custom.trim()).then(()=>location.reload()).catch(()=>alert('Não foi possível salvar o texto.'));
      return;
    }
    const attrs=['placeholder','title','aria-label'];
    const attr=attrs.find(a=>el.hasAttribute(a));if(!attr)return;
    const visible=(el.getAttribute(attr)||'').trim();if(!visible)return;
    const original=Object.keys(map).find(k=>map[k]===visible)||visible;
    const custom=prompt('Novo texto para “'+visible+'”:',visible);
    if(!custom || custom.trim()===visible)return;
    saveLabel(original,custom.trim()).then(()=>location.reload()).catch(()=>alert('Não foi possível salvar o texto.'));
  }
  async function saveControlVisibility(el,hidden){
    const key=el.dataset.uiControlKey;const label=el.dataset.uiControlLabel||controlLabel(el);
    const body=new URLSearchParams({control_key:key,control_label:label,hidden:hidden?'1':'0'});
    const r=await fetch('/aparencia/controle',{method:'POST',headers:{'X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'},body});
    if(!r.ok)throw new Error('Falha ao salvar');
    if(hidden){
      hiddenControls[key]=label;
      el.dataset.uiHidden='1';
      el.classList.add('ui-control-hidden');
      if(document.body.classList.contains('label-edit-mode')){
        el.hidden=false;
        el.classList.add('ui-hidden-preview');
      }else el.hidden=true;
    }else{
      delete hiddenControls[key];
      delete el.dataset.uiHidden;
      el.hidden=false;
      el.classList.remove('ui-control-hidden','ui-hidden-preview');
    }
    return r.json().catch(()=>({ok:true}));
  }
  function removeEditPopover(){document.querySelectorAll('.label-edit-popover').forEach(x=>x.remove());}
  function editableTargetInsideControl(el){
    if(directTextNodes(el).length)return el;
    const all=[...el.querySelectorAll(UI_TEXT_SELECTOR)];
    return all.find(x=>directTextNodes(x).length)||el;
  }
  function promptCardTitleEdit(el){
    const visible=normalizeControlText(el.dataset.uiControlLabel||controlLabel(el));
    const original=normalizeControlText(el.dataset.uiOriginalCardLabel||visible);
    const custom=prompt('Novo nome para o card “'+visible+'”:',visible);
    if(!custom || custom.trim()===visible)return;
    saveLabel(original,custom.trim()).then(()=>location.reload()).catch(()=>alert('Não foi possível salvar o nome do card.'));
  }
  function openControlPopover(el){
    removeEditPopover();
    const pop=document.createElement('div');pop.className='label-edit-popover';pop.setAttribute('role','dialog');pop.setAttribute('aria-label','Editar item da interface');
    const label=el.dataset.uiControlLabel||controlLabel(el);const isHidden=el.dataset.uiHidden==='1';const kind=el.dataset.uiControlKind==='card'?'card':'button';
    pop.innerHTML='<b></b><small></small><div class="label-edit-actions"></div>';
    pop.querySelector('b').textContent=isHidden?(kind==='card'?'Card oculto':'Botão oculto'):(kind==='card'?'Editar card':'Editar botão');pop.querySelector('small').textContent=label;
    const actions=pop.querySelector('.label-edit-actions');
    if(!isHidden){const edit=document.createElement('button');edit.type='button';edit.className='btn small';edit.textContent=kind==='card'?'Editar nome':'Editar texto';edit.addEventListener('click',()=>{removeEditPopover();if(kind==='card')promptCardTitleEdit(el);else promptTextEdit(editableTargetInsideControl(el));});actions.appendChild(edit);}
    const vis=document.createElement('button');vis.type='button';vis.className='btn small '+(isHidden?'primary':'danger-soft');vis.textContent=isHidden?(kind==='card'?'Exibir card':'Exibir botão'):(kind==='card'?'Ocultar card':'Ocultar botão');
    vis.addEventListener('click',()=>{vis.disabled=true;saveControlVisibility(el,!isHidden).then(()=>{removeEditPopover();if(!document.body.classList.contains('label-edit-mode'))location.reload();}).catch(()=>{vis.disabled=false;alert('Não foi possível salvar a visibilidade deste item.');});});actions.appendChild(vis);
    const cancel=document.createElement('button');cancel.type='button';cancel.className='btn small';cancel.textContent='Cancelar';cancel.addEventListener('click',removeEditPopover);actions.appendChild(cancel);
    document.body.appendChild(pop);const r=el.getBoundingClientRect();const pw=Math.min(330,window.innerWidth-24);pop.style.width=pw+'px';let left=Math.max(12,Math.min(r.left,window.innerWidth-pw-12));let top=Math.min(window.innerHeight-pop.offsetHeight-12,r.bottom+8);if(top<12)top=12;pop.style.left=left+'px';pop.style.top=top+'px';
  }

  const toggle=document.getElementById('label-editor-toggle');let editMode=false;
  if(toggle && canCustomize){
    toggle.addEventListener('click',()=>{editMode=!editMode;removeEditPopover();document.body.classList.toggle('label-edit-mode',editMode);toggle.classList.toggle('active',editMode);toggle.textContent=editMode?'✓ Finalizar edição':'✎ Editar interface';markEditable(editMode);});
    document.addEventListener('click',e=>{
      if(!editMode || e.target.closest('.label-edit-popover'))return;
      // V1.3.81: quando o usuário toca diretamente em um texto dentro de um
      // botão/card, o texto exato tem prioridade sobre o controle inteiro.
      // Isso permite editar título, unidade/rótulo e subtítulo separadamente.
      const el=closestEditable(e.target);
      if(el && el!==toggle && !el.closest('.label-editor-toggle')){
        e.preventDefault();e.stopPropagation();promptTextEdit(el);return;
      }
      const control=e.target.closest('[data-ui-control-key]');
      if(control && control!==toggle && !control.closest('.label-editor-toggle')){e.preventDefault();e.stopPropagation();openControlPopover(control);return;}
    },true);
  }

  // V1.3.80 — conteúdo carregado por AJAX (ex.: drawer do BI) também recebe
  // personalização e entra no modo de edição sem precisar recarregar a página.
  let uiMutationPending=false;
  const refreshDynamicUi=()=>{
    if(uiMutationPending)return;
    uiMutationPending=true;
    requestAnimationFrame(()=>{
      uiMutationPending=false;
      applyTextOverrides(document.body);
      assignControlKeys();assignSurfaceKeys();applyHiddenControls();
      if(document.body.classList.contains('label-edit-mode'))markEditable(true);
    });
  };
  const uiObserver=new MutationObserver(mutations=>{
    if(mutations.some(m=>m.addedNodes && m.addedNodes.length))refreshDynamicUi();
  });
  uiObserver.observe(document.body,{childList:true,subtree:true});

  function initTablePagination(table){
    if(table.dataset.noPagination!==undefined||table.classList.contains('operator-table')||table.classList.contains('routing-table')||table.classList.contains('closeout-table')||(table.closest('.panel')&&table.closest('.panel').querySelector('.pagination-bar')))return;
    const tbody=table.tBodies[0];if(!tbody)return;const rows=[...tbody.rows];if(rows.length<=25)return;
    let page=1,per=25;const wrap=table.closest('.table-wrap')||table.parentElement;const bar=document.createElement('div');bar.className='table-pager';bar.innerHTML='<span class="pager-info"></span><div><label>Linhas <select><option>25</option><option>50</option><option>100</option></select></label><button type="button" class="btn small prev" aria-label="Página anterior">‹</button><span class="pager-page"></span><button type="button" class="btn small next" aria-label="Próxima página">›</button></div>';wrap.appendChild(bar);
    const render=()=>{const pages=Math.max(1,Math.ceil(rows.length/per));page=Math.min(page,pages);rows.forEach((r,i)=>r.hidden=!(i>=(page-1)*per&&i<page*per));bar.querySelector('.pager-info').textContent=rows.length+' registro(s)';bar.querySelector('.pager-page').textContent=page+' / '+pages;bar.querySelector('.prev').disabled=page<=1;bar.querySelector('.next').disabled=page>=pages;};
    bar.querySelector('select').addEventListener('change',e=>{per=Number(e.target.value);page=1;render();});bar.querySelector('.prev').addEventListener('click',()=>{if(page>1){page--;render();}});bar.querySelector('.next').addEventListener('click',()=>{if(page<Math.ceil(rows.length/per)){page++;render();}});render();
  }
  document.querySelectorAll('.table-wrap table,.bi-card table,.panel table').forEach(initTablePagination);

  function initBiResultPanels(){
    if(!document.body.classList.contains('page-bi'))return;
    const section=document.querySelector('.bi-section');if(!section)return;
    const quality=document.querySelector('.bi-data-quality');
    if(quality && quality.parentElement!==section)section.appendChild(quality);
    const candidates=[...section.children].filter(el=>!el.classList.contains('bi-heading'));
    const groups=[];const cleanup=[];let summary=[];
    const cleanLabel=t=>(t||'').replace(/\s+/g,' ').trim();
    const labelFor=el=>{
      if(el.classList.contains('bi-data-quality'))return 'Qualidade dos dados';
      if(el.classList.contains('bi-alert-list'))return 'Alertas';
      const h=el.querySelector(':scope > h3,:scope > h2,:scope > .panel-title h3,:scope > .panel-title h2');
      if(h)return cleanLabel(h.dataset.uiOriginalText||h.textContent);
      const nested=el.querySelector('h3,h2');
      return nested?cleanLabel(nested.dataset.uiOriginalText||nested.textContent):'Detalhes';
    };
    const flushSummary=()=>{if(summary.length){groups.push({label:'Resumo',els:summary});summary=[];}};
    candidates.forEach(el=>{
      if(el.matches('.bi-kpi-grid,.bi-data-warning,.bi-executive-note,.sales-dashboard-head,.sales-compare-form,.dre-banner')){summary.push(el);return;}
      flushSummary();
      const cards=[...el.children].filter(ch=>ch.matches('article.bi-card,.bi-card'));
      if(cards.length>1 && (el.classList.contains('bi-two')||el.classList.contains('sales-main-grid'))){
        cleanup.push(el);cards.forEach(card=>groups.push({label:labelFor(card),els:[card]}));
      }else groups.push({label:labelFor(el),els:[el]});
    });
    flushSummary();
    const mergedGroups=[];
    const groupKey=label=>{const t=cleanLabel(label).toLowerCase();if(t.includes('qualidade'))return 'qualidade-dados';return t;};
    groups.forEach(g=>{const key=groupKey(g.label);const found=mergedGroups.find(x=>x.key===key);if(found)found.els.push(...g.els);else mergedGroups.push({key,label:g.label,els:[...g.els]});});
    if(mergedGroups.length<2)return;
    const nav=document.createElement('nav');nav.className='bi-mobile-section-nav bi-result-nav';nav.setAttribute('aria-label','Resultados desta área');
    const resultMeta=label=>{
      const t=cleanLabel(label).toLowerCase();
      if(t==='resumo'||t.includes('visão')||t.includes('resultados consolidados'))return {icon:'▦',tone:'blue',hint:'Indicadores principais'};
      if(t.includes('produto'))return {icon:'▣',tone:'amber',hint:'Itens e documentos'};
      if(t.includes('categoria')||t.includes('subcategoria'))return {icon:'≡',tone:'orange',hint:'Categorias e produtos'};
      if(t.includes('vendedor'))return {icon:'♙',tone:'teal',hint:'Desempenho por vendedor'};
      if(t.includes('motorista'))return {icon:'♙',tone:'teal',hint:'Entregas por motorista'};
      if(t.includes('roteiro'))return {icon:'⇢',tone:'indigo',hint:'Roteiros e custos'};
      if(t.includes('transportadora'))return {icon:'◇',tone:'orange',hint:'Transportadoras e entregas'};
      if(t.includes('status'))return {icon:'✓',tone:'emerald',hint:'Situação das entregas'};
      if(t.includes('cliente')||t.includes('devedor'))return {icon:'◎',tone:'blue',hint:'Clientes e documentos'};
      if(t.includes('fornecedor'))return {icon:'◇',tone:'orange',hint:'Fornecedores e compras'};
      if(t.includes('aging')||t.includes('atras')||t.includes('vencid'))return {icon:'!',tone:'rose',hint:'Faixas e vencimentos'};
      if(t.includes('previs')||t.includes('projet'))return {icon:'⌁',tone:'cyan',hint:'Projeções do período'};
      if(t.includes('carteira'))return {icon:'▤',tone:'violet',hint:'Carteiras e títulos'};
      if(t.includes('banco'))return {icon:'▥',tone:'slate',hint:'Bancos e títulos'};
      if(t.includes('estado')||t.includes('região')||t.includes('cidade'))return {icon:'⌖',tone:'emerald',hint:'Distribuição geográfica'};
      if(t.includes('recebid'))return {icon:'↓',tone:'emerald',hint:'Baixas e recebimentos'};
      if(t.includes('pago')||t.includes('pagos')||t.includes('pagas'))return {icon:'↑',tone:'teal',hint:'Baixas e pagamentos'};
      if(t.includes('fluxo')||t.includes('entrada')||t.includes('saída'))return {icon:'⇄',tone:'cyan',hint:'Movimentação financeira'};
      if(t.includes('dre')||t.includes('resultado'))return {icon:'Σ',tone:'violet',hint:'Resultado gerencial'};
      if(t.includes('qualidade')||t.includes('disponibilidade'))return {icon:'✓',tone:'emerald',hint:'Cobertura dos dados'};
      if(t.includes('alerta')||t.includes('oportunidade'))return {icon:'!',tone:'rose',hint:'Atenções gerenciais'};
      if(t.includes('nota')||t.includes('nf-e')||t.includes('título')||t.includes('detalhe'))return {icon:'▤',tone:'indigo',hint:'Documentos e detalhes'};
      return {icon:'•',tone:'slate',hint:'Abrir resultado'};
    };
    mergedGroups.forEach((g,i)=>{
      const wrap=document.createElement('div');wrap.className='bi-mobile-panel bi-result-panel';wrap.dataset.mobilePanel=String(i);
      section.appendChild(wrap);g.els.forEach(el=>wrap.appendChild(el));
      const meta=resultMeta(g.label);const displayLabel=cleanLabel(map[g.label]||g.label);
      const b=document.createElement('button');b.type='button';b.className='bi-mobile-section-btn bi-result-btn tone-'+meta.tone;b.dataset.target=String(i);b.title=displayLabel;b.dataset.uiOriginalCardLabel=g.label;
      const icon=document.createElement('span');icon.className='bi-result-icon';icon.textContent=meta.icon;
      const copy=document.createElement('span');copy.className='bi-result-copy';const title=document.createElement('b');title.textContent=displayLabel.length>38?displayLabel.slice(0,36)+'…':displayLabel;const hint=document.createElement('small');hint.textContent=meta.hint;copy.append(title,hint);b.append(icon,copy);nav.appendChild(b);
    });
    cleanup.forEach(el=>el.remove());
    section.before(nav);
    const activate=i=>{
      section.querySelectorAll('.bi-mobile-panel').forEach((p,idx)=>p.classList.toggle('active',idx===i));
      nav.querySelectorAll('button').forEach((b,idx)=>{b.classList.toggle('active',idx===i);b.setAttribute('aria-pressed',idx===i?'true':'false');});
    };
    nav.addEventListener('click',e=>{const b=e.target.closest('button[data-target]');if(!b)return;activate(Number(b.dataset.target));if(window.innerWidth<=900)section.scrollIntoView({behavior:'smooth',block:'start'});});
    activate(0);
    nav.querySelectorAll('.bi-result-btn').forEach((b,index)=>{
      if(!b.dataset.uiControlKey){
        const originalLabel=cleanLabel(b.dataset.uiOriginalCardLabel||b.title||b.textContent||('Card de navegação '+(index+1)));
        const visibleLabel=cleanLabel(b.title||b.textContent||originalLabel);
        b.dataset.uiControlKey=location.pathname+'|CARD|bi-result-btn|'+hashString(originalLabel.toLowerCase());
        b.dataset.uiControlLabel=visibleLabel;
        b.dataset.uiOriginalCardLabel=originalLabel;
        b.dataset.uiControlKind='card';
      }
    });
    applyHiddenControls();
    const firstVisible=[...nav.querySelectorAll('.bi-result-btn')].findIndex(b=>!b.hidden);
    if(firstVisible>=0)activate(firstVisible);
  }
  initBiResultPanels();

  document.querySelectorAll('.nav-dropdown,.mobile-app-menu').forEach(d=>{d.addEventListener('toggle',()=>{if(!d.open)return;document.querySelectorAll('.nav-dropdown[open],.mobile-app-menu[open]').forEach(o=>{if(o!==d)o.removeAttribute('open');});});});
})();
