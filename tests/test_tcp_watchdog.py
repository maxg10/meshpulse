import ast,contextlib,sys,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
source=(Path(__file__).resolve().parents[1]/'backend/meshpulse.py').read_text();tree=ast.parse(source)
cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='TCPMeshtasticInterface')
module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
class Clock:
 def __init__(self):self.now=0
 def monotonic(self):return self.now
class FakePub:
 def subscribe(self,*args):pass
 def unsubscribe(self,*args):pass
class FakeIface:
 def __init__(self,*args,**kwargs):self.frames=[]
 def _handleFromRadio(self,payload):
  if payload=='invalid':raise ValueError('bad protobuf')
  self.frames.append(payload)
 def close(self):pass
fake_pub=types.ModuleType('pubsub');fake_pub.pub=FakePub()
ns={'time':time,'meshtastic':types.SimpleNamespace(tcp_interface=types.SimpleNamespace(TCPInterface=FakeIface)), '_scan_capturing':lambda:False,'plugin_manager':None}
exec(compile(module,'candidate','exec'),ns);TCP=ns['TCPMeshtasticInterface']
class Tests(unittest.TestCase):
 def setUp(self):
  self.clock=Clock();self.patches=[patch.object(time,'monotonic',self.clock.monotonic),patch.dict(sys.modules,{'pubsub':fake_pub})]
  for p in self.patches:p.start();self.addCleanup(p.stop)
  self.w=TCP('host');self.w.connect()
 def test_quiet_radio_with_heartbeat_frames_stays_connected(self):
  self.clock.now=300;self.w.interface._handleFromRadio('queueStatus')
  self.clock.now=419;self.assertFalse(self.w.device_stream_silent(420))
  self.clock.now=600;self.w.interface._handleFromRadio('queueStatus')
  self.clock.now=900;self.w.interface._handleFromRadio('queueStatus')
  self.assertFalse(self.w.device_stream_silent(420))
 def test_no_frames_after_heartbeat_window_is_stale(self):
  self.clock.now=420;self.assertFalse(self.w.device_stream_silent(420))
  self.clock.now=421;self.assertTrue(self.w.device_stream_silent(420))
 def test_invalid_frame_does_not_reset(self):
  self.clock.now=400
  with self.assertRaises(ValueError):self.w.interface._handleFromRadio('invalid')
  self.clock.now=421;self.assertTrue(self.w.device_stream_silent(420))
 def test_regular_mesh_frames_reset_timer(self):
  self.clock.now=400;self.w.interface._handleFromRadio('packet')
  self.clock.now=800;self.assertFalse(self.w.device_stream_silent(420))
 def test_frame_handler_remains_transparent(self):
  self.w.interface._handleFromRadio('packet');self.assertEqual(self.w.interface.frames,['packet'])
if __name__=='__main__':unittest.main()
