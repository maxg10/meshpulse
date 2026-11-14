import ast, socket,struct,threading,time,types,meshtastic,meshtastic.tcp_interface
from meshtastic import mesh_pb2
from pathlib import Path
source=(Path(__file__).resolve().parents[1] / 'backend' / 'meshpulse.py').read_text();tree=ast.parse(source)
node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TCPMeshtasticInterface')
ns={'time':time,'meshtastic':meshtastic,'_scan_capturing':lambda:False,'plugin_manager':None};exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'candidate','exec'),ns)
s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);port=s.getsockname()[1];stopped=threading.Event();handshake=threading.Event();probe=threading.Event()
def recvexact(c,n):
 b=b''
 while len(b)<n:
  x=c.recv(n-len(b))
  if not x:return None
  b+=x
 return b
def send(c,p):
 b=p.SerializeToString();c.sendall(b'\x94\xc3'+struct.pack('>H',len(b))+b)
peer={}
def server():
 c,_=s.accept();peer['socket']=c;c.settimeout(8)
 try:
  while not stopped.is_set():
   first=recvexact(c,1)
   if not first:break
   if first!=b'\x94':continue
   tail=recvexact(c,3)
   if not tail or tail[:1]!=b'\xc3':continue
   b=recvexact(c,int.from_bytes(tail[1:3],'big'))
   if not b:break
   x=mesh_pb2.ToRadio();x.ParseFromString(b);o=mesh_pb2.FromRadio()
   if x.want_config_id:
    print('config requested',flush=True)
    info=mesh_pb2.FromRadio();info.my_info.my_node_num=12345;send(c,info)
    node=mesh_pb2.FromRadio();node.node_info.num=12345;node.node_info.user.id='!00003039';send(c,node)
    ch=mesh_pb2.FromRadio();ch.channel.index=0;ch.channel.role=1;ch.channel.settings.name='test';send(c,ch)
    o.config_complete_id=x.want_config_id;send(c,o);handshake.set()
   elif x.HasField('heartbeat'):o.queueStatus.free=4;o.queueStatus.maxlen=8;send(c,o);probe.set()
 except (OSError,TimeoutError) as e:print('simulated server stopped',type(e).__name__,flush=True)
 finally:c.close();s.close()
t=threading.Thread(target=server,daemon=True);t.start();w=ns['TCPMeshtasticInterface']('127.0.0.1',port);w.connect();assert handshake.wait(2);assert probe.wait(2);probe.clear();w._last_device_frame=time.monotonic()-500
w.interface.sendHeartbeat();assert probe.wait(2)
for _ in range(40):
 if not w.device_stream_silent(420):break
 time.sleep(.05)
assert not w.device_stream_silent(420),'heartbeat frame did not reset watchdog'
print('Real TCP framing: device handshake and heartbeat response reset the watchdog',flush=True)
stopped.set();s.close();peer['socket'].shutdown(socket.SHUT_RDWR);peer['socket'].close()
for _ in range(120):
 if not w.interface.isConnected.is_set():break
 time.sleep(.1)
assert not w.interface.isConnected.is_set(), 'simulated dead TCP link not detected'
print('Simulated peer loss cleared the actual library connection status',flush=True)
w.disconnect();t.join(timeout=2)
