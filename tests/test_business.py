
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,math
from pathlib import Path
from fractions import Fraction
from fault_tree.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_shared_event(self):
        r=analyze(sample());self.assertEqual(r['failure_probability'],'11/250');self.assertEqual(r['reliability'],'239/250');self.assertEqual(r['enumerated_states'],8)
    def test_absorption(self):
        d={'events':[{'id':'A','numerator':1,'denominator':3},{'id':'B','numerator':1,'denominator':2}],'gates':[{'id':'both','op':'AND','inputs':['A','B']},{'id':'root','op':'OR','inputs':['A','both']}],'root':'root'};self.assertEqual(analyze(d)['failure_probability'],'1/3')
    def test_probability_extremes(self):
        d=sample();d['events'][0]['numerator']=0;self.assertEqual(analyze(d)['failure_probability'],'0');d['events'][0]['numerator']=10;r=analyze(d);self.assertEqual(r['failure_probability'],'11/25')
    def test_event_root(self):
        d=sample();d['root']='A';self.assertEqual(analyze(d)['failure_probability'],'1/10')
    def test_cycle(self):
        d=sample();d['gates'][0]['inputs']=['top'];self.bad(d)
    def test_unknown(self):
        d=sample();d['gates'][0]['inputs']=['missing'];self.bad(d)
    def test_order(self):
        d=sample();d['events'].reverse();d['gates'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_duplicate_input(self):
        d=sample();d['gates'][0]['inputs']=['A','A'];self.bad(d)

class Command(unittest.TestCase):
    def call(self,src,out):
        return subprocess.run([sys.executable,'-m','fault_tree.cli','--source',str(src),'--output',str(out)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
