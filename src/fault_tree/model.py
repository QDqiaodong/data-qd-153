from fractions import Fraction
from .common import require,ident,number,rows
from .calculation import enumerate_states

def analyze(data):
    require(data,'events gates root');events={};gates={}
    for e in rows(data['events'],16):
        require(e,'id numerator denominator');eid=ident(e['id']);n=number(e['numerator'],0,10**9);d=number(e['denominator'],1,10**9)
        if eid in events or n>d:raise ValueError('duplicate event or invalid probability')
        events[eid]=Fraction(n,d)
    for g in rows(data['gates'],100):
        require(g,'id op inputs');gid=ident(g['id']);op=ident(g['op']);inputs=[ident(x) for x in rows(g['inputs'],100)]
        if gid in events or gid in gates or op not in ('AND','OR') or not inputs or len(set(inputs))!=len(inputs):raise ValueError('invalid gate')
        gates[gid]={'op':op,'inputs':inputs}
    root=ident(data['root']);known=set(events)|set(gates)
    if root not in known or any(x not in known for g in gates.values() for x in g['inputs']):raise ValueError('unknown node')
    order=[];pending=set(gates);resolved=set(events)
    while pending:
        ready=sorted(g for g in pending if set(gates[g]['inputs'])<=resolved)
        if not ready:raise ValueError('cyclic gates')
        order.extend(ready);resolved.update(ready);pending.difference_update(ready)
    return enumerate_states(events,gates,order,root)
