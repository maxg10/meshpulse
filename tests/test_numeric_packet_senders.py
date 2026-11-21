import ast, asyncio, contextlib, io, time, unittest
from pathlib import Path
from unittest.mock import Mock
source = (Path(__file__).resolve().parents[1] / 'backend/meshpulse.py').read_text()
tree = ast.parse(source)
klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ListenBasedMapper')
method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_telemetry_from_packet')
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'sanitize_node_id']
plugin = Mock()
ns = {'asyncio': asyncio, 'time': time, 'plugin_manager': plugin}
exec(compile(ast.Module(body=functions+[method], type_ignores=[]), 'candidate', 'exec'), ns)
class Mapper:
    parse = ns['parse_telemetry_from_packet']
    def __init__(self):
        self.local_node_id='!11111111'; self.nodes={'!e8225aac': {'id':'!e8225aac'}, '!abcdef01': {'id':'!abcdef01'}}; self.nodes_no_position={}; self.log_packet_to_stats=Mock()
    def _packet_route_tag(self,p): return ''
    def _packet_radio_meta(self,p): return (None,None,None,False,None)
    async def broadcast_node_update(self,*a): pass
    async def broadcast_stats_update(self): pass
class TestTelemetrySender(unittest.TestCase):
    def setUp(self): plugin.reset_mock(); self.mapper=Mapper()
    def parse(self, **fields):
        with contextlib.redirect_stdout(io.StringIO()): return self.mapper.parse({'decoded':{'telemetry':{'deviceMetrics':{'voltage':4.1}}}, **fields})
    def test_numeric_sender_reaches_hook(self):
        self.assertTrue(self.parse(**{'from':0xe8225aac, 'fromId':None})); self.assertEqual(plugin.dispatch_hook_sync.call_args.args[1]['node_id'],'!e8225aac'); self.assertEqual(self.mapper.nodes['!e8225aac']['voltage'],4.1)
    def test_formatted_sender_takes_precedence(self):
        self.assertTrue(self.parse(**{'from':0xe8225aac, 'fromId':'!abcdef01'})); self.assertEqual(plugin.dispatch_hook_sync.call_args.args[1]['node_id'],'!abcdef01')
    def test_invalid_numeric_sender_is_dropped(self):
        for sender in (None,0,-1,0xffffffff,0x100000000,True,'123'):
            with self.subTest(sender=sender): self.assertFalse(self.parse(**{'from':sender})); plugin.dispatch_hook_sync.assert_not_called()

class TestPositionSender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_position_from_packet')
        position_ns = {'asyncio':asyncio, 'time':time, 'plugin_manager':None, 'is_placeholder_position':lambda lat,lon:False}
        exec(compile(ast.Module(body=functions+[method], type_ignores=[]), 'candidate', 'exec'), position_ns)
        cls.parse = staticmethod(position_ns['parse_position_from_packet'])
    def mapper(self):
        m=Mapper(); m.known_names={}; return m
    def test_numeric_sender_creates_map_node(self):
        m=self.mapper()
        with contextlib.redirect_stdout(io.StringIO()): result=self.parse(m, {'from':0xe8225aac, 'decoded':{'position':{'latitude':52.3,'longitude':-1.5}}})
        self.assertTrue(result); self.assertEqual(m.nodes['!e8225aac']['lat'],52.3)
    def test_invalid_sender_cannot_create_node(self):
        for sender in (0,-1,0xffffffff,True,'123'):
            m=self.mapper()
            with contextlib.redirect_stdout(io.StringIO()): result=self.parse(m, {'from':sender, 'decoded':{'position':{'latitude':52.3,'longitude':-1.5}}})
            self.assertFalse(result)

class TestOtherSenderPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        methods=[n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name in ('parse_node_info_from_packet','_parse_neighbor_info')]
        more=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='sanitize_str']
        env={'asyncio':asyncio,'time':time,'plugin_manager':None,'is_placeholder_position':lambda a,b:False}
        exec(compile(ast.Module(body=functions+more+methods,type_ignores=[]),'candidate','exec'),env)
        cls.node=staticmethod(env['parse_node_info_from_packet']);cls.neighbor=staticmethod(env['_parse_neighbor_info'])
    def mapper(self):
        m=Mapper();m.known_names={};m.stats_db=Mock();m._remember_key=Mock();m._carry_sticky_fields=Mock();m._update_message_names=Mock()
        async def broadcast(*args): pass
        m.broadcast_neighbor_update=broadcast
        return m
    def test_node_info_numeric_sender_and_identity_precedence(self):
        m=self.mapper()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.node(m,{'from':0xe8225aac,'decoded':{'user':{'longName':'Solar'}}}))
            self.assertTrue(self.node(m,{'from':0xe8225aac,'decoded':{'user':{'id':'!abcdef01','longName':'Other'}}}))
        self.assertEqual(m.nodes_no_position['!e8225aac']['name'],'Solar');self.assertEqual(m.nodes_no_position['!abcdef01']['name'],'Other')
    def test_neighbor_numeric_sender_and_rejection(self):
        packet={'from':0xe8225aac,'decoded':{'neighborinfo':{'neighbors':[{'nodeId':0xabcdef01,'snr':8}]}}}
        m=self.mapper()
        with contextlib.redirect_stdout(io.StringIO()): self.neighbor(m,packet)
        self.assertEqual(m.stats_db.log_neighbor_info.call_args.args[0],'!e8225aac')
        for bad in (None,0,-1,0xffffffff,True,'123'):
            m=self.mapper();packet['from']=bad
            with contextlib.redirect_stdout(io.StringIO()): self.neighbor(m,packet)
            m.stats_db.log_neighbor_info.assert_not_called()
    def test_node_info_rejects_invalid_numeric_sender(self):
        for bad in (None,0,-1,0xffffffff,True,'123'):
            m=self.mapper()
            with contextlib.redirect_stdout(io.StringIO()): result=self.node(m,{'from':bad,'decoded':{'user':{'longName':'Invalid'}}})
            self.assertFalse(result);m._remember_key.assert_not_called()

if __name__ == '__main__':
    unittest.main()
