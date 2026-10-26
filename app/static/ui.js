(function(){
  const map = window.UI_LABEL_OVERRIDES || {};
  const canCustomize = !!window.UI_CAN_CUSTOMIZE;
  const SKIP_TAGS = new Set(['SCRIPT','STYLE','TEXTAREA','CODE','PRE']);
  const UI_TEXT_SELECTOR = [
    'h1','h2','h3','h4','h5','h6','thead th','button','.btn','.topbar a','.topbar summary','.commercial-brand b','.commercial-brand small',
    '.module-nav a','.module-nav-title span','.module-nav-group>b','label','.hint','.hint-block',
    '.hero p','.panel-title p','.panel-title span','.panel>p','.bi-card>p','.bi-executive-note p',
    '.bi-note','.bi-period','.bi-kicker','.bi-sync','.bi-data-warning','.bi-unavailable','.bi-empty',
    '.alert','.status-tabs a','.mobile-menu-divider','.mobile-bottom-nav a span','.mobile-home-card b','.mobile-home-card small','.mobile-home-feature b','.mobile-home-feature small','.mobile-home-feature em','.mobile-home-bi-shortcuts a','.bi-kpi>span','.bi-kpi>small','.metric>span',
    '.info-card>span','.theme-color-field span','select option[value=""]','select[name="situation"] option',
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
    document.querySelectorAll('.ui-editable-text').forEach(el=>el.classList.remove('ui-editable-text'));
    if(!on)return;
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
  const toggle=document.getElementById('label-editor-toggle');let editMode=false;
  if(toggle && canCustomize){
    toggle.addEventListener('click',()=>{editMode=!editMode;document.body.classList.toggle('label-edit-mode',editMode);toggle.classList.toggle('active',editMode);toggle.textContent=editMode?'✓ Finalizar edição':'✎ Editar textos';markEditable(editMode);});
    document.addEventListener('click',e=>{if(!editMode)return;const el=closestEditable(e.target);if(!el||el===toggle||el.closest('.label-editor-toggle'))return;e.preventDefault();e.stopPropagation();promptTextEdit(el);},true);
  }

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
      if(h)return cleanLabel(h.textContent);
      const nested=el.querySelector('h3,h2');
      return nested?cleanLabel(nested.textContent):'Detalhes';
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
      if(t.includes('cliente')||t.includes('devedor'))return {icon:'◎',tone:'blue',hint:'Clientes e documentos'};
      if(t.includes('fornecedor'))return {icon:'◇',tone:'orange',hint:'Fornecedores e compras'};
      if(t.includes('aging')||t.includes('atras')||t.includes('vencid'))return {icon:'!',tone:'rose',hint:'Faixas e vencimentos'};
      if(t.includes('previs')||t.includes('projet'))return {icon:'⌁',tone:'cyan',hint:'Projeções do período'};
      if(t.includes('carteira'))return {icon:'▤',tone:'violet',hint:'Carteiras e títulos'};
      if(t.includes('banco'))return {icon:'▥',tone:'slate',hint:'Bancos e títulos'};
      if(t.includes('estado')||t.includes('região')||t.includes('cidade'))return {icon:'⌖',tone:'emerald',hint:'Distribuição geográfica'};
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
      const meta=resultMeta(g.label);
      const b=document.createElement('button');b.type='button';b.className='bi-mobile-section-btn bi-result-btn tone-'+meta.tone;b.dataset.target=String(i);b.title=g.label;
      const icon=document.createElement('span');icon.className='bi-result-icon';icon.textContent=meta.icon;
      const copy=document.createElement('span');copy.className='bi-result-copy';const title=document.createElement('b');title.textContent=g.label.length>38?g.label.slice(0,36)+'…':g.label;const hint=document.createElement('small');hint.textContent=meta.hint;copy.append(title,hint);b.append(icon,copy);nav.appendChild(b);
    });
    cleanup.forEach(el=>el.remove());
    section.before(nav);
    const activate=i=>{
      section.querySelectorAll('.bi-mobile-panel').forEach((p,idx)=>p.classList.toggle('active',idx===i));
      nav.querySelectorAll('button').forEach((b,idx)=>{b.classList.toggle('active',idx===i);b.setAttribute('aria-pressed',idx===i?'true':'false');});
    };
    nav.addEventListener('click',e=>{const b=e.target.closest('button[data-target]');if(!b)return;activate(Number(b.dataset.target));if(window.innerWidth<=900)section.scrollIntoView({behavior:'smooth',block:'start'});});
    activate(0);
  }
  initBiResultPanels();

  document.querySelectorAll('.nav-dropdown,.mobile-app-menu').forEach(d=>{d.addEventListener('toggle',()=>{if(!d.open)return;document.querySelectorAll('.nav-dropdown[open],.mobile-app-menu[open]').forEach(o=>{if(o!==d)o.removeAttribute('open');});});});
})();
