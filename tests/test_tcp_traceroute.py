import ast
import asyncio
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

source = (Path(__file__).resolve().parents[1] / 'backend' / 'meshpulse.py').read_text()
tree = ast.parse(source)
function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run_traceroute')
module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
fake_lib = types.ModuleType('meshtastic')
fake_lib.mesh_pb2 = types.SimpleNamespace(RouteDiscovery=lambda: object())
fake_lib.portnums_pb2 = types.SimpleNamespace(PortNum=types.SimpleNamespace(TRACEROUTE_APP=70))


class FakeIface:
    def __init__(self):
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.responseHandlers = {}
        self.requests = []

    def sendData(self, data, destinationId, **kwargs):
        num = len(self.requests) + 1
        self.requests.append((destinationId, kwargs['onResponse']))
        self.responseHandlers[num] = kwargs['onResponse']
        return types.SimpleNamespace(id=num)


class Socket:
    def __init__(self): self.messages = []
    async def send(self, message): self.messages.append(json.loads(message))


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.modules_patch = patch.dict(sys.modules, {'meshtastic': fake_lib})
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        self.iface = FakeIface()
        self.mapper = types.SimpleNamespace(connection_type='tcp', _port_hold=threading.Event(),
            _text_send_lock=threading.Lock(), _mesh_iface=lambda: self.iface,
            nodes={}, nodes_no_position={})
        env = dict(asyncio=asyncio, json=json, time=time, mapper=self.mapper)
        exec(compile(module, '<candidate>', 'exec'), env)
        self.run_trace = env['run_traceroute']

    async def test_overlapping_responses_and_route_direction(self):
        a, b = Socket(), Socket()
        tasks = [asyncio.create_task(self.run_trace('!00000002', a)),
                 asyncio.create_task(self.run_trace('!00000003', b))]
        for _ in range(100):
            if len(self.iface.requests) == 2: break
            await asyncio.sleep(.01)
        self.assertEqual(len(self.iface.requests), 2)
        for dest, callback in reversed(self.iface.requests):
            callback({'from': int(dest[1:], 16), 'to': 1, 'hopStart': 3,
                      'decoded': {'portnum': 'TRACEROUTE_APP',
                      'traceroute': {'route': [4], 'routeBack': [5], 'snrBack': [4, 8]}}})
        await asyncio.gather(*tasks)
        self.assertEqual([h['id'] for h in a.messages[-1]['route']], ['!00000001', '!00000004', '!00000002'])
        self.assertEqual([h['id'] for h in b.messages[-1]['route_back']], ['!00000003', '!00000005', '!00000001'])
        self.assertFalse(self.iface.responseHandlers)

    async def test_routing_error(self):
        socket = Socket()
        task = asyncio.create_task(self.run_trace('!00000002', socket))
        while not self.iface.requests: await asyncio.sleep(.01)
        self.iface.requests[0][1]({'decoded': {'portnum': 'ROUTING_APP', 'routing': {'errorReason': 'NO_ROUTE'}}})
        await task
        self.assertIn('NO_ROUTE', socket.messages[-1]['error'])

    async def test_disconnected(self):
        self.iface.isConnected.clear()
        socket = Socket()
        await self.run_trace('!00000002', socket)
        self.assertIn('not connected', socket.messages[-1]['error'])
        self.assertFalse(self.iface.requests)

    async def test_wrong_sender_is_rejected(self):
        socket = Socket()
        task = asyncio.create_task(self.run_trace('!00000002', socket))
        while not self.iface.requests: await asyncio.sleep(.01)
        self.iface.requests[0][1]({'from': 3, 'to': 1,
                                   'decoded': {'portnum': 'TRACEROUTE_APP', 'traceroute': {}}})
        await task
        self.assertIn('unexpected node', socket.messages[-1]['error'])

    async def test_missing_return_path_is_not_invented(self):
        socket = Socket()
        task = asyncio.create_task(self.run_trace('!00000002', socket))
        while not self.iface.requests: await asyncio.sleep(.01)
        self.iface.requests[0][1]({'from': 2, 'to': 1,
                                   'decoded': {'portnum': 'TRACEROUTE_APP', 'traceroute': {}}})
        await task
        self.assertEqual(socket.messages[-1]['route_back'], [])


if __name__ == '__main__':
    unittest.main()
