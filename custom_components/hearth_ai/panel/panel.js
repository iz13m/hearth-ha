/* Hearth panel — generated from packages/ha-panel. Do not edit: run `pnpm --filter @hearth/ha-panel build`. */
var L=globalThis,U=L.ShadowRoot&&(L.ShadyCSS===void 0||L.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,W=Symbol(),re=new WeakMap,P=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==W)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(U&&t===void 0){let i=e!==void 0&&e.length===1;i&&(t=re.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&re.set(e,t))}return t}toString(){return this.cssText}},oe=o=>new P(typeof o=="string"?o:o+"",void 0,W),j=(o,...t)=>{let e=o.length===1?o[0]:t.reduce((i,s,r)=>i+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+o[r+1],o[0]);return new P(e,o,W)},ae=(o,t)=>{if(U)o.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let i=document.createElement("style"),s=L.litNonce;s!==void 0&&i.setAttribute("nonce",s),i.textContent=e.cssText,o.appendChild(i)}},V=U?o=>o:o=>o instanceof CSSStyleSheet?(t=>{let e="";for(let i of t.cssRules)e+=i.cssText;return oe(e)})(o):o;var{is:Ce,defineProperty:Re,getOwnPropertyDescriptor:Me,getOwnPropertyNames:He,getOwnPropertySymbols:Oe,getPrototypeOf:Ne}=Object,z=globalThis,le=z.trustedTypes,Le=le?le.emptyScript:"",Ue=z.reactiveElementPolyfillSupport,T=(o,t)=>o,J={toAttribute(o,t){switch(t){case Boolean:o=o?Le:null;break;case Object:case Array:o=o==null?o:JSON.stringify(o)}return o},fromAttribute(o,t){let e=o;switch(t){case Boolean:e=o!==null;break;case Number:e=o===null?null:Number(o);break;case Object:case Array:try{e=JSON.parse(o)}catch{e=null}}return e}},ce=(o,t)=>!Ce(o,t),de={attribute:!0,type:String,converter:J,reflect:!1,useDefault:!1,hasChanged:ce};Symbol.metadata??=Symbol("metadata"),z.litPropertyMetadata??=new WeakMap;var m=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=de){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let i=Symbol(),s=this.getPropertyDescriptor(t,i,e);s!==void 0&&Re(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){let{get:s,set:r}=Me(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:s,set(n){let a=s?.call(this);r?.call(this,n),this.requestUpdate(t,a,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??de}static _$Ei(){if(this.hasOwnProperty(T("elementProperties")))return;let t=Ne(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(T("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(T("properties"))){let e=this.properties,i=[...He(e),...Oe(e)];for(let s of i)this.createProperty(s,e[s])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[i,s]of e)this.elementProperties.set(i,s)}this._$Eh=new Map;for(let[e,i]of this.elementProperties){let s=this._$Eu(e,i);s!==void 0&&this._$Eh.set(s,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let i=new Set(t.flat(1/0).reverse());for(let s of i)e.unshift(V(s))}else t!==void 0&&e.push(V(t));return e}static _$Eu(t,e){let i=e.attribute;return i===!1?void 0:typeof i=="string"?i:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return ae(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){let i=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,i);if(s!==void 0&&i.reflect===!0){let r=(i.converter?.toAttribute!==void 0?i.converter:J).toAttribute(e,i.type);this._$Em=t,r==null?this.removeAttribute(s):this.setAttribute(s,r),this._$Em=null}}_$AK(t,e){let i=this.constructor,s=i._$Eh.get(t);if(s!==void 0&&this._$Em!==s){let r=i.getPropertyOptions(s),n=typeof r.converter=="function"?{fromAttribute:r.converter}:r.converter?.fromAttribute!==void 0?r.converter:J;this._$Em=s;let a=n.fromAttribute(e,r.type);this[s]=a??this._$Ej?.get(s)??a,this._$Em=null}}requestUpdate(t,e,i,s=!1,r){if(t!==void 0){let n=this.constructor;if(s===!1&&(r=this[t]),i??=n.getPropertyOptions(t),!((i.hasChanged??ce)(r,e)||i.useDefault&&i.reflect&&r===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,i))))return;this.C(t,e,i)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:s,wrapped:r},n){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),r!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),s===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[s,r]of this._$Ep)this[s]=r;this._$Ep=void 0}let i=this.constructor.elementProperties;if(i.size>0)for(let[s,r]of i){let{wrapped:n}=r,a=this[s];n!==!0||this._$AL.has(s)||a===void 0||this.C(s,void 0,r,a)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(i=>i.hostUpdate?.()),this.update(e)):this._$EM()}catch(i){throw t=!1,this._$EM(),i}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};m.elementStyles=[],m.shadowRootOptions={mode:"open"},m[T("elementProperties")]=new Map,m[T("finalized")]=new Map,Ue?.({ReactiveElement:m}),(z.reactiveElementVersions??=[]).push("2.1.2");var X=globalThis,he=o=>o,D=X.trustedTypes,pe=D?D.createPolicy("lit-html",{createHTML:o=>o}):void 0,ve="$lit$",g=`lit$${Math.random().toFixed(9).slice(2)}$`,ye="?"+g,ze=`<${ye}>`,x=document,C=()=>x.createComment(""),R=o=>o===null||typeof o!="object"&&typeof o!="function",ee=Array.isArray,De=o=>ee(o)||typeof o?.[Symbol.iterator]=="function",F=`[ 	
\f\r]`,k=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,ue=/-->/g,_e=/>/g,$=RegExp(`>|${F}(?:([^\\s"'>=/]+)(${F}*=${F}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),me=/'/g,fe=/"/g,$e=/^(?:script|style|textarea|title)$/i,te=o=>(t,...e)=>({_$litType$:o,strings:t,values:e}),c=te(1),Ye=te(2),Ze=te(3),w=Symbol.for("lit-noChange"),d=Symbol.for("lit-nothing"),ge=new WeakMap,b=x.createTreeWalker(x,129);function be(o,t){if(!ee(o)||!o.hasOwnProperty("raw"))throw Error("invalid template strings array");return pe!==void 0?pe.createHTML(t):t}var Ie=(o,t)=>{let e=o.length-1,i=[],s,r=t===2?"<svg>":t===3?"<math>":"",n=k;for(let a=0;a<e;a++){let l=o[a],p,u,h=-1,_=0;for(;_<l.length&&(n.lastIndex=_,u=n.exec(l),u!==null);)_=n.lastIndex,n===k?u[1]==="!--"?n=ue:u[1]!==void 0?n=_e:u[2]!==void 0?($e.test(u[2])&&(s=RegExp("</"+u[2],"g")),n=$):u[3]!==void 0&&(n=$):n===$?u[0]===">"?(n=s??k,h=-1):u[1]===void 0?h=-2:(h=n.lastIndex-u[2].length,p=u[1],n=u[3]===void 0?$:u[3]==='"'?fe:me):n===fe||n===me?n=$:n===ue||n===_e?n=k:(n=$,s=void 0);let f=n===$&&o[a+1].startsWith("/>")?" ":"";r+=n===k?l+ze:h>=0?(i.push(p),l.slice(0,h)+ve+l.slice(h)+g+f):l+g+(h===-2?a:f)}return[be(o,r+(o[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),i]},M=class o{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let r=0,n=0,a=t.length-1,l=this.parts,[p,u]=Ie(t,e);if(this.el=o.createElement(p,i),b.currentNode=this.el.content,e===2||e===3){let h=this.el.content.firstChild;h.replaceWith(...h.childNodes)}for(;(s=b.nextNode())!==null&&l.length<a;){if(s.nodeType===1){if(s.hasAttributes())for(let h of s.getAttributeNames())if(h.endsWith(ve)){let _=u[n++],f=s.getAttribute(h).split(g),N=/([.?@])?(.*)/.exec(_);l.push({type:1,index:r,name:N[2],strings:f,ctor:N[1]==="."?G:N[1]==="?"?Y:N[1]==="@"?Z:A}),s.removeAttribute(h)}else h.startsWith(g)&&(l.push({type:6,index:r}),s.removeAttribute(h));if($e.test(s.tagName)){let h=s.textContent.split(g),_=h.length-1;if(_>0){s.textContent=D?D.emptyScript:"";for(let f=0;f<_;f++)s.append(h[f],C()),b.nextNode(),l.push({type:2,index:++r});s.append(h[_],C())}}}else if(s.nodeType===8)if(s.data===ye)l.push({type:2,index:r});else{let h=-1;for(;(h=s.data.indexOf(g,h+1))!==-1;)l.push({type:7,index:r}),h+=g.length-1}r++}}static createElement(t,e){let i=x.createElement("template");return i.innerHTML=t,i}};function E(o,t,e=o,i){if(t===w)return t;let s=i!==void 0?e._$Co?.[i]:e._$Cl,r=R(t)?void 0:t._$litDirective$;return s?.constructor!==r&&(s?._$AO?.(!1),r===void 0?s=void 0:(s=new r(o),s._$AT(o,e,i)),i!==void 0?(e._$Co??=[])[i]=s:e._$Cl=s),s!==void 0&&(t=E(o,s._$AS(o,t.values),s,i)),t}var K=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:i}=this._$AD,s=(t?.creationScope??x).importNode(e,!0);b.currentNode=s;let r=b.nextNode(),n=0,a=0,l=i[0];for(;l!==void 0;){if(n===l.index){let p;l.type===2?p=new H(r,r.nextSibling,this,t):l.type===1?p=new l.ctor(r,l.name,l.strings,this,t):l.type===6&&(p=new Q(r,this,t)),this._$AV.push(p),l=i[++a]}n!==l?.index&&(r=b.nextNode(),n++)}return b.currentNode=x,s}p(t){let e=0;for(let i of this._$AV)i!==void 0&&(i.strings!==void 0?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}},H=class o{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,s){this.type=2,this._$AH=d,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=E(this,t,e),R(t)?t===d||t==null||t===""?(this._$AH!==d&&this._$AR(),this._$AH=d):t!==this._$AH&&t!==w&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):De(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==d&&R(this._$AH)?this._$AA.nextSibling.data=t:this.T(x.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:i}=t,s=typeof i=="number"?this._$AC(t):(i.el===void 0&&(i.el=M.createElement(be(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===s)this._$AH.p(e);else{let r=new K(s,this),n=r.u(this.options);r.p(e),this.T(n),this._$AH=r}}_$AC(t){let e=ge.get(t.strings);return e===void 0&&ge.set(t.strings,e=new M(t)),e}k(t){ee(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,i,s=0;for(let r of t)s===e.length?e.push(i=new o(this.O(C()),this.O(C()),this,this.options)):i=e[s],i._$AI(r),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let i=he(t).nextSibling;he(t).remove(),t=i}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},A=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,s,r){this.type=1,this._$AH=d,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=r,i.length>2||i[0]!==""||i[1]!==""?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=d}_$AI(t,e=this,i,s){let r=this.strings,n=!1;if(r===void 0)t=E(this,t,e,0),n=!R(t)||t!==this._$AH&&t!==w,n&&(this._$AH=t);else{let a=t,l,p;for(t=r[0],l=0;l<r.length-1;l++)p=E(this,a[i+l],e,l),p===w&&(p=this._$AH[l]),n||=!R(p)||p!==this._$AH[l],p===d?t=d:t!==d&&(t+=(p??"")+r[l+1]),this._$AH[l]=p}n&&!s&&this.j(t)}j(t){t===d?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},G=class extends A{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===d?void 0:t}},Y=class extends A{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==d)}},Z=class extends A{constructor(t,e,i,s,r){super(t,e,i,s,r),this.type=5}_$AI(t,e=this){if((t=E(this,t,e,0)??d)===w)return;let i=this._$AH,s=t===d&&i!==d||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,r=t!==d&&(i===d||s);s&&this.element.removeEventListener(this.name,this,i),r&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},Q=class{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){E(this,t)}};var qe=X.litHtmlPolyfillSupport;qe?.(M,H),(X.litHtmlVersions??=[]).push("3.3.3");var xe=(o,t,e)=>{let i=e?.renderBefore??t,s=i._$litPart$;if(s===void 0){let r=e?.renderBefore??null;i._$litPart$=s=new H(t.insertBefore(C(),r),r,void 0,e??{})}return s._$AI(o),s};var ie=globalThis,v=class extends m{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=xe(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return w}};v._$litElement$=!0,v.finalized=!0,ie.litElementHydrateSupport?.({LitElement:v});var Be=ie.litElementPolyfillSupport;Be?.({LitElement:v});(ie.litElementVersions??=[]).push("4.2.2");var we=j`
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
`;var We=["hide","read_only","diagnostic","setting","tile"];function I(o,t){let e=t.tiles.find(n=>n.members.some(a=>a.entity_id===o.entity_id))??null,i=e?.members.find(n=>n.entity_id===o.entity_id)?.slot??null,s=t.entities.find(n=>n.entity_id===o.entity_id);if(!o.exposed)return{placement:"unshared",source:"default",tile:e,slot:i};if(s?.placement)return{placement:s.placement,source:"profile",tile:e,slot:i};let r=We.find(n=>o.labels.includes(n));return r?{placement:r,source:"label",tile:e,slot:i}:o.entity_category==="config"?{placement:"setting",source:"category",tile:e,slot:i}:o.entity_category==="diagnostic"?{placement:"diagnostic",source:"category",tile:e,slot:i}:{placement:"tile",source:"default",tile:e,slot:i}}var je={tile:"Its own tile",read_only:"Shown, not changed",setting:"A device setting",diagnostic:"A device reading",hide:"Hidden from the app",unshared:"Not shared with Hearth"},Ve={profile:"set here",label:"from a Hearth label",category:"from Home Assistant",default:"by default"},q=o=>je[o],Ee=o=>Ve[o];function S(o){return o.name?o.name:o.entity_id.slice(o.entity_id.indexOf(".")+1).replace(/_/g," ").replace(/\b\w/g,e=>e.toUpperCase())}function Ae(o){let t=[];return o.members.some(e=>e.entity_id===o.primary)||t.push("Pick which device this tile is."),o.members.length||t.push("A tile needs at least one device in it."),t}var Se=["main","reading","setting"],Pe=["tile","read_only","setting","diagnostic","hide"],y=["name","icon","room","order","hide"];var B={version:1,entities:[],tiles:[]},Te={name:"Rename",icon:"Icon",room:"Move room",order:"Reorder",hide:"Hide"},ke={main:"Control",reading:"Reading",setting:"Setting"},O=class extends v{constructor(){super(...arguments);this._state=null;this._draft=B;this._tab="devices";this._query="";this._onlyProblems=!1;this._open=null;this._busy=!1;this._error=null;this._notice=null}connectedCallback(){super.connectedCallback(),this._load()}async _load(){this._busy=!0;try{let e=await this.hass.callWS({type:"hearth_ai/panel/state"});this._state=e,this._draft=se(e.profile??B),this._error=null}catch(e){this._error=ne(e)}finally{this._busy=!1}}get _dirty(){return JSON.stringify(this._draft)!==JSON.stringify(this._state?.profile??B)}async _save(){this._busy=!0,this._notice=null;try{let e=await this.hass.callWS({type:"hearth_ai/panel/save",profile:this._draft});this._state=this._state?{...this._state,profile:e.profile}:this._state,this._draft=se(e.profile),this._error=null,this._notice=e.warnings.length?e.warnings.join(" "):"Saved. The app will follow within a few seconds."}catch(e){this._error=ne(e)}finally{this._busy=!1}}async _expose(e,i){this._busy=!0;try{let s=await this.hass.callWS({type:"hearth_ai/panel/expose",entity_ids:[e],expose:i});s.refused.length?this._error=s.refused[0].reason:this._state&&(this._error=null,this._state={...this._state,entities:this._state.entities.map(r=>r.entity_id===e?{...r,exposed:i}:r)})}catch(s){this._error=ne(s)}finally{this._busy=!1}}render(){return c`
      <header>
        <h1>Hearth</h1>
        ${this._busy?c`<span class="pill">Working…</span>`:d}
      </header>
      <nav>
        <button aria-selected=${this._tab==="devices"} @click=${()=>this._go("devices")}>Devices</button>
        <button aria-selected=${this._tab==="tiles"} @click=${()=>this._go("tiles")}>Tiles</button>
      </nav>
      <main>
        ${this._error?c`<div class="banner error">${this._error}</div>`:d}
        ${this._notice?c`<div class="banner ok">${this._notice}</div>`:d}
        ${this._state?this._tab==="devices"?this._devices():this._tiles():c`<div class="empty">Reading your home…</div>`}
      </main>
      ${this._dirty?this._saveBar():d}
    `}_go(e){this._tab=e,this._open=null}_saveBar(){return c`
      <div class="save-bar">
        <span class="grow sub">Unsaved changes. This is how the app shows your home to everyone in it.</span>
        <button class="action quiet" ?disabled=${this._busy} @click=${()=>this._draft=se(this._state?.profile??B)}>Discard</button>
        <button class="action" ?disabled=${this._busy} @click=${()=>void this._save()}>Save</button>
      </div>
    `}_devices(){let e=this._state,i=new Map(e.areas.map(a=>[a.area_id,a.name])),s=this._query.trim().toLowerCase(),r=e.entities.filter(a=>{if(s&&!`${a.entity_id} ${S(a)} ${a.device_name??""}`.toLowerCase().includes(s))return!1;if(this._onlyProblems){let l=I(a,this._draft);return l.placement==="unshared"||!l.tile&&l.source==="default"&&a.entity_category===null&&a.device_id!==null}return!0}),n=new Map;for(let a of r){let l=a.area_id??"";n.set(l,[...n.get(l)??[],a])}return c`
      <div class="toolbar">
        <input type="search" placeholder="Search devices" .value=${this._query} @input=${a=>this._query=a.target.value} />
        <label class="check">
          <input type="checkbox" .checked=${this._onlyProblems} @change=${a=>this._onlyProblems=a.target.checked} />
          Only what needs attention
        </label>
      </div>
      ${r.length===0?c`<div class="card"><div class="empty">Nothing matches.</div></div>`:d}
      ${[...n.entries()].sort(([a],[l])=>(i.get(a)??"zzz").localeCompare(i.get(l)??"zzz")).map(([a,l])=>c`
            <div class="card">
              <h2>${i.get(a)??"No room"}</h2>
              ${l.map(p=>this._deviceRow(p))}
            </div>
          `)}
    `}_deviceRow(e){let i=I(e,this._draft),s=this._open===e.entity_id;return c`
      <div class="row">
        <div class="grow">
          <div class="name">${S(e)}</div>
          <div class="sub">${e.entity_id}${e.device_name?` \xB7 ${e.device_name}`:""}</div>
        </div>
        ${i.tile?c`<span class="pill">In “${i.tile.name??"a tile"}”</span>`:d}
        <span class="pill ${i.placement==="unshared"?"warn":""}">${q(i.placement)}</span>
        <button class="action quiet" @click=${()=>this._open=s?null:e.entity_id}>${s?"Close":"Change"}</button>
      </div>
      ${s?this._deviceEditor(e):d}
    `}_deviceEditor(e){let i=I(e,this._draft),s=this._draft.entities.find(n=>n.entity_id===e.entity_id),r=s?.editable??y;return c`
      <div class="row" style="align-items:flex-start;flex-direction:column;gap:12px">
        <label class="check">
          <input type="checkbox" .checked=${e.exposed} ?disabled=${this._busy} @change=${n=>void this._expose(e.entity_id,n.target.checked)} />
          Share with Hearth
        </label>
        ${e.exposed?d:c`<div class="sub">Hearth only ever sees what this home shares with Assist. Nothing below applies until it is shared.</div>`}
        <div class="toolbar" style="margin:0">
          <span class="sub">Show it as</span>
          <select
            .value=${s?.placement??""}
            @change=${n=>this._setPlacement(e.entity_id,n.target.value)}
          >
            <option value="">${`Leave as it is (${q(i.placement)}, ${Ee(i.source)})`}</option>
            ${Pe.map(n=>c`<option value=${n} ?selected=${s?.placement===n}>${q(n)}</option>`)}
          </select>
        </div>
        ${i.tile?c`<div class="sub">Part of the tile “${i.tile.name??i.tile.id}”, as its ${ke[i.slot??"main"].toLowerCase()}. Edit that on the Tiles page.</div>`:c`
              <button class="action quiet" @click=${()=>this._newTileFrom(e)}>Make this a tile of its own devices…</button>
            `}
        <div>
          <div class="sub" style="margin-bottom:6px">The app may still change</div>
          <div class="toolbar" style="margin:0">
            ${y.map(n=>c`
                <label class="check">
                  <input type="checkbox" .checked=${r.includes(n)} @change=${a=>this._setEditable(e.entity_id,n,a.target.checked)} />
                  ${Te[n]}
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
        ${e.length===0?c`<div class="empty">No tiles yet. Build one from the Devices page.</div>`:d}
        ${e.map(i=>this._tileRow(i))}
      </div>
    `}_tileRow(e){let i=this._open===e.id,s=Ae(e);return c`
      <div class="row">
        <div class="grow">
          <div class="name">${e.name??e.id}</div>
          <div class="sub">${e.members.length} device${e.members.length===1?"":"s"}${e.actions?.length?` \xB7 ${e.actions.length} action${e.actions.length===1?"":"s"}`:""}</div>
        </div>
        ${s.length?c`<span class="pill warn">${s[0]}</span>`:d}
        <button class="action quiet" @click=${()=>this._open=i?null:e.id}>${i?"Close":"Edit"}</button>
        <button class="action quiet" @click=${()=>this._removeTile(e.id)}>Delete</button>
      </div>
      ${i?this._tileEditor(e):d}
    `}_tileEditor(e){let i=this._state,s=new Map(i.entities.map(n=>[n.entity_id,n])),r=e.editable??[];return c`
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
                  <div class="name">${a?S(a):n.entity_id}</div>
                  <div class="sub">${n.entity_id}${a&&!a.exposed?" \xB7 not shared, so it will not appear":""}</div>
                </div>
                <select @change=${l=>this._setSlot(e.id,n.entity_id,l.target.value)}>
                  ${Se.map(l=>c`<option value=${l} ?selected=${n.slot===l}>${ke[l]}</option>`)}
                </select>
                <button class="action quiet" @click=${()=>this._removeMember(e.id,n.entity_id)}>Remove</button>
              </div>
            `})}
          <div class="row" style="padding-left:0;padding-right:0">
            <select class="grow" @change=${n=>this._addMember(e.id,n.target.value)}>
              <option value="">Add a device…</option>
              ${i.entities.filter(n=>!this._claimed().has(n.entity_id)).map(n=>c`<option value=${n.entity_id}>${S(n)} — ${n.entity_id}</option>`)}
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
            ${y.map(n=>c`
                <label class="check">
                  <input type="checkbox" .checked=${r.includes(n)} @change=${a=>this._setTileEditable(e.id,n,a.target.checked)} />
                  ${Te[n]}
                </label>
              `)}
          </div>
          <div class="sub" style="margin-top:6px">A tile's name, icon and room are set here, so leave these off unless you want the app to override them.</div>
        </div>
      </div>
    `}_claimed(){return new Set(this._draft.tiles.flatMap(e=>e.members.map(i=>i.entity_id)))}_setPlacement(e,i){this._patchEntity(e,s=>({...s,placement:i||void 0}))}_setEditable(e,i,s){this._patchEntity(e,r=>{let n=r.editable??y,a=s?[...n,i]:n.filter(l=>l!==i);return{...r,editable:a.length===y.length?void 0:y.filter(l=>a.includes(l))}})}_patchEntity(e,i){let s=[...this._draft.entities],r=s.findIndex(l=>l.entity_id===e),n=i(r>=0?s[r]:{entity_id:e}),a=n.placement===void 0&&n.editable===void 0;r>=0?a?s.splice(r,1):s[r]=n:a||s.push(n),this._draft={...this._draft,entities:s}}_newTileFrom(e){let i=`tile_${Math.random().toString(36).slice(2,10)}`,s={id:i,name:e.device_name??S(e),primary:e.entity_id,members:[{entity_id:e.entity_id,slot:"main"}]};for(let r of this._state.entities)r.entity_id===e.entity_id||!e.device_id||r.device_id!==e.device_id||this._claimed().has(r.entity_id)||s.members.push({entity_id:r.entity_id,slot:r.entity_category==="config"?"setting":r.entity_category==="diagnostic"?"reading":"main"});this._draft={...this._draft,tiles:[...this._draft.tiles,s]},this._tab="tiles",this._open=i}_patchTile(e,i){this._draft={...this._draft,tiles:this._draft.tiles.map(s=>s.id===e?{...s,...i}:s)}}_removeTile(e){this._draft={...this._draft,tiles:this._draft.tiles.filter(i=>i.id!==e)},this._open===e&&(this._open=null)}_addMember(e,i){if(!i)return;let s=this._draft.tiles.find(r=>r.id===e);!s||s.members.some(r=>r.entity_id===i)||this._patchTile(e,{members:[...s.members,{entity_id:i,slot:"main"}]})}_removeMember(e,i){let s=this._draft.tiles.find(a=>a.id===e);if(!s)return;let r=s.members.filter(a=>a.entity_id!==i),n=s.primary===i?r[0]?.entity_id??"":s.primary;this._patchTile(e,{members:r,primary:n})}_setSlot(e,i,s){let r=this._draft.tiles.find(n=>n.id===e);r&&this._patchTile(e,{members:r.members.map(n=>n.entity_id===i?{...n,slot:s}:n)})}_addAction(e,i){if(!i)return;let s=this._draft.tiles.find(n=>n.id===e);if(!s)return;let r=s.actions??[];r.some(n=>n.entity_id===i)||this._patchTile(e,{actions:[...r,{entity_id:i}]})}_removeAction(e,i){let s=this._draft.tiles.find(n=>n.id===e);if(!s)return;let r=(s.actions??[]).filter(n=>n.entity_id!==i);this._patchTile(e,{actions:r.length?r:void 0})}_setTileEditable(e,i,s){let r=this._draft.tiles.find(l=>l.id===e);if(!r)return;let n=r.editable??[],a=s?[...n,i]:n.filter(l=>l!==i);this._patchTile(e,{editable:y.filter(l=>a.includes(l))})}};O.styles=we,O.properties={hass:{attribute:!1},narrow:{type:Boolean},_state:{state:!0},_draft:{state:!0},_tab:{state:!0},_query:{state:!0},_onlyProblems:{state:!0},_open:{state:!0},_busy:{state:!0},_error:{state:!0},_notice:{state:!0}};function se(o){return JSON.parse(JSON.stringify(o))}function ne(o){return typeof o=="object"&&o!==null&&"message"in o&&typeof o.message=="string"?o.message:"Something went wrong. Try again."}customElements.get("hearth-ai-panel")||customElements.define("hearth-ai-panel",O);export{O as HearthPanel};
