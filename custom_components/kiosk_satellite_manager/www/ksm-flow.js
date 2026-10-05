/** KSM's existing Home Assistant flows rendered in the panel (KSM-BEHAVE-198). */
import { LitElement, html, css } from './lit/lit-all.min.js';

const PATHS = {
  config: 'config/config_entries/flow',
  options: 'config/config_entries/options/flow',
  subentry: 'config/config_entries/subentries/flow',
  repair: 'repairs/issues/fix',
};

export function fieldKind(field) {
  if (field.selector) {
    if (field.selector.select) return 'choice';
    if (field.selector.text) return field.selector.text.type === 'password' ? 'password' : 'text';
    if (field.selector.boolean) return 'boolean';
    if (field.selector.number) return 'integer';
    if (field.selector.area) return 'area';
    return 'unsupported';
  }
  if (field.type === 'boolean') return 'boolean';
  if (field.type === 'integer' || field.type === 'positive_int') return 'integer';
  if (field.type === 'password') return 'password';
  if (field.type === 'string' || field.type === 'str' || field.type === 'text') return 'text';
  if (field.type === 'select' || field.type === 'list') return 'choice';
  return 'unsupported';
}

class KsmFlow extends LitElement {
  static properties = {
    hass: { type: Object },
    flow: { state: true }, busy: { state: true }, error: { state: true },
    values: { state: true }, translations: { state: true }, areas: { state: true },
  };
  static styles = css`
    :host { display:block; } .field { display:grid; gap:5px; margin:13px 0; }
    label { font-size:13px; font-weight:600; } input,select { box-sizing:border-box; width:100%;
      background:var(--card-background-color); color:var(--primary-text-color);
      border:1px solid var(--divider-color); border-radius:7px; padding:9px; }
    input[type=checkbox] { width:auto; } button { border:0; border-radius:7px; padding:9px 14px;
      margin:5px 6px 5px 0; cursor:pointer; background:var(--primary-color); color:white; }
    button:disabled { opacity:.5; cursor:not-allowed; } .error { color:var(--error-color,#b00020); }
    .menu { display:grid; gap:7px; } .menu button { text-align:left; }
    .description { white-space:pre-wrap; line-height:1.5; }
  `;
  constructor() { super(); this.flow=null; this.busy=false; this.error=''; this.values={}; this.translations={}; this.areas=[]; this._path=''; this._unsub=null; }

  async open(kind, args={}) {
    await this._dispose();
    this.error=''; this.busy=true; this.kind=kind;
    this._path=PATHS[kind]; this._issueKey=args.translation_key;
    try {
      await this._loadTranslations();
      this.areas=await this.hass.connection.sendMessagePromise({type:'config/area_registry/list'});
      const body = kind === 'config' ? {handler:'kiosk_satellite_manager'} :
        kind === 'options' ? {handler:args.entry_id} :
        kind === 'subentry' ? {handler:[args.entry_id,'device'], subentry_id:args.subentry_id} :
        {handler:'kiosk_satellite_manager', issue_id:args.issue_id};
      const result = args.flow_id ? await this.hass.callApi('GET', `${this._path}/${args.flow_id}`) :
        await this.hass.callApi('POST', this._path, body);
      this._prefill = args.fleet_entry_id;
      this._onResult(result);
      if (args.menu_item && result.type === 'menu' && result.menu_options?.includes(args.menu_item)) {
        await this.submit({next_step_id:args.menu_item});
      }
      this._unsub = await this.hass.connection.subscribeEvents((event) => {
        if (event.data?.flow_id === this.flow?.flow_id && this.flow?.type === 'progress') this.refresh();
      }, 'data_entry_flow_progressed');
    } catch (e) { this.error=e?.body?.message || e.message || String(e); }
    finally { this.busy=false; }
  }

  async _dispose() {
    const flowId=this.flow?.flow_id;
    if (this._unsub) { this._unsub(); this._unsub=null; }
    this.flow=null;
    if (flowId && this._path) {
      try { await this.hass.callApi('DELETE', `${this._path}/${flowId}`); } catch (_) { /* already completed */ }
    }
  }
  async close() {
    await this._dispose();
    this.dispatchEvent(new CustomEvent('flow-close', {bubbles:true, composed:true}));
  }
  disconnectedCallback() { super.disconnectedCallback(); void this._dispose(); }

  async refresh() {
    if (!this.flow?.flow_id) return;
    try {
      const result=await this.hass.callApi('GET', `${this._path}/${this.flow.flow_id}`);
      if (result.type==='progress_done') await this._advanceProgress(result);
      else this._onResult(result);
    }
    catch (e) { this.error=e?.body?.message || e.message || String(e); }
  }

