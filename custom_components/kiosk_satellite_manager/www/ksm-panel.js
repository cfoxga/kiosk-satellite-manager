/** Kiosk Satellite Manager's Global → Fleet → Device split pane (KSM-BEHAVE-190–197). */
import { LitElement, html, css } from './lit/lit-all.min.js';
const _version = new URL(import.meta.url).searchParams.get('v');
await import(`./ksm-flow.js${_version ? `?v=${encodeURIComponent(_version)}` : ''}`);

const DOMAIN='kiosk_satellite_manager';
const STORE='ksm-tree-expanded';
const allNodes = root => root ? [root,...(root.children || []).flatMap(group=>[group,...(group.children || [])])] : [];
const nodeKey = node => node.kind==='offer' ? `${node.entry_id}:offer:${node.flow_id}` :
  node.subentry_id ? `${node.entry_id}:${node.subentry_id}` : node.entry_id;

class KsmPanel extends LitElement {
  static properties={hass:{type:Object},narrow:{type:Boolean},panel:{type:Object},
    tree:{state:true},selection:{state:true},showDetail:{state:true},flowOpen:{state:true},
    error:{state:true},expanded:{state:true},confirmDelete:{state:true},issuesText:{state:true}};
  static styles=css`
    :host { display:block; height:100%; color:var(--primary-text-color); font-family:var(--ha-font-family,sans-serif); }
    .shell { height:100%; display:grid; grid-template-columns:minmax(245px,310px) minmax(0,1fr); }
    aside { overflow:auto; background:var(--card-background-color); border-right:1px solid var(--divider-color); }
    main { overflow:auto; padding:22px clamp(18px,3vw,40px); }
    h1 { font-size:21px; margin:0 0 22px; } h2 { font-size:24px; margin:0 0 8px; }
    .treehead { padding:22px 18px 14px; font-weight:700; }
    .node { display:flex; align-items:center; width:100%; box-sizing:border-box; border:0; background:transparent;
      color:inherit; text-align:left; padding:9px 14px; gap:8px; cursor:pointer; font:inherit; min-height:42px; }
    .node:hover { background:var(--secondary-background-color); } .node.selected { background:var(--secondary-background-color); color:var(--primary-color); font-weight:600; }
    .node .label { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; flex:1; }
    .indent1 { padding-left:27px; } .indent2 { padding-left:47px; }
    .chevron { width:17px; cursor:pointer; } .badge,.chip { font-size:11px; border-radius:99px; padding:2px 7px; background:var(--secondary-background-color); }
    .offline { color:var(--error-color,#b00020); } .online { color:var(--success-color,#2e7d32); }
    .section { border:1px solid var(--divider-color); background:var(--card-background-color); border-radius:10px; padding:18px; margin:16px 0; }
    .section h3 { margin:0 0 14px; font-size:16px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
    .metric { display:grid; gap:4px; font-size:13px; } .metric span { color:var(--secondary-text-color); } .metric strong { font-size:17px; }
    button.action { padding:9px 13px; margin:4px 6px 4px 0; border:1px solid var(--divider-color); border-radius:7px;
      background:var(--card-background-color); color:var(--primary-text-color); cursor:pointer; }
    button.action:hover { border-color:var(--primary-color); } button.action:disabled { opacity:.5; cursor:not-allowed; }
    .error { color:var(--error-color,#b00020); } .muted { color:var(--secondary-text-color); } a { color:var(--primary-color); }
    .back { display:none; } .pending { font-style:italic; }
    @media (max-width:700px) { .shell { display:block; } .shell.detail aside { display:none; }
      .shell:not(.detail) main { display:none; } aside,main { height:100%; box-sizing:border-box; }
      .back { display:inline-block; margin-bottom:14px; } }
  `;
  constructor() { super(); this.tree=null; this.selection=null; this.showDetail=false; this.flowOpen=false; this.issuesText={};
    this.error=''; this.confirmDelete=false; try { this.expanded=JSON.parse(localStorage.getItem(STORE)||'{}'); } catch (_) { this.expanded={}; } }
  connectedCallback() { super.connectedCallback(); window.addEventListener('popstate',this._pop=()=>this._syncUrl()); }
  disconnectedCallback() { super.disconnectedCallback(); window.removeEventListener('popstate',this._pop);
    if (this._unsub) this._unsub(); this._discardFlow(); }
  updated(changed) { if (changed.has('hass') && this.hass && !this._unsub && !this._subscribing) this._subscribe(); }
  async _subscribe() {
    this._subscribing=true;
    try { const words=await this.hass.connection.sendMessagePromise({type:'frontend/get_translations',
        language:this.hass.language||'en',category:'issues',integration:[DOMAIN]});
      this.issuesText=words.resources||{};
      this._unsub=await this.hass.connection.subscribeMessage(msg=>{
      this.tree=msg; this._syncUrl();
    },{type:`${DOMAIN}/subscribe_tree`}); }
    catch (e) { this.error=e.message||String(e); }
    finally { this._subscribing=false; }
  }
  _syncUrl() {
    if (!this.tree) return;
    const wanted=new URLSearchParams(location.search).get('node');
    const found=allNodes(this.tree).find(n=>nodeKey(n)===wanted);
    this.selection=found || this.tree;
    this.showDetail=Boolean(found && wanted);
    if (!found && wanted) history.replaceState({},'',location.pathname);
  }
  async _discardFlow() { clearTimeout(this._finishTimer);
    const runner=this.renderRoot?.querySelector('ksm-flow');
    if (runner) await runner.close();
    this.flowOpen=false;
  }
  async _choose(node) { await this._discardFlow(); this.selection=node; this.showDetail=true; this.confirmDelete=false;
    const url=new URL(location.href); url.searchParams.set('node',nodeKey(node)); history.pushState({},'',url); }
  _toggle(node) { const key=nodeKey(node); this.expanded={...this.expanded,[key]:this.expanded[key]===false};
    localStorage.setItem(STORE,JSON.stringify(this.expanded)); }
  _row(node,level) {
    const key=nodeKey(node), open=this.expanded[key]!==false, branch=Boolean(node.children);
    return html`<div class="node ${level ? `indent${level}` : ''} ${node===this.selection?'selected':''} ${node.kind==='offer'?'pending':''}">
      ${branch ? html`<span class="chevron" @click=${()=>this._toggle(node)}>${open?'▾':'▸'}</span>` : html`<span class="chevron"></span>`}
      <span class="label" @click=${()=>this._choose(node)}>${node.title}</span>
      ${node.leader ? html`<span class="badge">Leader</span>` : ''}
      ${node.kind==='device' ? html`<span class="chip ${node.online?'online':'offline'}">${node.online?'Online':'Offline'}</span>` : ''}
      ${node.kind==='fleet' ? html`<span class="chip">${this._state(this._id(node,'sensor','fleet_online_count'))}/${this._state(this._id(node,'sensor','fleet_managed_count'))} online</span>` : ''}
      ${node.kind==='unmanaged' ? html`<span class="chip">${this._state(this._id(node,'sensor','fleet_managed_count'))} devices</span>` : ''}
      ${node.repairs?.length ? html`<span class="badge" title="Open repairs">${node.repairs.length}</span>` : ''}
    </div>${branch && open ? (node.children || []).map(child=>this._row(child,level+1)) : ''}`;
  }
  _ids(node,domain) { return node?.entities?.[domain] || []; }
  _id(node,domain,suffix) { return this._ids(node,domain).find(id=>id.endsWith(`_${suffix}`)); }
  _state(id) { const value=this.hass?.states?.[id]?.state; return !value || ['unavailable','unknown'].includes(value) ? 'Unknown' : value; }
  _metric(node,label,domain,suffix) { const id=this._id(node,domain,suffix);
    return html`<div class="metric"><span>${label}</span><strong>${id?this._state(id):'Unknown'}</strong></div>`; }
  _button(node,label,suffix) { const id=this._id(node,'button',suffix); if (!id) return '';
    return html`<button class="action" ?disabled=${this.hass?.states?.[id]?.state==='unavailable'} @click=${()=>this._service('button','press',id)}>${label}</button>`; }
  async _service(domain,service,entity_id,option=null) { try { await this.hass.callService(domain,service,{entity_id,...(option === null ? {} : {option})}); }
    catch (e) { this.error=e.message||String(e); } }
  _switch(node,label,suffix) { const id=this._id(node,'switch',suffix); if (!id) return '';
    const on=this._state(id)==='on'; return html`<button class="action" @click=${()=>this._service('switch',on?'turn_off':'turn_on',id)}>${label}: ${on?'On':'Off'}</button>`; }
  _onFlowFinished() { clearTimeout(this._finishTimer); this._finishTimer=setTimeout(()=>{
    this.renderRoot?.querySelector('ksm-flow')?.close();
  },4000); }
  async _flow(kind,args={}) { await this._discardFlow(); this.flowOpen=true; await this.updateComplete;
    await this.renderRoot.querySelector('ksm-flow').open(kind,args); }
  _repairs(node) { if (!node.repairs?.length) return '';
    return html`<section class="section"><h3>Repairs</h3>${node.repairs.map(issue=>html`<div>
      ${this.issuesText[`component.${DOMAIN}.issues.${issue.translation_key}.title`] || issue.translation_key}
      <button class="action" @click=${()=>this._flow('repair',{issue_id:issue.issue_id,translation_key:issue.translation_key})}>Fix</button>
    </div>`)}</section>`; }
  _global(node) { return html`
    <section class="section"><h3>Status</h3><div class="grid">
      ${this._metric(node,'Latest release','sensor','latest_kiosk_satellite_release')}
      ${this._metric(node,'Device catalog','sensor','device_catalog')}
      ${this._metric(node,'Install recipes','sensor','install_recipes')}
    </div></section>
    <section class="section"><h3>Actions</h3>
      ${this._button(node,'Check for updates','check_for_updates')}
      ${this._button(node,'Update all','update_all')}
      ${this._button(node,'Back up all','backup_all')}
      ${this._switch(node,'Auto-update all','auto_update_all')}
      <button class="action" @click=${()=>this._flow('options',{entry_id:node.entry_id})}>Settings</button>
      <button class="action" @click=${()=>this._flow('config',{fleet_entry_id:'unmanaged'})}>Add device</button>
    </section>`; }
  _fleet(node) { const fleet=node.kind==='fleet'; return html`
    <section class="section"><h3>Status</h3><div class="grid">
      ${fleet ? html`${['leader','managed_members','online_count','offline_count','version_mismatch','blocked_sync','pending_invitations','last_poll'].map(k=>this._metric(node,k.replaceAll('_',' '),'sensor',`fleet_${k}`))}` :
        this._metric(node,'Managed devices','sensor','fleet_managed_count')}
    </div></section>
    <section class="section"><h3>Devices and offers</h3>
      ${(node.children||[]).map(child=>html`<div>${child.title}
        ${child.kind==='offer' ? html`<button class="action" @click=${()=>this._flow('config',{flow_id:child.flow_id})}>Add</button>` : ''}</div>`)}
      <button class="action" @click=${()=>this._flow('config',{fleet_entry_id:fleet?node.entry_id:'unmanaged'})}>Add device${fleet?' to this fleet':''}</button>
    </section>`; }
  _device(node) { return html`
    <section class="section"><h3>Status</h3><div class="grid">
      <div class="metric"><span>Connection</span><strong>${node.online?'Online':'Offline'}</strong></div>
      ${this._metric(node,'IP address','sensor','ip_address')}
      ${this._metric(node,'Device type','sensor','device_type')}
      ${this._metric(node,'Install recipe','sensor','recipe')}
      ${this._metric(node,'Fleet membership','sensor','fleet_membership')}
      ${this._metric(node,'ADB enabled','binary_sensor','adb_enabled')}
      ${this._metric(node,'Permissions','binary_sensor','permissions')}
    </div></section>
    <section class="section"><h3>Actions</h3>
      ${this._button(node,'Install/Reinstall','install_kiosk_satellite')}${this._button(node,'Uninstall','uninstall_kiosk_satellite')}
      ${this._button(node,'Fix permissions','fix_permissions')}
      ${this._button(node,'Back up configuration','backup_config')}
      ${this._button(node,'Restore configuration','restore_config')}
      ${this._id(node,'select','config_backup') ? html`<select aria-label="Configuration backup" @change=${e=>this._service('select','select_option',this._id(node,'select','config_backup'),e.target.value)}>
        ${(this.hass?.states?.[this._id(node,'select','config_backup')]?.attributes?.options||[]).map(opt=>html`<option value=${opt}>${opt}</option>`)}</select>` : ''}
      ${this._switch(node,'Auto-update','auto_update_kiosk_satellite')}
      <button class="action" @click=${()=>this._flow('subentry',{entry_id:node.entry_id,subentry_id:node.subentry_id})}>Configure</button>
      <button class="action" @click=${()=>this._diagnostics(node)}>Download diagnostics</button>
      <button class="action" @click=${()=>{this.confirmDelete=true;}}>Delete device</button>
      ${this.confirmDelete ? html`<p>Delete ${node.title}?</p>
        <button class="action" @click=${()=>this._delete(node)}>Delete ${node.title}</button>
        <button class="action" @click=${()=>{this.confirmDelete=false;}}>Cancel</button>` : ''}
      ${node.device_id ? html`<a href="/config/devices/device/${node.device_id}">Open in Home Assistant</a>` : ''}
    </section>`; }
  async _delete(node) { try { await this.hass.connection.sendMessagePromise({type:'config_entries/subentries/delete',entry_id:node.entry_id,subentry_id:node.subentry_id});
      this.confirmDelete=false; this._choose(this.tree); } catch(e) { this.error=e.message||String(e); } }
  async _diagnostics(node) {
    if (!node.device_id) { this.error='No Home Assistant device is linked'; return; }
    try {
      const data=await this.hass.callApi('GET',`diagnostics/config_entry/${node.entry_id}/device/${node.device_id}`);
      const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
      const url=URL.createObjectURL(blob); const link=document.createElement('a');
      link.href=url; link.download=`ksm-${node.title}-diagnostics.json`; link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch(e) { this.error=e.message||String(e); }
  }
  render() { const selected=this.selection || this.tree;
    return html`<div class="shell ${this.showDetail?'detail':''}"><aside aria-label="KSM tree">
      <div class="treehead">Kiosk Satellite Manager</div>
      ${this.tree ? this._row(this.tree,0) : html`<p>Loading…</p>`}
    </aside><main aria-label="KSM detail">
      <button class="action back" @click=${()=>{this.showDetail=false;}}>Back to tree</button>
      <h2>${selected?.title || 'Kiosk Satellite Manager'}</h2>
      ${this.error ? html`<p class="error">${this.error}</p>` : ''}
      ${this.flowOpen ? html`<ksm-flow .hass=${this.hass} @flow-close=${()=>{this.flowOpen=false;}} @flow-finished=${()=>this._onFlowFinished()}></ksm-flow>` :
        selected?.kind==='global' ? this._global(selected) :
        selected?.kind==='fleet' || selected?.kind==='unmanaged' ? this._fleet(selected) :
        selected?.kind==='device' ? this._device(selected) :
        selected?.kind==='offer' ? html`<button class="action" @click=${()=>this._flow('config',{flow_id:selected.flow_id})}>Add ${selected.title}</button>` : ''}
      ${selected ? this._repairs(selected) : ''}
    </main></div>`; }
}
customElements.define('ksm-panel',KsmPanel);
