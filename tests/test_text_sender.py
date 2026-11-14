import ast,asyncio,contextlib,io,time,unittest
from pathlib import Path
from unittest.mock import Mock
source=(Path(__file__).resolve().parents[1] / 'backend' / 'meshpulse.py').read_text(); tree=ast.parse(source)
klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ListenBasedMapper')
method=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name=='parse_text_from_packet')
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('sanitize_node_id','sanitize_message')]
module=ast.Module(body=functions+[method],type_ignores=[]);ns={'asyncio':asyncio,'time':time,'plugin_manager':None};exec(compile(module,'candidate','exec'),ns)
class Mapper:
 parse=ns['parse_text_from_packet']
 def __init__(self): self.nodes={};self.nodes_no_position={};self.known_names={};self.messages={};self.connection_type='tcp';self.log_packet_to_stats=Mock()
 def _packet_route_tag(self,p): return ''
 def _packet_radio_meta(self,p): return (None,None,None,False,None)
 async def broadcast_message(self,m): pass
class TestSender(unittest.TestCase):
 def parse(self,p):
  m=Mapper();p={'decoded':{'text':'hello'},**p}
  with contextlib.redirect_stdout(io.StringIO()):r=m.parse(p)
  return r,m
 def test_numeric_sender_with_channel(self):
  r,m=self.parse({'from':0x12345678,'channel':2});self.assertTrue(r);self.assertEqual(m.messages[2][0]['from_id'],'!12345678');self.assertFalse(m.messages[2][0]['is_dm'])
 def test_formatted_sender_takes_precedence(self):
  r,m=self.parse({'fromId':'!abcdef01','from':0x12345678});self.assertTrue(r);self.assertEqual(m.messages[0][0]['from_id'],'!abcdef01')
 def test_invalid_sender_dropped(self):
  for n in (None,0,-1,0xffffffff,0x100000000,True,'123'):
   with self.subTest(sender=n):
    r,m=self.parse({'from':n});self.assertFalse(r);self.assertEqual(m.messages,{})
 def test_dm_preserved(self):
  r,m=self.parse({'from':0x12345678,'toId':'!23456789'});self.assertTrue(r);self.assertTrue(m.messages[0][0]['is_dm'])
if __name__=='__main__':unittest.main()
