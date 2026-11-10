"""Preset scan: listen on other LoRa modem presets for a while, then put the
radio back exactly as it was.

The question it answers is one a fixed mapper cannot: which presets does the
mesh around here use? The radio is moved through a list of presets, a few
minutes on each, and every packet it hears is counted per preset: which
nodes, heard directly or through a relay, how strong, how busy the channel.

Three rules decide the design:

* Listen only. While the radio sits on someone else's preset it must not
  transmit, relay their traffic or announce itself, so lora.tx_enabled is
  off for the whole scan, and MQTT on the radio too (it would publish the
  other mesh's packets under this gateway's name).
* Nothing from the scan reaches the map, stats.db's packet history, the
  plugins or Live Pulse. Packets are diverted at the two entry points in the
  core and counted here. Nodes the radio first learns during a scan are
  remembered, so the NodeDB backfill after reconnecting does not bring them
  in through the back door later.
* The radio always goes back. The original settings are written to disk
  before anything changes; the restore runs in a finally, is retried until
  it works, and a MeshPulse that restarts in the middle of a scan restores
  the radio before anything else.

Radio settings are changed through the meshtastic CLI, the same way the
Config page does it: the serial port is released, the CLI writes, the
listener reconnects.
"""

import json
import os
import subprocess
import threading
import time

ACTIVE = ('starting', 'switching', 'listening', 'restoring', 'restore_retry')
DWELL_MIN_S = 60
DWELL_MAX_S = 1800
CLI_TIMEOUT = 90
SWITCH_TIMEOUT = 240         # CLI + reboot + reconnect + config read
RESTORE_RETRY_S = 60
SAVE_EVERY_S = 30
STATUS_EVERY_S = 10
FOREIGN_KEEP_S = 30 * 86400
HISTORY_KEEP = 50

# Bandwidth of each preset in kHz (firmware RadioInterface). A preset wider
# than the region's band cannot be used there.
BANDWIDTH_KHZ = {
    'LONG_FAST': 250, 'LONG_SLOW': 125, 'VERY_LONG_SLOW': 62.5, 'MEDIUM_SLOW': 250,
    'MEDIUM_FAST': 250, 'SHORT_SLOW': 250, 'SHORT_FAST': 250, 'LONG_MODERATE': 125,
    'SHORT_TURBO': 500, 'LONG_TURBO': 500,
}
# Width of a region's band in kHz, for the regions where a preset can be too wide.
REGION_SPAN_KHZ = {'EU_868': 250, 'UA_868': 600, 'RU': 500}
# Offered by default: the classic presets. VERY_LONG_SLOW is deprecated, the
# newest ones are left out until they are seen in the wild.
DEFAULT_PRESETS = ['LONG_FAST', 'LONG_MODERATE', 'LONG_SLOW', 'MEDIUM_SLOW',
                   'MEDIUM_FAST', 'SHORT_SLOW', 'SHORT_FAST', 'SHORT_TURBO', 'LONG_TURBO']


def _preset_enum():
    try:
        from meshtastic.protobuf import config_pb2
    except ImportError:                                   # older meshtastic package
        from meshtastic import config_pb2
    return {v.name: v.number for v in config_pb2.Config.LoRaConfig.ModemPreset.DESCRIPTOR.values}


def _region_enum():
    try:
        from meshtastic.protobuf import config_pb2
    except ImportError:
        from meshtastic import config_pb2
    return {v.number: v.name for v in config_pb2.Config.LoRaConfig.RegionCode.DESCRIPTOR.values}


def available_presets(region_name):
    """Presets this radio and region can use, in the order they are offered."""
    try:
        known = _preset_enum()
    except Exception:
        known = {p: i for i, p in enumerate(DEFAULT_PRESETS)}
    span = REGION_SPAN_KHZ.get(region_name)
    out = []
    for p in DEFAULT_PRESETS:
        if p not in known:
            continue
        if span is not None and BANDWIDTH_KHZ.get(p, 0) > span:
            continue
        out.append(p)
    return out


