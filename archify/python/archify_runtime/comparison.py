"""Stable-ID Architecture comparison; no runtime impact is inferred."""
import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

from .common import DiagnosticError, diagnostic, esc


def fail(code, message, **evidence):
    raise DiagnosticError(message, [diagnostic(code, message, evidence=evidence,
        supported_fixes=evidence.get('supportedFixes', ['preserve authored stable identities across snapshots']))])


def js_order(value):
    return str(value).encode('utf-16-be', 'surrogatepass')


def canonical_json(value):
    """Canonical JSON with ECMAScript key order and number exponent spelling."""
    import math
    from decimal import Decimal
    if value is None:return 'null'
    if isinstance(value, bool):return 'true' if value else 'false'
    if isinstance(value, str):return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return '{' + ','.join(canonical_json(k)+':'+canonical_json(value[k]) for k in sorted(value,key=js_order)) + '}'
    if isinstance(value, list):return '[' + ','.join(canonical_json(v) for v in value) + ']'
    if isinstance(value, (int,float)):
        number=float(value)
        if not math.isfinite(number):raise ValueError('canonical JSON requires finite numbers')
        if number == 0:return '0'
        spelling=repr(number).lower()
        if 1e-6 <= abs(number) < 1e21:
            return format(Decimal(spelling),'f').rstrip('0').rstrip('.') if '.' in format(Decimal(spelling),'f') else format(Decimal(spelling),'f')
        mantissa,exponent=spelling.split('e') if 'e' in spelling else (spelling,'0')
        if mantissa.endswith('.0'):mantissa=mantissa[:-2]
        exp=int(exponent)
        return mantissa+'e'+('+' if exp>=0 else '-')+str(abs(exp))
    raise TypeError('unsupported canonical JSON value')


def repository(value):
    if not value:return None
    from .repository import parse_remote
    location=parse_remote(value['url'], authored=True)
    url=location['url'] if location else value['url']
    return {'url':url.lower() if location and location['provider']=='github' else url,
            'revision':value['revision'].lower(), **{k:value[k] for k in ('provider','link_mode')if k in value}}


def canonical_architecture(diagram):
    d=copy.deepcopy(diagram);d['meta'].pop('output',None)
    if d['meta'].get('repository'):d['meta']['repository']=repository(d['meta']['repository'])
    d['components']=sorted(d.get('components',[]),key=lambda x:js_order(x['id']))
    for node in d['components']:
        if 'sources'in node:node['sources']=sorted(node['sources'],key=lambda x:js_order(canonical_json(x)))
    d['connections']=sorted(d.get('connections',[]),key=lambda x:js_order(x.get('id','')))
    d['boundaries']=sorted(d.get('boundaries',[]),key=lambda x:js_order(x['kind']+'\x1f'+x['label']))
    for boundary in d['boundaries']:boundary['wraps']=sorted(boundary.get('wraps',[]),key=js_order)
    return d


def index(items, collection, side):
    result={};missing=[];duplicates=[]
    for n,item in enumerate(items):
        key=item.get('id')
        if not key:missing.append(f'/{collection}/{n}/id')
        elif key in result:duplicates.append(key)
        else:result[key]=item
    if missing:fail('delta/relationship-id-required'if collection=='connections'else'delta/stable-id-required',f'{side} {collection} require authored stable ids.',side=side,paths=sorted(missing))
    if duplicates:fail('delta/duplicate-stable-id',f'{side} {collection} contain duplicate ids.',side=side,collection=collection,ids=sorted(set(duplicates)))
    return result


def boundary_index(items,side):
    result={}
    for item in items:
        key=item['kind']+'\x1f'+item['label']
        if key in result:fail('delta/boundary-key-ambiguous','Boundary kind + label must be unique.',side=side,boundaries=[item['kind']+':'+item['label']])
        result[key]=item
    return result


FIELDS={'component':{'semantic':['type','label','sublabel','tag','brand','icon'],'evidence':['sources'],'geometry':['row','col','pos','size']},
        'connection':{'topology':['from','to'],'semantic':['label','variant'],'geometry':['fromSide','toSide','route','via','labelAt','labelDx','labelDy','labelSegment','width']},
        'boundary':{'scope':['wraps'],'geometry':['pad']}}