  _onResult(result) {
    this.flow=result;
    this.values={};
    for (const field of result.data_schema || []) {
      if (field.type === 'password' || field.selector?.text?.type === 'password') continue;
      if (field.name === 'fleet_entry_id' && this._prefill) this.values[field.name]=this._prefill;
      else if (field.default !== undefined) this.values[field.name]=field.default;
    }
    if (result.type === 'create_entry' || result.type === 'abort') {
      this.dispatchEvent(new CustomEvent('flow-finished', {bubbles:true, composed:true, detail:result}));
    }
  }
  async _advanceProgress(result) {
    if (result.type !== 'progress_done') return this._onResult(result);
    this._onResult(await this.hass.callApi('POST', `${this._path}/${result.flow_id}`, {next_step_id:result.next_step_id}));
  }
  async submit(data=null) {
    if (!this.flow?.flow_id || this.busy) return;
    this.busy=true; this.error='';
    try { await this._advanceProgress(await this.hass.callApi('POST', `${this._path}/${this.flow.flow_id}`, data || this.values)); }
    catch (e) { this.error=e?.body?.message || e.message || String(e); }
    finally { this.busy=false; }
  }
  _set(name,value) { this.values={...this.values,[name]:value}; }
  async _loadTranslations() {
    const categories=['config','options','config_subentries','issues'];
    const language=this.hass?.language || 'en';
    const results=await Promise.all(categories.map(category=>this.hass.connection.sendMessagePromise({
      type:'frontend/get_translations', language, category,
      integration:['kiosk_satellite_manager'], config_flow:true,
    })));
    this.translations=Object.assign({},...results.map(x=>x.resources || {}));
  }
  _translate(path,fallback) {
    const category={config:'config',options:'options',subentry:'config_subentries',repair:'issues'}[this.kind];
    const prefix=category==='issues' ?
      `component.kiosk_satellite_manager.issues.${this._issueKey}.fix_flow.` :
      category==='config_subentries' ?
        'component.kiosk_satellite_manager.config_subentries.device.' :
        `component.kiosk_satellite_manager.${category}.`;
    const raw=this.translations[prefix+path] || fallback;
    return String(raw).replace(/\{([^{}]+)\}/g, (match,key)=>
      this.flow?.description_placeholders?.[key] ?? match);
  }
  _field(field) {
    const name=field.name, kind=fieldKind(field), value=this.values[name];
    if (kind === 'unsupported') return html`<div class="error">Unsupported field: ${name}</div>`;
    const opts=field.options || field.selector?.select?.options || [];
    const normalized=opts.map(o => typeof o === 'object' ? o : {value:o,label:String(o)});
    const step=this.flow.step_id || 'init';
    const description=this._translate(`step.${step}.data_description.${name}`,'');
    return html`<div class="field"><label for=${name}>${this._translate(`step.${step}.data.${name}`,name)}${field.required ? ' *' : ''}</label>
      ${description ? html`<div class="description">${description}</div>` : ''}
      ${kind === 'boolean' ? html`<input id=${name} type="checkbox" .checked=${Boolean(value)} @change=${e=>this._set(name,e.target.checked)}>` :
        kind === 'area' ? html`<select id=${name} @change=${e=>this._set(name,e.target.value)}>
          <option value="">No area</option>${this.areas.map(area=>html`<option value=${area.area_id} ?selected=${value===area.area_id}>${area.name}</option>`)}
        </select>` :
        kind === 'choice' ? html`<select id=${name} @change=${e=>this._set(name,e.target.value)}>
          <option value="">Select…</option>${normalized.map(o=>html`<option value=${o.value} ?selected=${value===o.value}>${o.label || o.value}</option>`)}
        </select>${field.selector?.select?.custom_value ? html`<input type="text" placeholder="Custom value" @input=${e=>this._set(name,e.target.value)}>` : ''}` :
        html`<input id=${name} type=${kind === 'integer' ? 'number' : kind === 'password' ? 'password' : 'text'}
          min=${field.min ?? field.selector?.number?.min ?? ''} max=${field.max ?? field.selector?.number?.max ?? ''}
          .value=${kind === 'password' ? '' : value ?? ''}
          @input=${e=>this._set(name,kind === 'integer' && e.target.value !== '' ? Number(e.target.value) : e.target.value)}>`}
      ${this.flow?.errors?.[name] ? html`<span class="error">${this._translate(`error.${this.flow.errors[name]}`,this.flow.errors[name])}</span>` : ''}
    </div>`;
  }
  render() {
    const f=this.flow;
    if (!f) return html`${this.error ? html`<p class="error">${this.error}</p>` : ''}`;
    const unsupported=(f.data_schema || []).some(x=>fieldKind(x)==='unsupported');
    return html`<section aria-label="KSM flow">
      <h3>${this._translate(`step.${f.step_id}.title`,f.step_id || f.type)}</h3>
      ${this._translate(`step.${f.step_id}.description`,'') ? html`<p class="description">${this._translate(`step.${f.step_id}.description`,'')}</p>` : ''}
      ${this.error ? html`<p class="error">${this.error}</p>` : ''}
      ${f.errors?.base ? html`<p class="error">${this._translate(`error.${f.errors.base}`,f.errors.base)}</p>` : ''}
      ${f.type === 'form' ? html`${(f.data_schema || []).map(field=>this._field(field))}
        <button ?disabled=${this.busy || unsupported} @click=${()=>this.submit()}>Submit</button>` : ''}
      ${f.type === 'menu' ? html`<div class="menu">${(f.menu_options || []).map(item=>html`
        <button @click=${()=>this.submit({next_step_id:item})}>${this._translate(`step.${f.step_id}.menu_options.${item}`,item)}</button>`)}</div>` : ''}
      ${f.type === 'progress' ? html`<p role="status">In progress…</p>` : ''}
      ${f.type === 'abort' ? html`<p role="status">${this._translate(`abort.${f.reason}`,f.reason || 'Done')}</p>` : ''}
      ${f.type === 'create_entry' ? html`<p role="status">${f.title || 'Done'}</p>` : ''}
      <button @click=${()=>this.close()}>${f.type === 'abort' || f.type === 'create_entry' ? 'Done' : 'Cancel'}</button>
    </section>`;
  }
}
customElements.define('ksm-flow', KsmFlow);
