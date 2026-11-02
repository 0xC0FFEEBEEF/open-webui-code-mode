import json,sys
d=json.load(open(sys.argv[1])); comps=d.get('components',{}).get('schemas',{})
def res(s,depth=0,seen=()):
    if depth>5: return '…'
    if isinstance(s,dict):
        if '$ref' in s:
            n=s['$ref'].split('/')[-1]
            if n in seen: return f'<{n}>'
            return {f'<{n}>':res(comps.get(n,{}),depth+1,seen+(n,))}
        out={}
        for k,v in s.items():
            if k in('description','title','examples','example'): continue
            out[k]=res(v,depth+1,seen)
        return out
    if isinstance(s,list): return [res(x,depth+1,seen) for x in s]
    return s
ops={o.get('operationId'):(p,m,o) for p,ms in d['paths'].items() for m,o in ms.items() if isinstance(o,dict)}
for op in sys.argv[2:]:
    p,m,o=ops[op]; print(f'===== {op}  {m.upper()} {p}')
    if o.get('description'): print('  desc:',o['description'][:400])
    for prm in o.get('parameters',[]): print('  param',prm.get('in'),prm.get('name'),json.dumps(res(prm.get('schema',{})))[:200])
    rb=o.get('requestBody',{}).get('content',{})
    for ct,c in rb.items(): print('  body',ct,json.dumps(res(c.get('schema',{})))[:1800])
    for code,r in o.get('responses',{}).items():
        for ct,c in (r.get('content') or {}).items(): print('  resp',code,ct,json.dumps(res(c.get('schema',{})))[:900])