def changes(before,after,kind):
    result=[]
    def value(item,key):
        v=item.get(key)
        if key=='sources'and isinstance(v,list):return sorted(v,key=lambda x:js_order(canonical_json(x)))
        if key=='wraps'and isinstance(v,list):return sorted(v,key=js_order)
        return v
    for key in sorted(set(before)|set(after),key=js_order):
        a,b=before.get(key),after.get(key)
        classifications=[];fields=[]
        if a is None or b is None:
            status='added'if a is None else'removed'
            classifications=[{'component':'semantic','connection':'topology','boundary':'scope'}[kind]]
        else:
            for group,names in FIELDS[kind].items():
                affected=[name for name in names if canonical_json(value(a,name))!=canonical_json(value(b,name))]
                if affected:classifications.append(group);fields += ['/'+name for name in affected]
            status=('changed'if any(c in classifications for c in ['semantic','topology','scope'])else
                    'evidence-changed'if'evidence'in classifications else
                    {'component':'moved','connection':'rerouted','boundary':'geometry-changed'}[kind]if'geometry'in classifications else'same')
        if status=='same':continue
        if kind=='component':
            row={'id':key}
            if a is not None:row['baseLabel']=a.get('label')
            if b is not None:row['headLabel']=b.get('label')
        elif kind=='connection':
            row={'id':key}
            for side,item in [('base',a),('head',b)]:
                if item is not None:row[side]={'from':item['from'],'to':item['to'],'label':item.get('label','')}
        else:
            item=b or a;row={'key':item['kind']+':'+item['label'],'kind':item['kind'],'label':item['label']}
        row.update(status=status,classifications=sorted(classifications),changedFields=sorted(fields));result.append(row)
    return result


def compare_architecture(base,head,evidence=None):
    evidence=evidence or {}
    for side,d in [('base',base),('head',head)]:
        if d.get('schema_version')!=1:fail('delta/schema-version-mismatch','Comparison requires schema_version 1.',side=side)
        if d.get('diagram_type')!='architecture':fail('delta/type-mismatch','Comparison requires architecture snapshots.',side=side)
    A=index(base.get('components',[]),'components','base');B=index(head.get('components',[]),'components','head')
    if not set(A)&set(B):fail('delta/no-shared-component-id','Snapshots share no component identity.')
    a,b=repository(base['meta'].get('repository')),repository(head['meta'].get('repository'))
    if a and b:
        from .repository import parse_remote
        if parse_remote(a['url'],authored=True)['identity']!=parse_remote(b['url'],authored=True)['identity']:
            fail('delta/repository-mismatch','Snapshots name different repositories.',baseRepository=a['url'],headRepository=b['url'])
    groups={'components':changes(A,B,'component'),
            'connections':changes(index(base.get('connections',[]),'connections','base'),index(head.get('connections',[]),'connections','head'),'connection'),
            'boundaries':changes(boundary_index(base.get('boundaries',[]),'base'),boundary_index(head.get('boundaries',[]),'head'),'boundary')}
    summary={}
    for collection,statuses in [('components',['added','changed','evidenceChanged','removed','moved']),('connections',['added','changed','removed','rerouted']),('boundaries',['added','changed','removed','geometryChanged'])]:
        counts=dict.fromkeys(statuses,0)
        for row in groups[collection]:counts[re.sub(r'-([a-z])',lambda m:m[1].upper(),row['status'])]+=1
        summary[collection]=counts
    fields=['title','locale','subtitle','animation','visual_preset','quality_profile','engineering_profile','legend','viewBox']
    presentation=lambda d:{'meta':{k:d['meta'].get(k)for k in fields},'layout':d.get('layout'),'cards':d.get('cards')}
    summary.update(presentationChanged=canonical_json(presentation(base))!=canonical_json(presentation(head)),provenanceChanged=a!=b)
    result={'schemaVersion':1,'ok':True,'command':'compare','type':'architecture','comparatorVersion':1,'canonicalVersion':1,'completeness':'complete',
            'proofLevel':'revision-pinned'if a and b and evidence.get('baseVerified')and evidence.get('headVerified')else'authored',
            'summary':summary,'changes':groups,'identity':{'components':'components[].id','connections':'connections[].id (required)','boundaries':'boundaries[].kind + boundaries[].label (derived)'},
            'view':{'visualPreset':head['meta'].get('visual_preset','classic')},'limitations':['Authored Architecture IR only; no runtime impact, causality, risk, or mergeability is inferred.','Boundary identity is conservatively derived from kind + label.']}
    for side,d,repo in [('base',base,a),('head',head,b)]:
        info={'title':d['meta'].get('title','')}
        for short,long in [('rawSha256','RawSha256'),('semanticSha256','SemanticSha256'),('bytes','Bytes')]:
            if side+long in evidence:info[short]=evidence[side+long]
        if repo:info['revision']=repo['revision']
        result[side]=info
    if a!=b:
        def provenance(repo):
            if not repo:return None
            return {'revision':repo['revision'],**({'provider':repo['provider']}if'provider'in repo else{}),**({'linkMode':repo['link_mode']}if'link_mode'in repo else{})}
        result['provenance']={'changedFields':['/repository']if not a or not b else sorted('/'+k for k in ['url','revision','provider','link_mode']if a.get(k)!=b.get(k)), 'base':provenance(a),'head':provenance(b)}
    return result


