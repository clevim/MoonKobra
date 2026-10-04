"""
kobrax_client.py – LAN MQTT client for the Anycubic Kobra X

Protocol fully reconstructed with a sniffer on 2026-04-17 (953 messages).

Requirements:
  - The printer's own mTLS certificate (cert_provider) or, as a fallback,
    anycubic_slicer.crt/.key next to this file
  - Printer in LAN mode, reachable on port 9883

Usage:
  client = KobraXClient(env_loader.PRINTER_IP, mode_id=env_loader.MODE_ID,
                        device_id=env_loader.DEVICE_ID)
  client.connect()
  info = client.query_info()
  print(info["data"]["temp"])
  client.disconnect()

────────────────────────────────────────────────────────────────────────────
Copyright (C) 2026 viewit (KX-Bridge contributors)

Licensed under GPLv3 — see LICENSE at the project root.
Protocol obtained by reverse engineering for interoperability (§69e UrhG / EU Software
Directive Art. 6). Not affiliated with Anycubic. See NOTICE.md.
"""

import hashlib
import json
import logging
import os
import select
import socket
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime

import env_loader

log = logging.getLogger("kobrax.mqtt")

_SCRIPT_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(_SCRIPT_DIR, "anycubic_slicer.crt")
KEY_FILE  = os.path.join(_SCRIPT_DIR, "anycubic_slicer.key")


# ---------------------------------------------------------------------------
# Low-level MQTT framing
# ---------------------------------------------------------------------------

def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out.append(d)
        if n == 0:
            break
    return bytes(out)


def _build_connect(client_id: str, username: str, password: str) -> bytes:
    proto = b"\x00\x04MQTT\x04"
    ka    = b"\x00\x3c"           # keepalive = 60s
    flags = 0xC2                  # username + password, clean session
    payload = _enc_str(client_id) + _enc_str(username) + _enc_str(password)
    body = proto + bytes([flags]) + ka + payload
    return bytes([0x10]) + _enc_len(len(body)) + body


def _build_subscribe(topic: str, pid: int) -> bytes:
    p = pid.to_bytes(2, "big") + _enc_str(topic) + b"\x00"
    return bytes([0x82]) + _enc_len(len(p)) + p


def _build_publish(topic: str, payload: str) -> bytes:
    body = _enc_str(topic) + payload.encode("utf-8")
    return bytes([0x30]) + _enc_len(len(body)) + body


def _build_pingreq() -> bytes:
    return bytes([0xC0, 0x00])


def _parse_publish(pkt: bytes):
    if len(pkt) < 2:
        return None, None
    tlen = (pkt[0] << 8) | pkt[1]
    if 2 + tlen > len(pkt):
        return None, None
    topic   = pkt[2:2 + tlen].decode("utf-8", errors="replace")
    payload = pkt[2 + tlen:]
    return topic, payload