def _now():
    return int(time.time())


def _is_placeholder(lat, lon):
    return abs(lat) < 0.5 and abs(lon) < 0.5


class PresetScan:
    """One scan at a time, driven by a background thread.

    mapper_getter returns the current mapper (the core rebuilds it when the
    connection changes). broadcast(dict) sends a message to every browser.
    stats_db_getter returns the StatsDB, where finished scans are kept.
    """

    def __init__(self, mapper_getter, state_path, broadcast=None, stats_db_getter=None,
                 cli_runner=None, sleep=time.sleep):
        self._mapper = mapper_getter
        self._path = state_path
        self._broadcast = broadcast or (lambda msg: None)
        self._stats_db = stats_db_getter or (lambda: None)
        self._cli_runner = cli_runner          # tests replace the CLI
        self._sleep = sleep
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread = None
        self._state = self._load() or {}
        self._known_at_start = set()
        self._step = None
        self._last_save = 0
        self._last_status = 0
        self.foreign = dict(self._state.get('foreign') or {})   # node id -> ts first heard in a scan
        self._prune_foreign()

    # ── state on disk ───────────────────────────────────────────────────

    def _load(self):
        try:
            with open(self._path) as f:
                s = json.load(f)
            return s if isinstance(s, dict) else None
        except (OSError, ValueError):
            return None

    def _save(self):
        """Write the state atomically, readable by the MeshPulse user only."""
        with self._lock:
            self._state['foreign'] = self.foreign
            data = json.dumps(self._state, ensure_ascii=False)
            tmp = self._path + '.tmp'
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            except OSError as e:
                print(f"[SCAN] Could not save scan state: {e}")
            self._last_save = time.time()

    def _prune_foreign(self):
        cutoff = _now() - FOREIGN_KEEP_S
        for k in [k for k, ts in self.foreign.items() if ts < cutoff]:
            del self.foreign[k]

    # ── what the rest of the core asks ──────────────────────────────────

    @property
    def capturing(self):
        """True while the radio may be on another preset: packets must not reach the core."""
        return self._state.get('status') in ACTIVE

    def is_foreign(self, node_id):
        return node_id in self.foreign

    def seen_live(self, node_id):
        """A node first met during a scan was heard on our own preset: it is ours now."""
        if node_id in self.foreign:
            with self._lock:
                self.foreign.pop(node_id, None)

    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        s = self._state
        out = {k: s.get(k) for k in ('id', 'status', 'presets', 'dwell_s', 'idx', 'started', 'ended',
                                      'step_started', 'current', 'error', 'original_preset', 'region')}
        out['now'] = _now()
        out['active'] = self.capturing
        out['steps'] = [self._summary(st) for st in (s.get('steps') or [])]
        if self._step is not None and s.get('status') == 'listening':
            out['live'] = self._summary(self._step)
        return out

    def options(self):
        """What the browser needs to offer a scan: presets, the current one, defaults."""
        m = self._mapper()
        lora = self._lora(self._iface(m)) if m else None
        region = _region_enum().get(lora.region, str(lora.region)) if lora is not None else None
        current = self._preset_name(lora.modem_preset) if lora is not None else None
        problem = self._cannot_scan(m, lora)
        return {'presets': available_presets(region), 'current': current, 'region': region,
                'connected': lora is not None, 'problem': problem,
                'dwell_min_s': DWELL_MIN_S, 'dwell_max_s': DWELL_MAX_S,
                'local_stats_every_s': 900}

    # ── packets ─────────────────────────────────────────────────────────

    def capture(self, packet, local_id=None):
        """Count a packet heard during the scan. Called instead of the core's parsers."""
        if self._state.get('status') != 'listening' or self._step is None:
            return
        try:
            with self._lock:                         # status() reads the same dicts from another thread
                self._count(packet, local_id)
        except Exception as e:                      # a strange packet must not stop the scan
            print(f"[SCAN] Packet not counted: {e}")
        if time.time() - self._last_save > SAVE_EVERY_S:
            self._save()

    def _count(self, packet, local_id):
        step = self._step
        frm = packet.get('fromId')
        if not frm:
            num = packet.get('from')
            frm = f'!{num:08x}' if isinstance(num, int) else None
        if not frm:
            return
        dec = packet.get('decoded') or {}
        port = dec.get('portnum')
        port = port if isinstance(port, str) else (str(port) if port is not None else 'ENCRYPTED')
        now = _now()
        if frm == local_id:
            tel = dec.get('telemetry') or {}
            dm = tel.get('deviceMetrics') or {}
            ls = tel.get('localStats') or {}
            if dm.get('channelUtilization') is not None:
                step['cu'].append(round(float(dm['channelUtilization']), 2))
            if dm.get('airUtilTx') is not None:
                step['air'].append(round(float(dm['airUtilTx']), 3))
            if ls:
                keep = {k: ls[k] for k in ('numPacketsRx', 'numPacketsRxBad', 'numRxDupe', 'numPacketsTx',
                                           'numOnlineNodes', 'numTotalNodes', 'channelUtilization',
                                           'airUtilTx') if k in ls}
                if step.get('ls_first') is None:
                    step['ls_first'] = keep
                step['ls_last'] = keep
                step['ls_count'] = step.get('ls_count', 0) + 1
            return
        if packet.get('viaMqtt'):
            return                                   # the internet, not the air
        step['packets'] += 1
        step['ports'][port] = step['ports'].get(port, 0) + 1
        hs, hl = packet.get('hopStart'), packet.get('hopLimit')
        hops = (hs - (hl or 0)) if isinstance(hs, int) else None
        n = step['nodes'].get(frm)
        if n is None:
            n = step['nodes'][frm] = {'n': 0, 'direct': 0, 'snr_sum': 0.0, 'n_snr': 0, 'snr_best': None,
                                      'rssi_best': None, 'hops_min': None, 'name': None, 'lat': None,
                                      'lon': None, 'first': now}
            if frm not in self._known_at_start and frm not in self.foreign:
                with self._lock:
                    self.foreign[frm] = now
        n['n'] += 1
        n['last'] = now
        if hops is not None:
            n['hops_min'] = hops if n['hops_min'] is None else min(n['hops_min'], hops)
        if hops == 0:
            # The SNR of a relayed packet is the relay's, not the sender's:
            # only direct packets say how well this sender is heard.
            n['direct'] += 1
            snr, rssi = packet.get('rxSnr'), packet.get('rxRssi')
            if isinstance(snr, (int, float)):
                n['snr_sum'] += float(snr)
                n['n_snr'] += 1
                n['snr_best'] = snr if n['snr_best'] is None else max(n['snr_best'], snr)
            if isinstance(rssi, int) and rssi < 0:
                n['rssi_best'] = rssi if n['rssi_best'] is None else max(n['rssi_best'], rssi)
        user = dec.get('user') or {}
        if user.get('longName'):
            n['name'] = str(user['longName'])[:60]
        pos = dec.get('position') or {}
        lat, lon = pos.get('latitude'), pos.get('longitude')
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not _is_placeholder(lat, lon):
            n['lat'], n['lon'] = round(lat, 5), round(lon, 5)

    @staticmethod
    def _summary(st):
        """What a browser gets for one step: counts, not every packet."""
        nodes = st.get('nodes') or {}
        cu, air = st.get('cu') or [], st.get('air') or []
        first, last = st.get('ls_first'), st.get('ls_last')
        rf = None
        if last:
            # Counters run from the radio's last boot. When the step began with a
            # reboot they cover the step; a delta is used when two reports came in.
            src = last
            if first is not None and first is not last and st.get('ls_count', 0) > 1:
                src = {k: last.get(k, 0) - first.get(k, 0) for k in ('numPacketsRx', 'numPacketsRxBad', 'numRxDupe')}
                src.update({k: last.get(k) for k in ('numOnlineNodes', 'numTotalNodes')})
            rx, bad = src.get('numPacketsRx') or 0, src.get('numPacketsRxBad') or 0
            rf = {'rx': rx, 'bad': bad, 'dupe': src.get('numRxDupe'), 'online': src.get('numOnlineNodes'),
                  'total': src.get('numTotalNodes'),
                  'success_pct': round(100.0 * rx / (rx + bad), 1) if (rx + bad) else None}
        direct = sum(1 for n in nodes.values() if n.get('direct'))
        return {'preset': st.get('preset'), 'started': st.get('started'), 'ended': st.get('ended'),
                'packets': st.get('packets', 0), 'nodes': len(nodes), 'direct': direct,
                'mesh': len(nodes) - direct, 'ports': st.get('ports') or {},
                'messages': (st.get('ports') or {}).get('TEXT_MESSAGE_APP', 0),
                'sensor': (st.get('ports') or {}).get('TELEMETRY_APP', 0),
                'encrypted': (st.get('ports') or {}).get('ENCRYPTED', 0),
                'channel_util': round(sum(cu) / len(cu), 2) if cu else None,
                'air_util_tx': round(sum(air) / len(air), 3) if air else None,
                'rf': rf}

    # ── radio access ────────────────────────────────────────────────────

    @staticmethod
    def _iface(m):
        if m is None:
            return None
        try:
            if m.connection_type == 'serial':
                si = getattr(m, '_serial_iface', None)
                return si.iface if si is not None else None
            return getattr(m, '_tcp_iface', None)
        except Exception:
            return None

    @staticmethod
    def _lora(iface):
        try:
            return iface.localNode.localConfig.lora
        except Exception:
            return None

    @staticmethod
    def _mqtt(iface):
        try:
            return iface.localNode.moduleConfig.mqtt
        except Exception:
            return None

    @staticmethod
    def _preset_name(num):
        try:
            for k, v in _preset_enum().items():
                if v == int(num):
                    return k
        except Exception:
            pass
        return str(num)

    def _cannot_scan(self, m, lora):
        if m is None or lora is None:
            return 'The radio is not connected.'
        if getattr(m, 'connection_type', None) not in ('serial', 'tcp'):
            return 'Preset scan needs a serial or TCP connection.'
        if not lora.use_preset:
            return ('The radio uses custom LoRa settings (bandwidth, spreading factor) instead of '
                    'a preset. A scan could not put them back exactly, so it is not offered.')
        if not lora.region:
            return 'The radio has no region set.'
        return None

    def _base_cmd(self, m):
        if m.connection_type == 'serial':
            port = self._state.get('port') or m.port
            si = getattr(m, '_serial_iface', None)
            if not port and si is not None and si.iface is not None:
                port = getattr(si.iface, 'devPath', None)
            if not port:
                raise RuntimeError('serial port unknown')
            return [m.meshtastic_cmd, '--port', port]
        return [m.meshtastic_cmd, '--host', m.host]

    def _cli(self, pairs):
        """Write settings with the meshtastic CLI, as the Config page does."""
        m = self._mapper()
        if m is None:
            raise RuntimeError('no mapper')
        args = self._base_cmd(m)
        for k, v in pairs:
            args += ['--set', k, str(v)]
        if self._cli_runner is not None:
            return self._cli_runner(args, m)
        hold = getattr(m, '_port_hold', None)
        if hold is not None:
            hold.set()                               # the listener waits instead of grabbing the port
        try:
            if m.connection_type == 'serial' and getattr(m, '_serial_iface', None) is not None:
                m._serial_iface.disconnect()
                m._serial_iface = None
                time.sleep(2)
            m._cli_reboot_pending = True
            print(f"[SCAN] Running: {' '.join(args)}")
            r = subprocess.run(args, capture_output=True, text=True, timeout=CLI_TIMEOUT)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or 'meshtastic CLI failed').strip()[-300:])
        finally:
            if hold is not None:
                hold.clear()
            m._force_reconnect = True

    def _wait_applied(self, preset, tx_enabled, old_iface):
        """Wait for the listener to reconnect and the radio to report the wanted settings."""
        want = _preset_enum().get(preset)
        deadline = time.time() + SWITCH_TIMEOUT
        while time.time() < deadline:
            if self._halt.is_set() and self._state.get('status') not in ('restoring', 'restore_retry'):
                return False
            iface = self._iface(self._mapper())
            lora = self._lora(iface)
            if iface is not None and iface is not old_iface and lora is not None:
                try:
                    if lora.modem_preset == want and bool(lora.tx_enabled) == bool(tx_enabled):
                        return True
                except Exception:
                    pass
            self._sleep(2)
        return False

    def _apply(self, preset, scanning):
        """Move the radio to a preset: listen-only for a scan step, original settings for the restore."""
        orig = self._state['original']
        if scanning:
            pairs = [('lora.modem_preset', preset), ('lora.use_preset', 'true'), ('lora.tx_enabled', 'false'),
                     ('lora.channel_num', 0), ('lora.override_frequency', 0)]
            if orig.get('mqtt_enabled') and not self._state.get('mqtt_off'):
                pairs.append(('mqtt.enabled', 'false'))
        else:
            o = orig['lora']
            pairs = [('lora.modem_preset', o['modem_preset']), ('lora.use_preset', 'true'),
                     ('lora.tx_enabled', 'true' if o['tx_enabled'] else 'false'),
                     ('lora.channel_num', o['channel_num']), ('lora.override_frequency', o['override_frequency'])]
            if orig.get('mqtt_enabled'):
                pairs.append(('mqtt.enabled', 'true'))
        old = self._iface(self._mapper())
        self._cli(pairs)
        if scanning and orig.get('mqtt_enabled'):
            self._state['mqtt_off'] = True           # written once; the restore turns it back on
            self._save()
        tx = False if scanning else bool(orig['lora']['tx_enabled'])
        if not self._wait_applied(preset, tx, old):
            raise RuntimeError(f'the radio did not come back on {preset} in {SWITCH_TIMEOUT} s')

    # ── control ─────────────────────────────────────────────────────────

    def start(self, presets, dwell_s):
        with self._lock:
            if self.capturing:
                return {'ok': False, 'error': 'A scan is already running.'}
            m = self._mapper()
            iface = self._iface(m)
            lora = self._lora(iface)
            problem = self._cannot_scan(m, lora)
            if problem:
                return {'ok': False, 'error': problem}
            region = _region_enum().get(lora.region, str(lora.region))
            allowed = available_presets(region)
            presets = [p for p in (presets or []) if p in allowed]
            seen = set()
            presets = [p for p in presets if not (p in seen or seen.add(p))]
            if not presets:
                return {'ok': False, 'error': 'Pick at least one preset this region can use.'}
            try:
                dwell = int(dwell_s)
            except (TypeError, ValueError):
                dwell = 300
            dwell = max(DWELL_MIN_S, min(DWELL_MAX_S, dwell))
            mq = self._mqtt(iface)
            port = None
            if m.connection_type == 'serial':
                port = m.port or getattr(iface, 'devPath', None)
            # The original settings go to disk before anything changes.
            self._state = {
                'id': time.strftime('%Y%m%d-%H%M%S'), 'status': 'starting', 'presets': presets,
                'dwell_s': dwell, 'idx': -1, 'started': _now(), 'ended': None, 'step_started': None,
                'current': None, 'error': None, 'region': region, 'port': port,
                'original_preset': self._preset_name(lora.modem_preset),
                'original': {'lora': {'modem_preset': self._preset_name(lora.modem_preset),
                                      'tx_enabled': bool(lora.tx_enabled),
                                      'channel_num': int(lora.channel_num),
                                      'override_frequency': float(lora.override_frequency)},
                             'mqtt_enabled': bool(mq.enabled) if mq is not None else False},
                'steps': [],
            }
            self._known_at_start = set(getattr(m, 'nodes', {}) or {}) | set(getattr(m, 'nodes_no_position', {}) or {})
            self._halt.clear()
            self._save()
            print(f"[SCAN] Starting: {', '.join(presets)}, {dwell} s each, "
                  f"radio back to {self._state['original_preset']} afterwards")
            self._thread = threading.Thread(target=self._run, name='preset-scan', daemon=True)
            self._thread.start()
            self._push(force=True)
            return {'ok': True, 'id': self._state['id']}

    def stop(self):
        if not self.capturing:
            return {'ok': False, 'error': 'No scan is running.'}
        print("[SCAN] Stop requested, putting the radio back")
        self._halt.set()
        return {'ok': True}

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)
        self._save()
        self._push(force=True)

    def _push(self, force=False):
        if not force and time.time() - self._last_status < STATUS_EVERY_S:
            return
        self._last_status = time.time()
        try:
            self._broadcast({'type': 'preset_scan_status', **self.status()})
        except Exception as e:
            print(f"[SCAN] Status broadcast failed: {e}")

    def _run(self):
        outcome, error = 'done', None
        try:
            for i, preset in enumerate(self._state['presets']):
                if self._halt.is_set():
                    outcome = 'cancelled'
                    break
                self._set(status='switching', idx=i, current=preset, step_started=None)
                self._apply(preset, scanning=True)
                if self._halt.is_set():
                    outcome = 'cancelled'
                    break
                self._step = {'preset': preset, 'started': _now(), 'ended': None, 'packets': 0,
                              'nodes': {}, 'ports': {}, 'cu': [], 'air': [], 'ls_first': None,
                              'ls_last': None, 'ls_count': 0}
                self._set(status='listening', step_started=self._step['started'])
                end = time.time() + self._state['dwell_s']
                while time.time() < end and not self._halt.is_set():
                    self._sleep(1)
                    self._push()
                with self._lock:
                    self._step['ended'] = _now()
                    self._state['steps'].append(self._step)
                    self._step = None
                self._save()
                if self._halt.is_set():
                    outcome = 'cancelled'
                    break
        except Exception as e:
            if self._halt.is_set():
                outcome = 'cancelled'                # stopped while the radio was switching
            else:
                outcome, error = 'failed', str(e)
                print(f"[SCAN] Scan failed: {e}")
        finally:
            with self._lock:
                if self._step is not None:
                    self._step['ended'] = _now()
                    self._state['steps'].append(self._step)
                    self._step = None
            self._restore_until_done(outcome, error)

    def _restore_until_done(self, outcome='interrupted', error=None):
        """Put the radio back. Tried until it works: a radio left listen-only on
        another preset is worse than a scan that failed."""
        attempt = 0
        while True:
            attempt += 1
            self._set(status='restoring' if attempt == 1 else 'restore_retry', current=None,
                      error=error if attempt == 1 else self._state.get('error'))
            try:
                self._apply(self._state['original']['lora']['modem_preset'], scanning=False)
                break
            except Exception as e:
                msg = f'Could not put the radio back yet (attempt {attempt}): {e}'
                print(f"[SCAN] {msg}")
                self._set(status='restore_retry', error=msg)
                self._sleep(RESTORE_RETRY_S)
        print(f"[SCAN] Radio back on {self._state['original']['lora']['modem_preset']}, scan {outcome}")
        with self._lock:
            self._state['status'] = outcome
            self._state['ended'] = _now()
            self._state['error'] = error
            self._state.pop('mqtt_off', None)
        self._save()
        self._archive()
        self._push(force=True)

    def resume(self):
        """At start-up: a scan that was running when MeshPulse stopped is not
        continued, but the radio is put back. Waits for the radio to connect."""
        if not self.capturing:
            return
        print(f"[SCAN] MeshPulse restarted during a scan ({self._state.get('status')}), "
              f"restoring the radio to {self._state.get('original_preset')}")

        def run():
            deadline = time.time() + 600
            while self._lora(self._iface(self._mapper())) is None and time.time() < deadline:
                self._sleep(2)
            self._restore_until_done('interrupted', 'MeshPulse restarted during the scan')

        self._thread = threading.Thread(target=run, name='preset-scan-restore', daemon=True)
        self._thread.start()

    # ── history ─────────────────────────────────────────────────────────

    def _archive(self):
        """Keep the finished scan in stats.db: its steps and every node heard."""
        db = self._stats_db()
        if db is None:
            return
        s = self._state
        record = {k: s.get(k) for k in ('id', 'status', 'presets', 'dwell_s', 'started', 'ended', 'error',
                                         'region', 'original_preset')}
        record['steps'] = []
        names = {}
        m = self._mapper()
        for d in (getattr(m, 'nodes', {}) or {}, getattr(m, 'nodes_no_position', {}) or {}):
            for nid, n in d.items():
                if n.get('name') and n.get('name') != nid:
                    names[nid] = n['name']
        for st in s.get('steps') or []:
            summary = self._summary(st)
            nodes = []
            for nid, n in (st.get('nodes') or {}).items():
                nodes.append({'id': nid, 'name': n.get('name') or names.get(nid) or nid, 'n': n['n'],
                              'direct': n['direct'], 'hops_min': n['hops_min'],
                              'snr': round(n['snr_sum'] / n['n_snr'], 1) if n['n_snr'] else None,
                              'snr_best': n['snr_best'], 'rssi_best': n['rssi_best'],
                              'lat': n['lat'], 'lon': n['lon'], 'known': nid in self._known_at_start})
            nodes.sort(key=lambda x: (-x['direct'], -x['n']))
            summary['node_list'] = nodes
            record['steps'].append(summary)
        try:
            with db.lock:
                conn = db._connect()
                conn.execute('''CREATE TABLE IF NOT EXISTS preset_scans (
                                  id TEXT PRIMARY KEY, started INTEGER, ended INTEGER,
                                  status TEXT, data TEXT)''')
                conn.execute('INSERT OR REPLACE INTO preset_scans (id, started, ended, status, data) VALUES (?,?,?,?,?)',
                             (record['id'], record['started'], record['ended'], record['status'],
                              json.dumps(record, ensure_ascii=False)))
                conn.execute('''DELETE FROM preset_scans WHERE id NOT IN
                                (SELECT id FROM preset_scans ORDER BY started DESC LIMIT ?)''', (HISTORY_KEEP,))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[SCAN] Could not archive the scan: {e}")

    def history(self, scan_id=None):
        """Finished scans: a list without node lists, or one scan in full."""
        db = self._stats_db()
        if db is None:
            return []
        try:
            with db.lock:
                conn = db._connect()
                conn.execute('''CREATE TABLE IF NOT EXISTS preset_scans (
                                  id TEXT PRIMARY KEY, started INTEGER, ended INTEGER,
                                  status TEXT, data TEXT)''')
                if scan_id:
                    rows = conn.execute('SELECT data FROM preset_scans WHERE id = ?', (str(scan_id),)).fetchall()
                else:
                    rows = conn.execute('SELECT data FROM preset_scans ORDER BY started DESC LIMIT ?',
                                        (HISTORY_KEEP,)).fetchall()
                conn.close()
        except Exception as e:
            print(f"[SCAN] Could not read scan history: {e}")
            return []
        out = []
        for (data,) in rows:
            try:
                rec = json.loads(data)
            except ValueError:
                continue
            if not scan_id:
                for st in rec.get('steps') or []:
                    st.pop('node_list', None)
            out.append(rec)
        return out