def annotate(svg,receipt,side,prefix):
    root=ET.fromstring(svg)
    maps={k:{row.get('id',row.get('key')):row for row in receipt['changes'][k]}for k in ['components','connections','boundaries']}
    for element in root.iter():
        key=element.get('data-node-id');row=maps['components'].get(key)if key else None
        if element.get('data-edge-id'):row=maps['connections'].get(element.get('data-edge-id'))
        if element.get('data-composition-frame-kind'):
            key=element.get('data-composition-frame-kind')+':'+element.get('data-composition-frame-label','');row=maps['boundaries'].get(key);element.set('data-delta-boundary-key',key)
        if key or element.get('data-edge-id'):
            state=row['status']if row else'same'
            if(side=='base'and state=='added')or(side=='head'and state=='removed'):state='same'
            element.set('data-delta-state',state)
            if row:element.set('data-delta-classifications',','.join(row['classifications']))
        for attr,value in list(element.attrib.items()):
            if attr=='id':element.set(attr,prefix+'-'+value)
            elif attr=='aria-labelledby':element.set(attr,' '.join(prefix+'-'+v for v in value.split()))
            elif attr in ('href', '{http://www.w3.org/1999/xlink}href') and value.startswith('#'):element.set(attr,'#'+prefix+'-'+value[1:])
            else:element.set(attr,re.sub(r'url\(#([^)]*)\)',lambda m:'url(#'+prefix+'-'+m[1]+')',value))
    return root