def _enable_tcp_keepalive(sock: socket.socket) -> None:
    """Without this, a printer that disappears without closing TCP cleanly (e.g.
    unplugged, no orderly shutdown) leaves the socket looking alive to
    is_connected() for the OS's default dead-connection timeout
    (often 15+ minutes on Linux) - sendall() on a half-open connection is
    buffered by the kernel and does not fail right away, so the is_connected()
    check of the poll loop (_poll_loop in kobrax_moonraker_bridge.py) never
    sees the failure it needs to switch kobra_state to "offline". Short
    keepalive probes make the OS notice and drop the socket within seconds.
    Linux/macOS only (TCP_KEEPIDLE/INTVL/CNT); best effort on other
    platforms - not fatal if unsupported.

    SO_KEEPALIVE alone is NOT enough, verified live by unplugging a real
    printer mid-connection: keepalive probes only fire while the
    connection is idle (no unacknowledged data pending). If the printer vanishes with a
    send still in flight - the common case, since the poll loop
    sends a request roughly every poll_interval - the kernel retries
    that send on the normal TCP retransmission timer
    (tcp_retries2, default 15 attempts with exponential backoff = 13-30+
    minutes on Linux), which the keepalive settings do not affect.
    TCP_USER_TIMEOUT (Linux-specific) closes that gap: it caps how long
    ANY unacknowledged data may sit in the send queue before the kernel gives up
    on the connection, whichever mechanism (keepalive or retransmission)
    would still be trying."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
        elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 5)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 15000)
    except OSError as e:
        log.debug("TCP keepalive is not fully supported on this platform: %s", e)


# ---------------------------------------------------------------------------
# KobraXClient
# ---------------------------------------------------------------------------

class KobraXClient:
    def __init__(self, host: str, username: str, password: str,
                 mode_id: str, device_id: str,
                 port: int = 9883, client_id: str = "kobrax_py",
                 cert_provider=None):
        self.host      = host
        self.port      = port
        self.username  = username
        self.password  = password
        self.mode_id   = mode_id
        self.device_id = device_id
        self.client_id = client_id
        # cert_provider(refresh) -> (crt, key) | None: mTLS certificate that the printer
        # itself hands over (see _printer_cert_provider in the bridge). Without it, or if
        # it has nothing, anycubic_slicer.crt/.key next to this file are used.
        self.cert_provider = cert_provider

        self._sock    = None
        self._buf     = b""
        self._pid     = 1
        self._lock    = threading.Lock()
        # Generation marker: bumped on every socket swap/close so that
        # the reader thread notices when _reconnect/_do_connect replaced the socket
        # under it (Issue #53). Guards against recv on a stale fd.
        self._sock_gen = 0
        self._running = False
        # Guards _reconnect() against concurrent calls - both the reader
        # thread (keepalive ping failure) and publish()/publish_web() (send
        # failure) can trigger a reconnect independently. Without this, two
        # threads could enter _do_connect() at once, each opening its
        # own concurrent TLS handshake with a printer that most likely only accepts
        # one mTLS session at a time (Issue #105).
        self._reconnect_lock = threading.Lock()
        # Serializes _ensure_reader(): publish() calls it on every send,
        # connect() too. Without a lock, two threads see "no live
        # reader" at the same time and each starts one - two reader threads mean two
        # 30-second keepalives and two independent reconnect triggers.
        self._reader_lock = threading.Lock()

        # Pending requests by msgid (for the response ACK)
        self._pending_msgid: dict[str, dict] = {}
        # Pending requests by msg_type/report topic suffix
        self._pending_report: dict[str, dict] = {}
        # Guards _pending_msgid/_pending_report against concurrent mutation:
        # the reader thread resolves entries in _dispatch() while publish()
        # (called by the poll loop and, through run_in_executor, by HTTP
        # handler threads) registers/clears them - without this, two concurrent
        # publish() calls for the same msg_type could race on the
        # check-then-set of a report_key slot, and _dispatch() could see
        # a dict in the middle of a mutation.
        self._pending_lock = threading.Lock()

        # Optional callbacks: topic suffix → callable(payload_dict)
        self.callbacks: dict[str, callable] = {}
        # Monotonic time of the last received message (or the last CONNACK). The poll
        # loop sends queries without waiting for a reply and uses this to tell whether
        # the session is still alive.
        self.last_rx = time.monotonic()

        # Dedup: last hash per topic suffix to suppress repeated identical messages
        self._last_rx_hash: dict[str, str] = {}
        # Debug switch (MQTT_RAW_LOG=1): logs every RX message unfiltered at
        # INFO, including dedup duplicates and topics with no registered
        # callback - to capture printer behaviour the bridge normally
        # does not show (e.g. reverse engineering a rejected command).
        self._raw_log = os.environ.get("MQTT_RAW_LOG", "").strip().lower() in ("1", "true", "yes")
        # Fields that change every tick and must be stripped before the dedup hash
        _VOLATILE = {"timestamp", "msgid", "progress", "curr_layer",
                     "curr_nozzle_temp", "curr_hotbed_temp",
                     "target_nozzle_temp", "target_hotbed_temp"}

    # -- Topics --------------------------------------------------------------

    def _pub_topic(self, msg_type: str) -> str:
        return (f"anycubic/anycubicCloud/v1/slicer/printer/"
                f"{self.mode_id}/{self.device_id}/{msg_type}")

    def _web_topic(self, msg_type: str) -> str:
        return (f"anycubic/anycubicCloud/v1/web/printer/"
                f"{self.mode_id}/{self.device_id}/{msg_type}")

    def _sub_topic(self) -> str:
        return (f"anycubic/anycubicCloud/v1/printer/public/"
                f"{self.mode_id}/{self.device_id}/#")

    # -- Connection ----------------------------------------------------------

    def _cert_files(self, refresh: bool = False):
        files = self.cert_provider(refresh) if self.cert_provider else None
        if files:
            return files
        if refresh:
            return None
        if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
            return CERT_FILE, KEY_FILE
        raise FileNotFoundError(
            "No TLS certificate: the printer did not hand over its own (is it on and "
            "in LAN mode?) and there is no anycubic_slicer.crt/.key next to moonkobra."
        )

    def _do_connect(self):
        files = self._cert_files()
        try:
            self._connect_with(*files)
        except (ssl.SSLError, ConnectionResetError, RuntimeError) as e:
            # Certificate rejected (e.g. the printer generated a new one after a reset):
            # fetch a new one once. Timeouts/network down do not end up here.
            fresh = self._cert_files(refresh=True) if self.cert_provider else None
            if not fresh:
                raise
            log.info("Connection refused (%s) - retrying with a fresh certificate from the printer", e)
            self._connect_with(*fresh)

    def _connect_with(self, cert_file: str, key_file: str):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.load_cert_chain(cert_file, key_file)

        # Build the socket as a local variable - the handshake (connect + CONNACK)
        # runs WITHOUT holding the lock so a slow connect does not block
        # senders. Only the ready socket is swapped in under the lock (#53).
        _ai      = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        raw      = socket.create_connection(_ai[0][4], timeout=5)
        new_sock = None
        # Any failure from here up to CONNACK (handshake timeout, recv, CONNACK
        # refused) must close the socket: the printer only accepts one mTLS
        # session, and a half-open handshake left hanging takes that slot -
        # the next backoff attempts got "handshake operation timed out"
        # and each one leaked another fd.
        try:
            _enable_tcp_keepalive(raw)
            new_sock = ctx.wrap_socket(raw)
            log.info("TLS connected  cipher=%s", new_sock.cipher()[0])

            new_sock.sendall(_build_connect(self.client_id, self.username, self.password))
            new_sock.settimeout(3)
            r = new_sock.recv(64)
            if len(r) < 4 or r[0] != 0x20 or r[3] != 0:
                raise RuntimeError(f"CONNACK failed: {r.hex()}")
        except Exception:
            try:
                (new_sock or raw).close()
            except Exception:
                pass
            raise
        log.info("CONNACK rc=0")
        self.last_rx = time.monotonic()

        new_sock.settimeout(0.2)
        with self._lock:
            self._sock = new_sock
            self._sock_gen += 1
            self._buf = b""
        self._subscribe(self._sub_topic())  # takes the lock itself - do not nest
        log.debug("MQTT connected to %s:%s", self.host, self.port)

    def connect(self):
        """Establishes the MQTT session.

        Goes through the same _reconnect_lock as _reconnect(): the printer
        only accepts one mTLS session, so a second handshake kills the one
        in progress. Previously only _reconnect() took the lock - connect()
        bypassed it through three other paths (startup, coming back online in the
        poll loop, the "Connect" button in the UI) and so raced against
        the reader thread's reconnect. That is exactly what produces the log signature of
        two interleaved attempt counters and a "handshake operation
        timed out" that the bridge never recovers from without restarting the
        container.

        Deliberately does NOT wait for a reconnect in progress: the caller (poll
        loop, HTTP handler) must not stay blocked for a whole
        outage. If a reconnect is already running, we leave the field to it.
        """
        if not self._reconnect_lock.acquire(blocking=False):
            self._running = True
            self._ensure_reader()
            if self.is_connected():
                return
            raise ConnectionError("reconnect already in progress")
        try:
            self._do_connect()
            self._running = True
        finally:
            self._reconnect_lock.release()
        self._ensure_reader()
        time.sleep(0.3)

    def _ensure_reader(self):
        """Makes sure the reader thread is alive. If the reader died after
        an earlier disconnect/reconnect sequence or an unhandled error,
        replies would never arrive - publish()
        would keep sending, but wait for replies forever."""
        if not self._running:
            return  # disconnect intencional
        with self._reader_lock:
            t = getattr(self, "_reader_thread", None)
            if t is not None and t.is_alive():
                return
            self._reader_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="kobrax-mqtt-reader",
            )
            self._reader_thread.start()

    def disconnect(self):
        self._running = False
        with self._lock:
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._sock_gen += 1

    def is_connected(self) -> bool:
        """Thread-safe check whether the MQTT socket is up right now. Used by the
        bridge's poll loop to detect a dead session even when publish()
        already swallowed the send failure and returned None instead of
        raising (Issue #105) - a printer reachable over TCP does not mean the
        MQTT/TLS session is still alive."""
        with self._lock:
            return self._sock is not None

    def _reconnect(self, wait_if_in_progress: bool = True, persist: bool = True):
        """Reconnects the MQTT/TLS session. With persist=True (the default, used
        by the reader thread's keepalive path) it keeps trying until
        the printer answers or disconnect() is called, backoff capped at 60s.
        The first 5 attempts log as WARNING (acute connection problem), after that
        only DEBUG to avoid log spam during long printer outages (e.g. switched off).

        Guarded by _reconnect_lock (Issue #105): if another thread's reconnect
        is already in progress, this call normally waits for it to finish instead
        of starting a second concurrent _do_connect() - the printer most likely only
        accepts one mTLS session at a time, so two parallel handshakes would only
        get in each other's way and neither would converge.

        wait_if_in_progress=False + persist=False are used by the
        poll loop's publish()/publish_web(): that thread MUST return quickly so the
        poll loop can notice the dead session (through is_connected()) and switch
        kobra_state to "offline". It may neither block on the lock waiting for the
        reader thread's persistent reconnect (wait_if_in_progress=False),
        nor run the multi-minute backoff loop itself (persist=False -> at most
        one immediate attempt). Otherwise the poll loop hangs inside publish()
        for the whole outage and the dashboard stays stuck on the last known
        state - exactly the bug seen when a printer was switched off mid-connection."""
        if not self._reconnect_lock.acquire(blocking=False):
            if not wait_if_in_progress:
                return self._sock is not None
            self._reconnect_lock.acquire()
            self._reconnect_lock.release()
            return self._sock is not None
        try:
            log.warning("Connection lost - reconnecting...")
            # Close + invalidate under the lock so no sender in the middle of a sendall
            # runs into the freshly closed socket (Issue #53).
            with self._lock:
                try:
                    if self._sock is not None:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
                self._sock_gen += 1
            delays = [2, 4, 8, 15, 30, 60]
            attempt = 0
            while self._running:
                delay = delays[min(attempt, len(delays) - 1)]
                try:
                    self._do_connect()
                    log.info("Reconnected (after %d attempts)", attempt + 1)
                    return True
                except Exception as e:
                    attempt += 1
                    if not persist:
                        # Single attempt: do not block the caller (poll loop) in the
                        # backoff loop - leave the persistent attempts to the
                        # reader thread's keepalive path.
                        log.debug("Reconnect (single attempt) failed: %s", e)
                        return False
                    lvl = log.warning if attempt <= 5 else log.debug
                    lvl("Reconnect failed (%s, attempt %d), waiting %ss…", e, attempt, delay)
                    # Sliced sleep so disconnect() leaves the loop faster.
                    slept = 0.0
                    while slept < delay and self._running:
                        time.sleep(min(0.5, delay - slept))
                        slept += 0.5
            return False  # only when disconnect() was called
        finally:
            self._reconnect_lock.release()

    def _subscribe(self, topic: str):
        with self._lock:
            pid = self._pid
            # MQTT packet IDs are a 16-bit field (1-65535, 0 reserved) - wrap around
            # instead of growing without bound, otherwise a long-running bridge with
            # frequent reconnects ends up overflowing pid.to_bytes(2, "big")
            # (OverflowError: int too big to convert), breaking every future
            # connection attempt, including the manual "Connect" button.
            self._pid = 1 if self._pid >= 0xFFFF else self._pid + 1
            if self._sock is not None:
                self._sock.sendall(_build_subscribe(topic, pid))
        log.info("SUB %s", topic)

    # -- Read loop -----------------------------------------------------------

    def _read_loop(self):
        last_ping = time.time()
        _empty_count = 0
        while self._running:
            if time.time() - last_ping > 30:
                ping_ok = False
                with self._lock:
                    try:
                        if self._sock is not None:
                            self._sock.sendall(_build_pingreq())
                            ping_ok = True
                    except Exception:
                        ping_ok = False
                # Call _reconnect() OUTSIDE the lock - it takes the lock
                # itself, and threading.Lock is not reentrant (otherwise, deadlock).
                if not ping_ok:
                    if self._running and not self._reconnect():
                        break
                last_ping = time.time()
            # Grab the current socket + generation under the lock so a
            # parallel _reconnect/_do_connect swap does not leave us polling
            # a stale fd (Issue #53).
            with self._lock:
                sock = self._sock
                gen  = self._sock_gen
            if sock is None:
                time.sleep(0.05)
                continue

            # Idle wait WITHOUT the lock - select only probes readiness, so
            # the reader never holds the shared lock while idle.
            try:
                ready, _, _ = select.select([sock], [], [], 0.2)
            except (OSError, ValueError):
                # fd closed/invalid (reconnect or disconnect in the middle of select)
                if not self._running:
                    break
                time.sleep(0.05)
                continue
            if not ready:
                continue  # idle, no lock

            # Data pending: take the lock briefly just for the recv, serialized
            # against every sendall caller. The recv barely blocks (select said
            # ready, the socket timeout is 0.2s).
            try:
                with self._lock:
                    # The socket may have been swapped between select and here.
                    if self._sock_gen != gen or self._sock is not sock:
                        continue
                    data = sock.recv(65536)
                if not data:
                    # SSL on Windows may briefly return b"" without a real EOF
                    _empty_count += 1
                    if _empty_count >= 5:
                        raise ConnectionResetError("EOF")
                    continue
                _empty_count = 0
                self._buf += data
                self._drain()  # outside the lock - dispatch/event.set() stays snappy
            except ssl.SSLWantReadError:
                continue
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.warning("reader error: %s", e)
                    if not self._reconnect():
                        break
                    last_ping = time.time()
                else:
                    break

    def _drain(self):
        buf = self._buf
        idx = 0
        try:
            while idx < len(buf):
                ptype = buf[idx] & 0xF0
                i = idx + 1
                mul = 1
                rem = 0
                while i < len(buf):
                    b = buf[i]
                    rem += (b & 0x7F) * mul
                    mul *= 128
                    i += 1
                    if not (b & 0x80):
                        break
                if i + rem > len(buf):
                    break
                pkt = buf[i:i + rem]
                idx = i + rem

                if ptype == 0x30:
                    topic, raw_payload = _parse_publish(pkt)
                    if topic is None:
                        continue
                    try:
                        payload = json.loads(raw_payload)
                    except Exception:
                        payload = {"_raw": raw_payload.decode("utf-8", errors="replace")}
                    try:
                        self._dispatch(topic, payload)
                    except Exception as e:
                        # A single malformed/unexpected message (e.g. valid JSON
                        # that is not an object, such as a bare number or list) must not
                        # be reprocessed forever: without this, an exception
                        # here would skip the buffer advance below, leaving the
                        # same bad packet at the start of self._buf, so every
                        # future _drain() call would break on it again - each
                        # forcing a reconnect through the read loop's exception handler,
                        # an endless reconnect loop of our own making.
                        log.warning("dispatch error for %s: %s", topic, e)
        finally:
            self._buf = buf[idx:]

    def _dedup_hash(self, suffix: str, payload: dict) -> str:
        """Payload hash ignoring each tick's volatile fields, for dedup."""
        stable = {k: v for k, v in payload.items()
                  if k not in {"timestamp", "msgid", "progress", "curr_layer",
                               "curr_nozzle_temp", "curr_hotbed_temp",
                               "target_nozzle_temp", "target_hotbed_temp"}}
        return hashlib.md5(json.dumps(stable, sort_keys=True).encode(), usedforsecurity=False).hexdigest()

    def _dispatch(self, topic: str, payload: dict):
        self.last_rx = time.monotonic()
        if not isinstance(payload, dict):
            log.warning("dispatch: non-dict payload on %s: %r", topic, payload)
            return
        suffix = "/".join(topic.split("/")[-2:])

        if self._raw_log:
            log.info("RX [raw] %s  %s", topic, json.dumps(payload, ensure_ascii=False))

        # Structured RX log with duplicate suppression
        h = self._dedup_hash(suffix, payload)
        is_dup = self._last_rx_hash.get(suffix) == h
        self._last_rx_hash[suffix] = h
        if is_dup:
            log.debug("RX [dup] %-25s  state=%-12s", suffix, payload.get("state", ""))
        else:
            data = payload.get("data") or {}
            state = payload.get("state", "")
            if "progress" in data:
                log.info("RX %-25s  state=%-12s  progress=%s%%  layer=%s/%s",
                         suffix, state, data["progress"],
                         data.get("curr_layer", "?"), data.get("total_layers", "?"))
            elif "curr_nozzle_temp" in data:
                log.info("RX %-25s  nozzle=%s°C/%s°C  bed=%s°C/%s°C",
                         suffix,
                         data["curr_nozzle_temp"], data.get("target_nozzle_temp", 0),
                         data.get("curr_hotbed_temp", "?"), data.get("target_hotbed_temp", 0))
            else:
                log.info("RX %-25s  state=%-12s  data=%s",
                         suffix, state, json.dumps(payload.get("data"), ensure_ascii=False))

        msgid = payload.get("msgid")
        with self._pending_lock:
            report_entry = self._pending_report.get(suffix)
            msgid_entry  = self._pending_msgid.get(msgid) if msgid else None

        # Resolve by the report topic suffix (e.g. "info/report"). If the payload
        # carries a msgid that does not match what this waiter actually expects,
        # it is a stale/late reply to another request that already timed out
        # and happens to share the same report_key - do not deliver it
        # to the wrong caller.
        if report_entry is not None:
            entry_msgid = report_entry.get("msgid")
            if not entry_msgid or not msgid or entry_msgid == msgid:
                report_entry["result"] = payload
                report_entry["event"].set()
            else:
                log.debug("dispatch: msgid mismatch for report %s (expected=%s, got=%s) - ignoring stale reply",
                           suffix, entry_msgid, msgid)

        # Resolve by msgid (for the generic response ACK)
        if msgid_entry is not None:
            msgid_entry["result"] = payload
            msgid_entry["event"].set()

        # User callbacks by topic suffix (the last two path components)
        if suffix in self.callbacks:
            try:
                self.callbacks[suffix](payload)
            except Exception as e:
                log.error("callback error for %s: %s", suffix, e)

        # Generic wildcard callback
        if "*" in self.callbacks:
            try:
                self.callbacks["*"](topic, payload)
            except Exception as e:
                log.error("wildcard callback error: %s", e)

    # -- Publish + request/response -------------------------------------------

    def publish(self, msg_type: str, action: str, data=None, timeout: float = 5.0) -> dict | None:
        # If the reader thread died for historical reasons, revive it -
        # otherwise replies would never arrive and event.wait() would time out.
        self._ensure_reader()
        msgid   = str(uuid.uuid4())
        payload = json.dumps({
            "type":      msg_type,
            "action":    action,
            "msgid":     msgid,
            "timestamp": int(time.time() * 1000),
            "data":      data,
        }, separators=(",", ":"))

        # Wait on msgid only — avoids collisions when several threads
        # call publish() for the same msg_type at the same time.
        # Also registers by the report topic as a fallback for replies without a msgid.
        report_key = f"{msg_type}/report"
        event  = threading.Event()
        # the entry carries its own msgid so the report-suffix path in _dispatch()
        # confirms the reply REALLY belongs to this request before delivering it
        # - without that, a late reply to a request A that already timed out
        # could be delivered to a newer request B waiting on the same report_key.
        entry  = {"event": event, "result": None, "msgid": msgid}
        report_registered = False
        with self._pending_lock:
            self._pending_msgid[msgid] = entry
            # Only register a report-key waiter if nobody else is waiting on it
            if report_key not in self._pending_report:
                self._pending_report[report_key] = entry
                report_registered = True

        topic = self._pub_topic(msg_type)
        # Status polling TX (query/getInfo) is pure noise (every few seconds) ->
        # goes to DEBUG. Action TX (start/set/control/move/…) stays visible at INFO.
        _tx_level = logging.DEBUG if action in ("query", "getInfo") else logging.INFO
        log.log(_tx_level, "TX %-25s  action=%-12s  data=%s",
                f"{msg_type}/request", action,
                json.dumps(data, ensure_ascii=False) if data else "null")
        try:
            with self._lock:
                if self._sock is None:
                    raise ConnectionError("not connected")
                self._sock.sendall(_build_publish(topic, payload))
        except Exception as e:
            log.error("send error: %s, reconnecting…", e)
            with self._pending_lock:
                self._pending_msgid.pop(msgid, None)
                if report_registered:
                    self._pending_report.pop(report_key, None)
            # Non-blocking: never hang the poll loop thread inside publish()
            # while a reconnect runs / during backoff (see the _reconnect
            # docstring) - it must return so kobra_state can turn "offline".
            if not self._reconnect(wait_if_in_progress=False, persist=False):
                return None
            # retry once after the reconnect
            try:
                with self._lock:
                    if self._sock is None:
                        raise ConnectionError("not connected")
                    self._sock.sendall(_build_publish(topic, payload))
                with self._pending_lock:
                    self._pending_msgid[msgid] = entry
                    if report_registered:
                        self._pending_report[report_key] = entry
            except Exception:
                return None

        if timeout <= 0:
            with self._pending_lock:
                self._pending_msgid.pop(msgid, None)
                if report_registered:
                    self._pending_report.pop(report_key, None)
            return None

        received = event.wait(timeout)
        with self._pending_lock:
            self._pending_msgid.pop(msgid, None)
            if report_registered:
                self._pending_report.pop(report_key, None)
        if not received:
            return None
        return entry["result"]

    def publish_web(self, msg_type: str, action: str, data=None) -> None:
        """Publish without waiting for a reply on the web/printer topic (used for runtime updates while printing)."""
        self._ensure_reader()
        msgid   = str(uuid.uuid4())
        payload = json.dumps({
            "type":      msg_type,
            "action":    action,
            "msgid":     msgid,
            "timestamp": int(time.time() * 1000),
            "data":      data,
        }, separators=(",", ":"))
        topic = self._web_topic(msg_type)
        log.info("TX(web) %-23s  action=%-12s  data=%s",
                 f"{msg_type}/request", action,
                 json.dumps(data, ensure_ascii=False) if data else "null")
        try:
            with self._lock:
                if self._sock is None:
                    raise ConnectionError("not connected")
                self._sock.sendall(_build_publish(topic, payload))
        except Exception as e:
            log.error("send error (web): %s, reconnecting…", e)
            # Triggers a reconnect (like publish()); no retry because it is
            # fire-and-forget - the next call already picks up the new socket.
            # Non-blocking for the same reason as publish() (see the _reconnect
            # docstring) - never hang this thread in a backoff loop.
            try:
                self._reconnect(wait_if_in_progress=False, persist=False)
            except Exception:
                pass

    # -- High-level commands --------------------------------------------------

    def query_info(self) -> dict | None:
        return self.publish("info", "query")

    def query_status(self) -> dict | None:
        return self.publish("status", "query")

    def query_multicolor_box(self) -> dict | None:
        return self.publish("multiColorBox", "getInfo")

    def set_temperature(self, nozzle: int, bed: int) -> dict | None:
        return self.publish("tempature", "set",
                            {"target_nozzle_temp": nozzle, "target_hotbed_temp": bed})

    def set_fan(self, pct: int) -> dict | None:
        return self.publish("fan", "set", {"fan_speed_pct": pct})

    def set_light(self, on: bool, brightness: int = 80) -> dict | None:
        return self.publish("light", "control",
                            {"type": 2, "status": 1 if on else 0, "brightness": brightness})

    def start_camera(self) -> dict | None:
        return self.publish("video", "startCapture")

    def stop_camera(self) -> dict | None:
        return self.publish("video", "stopCapture")

    def pause_print(self, taskid: str = "-1") -> dict | None:
        return self.publish("print", "pause", {"taskid": taskid})

    def resume_print(self, taskid: str = "-1") -> dict | None:
        return self.publish("print", "resume", {"taskid": taskid})

    def stop_print(self, taskid: str = "-1") -> dict | None:
        return self.publish("print", "stop", {"taskid": taskid})

    # -- Skip part ("Exclude Object") ----------------------------------------

    def query_skip_objects(self) -> dict | None:
        """Asks the printer for the current object/skipped list."""
        return self.publish("skip", "query_obj")

    def skip_objects(self, names: list[str]) -> dict | None:
        """Skips the objects with these names - also works while printing.

        The names match the EXCLUDE_OBJECT_DEFINE NAME=... entries
        in the G-code header or in file_details.objects_skip_parts.
        """
        return self.publish("skip", "start", {"objects_skip_parts": list(names)})

    # -- G-code upload -------------------------------------------------------

    def upload_gcode(self, filepath: str, remote_filename: str | None = None,
                     upload_url: str | None = None, progress=None) -> dict:
        """Sends a G-code or .3mf file via HTTP POST to port 18910.

        Returns the printer's parsed JSON response.
        Raises RuntimeError on HTTP or connection errors.

        Protocol captured with Wireshark on 2026-04-18:
          POST /gcode_upload?s={session_token}
          Multipart fields: 'filename' (text) + 'gcode' (file bytes)
          Required headers: X-File-Length, X-BBL-* (inherited from BambuLab)
        """
        if not upload_url:
            info = self.query_info()
            if not info:
                raise RuntimeError("Could not get info/report for the upload URL")
            upload_url = info["data"]["urls"]["fileUploadurl"]
        # extract the token from the URL's query string
        if "?s=" not in upload_url:
            raise RuntimeError(f"Upload: no session token ('?s=') in the upload URL: {upload_url!r}")
        token = upload_url.split("?s=")[1]

        if remote_filename is None:
            remote_filename = os.path.basename(filepath)
        size = os.path.getsize(filepath)

        boundary = "------------------------a3a050b927d92a4c"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="filename"\r\n\r\n{remote_filename}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="gcode"; filename="{remote_filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        headers = {
            "User-Agent": "AnycubicSlicerNext/1.3.9.4",
            "Accept": "*/*",
            "X-BBL-Client-Name": "AnycubicSlicerNext",
            "X-BBL-Client-Type": "slicer",
            "X-BBL-Client-Version": "01.03.09.04",
            "X-BBL-Device-ID": str(uuid.uuid4()),
            "X-BBL-Language": "de-DE",
            "X-BBL-OS-Type": "windows",
            "X-BBL-OS-Version": "10.0.26200",
            "X-File-Length": str(size),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)),
            "Connection": "close",
        }

        # Body streamed straight from disk: the whole file used to be read and
        # concatenated ~4x in memory (data, multipart part, body, header+body),
        # with no way to measure progress. Here each block read advances `progress(sent, total)`.
        class _Body:
            def __init__(self):
                self._f = open(filepath, "rb")
                self._parts = [head, None, tail]   # None = file contents
                self.sent = 0
            def read(self, n=-1):
                while self._parts:
                    if self._parts[0] is None:
                        chunk = self._f.read(n if n and n > 0 else 1 << 20)
                        if chunk:
                            break
                        self._parts.pop(0)
                        continue
                    chunk = self._parts.pop(0)
                    break
                else:
                    return b""
                self.sent += len(chunk)
                if progress:
                    try:
                        progress(min(self.sent, size), size)
                    except Exception:
                        pass
                return chunk
            def close(self):
                self._f.close()

        import http.client
        body = _Body()
        conn = http.client.HTTPConnection(self.host, 18910, timeout=10)
        conn.blocksize = 1 << 16
        try:
            try:
                conn.connect()
            except OSError as e:
                raise RuntimeError(
                    f"the printer did not accept a connection on upload port 18910 ({e}). "
                    "If it persists, restart the printer.") from e
            # The timeout is per socket operation: 60 s without being able to send or
            # receive anything = printer stuck. The code used to wait up to 180 s
            # for the printer to CLOSE the connection, even when it had already replied.
            conn.sock.settimeout(60)
            conn.request("POST", f"/gcode_upload?s={token}", body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
        except socket.timeout as e:
            raise RuntimeError("the printer stopped responding during the upload (60 s without a reply)") from e
        finally:
            body.close()
            conn.close()

        try:
            result = json.loads(resp_body)
        except Exception:
            raise RuntimeError(f"Upload: unexpected response (HTTP {resp.status}): {resp_body[:200]!r}")
        if resp.status >= 400:
            raise RuntimeError(f"Upload refused by the printer (HTTP {resp.status}): {result}")
        return result

    def move_axis(self, axis: int, move_type: int = 2, distance: int = 0) -> dict | None:
        return self.publish("axis", "move",
                            {"axis": axis, "move_type": move_type, "distance": distance})

    def home_all(self) -> dict | None:
        # axis=4 move_type=2 = Home all axes (~4-15s)
        return self.publish("axis", "move", {"axis": 4, "move_type": 2, "distance": 0}, timeout=30.0)

    def home_axis(self, axis: int) -> dict | None:
        # axis: 1=Y, 2=X, 3=Z
        return self.publish("axis", "move", {"axis": axis, "move_type": 2, "distance": 0}, timeout=30.0)

    def jog(self, axis: int, direction: int, distance_mm: int = 1) -> dict | None:
        # axis: 1=Y, 2=X, 3=Z  direction: 0=neg, 1=pos
        return self.move_axis(axis=axis, move_type=direction, distance=distance_mm)


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Anycubic Kobra X LAN-Client")
    parser.add_argument("--ip",        default=env_loader.PRINTER_IP)
    parser.add_argument("--port",      type=int, default=env_loader.MQTT_PORT)
    parser.add_argument("--username",  default=env_loader.USERNAME)
    parser.add_argument("--password",  default=env_loader.PASSWORD)
    parser.add_argument("--mode-id",   default=env_loader.MODE_ID)
    parser.add_argument("--device-id", default=env_loader.DEVICE_ID)
    parser.add_argument("--monitor",   action="store_true",
                        help="Listen continuously and print all reports")
    args = parser.parse_args()

    client = KobraXClient(
        host=args.ip, port=args.port,
        username=args.username, password=args.password,
        mode_id=args.mode_id, device_id=args.device_id,
    )

    if args.monitor:
        def on_msg(topic, payload):
            suffix = "/".join(topic.split("/")[-2:])
            ts = datetime.now().strftime("%H:%M:%S")
            state = payload.get("state", "")
            data  = payload.get("data") or {}
            if "progress" in data:
                print(f"[{ts}] {suffix:25}  state={state:12}  progress={data['progress']}%  layer={data.get('curr_layer','?')}/{data.get('total_layers','?')}")
            elif "curr_nozzle_temp" in data:
                print(f"[{ts}] {suffix:25}  nozzle={data['curr_nozzle_temp']}°C/{data.get('target_nozzle_temp',0)}°C  bed={data['curr_hotbed_temp']}°C/{data.get('target_hotbed_temp',0)}°C")
            else:
                print(f"[{ts}] {suffix:25}  state={state}")

        client.callbacks["*"] = on_msg
        client.connect()
        print("[kobrax] Monitor mode on (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        client.disconnect()
    else:
        client.connect()

        print("\n--- query_info ---")
        info = client.query_info()
        if info:
            d = info.get("data", {})
            print(f"  Printer:  {d.get('printerName')}  FW {d.get('version')}")
            print(f"  Status:   {d.get('state')}")
            t = d.get("temp", {})
            print(f"  Nozzle:   {t.get('curr_nozzle_temp')}°C → {t.get('target_nozzle_temp')}°C")
            print(f"  Bed:      {t.get('curr_hotbed_temp')}°C → {t.get('target_hotbed_temp')}°C")
            urls = d.get("urls", {})
            print(f"  Upload:   {urls.get('fileUploadurl')}")
            print(f"  Camera:   {urls.get('rtspUrl')}")
        else:
            print("  No response")

        client.disconnect()
