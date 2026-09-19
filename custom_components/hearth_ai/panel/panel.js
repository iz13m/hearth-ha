/* Hearth panel — generated from packages/ha-panel. Do not edit: run `pnpm --filter @hearth/ha-panel build`. */
var U=globalThis,D=U.ShadowRoot&&(U.ShadyCSS===void 0||U.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,W=Symbol(),oe=new WeakMap,k=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==W)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(D&&t===void 0){let i=e!==void 0&&e.length===1;i&&(t=oe.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&oe.set(e,t))}return t}toString(){return this.cssText}},ae=r=>new k(typeof r=="string"?r:r+"",void 0,W),j=(r,...t)=>{let e=r.length===1?r[0]:t.reduce((i,s,o)=>i+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+r[o+1],r[0]);return new k(e,r,W)},le=(r,t)=>{if(D)r.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let i=document.createElement("style"),s=U.litNonce;s!==void 0&&i.setAttribute("nonce",s),i.textContent=e.cssText,r.appendChild(i)}},V=D?r=>r:r=>r instanceof CSSStyleSheet?(t=>{let e="";for(let i of t.cssRules)e+=i.cssText;return ae(e)})(r):r;var{is:He,defineProperty:Oe,getOwnPropertyDescriptor:Ne,getOwnPropertyNames:Le,getOwnPropertySymbols:Ue,getPrototypeOf:De}=Object,z=globalThis,de=z.trustedTypes,ze=de?de.emptyScript:"",Ie=z.reactiveElementPolyfillSupport,C=(r,t)=>r,F={toAttribute(r,t){switch(t){case Boolean:r=r?ze:null;break;case Object:case Array:r=r==null?r:JSON.stringify(r)}return r},fromAttribute(r,t){let e=r;switch(t){case Boolean:e=r!==null;break;case Number:e=r===null?null:Number(r);break;case Object:case Array:try{e=JSON.parse(r)}catch{e=null}}return e}},he=(r,t)=>!He(r,t),ce={attribute:!0,type:String,converter:F,reflect:!1,useDefault:!1,hasChanged:he};Symbol.metadata??=Symbol("metadata"),z.litPropertyMetadata??=new WeakMap;var g=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=ce){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let i=Symbol(),s=this.getPropertyDescriptor(t,i,e);s!==void 0&&Oe(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){let{get:s,set:o}=Ne(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:s,set(n){let a=s?.call(this);o?.call(this,n),this.requestUpdate(t,a,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??ce}static _$Ei(){if(this.hasOwnProperty(C("elementProperties")))return;let t=De(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(C("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(C("properties"))){let e=this.properties,i=[...Le(e),...Ue(e)];for(let s of i)this.createProperty(s,e[s])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[i,s]of e)this.elementProperties.set(i,s)}this._$Eh=new Map;for(let[e,i]of this.elementProperties){let s=this._$Eu(e,i);s!==void 0&&this._$Eh.set(s,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let i=new Set(t.flat(1/0).reverse());for(let s of i)e.unshift(V(s))}else t!==void 0&&e.push(V(t));return e}static _$Eu(t,e){let i=e.attribute;return i===!1?void 0:typeof i=="string"?i:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return le(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){let i=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,i);if(s!==void 0&&i.reflect===!0){let o=(i.converter?.toAttribute!==void 0?i.converter:F).toAttribute(e,i.type);this._$Em=t,o==null?this.removeAttribute(s):this.setAttribute(s,o),this._$Em=null}}_$AK(t,e){let i=this.constructor,s=i._$Eh.get(t);if(s!==void 0&&this._$Em!==s){let o=i.getPropertyOptions(s),n=typeof o.converter=="function"?{fromAttribute:o.converter}:o.converter?.fromAttribute!==void 0?o.converter:F;this._$Em=s;let a=n.fromAttribute(e,o.type);this[s]=a??this._$Ej?.get(s)??a,this._$Em=null}}requestUpdate(t,e,i,s=!1,o){if(t!==void 0){let n=this.constructor;if(s===!1&&(o=this[t]),i??=n.getPropertyOptions(t),!((i.hasChanged??he)(o,e)||i.useDefault&&i.reflect&&o===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,i))))return;this.C(t,e,i)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:s,wrapped:o},n){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),o!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),s===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[s,o]of this._$Ep)this[s]=o;this._$Ep=void 0}let i=this.constructor.elementProperties;if(i.size>0)for(let[s,o]of i){let{wrapped:n}=o,a=this[s];n!==!0||this._$AL.has(s)||a===void 0||this.C(s,void 0,o,a)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(i=>i.hostUpdate?.()),this.update(e)):this._$EM()}catch(i){throw t=!1,this._$EM(),i}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};g.elementStyles=[],g.shadowRootOptions={mode:"open"},g[C("elementProperties")]=new Map,g[C("finalized")]=new Map,Ie?.({ReactiveElement:g}),(z.reactiveElementVersions??=[]).push("2.1.2");var X=globalThis,pe=r=>r,I=X.trustedTypes,ue=I?I.createPolicy("lit-html",{createHTML:r=>r}):void 0,ye="$lit$",v=`lit$${Math.random().toFixed(9).slice(2)}$`,be="?"+v,qe=`<${be}>`,E=document,M=()=>E.createComment(""),H=r=>r===null||typeof r!="object"&&typeof r!="function",ee=Array.isArray,Be=r=>ee(r)||typeof r?.[Symbol.iterator]=="function",J=`[ 	
\f\r]`,R=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,me=/-->/g,_e=/>/g,x=RegExp(`>|${J}(?:([^\\s"'>=/]+)(${J}*=${J}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),fe=/'/g,ge=/"/g,$e=/^(?:script|style|textarea|title)$/i,te=r=>(t,...e)=>({_$litType$:r,strings:t,values:e}),c=te(1),tt=te(2),it=te(3),A=Symbol.for("lit-noChange"),h=Symbol.for("lit-nothing"),ve=new WeakMap,w=E.createTreeWalker(E,129);function xe(r,t){if(!ee(r)||!r.hasOwnProperty("raw"))throw Error("invalid template strings array");return ue!==void 0?ue.createHTML(t):t}var We=(r,t)=>{let e=r.length-1,i=[],s,o=t===2?"<svg>":t===3?"<math>":"",n=R;for(let a=0;a<e;a++){let l=r[a],d,u,p=-1,m=0;for(;m<l.length&&(n.lastIndex=m,u=n.exec(l),u!==null);)m=n.lastIndex,n===R?u[1]==="!--"?n=me:u[1]!==void 0?n=_e:u[2]!==void 0?($e.test(u[2])&&(s=RegExp("</"+u[2],"g")),n=x):u[3]!==void 0&&(n=x):n===x?u[0]===">"?(n=s??R,p=-1):u[1]===void 0?p=-2:(p=n.lastIndex-u[2].length,d=u[1],n=u[3]===void 0?x:u[3]==='"'?ge:fe):n===ge||n===fe?n=x:n===me||n===_e?n=R:(n=x,s=void 0);let f=n===x&&r[a+1].startsWith("/>")?" ":"";o+=n===R?l+qe:p>=0?(i.push(d),l.slice(0,p)+ye+l.slice(p)+v+f):l+v+(p===-2?a:f)}return[xe(r,o+(r[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),i]},O=class r{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let o=0,n=0,a=t.length-1,l=this.parts,[d,u]=We(t,e);if(this.el=r.createElement(d,i),w.currentNode=this.el.content,e===2||e===3){let p=this.el.content.firstChild;p.replaceWith(...p.childNodes)}for(;(s=w.nextNode())!==null&&l.length<a;){if(s.nodeType===1){if(s.hasAttributes())for(let p of s.getAttributeNames())if(p.endsWith(ye)){let m=u[n++],f=s.getAttribute(p).split(v),_=/([.?@])?(.*)/.exec(m);l.push({type:1,index:o,name:_[2],strings:f,ctor:_[1]==="."?G:_[1]==="?"?Y:_[1]==="@"?Z:P}),s.removeAttribute(p)}else p.startsWith(v)&&(l.push({type:6,index:o}),s.removeAttribute(p));if($e.test(s.tagName)){let p=s.textContent.split(v),m=p.length-1;if(m>0){s.textContent=I?I.emptyScript:"";for(let f=0;f<m;f++)s.append(p[f],M()),w.nextNode(),l.push({type:2,index:++o});s.append(p[m],M())}}}else if(s.nodeType===8)if(s.data===be)l.push({type:2,index:o});else{let p=-1;for(;(p=s.data.indexOf(v,p+1))!==-1;)l.push({type:7,index:o}),p+=v.length-1}o++}}static createElement(t,e){let i=E.createElement("template");return i.innerHTML=t,i}};function S(r,t,e=r,i){if(t===A)return t;let s=i!==void 0?e._$Co?.[i]:e._$Cl,o=H(t)?void 0:t._$litDirective$;return s?.constructor!==o&&(s?._$AO?.(!1),o===void 0?s=void 0:(s=new o(r),s._$AT(r,e,i)),i!==void 0?(e._$Co??=[])[i]=s:e._$Cl=s),s!==void 0&&(t=S(r,s._$AS(r,t.values),s,i)),t}var K=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:i}=this._$AD,s=(t?.creationScope??E).importNode(e,!0);w.currentNode=s;let o=w.nextNode(),n=0,a=0,l=i[0];for(;l!==void 0;){if(n===l.index){let d;l.type===2?d=new N(o,o.nextSibling,this,t):l.type===1?d=new l.ctor(o,l.name,l.strings,this,t):l.type===6&&(d=new Q(o,this,t)),this._$AV.push(d),l=i[++a]}n!==l?.index&&(o=w.nextNode(),n++)}return w.currentNode=E,s}p(t){let e=0;for(let i of this._$AV)i!==void 0&&(i.strings!==void 0?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}},N=class r{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,s){this.type=2,this._$AH=h,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=S(this,t,e),H(t)?t===h||t==null||t===""?(this._$AH!==h&&this._$AR(),this._$AH=h):t!==this._$AH&&t!==A&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):Be(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==h&&H(this._$AH)?this._$AA.nextSibling.data=t:this.T(E.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:i}=t,s=typeof i=="number"?this._$AC(t):(i.el===void 0&&(i.el=O.createElement(xe(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===s)this._$AH.p(e);else{let o=new K(s,this),n=o.u(this.options);o.p(e),this.T(n),this._$AH=o}}_$AC(t){let e=ve.get(t.strings);return e===void 0&&ve.set(t.strings,e=new O(t)),e}k(t){ee(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,i,s=0;for(let o of t)s===e.length?e.push(i=new r(this.O(M()),this.O(M()),this,this.options)):i=e[s],i._$AI(o),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let i=pe(t).nextSibling;pe(t).remove(),t=i}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},P=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,s,o){this.type=1,this._$AH=h,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=o,i.length>2||i[0]!==""||i[1]!==""?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=h}_$AI(t,e=this,i,s){let o=this.strings,n=!1;if(o===void 0)t=S(this,t,e,0),n=!H(t)||t!==this._$AH&&t!==A,n&&(this._$AH=t);else{let a=t,l,d;for(t=o[0],l=0;l<o.length-1;l++)d=S(this,a[i+l],e,l),d===A&&(d=this._$AH[l]),n||=!H(d)||d!==this._$AH[l],d===h?t=h:t!==h&&(t+=(d??"")+o[l+1]),this._$AH[l]=d}n&&!s&&this.j(t)}j(t){t===h?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},G=class extends P{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===h?void 0:t}},Y=class extends P{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==h)}},Z=class extends P{constructor(t,e,i,s,o){super(t,e,i,s,o),this.type=5}_$AI(t,e=this){if((t=S(this,t,e,0)??h)===A)return;let i=this._$AH,s=t===h&&i!==h||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,o=t!==h&&(i===h||s);s&&this.element.removeEventListener(this.name,this,i),o&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},Q=class{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){S(this,t)}};var je=X.litHtmlPolyfillSupport;je?.(O,N),(X.litHtmlVersions??=[]).push("3.3.3");var we=(r,t,e)=>{let i=e?.renderBefore??t,s=i._$litPart$;if(s===void 0){let o=e?.renderBefore??null;i._$litPart$=s=new N(t.insertBefore(M(),o),o,void 0,e??{})}return s._$AI(r),s};var ie=globalThis,y=class extends g{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=we(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return A}};y._$litElement$=!0,y.finalized=!0,ie.litElementHydrateSupport?.({LitElement:y});var Ve=ie.litElementPolyfillSupport;Ve?.({LitElement:y});(ie.litElementVersions??=[]).push("4.2.2");var Ee=j`
  :host {
    display: block;
    height: 100%;
    overflow: auto;
    background: var(--primary-background-color, #f5f5f5);
    color: var(--primary-text-color, #212121);
    font-family: var(--paper-font-body1_-_font-family, Roboto, system-ui, sans-serif);
    -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 16px;
    background: var(--app-header-background-color, var(--primary-color, #03a9f4));
    color: var(--app-header-text-color, #fff);
  }
  header h1 {
    font-size: 20px;
    font-weight: 400;
    margin: 0;
    flex: 1;
  }
  nav {
    display: flex;
    gap: 4px;
    padding: 0 16px;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    position: sticky;
    top: 52px;
    z-index: 1;
  }
  nav button {
    appearance: none;
    border: none;
    background: none;
    color: inherit;
    font: inherit;
    padding: 14px 16px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    opacity: 0.7;
  }
  nav button[aria-selected="true"] {
    border-bottom-color: var(--primary-color, #03a9f4);
    opacity: 1;
    font-weight: 500;
  }
  main {
    padding: 16px;
    max-width: 920px;
    margin: 0 auto;
  }
  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0, 0, 0, 0.08));
    margin-bottom: 16px;
    overflow: hidden;
  }
  .card h2 {
    font-size: 15px;
    font-weight: 500;
    margin: 0;
    padding: 16px 16px 8px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .row:first-of-type {
    border-top: none;
  }
  .grow {
    flex: 1;
    min-width: 0;
  }
  .name {
    font-size: 14px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sub {
    font-size: 12px;
    color: var(--secondary-text-color, #727272);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pill {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--secondary-background-color, #e8e8e8);
    color: var(--secondary-text-color, #727272);
    white-space: nowrap;
  }
  .pill.warn {
    background: var(--warning-color, #ff9800);
    color: #fff;
  }
  .toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  input[type="text"],
  input[type="search"],
  select {
    font: inherit;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: inherit;
    min-width: 0;
  }
  input[type="search"] {
    flex: 1;
    min-width: 180px;
  }
  button.action {
    font: inherit;
    padding: 8px 14px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  button.action.quiet {
    background: var(--secondary-background-color, #e8e8e8);
    color: var(--primary-text-color, #212121);
  }
  button.action[disabled] {
    opacity: 0.5;
    cursor: default;
  }
  label.check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    white-space: nowrap;
  }
  .empty {
    padding: 32px 16px;
    text-align: center;
    color: var(--secondary-text-color, #727272);
    font-size: 14px;
  }
  .note {
    font-size: 13px;
    line-height: 1.5;
    color: var(--secondary-text-color, #727272);
    padding: 0 16px 16px;
  }
  .banner {
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 16px;
    font-size: 14px;
  }
  .banner.error {
    background: var(--error-color, #db4437);
    color: #fff;
  }
  .banner.ok {
    background: var(--success-color, #43a047);
    color: #fff;
  }
  .banner.warn {
    background: var(--warning-color, #ff9800);
    color: #fff;
  }
  /*
   * A whole-row disclosure. The click target is the header itself, not a small trailing button —
   * a room or a device is opened by tapping it, which is what every list in Home Assistant does.
   */
  .disclosure {
    appearance: none;
    border: none;
    background: none;
    color: inherit;
    font: inherit;
    text-align: start;
    width: 100%;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 16px;
    cursor: pointer;
  }
  .disclosure .name {
    display: block;
    font-weight: 500;
  }
  .disclosure .sub {
    display: block;
  }
  .disclosure.sub-level {
    padding: 10px 16px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .chevron {
    flex: none;
    width: 8px;
    height: 8px;
    border-right: 2px solid var(--secondary-text-color, #727272);
    border-bottom: 2px solid var(--secondary-text-color, #727272);
    transform: rotate(-45deg);
    transition: transform 120ms ease;
    margin-inline-start: 2px;
  }
  .chevron.open {
    transform: rotate(45deg);
  }
  .group:first-of-type > .disclosure.sub-level {
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  /*
   * Rows inside a device group are indented and lose the divider the flat list gave them —
   * .row:first-of-type is scoped to its parent, and these now have a different one.
   */
  .group .row.indent,
  .card > .row.indent {
    padding-inline-start: 36px;
    border-top: none;
  }
  .members {
    padding-bottom: 8px;
  }
  .save-bar {
    position: sticky;
    bottom: 0;
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 12px 16px;
    background: var(--card-background-color, #fff);
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  @media (max-width: 600px) {
    main {
      padding: 12px;
    }
    .row {
      flex-wrap: wrap;
    }
  }
`;var Fe=["hide","read_only","diagnostic","setting","tile"],Ae=new Set(["number","select"]);function Je(r,t){return r.entity_category||!r.device_id||!Ae.has(r.domain)?!1:t.some(e=>e.device_id===r.device_id&&!e.entity_category&&!Ae.has(e.domain))}function T(r,t,e=[]){let i=t.tiles.find(a=>a.members.some(l=>l.entity_id===r.entity_id))??null,s=i?.members.find(a=>a.entity_id===r.entity_id)?.slot??null,o=t.entities.find(a=>a.entity_id===r.entity_id);if(!r.exposed)return{placement:"unshared",source:"default",tile:i,slot:s};if(o?.placement)return{placement:o.placement,source:"profile",tile:i,slot:s};let n=Fe.find(a=>r.labels.includes(a));return n?{placement:n,source:"label",tile:i,slot:s}:r.entity_category==="config"?{placement:"setting",source:"category",tile:i,slot:s}:r.entity_category==="diagnostic"?{placement:"diagnostic",source:"category",tile:i,slot:s}:Je(r,e)?{placement:"setting",source:"guessed",tile:i,slot:s}:{placement:"tile",source:"default",tile:i,slot:s}}var Ke={tile:"Its own tile",control:"A control, inside its device",read_only:"Shown, not changed",setting:"A device setting",diagnostic:"A device reading",hide:"Hidden from the app",unshared:"Not shared with Hearth"},Ge={profile:"set here",label:"from a Hearth label",category:"from Home Assistant",guessed:"guessed by Hearth",default:"by default"},q=r=>Ke[r],Se=r=>Ge[r];function b(r){return r.name?r.name:r.entity_id.slice(r.entity_id.indexOf(".")+1).replace(/_/g," ").replace(/\b\w/g,e=>e.toUpperCase())}function Pe(r){let t=[];return r.members.some(e=>e.entity_id===r.primary)||t.push("Pick which device this tile is."),r.members.length||t.push("A tile needs at least one device in it."),t}var Te=["main","reading","setting"],ke=["tile","control","read_only","setting","diagnostic","hide"],$=["name","icon","room","order","hide"];var B={version:1,entities:[],tiles:[]},Ce={name:"Rename",icon:"Icon",room:"Move room",order:"Reorder",hide:"Hide"},Re={main:"Control",reading:"Reading",setting:"Setting"},L=class extends y{constructor(){super(...arguments);this._state=null;this._draft=B;this._tab="devices";this._query="";this._onlyProblems=!1;this._open=new Set;this._busy=!1;this._error=null;this._notice=null}connectedCallback(){super.connectedCallback(),this._load()}async _load(){this._busy=!0;try{let e=await this.hass.callWS({type:"hearth_ai/panel/state"});this._state=e,this._draft=se(e.profile??B),this._error=null}catch(e){this._error=ne(e)}finally{this._busy=!1}}get _dirty(){return JSON.stringify(this._draft)!==JSON.stringify(this._state?.profile??B)}async _save(){this._busy=!0,this._notice=null;try{let e=await this.hass.callWS({type:"hearth_ai/panel/save",profile:this._draft});this._state=this._state?{...this._state,profile:e.profile}:this._state,this._draft=se(e.profile),this._error=null,this._notice=e.warnings.length?e.warnings.join(" "):"Saved. The app will follow within a few seconds."}catch(e){this._error=ne(e)}finally{this._busy=!1}}async _expose(e,i){this._busy=!0;try{let s=await this.hass.callWS({type:"hearth_ai/panel/expose",entity_ids:[e],expose:i});s.refused.length?this._error=s.refused[0].reason:this._state&&(this._error=null,this._state={...this._state,entities:this._state.entities.map(o=>o.entity_id===e?{...o,exposed:i}:o)})}catch(s){this._error=ne(s)}finally{this._busy=!1}}render(){return c`
      <header>
        <h1>Hearth</h1>
        ${this._busy?c`<span class="pill">Working…</span>`:h}
      </header>
      <nav>
        <button aria-selected=${this._tab==="devices"} @click=${()=>this._go("devices")}>Devices</button>
        <button aria-selected=${this._tab==="tiles"} @click=${()=>this._go("tiles")}>Tiles</button>
      </nav>
      <main>
        ${this._error?c`<div class="banner error">${this._error}</div>`:h}
        ${this._notice?c`<div class="banner ok">${this._notice}</div>`:h}
        ${this._state?this._tab==="devices"?this._devices():this._tiles():c`<div class="empty">Reading your home…</div>`}
      </main>
      ${this._dirty?this._saveBar():h}
    `}_go(e){this._tab=e,this._open=new Set}_saveBar(){return c`
      <div class="save-bar">
        <span class="grow sub">Unsaved changes. This is how the app shows your home to everyone in it.</span>
        <button class="action quiet" ?disabled=${this._busy} @click=${()=>this._draft=se(this._state?.profile??B)}>Discard</button>
        <button class="action" ?disabled=${this._busy} @click=${()=>void this._save()}>Save</button>
      </div>
    `}_devices(){let e=this._state,i=new Map(e.areas.map(d=>[d.area_id,d.name])),s=this._query.trim().toLowerCase(),o=e.entities,n=o.filter(d=>s&&!`${d.entity_id} ${b(d)} ${d.device_name??""}`.toLowerCase().includes(s)?!1:this._onlyProblems?this._needsAttention(d,o):!0),a=new Map;for(let d of n){let u=d.area_id??"";a.set(u,[...a.get(u)??[],d])}let l=!!s||this._onlyProblems;return c`
      <div class="toolbar">
        <input type="search" placeholder="Search devices" .value=${this._query} @input=${d=>this._query=d.target.value} />
        <label class="check">
          <input type="checkbox" .checked=${this._onlyProblems} @change=${d=>this._onlyProblems=d.target.checked} />
          Only what needs attention
        </label>
      </div>
      ${n.length===0?c`<div class="card"><div class="empty">Nothing matches.</div></div>`:h}
      ${[...a.entries()].sort(([d],[u])=>(i.get(d)??"zzz").localeCompare(i.get(u)??"zzz")).map(([d,u])=>this._room(d,i.get(d)??"No room",u,o,l))}
    `}_needsAttention(e,i){let s=T(e,this._draft,i);return s.placement==="unshared"?!0:!s.tile&&(s.source==="default"||s.source==="guessed")&&e.device_id!==null}_room(e,i,s,o,n){let a=`room:${e}`,l=n||this._open.has(a),d=s.filter(m=>this._needsAttention(m,o)).length,u=new Map,p=[];for(let m of s)m.device_id?u.set(m.device_id,[...u.get(m.device_id)??[],m]):p.push(m);return c`
      <div class="card">
        <button class="disclosure" aria-expanded=${l} @click=${()=>this._toggle(a)}>
          <span class="chevron ${l?"open":""}"></span>
          <span class="grow">
            <span class="name">${i}</span>
            <span class="sub">${Me(s.length,"thing")}${d?` \xB7 ${d} need${d===1?"s":""} attention`:""}</span>
          </span>
        </button>
        ${l?c`
              ${[...u.entries()].sort(([,m],[,f])=>(m[0]?.device_name??"").localeCompare(f[0]?.device_name??"")).map(([m,f])=>this._device(m,f,o,n))}
              ${p.map(m=>this._deviceRow(m,o))}
            `:h}
      </div>
    `}_device(e,i,s,o){let n=`device:${e}`,a=o||this._open.has(n),l=i[0]?.device_name??"Device",d=[...i].sort((_,re)=>_.domain.localeCompare(re.domain)||b(_).localeCompare(b(re))),u=d.map(_=>T(_,this._draft,s)),p=u.find(_=>_.tile)?.tile??null,m=u.filter(_=>_.placement==="unshared").length,f=u.filter(_=>_.placement==="control"||_.slot==="main").length;return c`
      <div class="group">
        <button class="disclosure sub-level" aria-expanded=${a} @click=${()=>this._toggle(n)}>
          <span class="chevron ${a?"open":""}"></span>
          <span class="grow">
            <span class="name">${l}</span>
            <span class="sub">
              ${Me(i.length,"entity","entities")}${f?` \xB7 ${f} control${f===1?"":"s"}`:""}${m?` \xB7 ${m} not shared`:""}
            </span>
          </span>
          ${p?c`<span class="pill">Tile: ${p.name??p.id}</span>`:h}
        </button>
        ${a?c`
              ${d.map(_=>this._deviceRow(_,s))}
              ${p?h:c`<div class="row">
                    <button class="action quiet" @click=${()=>this._newTileFrom(d[0],s)}>Make these one tile…</button>
                  </div>`}
            `:h}
      </div>
    `}_toggle(e){let i=new Set(this._open);i.has(e)?i.delete(e):i.add(e),this._open=i}_deviceRow(e,i){let s=T(e,this._draft,i),o=this._open.has(e.entity_id);return c`
      <div class="row indent">
        <div class="grow">
          <div class="name">${b(e)}</div>
          <div class="sub">${e.entity_id}</div>
        </div>
        ${s.tile?c`<span class="pill">In “${s.tile.name??"a tile"}”</span>`:h}
        <span class="pill ${s.placement==="unshared"?"warn":""}">${q(s.placement)}</span>
        <button class="action quiet" @click=${()=>this._toggle(e.entity_id)}>${o?"Close":"Change"}</button>
      </div>
      ${o?this._deviceEditor(e,i):h}
    `}_deviceEditor(e,i){let s=T(e,this._draft,i),o=this._draft.entities.find(a=>a.entity_id===e.entity_id),n=o?.editable??$;return c`
      <div class="row" style="align-items:flex-start;flex-direction:column;gap:12px">
        <label class="check">
          <input type="checkbox" .checked=${e.exposed} ?disabled=${this._busy} @change=${a=>void this._expose(e.entity_id,a.target.checked)} />
          Share with Hearth
        </label>
        ${e.exposed?h:c`<div class="sub">Hearth only ever sees what this home shares with Assist. Nothing below applies until it is shared.</div>`}
        <div class="toolbar" style="margin:0">
          <span class="sub">Show it as</span>
          <select
            .value=${o?.placement??""}
            @change=${a=>this._setPlacement(e.entity_id,a.target.value)}
          >
            <option value="">${`Leave as it is (${q(s.placement)}, ${Se(s.source)})`}</option>
            ${ke.map(a=>c`<option value=${a} ?selected=${o?.placement===a}>${q(a)}</option>`)}
          </select>
        </div>
        ${s.tile?c`<div class="sub">Part of the tile “${s.tile.name??s.tile.id}”, as its ${Re[s.slot??"main"].toLowerCase()}. Edit that on the Tiles page.</div>`:c`
              <button class="action quiet" @click=${()=>this._newTileFrom(e,i)}>Make this a tile of its own devices…</button>
            `}
        <div>
          <div class="sub" style="margin-bottom:6px">The app may still change</div>
          <div class="toolbar" style="margin:0">
            ${$.map(a=>c`
                <label class="check">
                  <input type="checkbox" .checked=${n.includes(a)} @change=${l=>this._setEditable(e.entity_id,a,l.target.checked)} />
                  ${Ce[a]}
                </label>
              `)}
          </div>
        </div>
      </div>
    `}_tiles(){let e=this._draft.tiles;return c`
      <div class="card">
        <h2>Tiles you have built</h2>
        <div class="note">
          A tile is several entities the app shows as one thing — a soundbar's mute, its input format and
          the scene that powers it. Pick which one the tile <em>is</em>; the rest appear inside it.
        </div>
        ${e.length===0?c`<div class="empty">No tiles yet. Build one from the Devices page.</div>`:h}
        ${e.map(i=>this._tileRow(i))}
      </div>
    `}_tileRow(e){let i=this._open.has(e.id),s=Pe(e);return c`
      <div class="row">
        <div class="grow">
          <div class="name">${e.name??e.id}</div>
          <div class="sub">${e.members.length} device${e.members.length===1?"":"s"}${e.actions?.length?` \xB7 ${e.actions.length} action${e.actions.length===1?"":"s"}`:""}</div>
        </div>
        ${s.length?c`<span class="pill warn">${s[0]}</span>`:h}
        <button class="action quiet" @click=${()=>this._toggle(e.id)}>${i?"Close":"Edit"}</button>
        <button class="action quiet" @click=${()=>this._removeTile(e.id)}>Delete</button>
      </div>
      ${i?this._tileEditor(e):h}
    `}_tileEditor(e){let i=this._state,s=new Map(i.entities.map(n=>[n.entity_id,n])),o=e.editable??[];return c`
      <div class="row" style="flex-direction:column;align-items:stretch;gap:12px">
        <div class="toolbar" style="margin:0">
          <input type="text" placeholder="Name this tile" .value=${e.name??""} @change=${n=>this._patchTile(e.id,{name:n.target.value||void 0})} />
          <input type="text" placeholder="Icon, e.g. mdi:speaker" .value=${e.icon??""} @change=${n=>this._patchTile(e.id,{icon:n.target.value||void 0})} />
          <select @change=${n=>this._patchTile(e.id,{area_id:n.target.value||void 0})}>
            <option value="">Room: wherever its main device is</option>
            ${i.areas.map(n=>c`<option value=${n.area_id} ?selected=${e.area_id===n.area_id}>${n.name}</option>`)}
          </select>
        </div>

        <div class="members">
          <div class="sub" style="margin-bottom:6px">In this tile</div>
          ${e.members.map(n=>{let a=s.get(n.entity_id);return c`
              <div class="row" style="padding-left:0;padding-right:0">
                <label class="check">
                  <input type="radio" name=${`primary-${e.id}`} .checked=${e.primary===n.entity_id} @change=${()=>this._patchTile(e.id,{primary:n.entity_id})} />
                  This is the tile
                </label>
                <div class="grow">
                  <div class="name">${a?b(a):n.entity_id}</div>
                  <div class="sub">${n.entity_id}${a&&!a.exposed?" \xB7 not shared, so it will not appear":""}</div>
                </div>
                <select @change=${l=>this._setSlot(e.id,n.entity_id,l.target.value)}>
                  ${Te.map(l=>c`<option value=${l} ?selected=${n.slot===l}>${Re[l]}</option>`)}
                </select>
                <button class="action quiet" @click=${()=>this._removeMember(e.id,n.entity_id)}>Remove</button>
              </div>
            `})}
          <div class="row" style="padding-left:0;padding-right:0">
            <select class="grow" @change=${n=>this._addMember(e.id,n.target.value)}>
              <option value="">Add a device…</option>
              ${i.entities.filter(n=>!this._claimed().has(n.entity_id)).map(n=>c`<option value=${n.entity_id}>${b(n)} — ${n.entity_id}</option>`)}
            </select>
          </div>
        </div>

        <div>
          <div class="sub" style="margin-bottom:6px">Run buttons inside this tile</div>
          ${(e.actions??[]).map(n=>c`
              <div class="row" style="padding-left:0;padding-right:0">
                <div class="grow">
                  <div class="name">${i.routines.find(a=>a.entity_id===n.entity_id)?.name??n.entity_id}</div>
                  <div class="sub">${n.entity_id}</div>
                </div>
                <button class="action quiet" @click=${()=>this._removeAction(e.id,n.entity_id)}>Remove</button>
              </div>
            `)}
          <div class="row" style="padding-left:0;padding-right:0">
            <select class="grow" @change=${n=>this._addAction(e.id,n.target.value)}>
              <option value="">Add a scene or script…</option>
              ${i.routines.map(n=>c`<option value=${n.entity_id}>${n.name}</option>`)}
            </select>
          </div>
        </div>

        <div>
          <div class="sub" style="margin-bottom:6px">The app may still change</div>
          <div class="toolbar" style="margin:0">
            ${$.map(n=>c`
                <label class="check">
                  <input type="checkbox" .checked=${o.includes(n)} @change=${a=>this._setTileEditable(e.id,n,a.target.checked)} />
                  ${Ce[n]}
                </label>
              `)}
          </div>
          <div class="sub" style="margin-top:6px">A tile's name, icon and room are set here, so leave these off unless you want the app to override them.</div>
        </div>
      </div>
    `}_claimed(){return new Set(this._draft.tiles.flatMap(e=>e.members.map(i=>i.entity_id)))}_setPlacement(e,i){this._patchEntity(e,s=>({...s,placement:i||void 0}))}_setEditable(e,i,s){this._patchEntity(e,o=>{let n=o.editable??$,a=s?[...n,i]:n.filter(l=>l!==i);return{...o,editable:a.length===$.length?void 0:$.filter(l=>a.includes(l))}})}_patchEntity(e,i){let s=[...this._draft.entities],o=s.findIndex(l=>l.entity_id===e),n=i(o>=0?s[o]:{entity_id:e}),a=n.placement===void 0&&n.editable===void 0;o>=0?a?s.splice(o,1):s[o]=n:a||s.push(n),this._draft={...this._draft,entities:s}}_newTileFrom(e,i){let s=`tile_${Math.random().toString(36).slice(2,10)}`,o={id:s,name:e.device_name??b(e),primary:e.entity_id,members:[{entity_id:e.entity_id,slot:"main"}]};for(let n of i)n.entity_id===e.entity_id||!e.device_id||n.device_id!==e.device_id||this._claimed().has(n.entity_id)||o.members.push({entity_id:n.entity_id,slot:Ye(T(n,this._draft,i).placement)});this._draft={...this._draft,tiles:[...this._draft.tiles,o]},this._tab="tiles",this._open=new Set([s])}_patchTile(e,i){this._draft={...this._draft,tiles:this._draft.tiles.map(s=>s.id===e?{...s,...i}:s)}}_removeTile(e){this._draft={...this._draft,tiles:this._draft.tiles.filter(i=>i.id!==e)},this._open.has(e)&&this._toggle(e)}_addMember(e,i){if(!i)return;let s=this._draft.tiles.find(o=>o.id===e);!s||s.members.some(o=>o.entity_id===i)||this._patchTile(e,{members:[...s.members,{entity_id:i,slot:"main"}]})}_removeMember(e,i){let s=this._draft.tiles.find(a=>a.id===e);if(!s)return;let o=s.members.filter(a=>a.entity_id!==i),n=s.primary===i?o[0]?.entity_id??"":s.primary;this._patchTile(e,{members:o,primary:n})}_setSlot(e,i,s){let o=this._draft.tiles.find(n=>n.id===e);o&&this._patchTile(e,{members:o.members.map(n=>n.entity_id===i?{...n,slot:s}:n)})}_addAction(e,i){if(!i)return;let s=this._draft.tiles.find(n=>n.id===e);if(!s)return;let o=s.actions??[];o.some(n=>n.entity_id===i)||this._patchTile(e,{actions:[...o,{entity_id:i}]})}_removeAction(e,i){let s=this._draft.tiles.find(n=>n.id===e);if(!s)return;let o=(s.actions??[]).filter(n=>n.entity_id!==i);this._patchTile(e,{actions:o.length?o:void 0})}_setTileEditable(e,i,s){let o=this._draft.tiles.find(l=>l.id===e);if(!o)return;let n=o.editable??[],a=s?[...n,i]:n.filter(l=>l!==i);this._patchTile(e,{editable:$.filter(l=>a.includes(l))})}};L.styles=Ee,L.properties={hass:{attribute:!1},narrow:{type:Boolean},_state:{state:!0},_draft:{state:!0},_tab:{state:!0},_query:{state:!0},_onlyProblems:{state:!0},_open:{state:!0},_busy:{state:!0},_error:{state:!0},_notice:{state:!0}};function Ye(r){return r==="setting"?"setting":r==="diagnostic"||r==="read_only"?"reading":"main"}function Me(r,t,e=`${t}s`){return`${r} ${r===1?t:e}`}function se(r){return JSON.parse(JSON.stringify(r))}function ne(r){return typeof r=="object"&&r!==null&&"message"in r&&typeof r.message=="string"?r.message:"Something went wrong. Try again."}customElements.get("hearth-ai-panel")||customElements.define("hearth-ai-panel",L);export{L as HearthPanel};