def render_comparison(receipt,base_svg,head_svg,template):
    base=annotate(base_svg,receipt,'base','base');head=annotate(head_svg,receipt,'head','head');delta=annotate(head_svg,receipt,'head','delta')
    ghost=annotate(base_svg,receipt,'base','ghost')
    delta.set('viewBox','0 0 '+str(max(float(base.get('viewBox').split()[2]),float(head.get('viewBox').split()[2])))+' '+str(max(float(base.get('viewBox').split()[3]),float(head.get('viewBox').split()[3]))))
    defs=next((e for e in ghost if e.tag.split('}')[-1]=='defs'),None)
    if defs is not None:delta.insert(0,copy.deepcopy(defs))
    # Retain old geometry of removed/moved entities and changed relationship routes.
    for element in ghost.iter():
        state=element.get('data-delta-state')
        groups=set(element.get('data-delta-classifications','').split(','))
        is_node=bool(element.get('data-node-id'))
        is_edge=bool(element.get('data-edge-id'))
        is_boundary=bool(element.get('data-delta-boundary-key'))
        if ((is_node and (state=='removed' or 'geometry' in groups)) or
                (is_edge and (state=='removed' or bool(groups & {'topology','geometry'}))) or
                (is_boundary and state in ['removed','changed','geometry-changed'])):
            copied=copy.deepcopy(element)
            copied.set('data-delta-state','removed' if state=='removed' or is_edge and 'topology' in groups else 'moved-from')
            delta.append(copied)
    fragments=[ET.tostring(root,encoding='unicode')for root in [base,delta,head]]
    all_ids=[]
    for root in [base,delta,head]:all_ids += [e.get('id')for e in root.iter()if e.get('id')]
    if len(all_ids)!=len(set(all_ids)):fail('delta/duplicate-svg-id','Comparison contains duplicate SVG identities.')
    css=re.search(r'<style>([\s\S]*?)</style>',template)[1]
    rows=[]
    for collection in ['components','connections','boundaries']:
        for row in receipt['changes'][collection]:
            key=row.get('id',row.get('key'));label=row.get('headLabel',row.get('baseLabel',row.get('label',key)))
            rows.append('<button type="button" data-change-id="'+esc(key)+'" data-change-kind="'+collection+'">'+esc(row['status']+' · '+str(label))+'</button>')
    data=json.dumps(receipt,ensure_ascii=False,indent=2).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    controls=''.join('<button data-tab="'+name+'">'+name.title()+'</button>'for name in ['base','delta','head'])
    controls += '<button id="review-overview">Overview</button><button id="review-previous">Previous</button><button id="review-next">Next</button><button id="review-play" aria-pressed="false">Review</button><button id="theme">Theme</button><button id="export-svg">Export SVG</button><button id="export-png">Export PNG</button><span id="review-status" role="status" aria-live="polite">Overview</span>'
    panels=''.join('<section data-view="'+name+'"'+(' hidden'if name!='delta'else'')+'>'+svg+'</section>'for name,svg in zip(['base','delta','head'],fragments))
    extra='''body{padding:20px}main{max-width:1500px;margin:auto}nav,aside{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}button{padding:8px;cursor:pointer}section>svg{width:100%;height:auto}section[hidden]{display:none}[data-delta-state="added"]{filter:drop-shadow(0 0 3px #16a34a)}[data-delta-state="removed"]{opacity:.5}[data-delta-state="moved-from"]{opacity:.3}[data-review-active]{filter:drop-shadow(0 0 5px #f59e0b)}'''
    script='''document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>document.querySelectorAll('[data-view]').forEach(p=>p.hidden=p.dataset.view!==b.dataset.tab));document.querySelectorAll('[data-change-id]').forEach(b=>b.onclick=()=>{document.querySelector('[data-tab="delta"]').click();document.querySelectorAll('[data-review-active]').forEach(e=>e.removeAttribute('data-review-active'));document.querySelectorAll('[data-view="delta"] [data-node-id],[data-view="delta"] [data-edge-id],[data-view="delta"] [data-delta-boundary-key]').forEach(e=>{if([e.dataset.nodeId,e.dataset.edgeId,e.dataset.deltaBoundaryKey].includes(b.dataset.changeId)){e.setAttribute('data-review-active','');e.scrollIntoView({block:'center',inline:'nearest'});}})});'''
    script += r"""
const changes=[...document.querySelectorAll('[data-change-id]')];
let reviewIndex=-1, timer=null;
const status=document.querySelector('#review-status');
const play=document.querySelector('#review-play');
function stop(){clearInterval(timer);timer=null;play.setAttribute('aria-pressed','false');}
function overview(){stop();reviewIndex=-1;document.querySelectorAll('[data-review-active]').forEach(e=>e.removeAttribute('data-review-active'));status.textContent='Overview';}
function step(n){if(!changes.length)return;reviewIndex=(n+changes.length)%changes.length;changes[reviewIndex].click();status.textContent=(reviewIndex+1)+' / '+changes.length+' · '+changes[reviewIndex].textContent;}
document.querySelector('#review-overview').onclick=overview;
document.querySelector('#review-next').onclick=()=>{stop();step(reviewIndex+1);};
document.querySelector('#review-previous').onclick=()=>{stop();step(reviewIndex-1);};
play.onclick=()=>{if(timer){stop();return;}step(reviewIndex+1);if(!matchMedia('(prefers-reduced-motion: reduce)').matches){play.setAttribute('aria-pressed','true');timer=setInterval(()=>step(reviewIndex+1),2500);}};
document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;if(e.key==='Escape')overview();else if(e.key==='ArrowRight'){stop();step(reviewIndex+1);}else if(e.key==='ArrowLeft'){stop();step(reviewIndex-1);}});
document.querySelector('#theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';};
function canonicalSvg(){const svg=document.querySelector('[data-view="delta"] svg').cloneNode(true);svg.querySelectorAll('[data-review-active]').forEach(e=>e.removeAttribute('data-review-active'));const style=document.createElementNS('http://www.w3.org/2000/svg','style');style.textContent=document.querySelector('style').textContent;svg.insertBefore(style,svg.firstChild);const computed=getComputedStyle(document.documentElement);const names=[...new Set(style.textContent.match(/--[a-zA-Z0-9-]+/g)||[])];svg.setAttribute('style',names.map(n=>n+':'+computed.getPropertyValue(n)).join(';'));return new XMLSerializer().serializeToString(svg);}
function download(blob,name){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function exportSvg(){download(new Blob([canonicalSvg()],{type:'image/svg+xml'}),'architecture-delta.svg');}
async function exportPng(){const svg=canonicalSvg();const url=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml'}));try{const img=new Image();await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=reject;img.src=url;});const box=document.querySelector('[data-view="delta"] svg').viewBox.baseVal;const scale=Math.min(2,4096/Math.max(box.width,box.height));const canvas=document.createElement('canvas');canvas.width=Math.ceil(box.width*scale);canvas.height=Math.ceil(box.height*scale);const ctx=canvas.getContext('2d');ctx.fillStyle=getComputedStyle(document.body).backgroundColor;ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(img,0,0,canvas.width,canvas.height);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(blob)download(blob,'architecture-delta.png');}finally{URL.revokeObjectURL(url);}}
document.querySelector('#export-svg').onclick=exportSvg;
document.querySelector('#export-png').onclick=()=>exportPng().catch(e=>{status.textContent='Export failed: '+e.message;});
window.Archify={deltaExport:{canonicalSvg,exportSvg,exportPng},review:{overview,step,stop}};
"""
    return ('<!DOCTYPE html><html lang="en" data-theme="dark" data-preset="'+esc(receipt['view']['visualPreset'])+'"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Architecture comparison</title><style>'+css+extra+'</style></head><body><main><h1>'+esc(receipt['base']['title'])+' → '+esc(receipt['head']['title'])+'</h1><p>'+esc(receipt['proofLevel'])+' · Authored changes; no runtime impact inferred</p><nav>'+controls+'</nav><aside>'+''.join(rows)+'</aside>'+panels+'<script id="archify-compare-receipt" type="application/json">'+data+'</script></main><script>'+script+'</script></body></html>').encode()
