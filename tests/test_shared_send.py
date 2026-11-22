import ast,asyncio,contextlib,io,json,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
root=Path(__file__).resolve().parents[1]
def extract(path, cls, name, ns):
 tree=ast.parse(path.read_text()); items=tree.body
 if cls:items=next(n for n in items if isinstance(n,ast.ClassDef) and n.name==cls).body
 fn=next(n for n in items if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
 exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),ns);return ns[name]
ns={'asyncio':asyncio,'json':json,'time':time,'mapper':None}
send=extract(root/'backend/meshpulse.py','ListenBasedMapper','send_text_via_listener',ns)
chat=extract(root/'backend/meshpulse.py',None,'run_send_message',ns)
plugin=extract(root/'mapper/plugin_manager.py','PluginManager','send_mesh_message',ns)
class Interface:
 def __init__(self):self.isConnected=threading.Event();self.isConnected.set();self.calls=[];self.active=0;self.maxactive=0;self.lock=threading.Lock();self.failure=None
 def sendText(self,text,**kw):
  with self.lock:self.active+=1;self.maxactive=max(self.maxactive,self.active)
  time.sleep(.03)
  try:
   if self.failure:raise self.failure
   self.calls.append((text,kw))
  finally:
   with self.lock:self.active-=1
class Mapper:
 send_text_via_listener=send
 def __init__(self):self._text_send_lock=threading.Lock();self._port_hold=threading.Event();self.iface=Interface();self.connection_type='tcp';self.local_node_id='!11111111';self.messages={};self.pushed=[]
 def _mesh_iface(self):return self.iface
 async def broadcast_message(self,m):self.pushed.append(m)
class WS:
 def __init__(self):self.events=[]
 async def send(self,data):self.events.append(json.loads(data))
class Manager:send_mesh_message=plugin
class Tests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):self.m=Mapper();ns['mapper']=self.m
 async def test_both_paths_one_interface_channel_destination(self):
  ws=WS();manager=Manager();manager.mapper=self.m
  await asyncio.gather(chat('public',2,None,ws),manager.send_mesh_message('reply','!12345678',1))
  self.assertCountEqual(self.m.iface.calls,[('public',{'channelIndex':2,'destinationId':'^all'}),('reply',{'channelIndex':1,'destinationId':'!12345678'})]);self.assertEqual(self.m.iface.maxactive,1);self.assertTrue(next(x for x in ws.events if x['type']=='send_result')['success']);self.assertEqual(self.m.messages[2][0]['text'],'public')
 async def test_disconnected_is_failure_no_new_connection(self):
  self.m.iface.isConnected.clear();ws=WS();manager=Manager();manager.mapper=self.m
  with contextlib.redirect_stdout(io.StringIO()):await chat('hello',0,None,ws)
  self.assertFalse(next(x for x in ws.events if x['type']=='send_result')['success']);self.assertEqual(self.m.messages,{})
  with self.assertRaisesRegex(RuntimeError,'not connected'):await manager.send_mesh_message('reply')
  self.assertEqual(self.m.iface.calls,[])
 async def test_busy_and_error_propagate(self):
  self.m._port_hold.set();manager=Manager();manager.mapper=self.m
  with self.assertRaisesRegex(RuntimeError,'busy'):await manager.send_mesh_message('reply')
  self.m._port_hold.clear();self.m.iface.failure=OSError('socket closed');ws=WS()
  with contextlib.redirect_stdout(io.StringIO()):await chat('hello',0,None,ws)
  self.assertFalse(next(x for x in ws.events if x['type']=='send_result')['success']);self.assertEqual(self.m.messages,{})
 async def test_parallel_event_loops_are_serialized(self):
  def call(i):return asyncio.run(asyncio.to_thread(self.m.send_text_via_listener,str(i)))
  with ThreadPoolExecutor(max_workers=4) as ex:await asyncio.to_thread(lambda:list(ex.map(call,range(4))))
  self.assertEqual(self.m.iface.maxactive,1);self.assertEqual(len(self.m.iface.calls),4)
if __name__=='__main__':unittest.main()
