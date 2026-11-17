from fractions import Fraction
from itertools import product

def enumerate_states(events,gates,order,root):
    names=sorted(events);probability=Fraction();failure_states=0
    for bits in product((False,True),repeat=len(names)):
        state=dict(zip(names,bits));weight=Fraction(1)
        for name,failed in state.items():weight*=events[name] if failed else 1-events[name]
        for gid in order:
            g=gates[gid];values=[state[x] for x in g['inputs']];state[gid]=all(values) if g['op']=='AND' else any(values)
        if state[root]:probability+=weight;failure_states+=1
    return {'root':root,'failure_probability':str(probability),'reliability':str(1-probability),'enumerated_states':2**len(names),'logical_failure_states':failure_states,'independence_assumption':'basic event occurrences are mutually independent; repeated references share the same event state'}
