# -*- coding: utf-8 -*-
"""LAN multiplayer transport + protocol for HP Battle Chess.

The network layer is deliberately independent from Pygame.  It implements:
- UDP discovery (HPBC_DISCOVER / HPBC_GAME)
- one Host <-> one Client TCP session
- protocol versioning
- host-authoritative action validation
- turn_id/action_id de-duplication
- canonical state/view snapshots + SHA-256 hashes
- lightweight reconnect support

The host owns the real GameState.  Clients receive a player view; hidden Fog of
War units are omitted entirely from the payload.
"""
from __future__ import annotations

import hashlib
import json
import queue
import socket
import struct
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import actions as actions_mod
import protocol
import config
import rules
import fog
import turn_system
from game_state import GameState
from pieces import Piece

PROTOCOL_VERSION = protocol.PROTOCOL_VERSION
DISCOVERY_MAGIC = protocol.DISCOVERY_MAGIC
GAME_MAGIC = protocol.GAME_MAGIC
DISCOVERY_PORT = 38477
DEFAULT_TCP_PORT = 38478
DISCOVERY_TIMEOUT = 0.35
MAX_FRAME = 4 * 1024 * 1024


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_action(action: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(action)
    if isinstance(out.get("target"), tuple):
        out["target"] = list(out["target"])
    return out


def _piece_to_dict(piece: Piece) -> Dict[str, Any]:
    return {
        "id": piece.id,
        "type": piece.type,
        "color": piece.color,
        "hp": piece.hp,
        "max_hp": piece.max_hp,
        "damage": piece.damage,
        "col": piece.col,
        "row": piece.row,
        "actions_used": piece.actions_used,
        "mounted_knight_id": piece.mounted_knight_id,
        "is_mounted_knight": piece.is_mounted_knight,
        "host_id": piece.host_id,
        "queen_locked_mode": piece.queen_locked_mode,
    }


def visible_ids_for_view(state: GameState, viewer_color: Optional[str]) -> Optional[set]:
    if viewer_color is None or not state.fog_enabled:
        return None
    return fog.visible_enemy_ids(state, viewer_color)


def state_to_dict(state: GameState, viewer_color: Optional[str] = None) -> Dict[str, Any]:
    """Serialize a full state or a player-specific view.

    In Fog of War, hidden enemy pieces are absent from the payload entirely.
    Action history is omitted for fogged views because prior actions may leak
    hidden coordinates.
    """
    visible = visible_ids_for_view(state, viewer_color)
    pieces = []
    for p in sorted(state.pieces.values(), key=lambda x: x.id):
        if visible is not None and p.color != viewer_color and p.id not in visible:
            continue
        data = _piece_to_dict(p)
        # Do not leak a hidden mounted knight through its carrier reference.
        if visible is not None and data["mounted_knight_id"] is not None:
            if data["mounted_knight_id"] not in {q["id"] for q in pieces} and data["mounted_knight_id"] not in state.pieces:
                data["mounted_knight_id"] = None
            elif data["mounted_knight_id"] not in visible and p.color != viewer_color:
                data["mounted_knight_id"] = None
            elif p.color == viewer_color:
                mounted = state.pieces.get(data["mounted_knight_id"])
                if mounted is not None and mounted.color != viewer_color and data["mounted_knight_id"] not in visible:
                    data["mounted_knight_id"] = None
        pieces.append(data)

    payload = {
        "turn_color": state.turn_color,
        "phase": state.phase,
        "king_stage_timer": max(0.0, float(state.king_stage_timer)),
        "main_timer": max(0.0, float(state.main_timer)),
        "game_timer": max(0.0, float(getattr(state, "game_timer", config.STANDARD_GAME_TIME))),
        "captured": dict(getattr(state, "captured", {"white": 0, "black": 0})),
        "turn_number": state.turn_number,
        "winner": state.winner,
        "game_mode": state.game_mode,
        "board_size": list(state.board_size) if isinstance(state.board_size, tuple) else state.board_size,
        "base_compromised": dict(state.base_compromised),
        "fog_enabled": state.fog_enabled,
        "last_event": state.last_event,
        "action_history": [] if visible is not None else list(state.action_history[-12:]),
        "pieces": pieces,
    }
    return payload


def state_hash(state: GameState, viewer_color: Optional[str] = None) -> str:
    raw = _json_bytes(state_to_dict(state, viewer_color=viewer_color))
    return hashlib.sha256(raw).hexdigest().upper()


def state_from_dict(data: Dict[str, Any]) -> GameState:
    """Rebuild a GameState snapshot using explicit piece ids."""
    raw_size = data.get("board_size", [config.BOARD_WIDTH, config.BOARD_HEIGHT])
    if isinstance(raw_size, (list, tuple)):
        board_size = (int(raw_size[0]), int(raw_size[1]))
    else:
        board_size = int(raw_size)
    current_size = (config.BOARD_WIDTH, config.BOARD_HEIGHT)
    if board_size != current_size:
        config.configure_board_size(board_size)
    gs = GameState()
    gs.turn_color = data.get("turn_color", "white")
    gs.phase = data.get("phase", "setup_white")
    gs.king_stage_timer = float(data.get("king_stage_timer", config.KING_ACTION_TIME))
    gs.main_timer = float(data.get("main_timer", config.MAIN_TURN_TIME))
    gs.game_timer = float(data.get("game_timer", config.STANDARD_GAME_TIME))
    gs.captured = dict(data.get("captured", {"white": 0, "black": 0}))
    gs.turn_number = int(data.get("turn_number", 1))
    gs.winner = data.get("winner")
    gs.game_mode = data.get("game_mode", config.DEFAULT_GAME_MODE)
    gs.board_size = board_size
    gs.base_compromised = dict(data.get("base_compromised", {"white": False, "black": False}))
    gs.fog_enabled = bool(data.get("fog_enabled", False))
    gs.last_event = data.get("last_event", "")
    gs.action_history = list(data.get("action_history", []))[-12:]

    for raw in data.get("pieces", []):
        p = Piece(
            raw["type"], raw["color"], int(raw["hp"]), int(raw["max_hp"]),
            int(raw["damage"]), int(raw["col"]), int(raw["row"]), _id=int(raw["id"]),
        )
        p.actions_used = int(raw.get("actions_used", 0))
        p.mounted_knight_id = raw.get("mounted_knight_id")
        p.is_mounted_knight = bool(raw.get("is_mounted_knight", False))
        p.host_id = raw.get("host_id")
        p.queen_locked_mode = raw.get("queen_locked_mode")
        gs.pieces[p.id] = p
    return gs


def make_snapshot(state: GameState, viewer_color: Optional[str]) -> Dict[str, Any]:
    view = state_to_dict(state, viewer_color=viewer_color)
    return {
        "state": view,
        "state_hash": state_hash(state, viewer_color=viewer_color),
    }


def _recv_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, message: Dict[str, Any]) -> None:
    raw = _json_bytes(message)
    if len(raw) > MAX_FRAME:
        raise ValueError("network frame too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def recv_frame(sock: socket.socket) -> Optional[Dict[str, Any]]:
    hdr = _recv_exact(sock, 4)
    if hdr is None:
        return None
    size = struct.unpack("!I", hdr)[0]
    if size <= 0 or size > MAX_FRAME:
        raise ValueError("invalid network frame size")
    raw = _recv_exact(sock, size)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


class DiscoveryHost:
    def __init__(self, game_name: str, mode: str, board_size: int, tcp_port: int, game_id: str):
        self.game_name = game_name
        self.mode = mode
        self.board_size = board_size
        self.tcp_port = tcp_port
        self.game_id = game_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="hpbc-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
            sock.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    raw, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("type") != DISCOVERY_MAGIC:
                    continue
                # ``addr`` is the DISCOVERY CLIENT, not the host. Advertising
                # addr[0] made clients try to connect back to themselves.
                reply = {
                    "type": GAME_MAGIC,
                    "protocol_version": PROTOCOL_VERSION,
                    "game_id": self.game_id,
                    "name": self.game_name,
                    "mode": self.mode,
                    "board": f"{(self.board_size[0] if isinstance(self.board_size, (tuple,list)) else self.board_size)}x{(self.board_size[1] if isinstance(self.board_size, (tuple,list)) else self.board_size)}",
                    "board_size": self.board_size,
                    "port": self.tcp_port,
                    "state": "WAITING",
                    "host": get_local_ipv4(),
                }
                try:
                    sock.sendto(_json_bytes(reply), addr)
                except OSError:
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass


class DiscoveryClient:
    @staticmethod
    def find_games(timeout: float = 1.0) -> List[Dict[str, Any]]:
        deadline = time.monotonic() + max(0.1, timeout)
        results: Dict[str, Dict[str, Any]] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.15)
            request = _json_bytes({"type": DISCOVERY_MAGIC, "protocol_version": PROTOCOL_VERSION})
            targets = {"255.255.255.255"}
            try:
                local_ip = get_local_ipv4()
                octets = local_ip.split(".")
                if len(octets) == 4 and all(x.isdigit() for x in octets):
                    targets.add(".".join(octets[:3] + ["255"]))
            except Exception:
                pass
            for address in sorted(targets):
                try:
                    sock.sendto(request, (address, DISCOVERY_PORT))
                except OSError:
                    pass
            while time.monotonic() < deadline:
                try:
                    raw, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    game = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if game.get("type") != GAME_MAGIC:
                    continue
                if game.get("protocol_version") != PROTOCOL_VERSION:
                    continue
                game["host"] = game.get("host") or addr[0]
                game["port"] = int(game.get("port", DEFAULT_TCP_PORT))
                results[game.get("game_id", f"{game['host']}:{game['port']}")] = game
        finally:
            sock.close()
        return sorted(results.values(), key=lambda x: (x.get("name", ""), x.get("host", "")))


class _TCPPeer:
    def __init__(self, sock: socket.socket, incoming_queue: queue.Queue):
        self.sock = sock
        self.queue = incoming_queue
        self.send_lock = threading.Lock()
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._reader_loop, name="hpbc-reader", daemon=True)
        self.reader.start()

    def send(self, message: Dict[str, Any]) -> bool:
        try:
            with self.send_lock:
                send_frame(self.sock, message)
            return True
        except (OSError, ValueError):
            self.close()
            return False

    def _reader_loop(self) -> None:
        while not self.closed.is_set():
            try:
                msg = recv_frame(self.sock)
            except Exception as exc:
                self.queue.put({"_event": "error", "error": str(exc)})
                break
            if msg is None:
                break
            self.queue.put(msg)
        self.closed.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.queue.put({"_event": "disconnected"})

    def close(self) -> None:
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def action_visible_to_view(before: GameState, after: GameState, action: Dict[str, Any], viewer_color: str) -> bool:
    """Whether an action can be safely disclosed without leaking Fog data."""
    if not before.fog_enabled:
        return True
    pid = action.get("piece_id")
    p_before = before.pieces.get(pid)
    p_after = after.pieces.get(pid)
    if p_before is None:
        return False
    # Own actions are always safe to disclose to the player.
    if p_before.color == viewer_color:
        if action.get("type") in ("attack", "mount", "heal"):
            tid = action.get("target_id")
            target = before.pieces.get(tid)
            return target is None or target.color == viewer_color or tid in fog.visible_enemy_ids(before, viewer_color)
        return True
    # Enemy action: attacker must have been visible before and remain visible
    # afterwards. Otherwise action metadata would reveal a hidden coordinate.
    vis_before = fog.visible_enemy_ids(before, viewer_color)
    vis_after = fog.visible_enemy_ids(after, viewer_color)
    if pid not in vis_before or pid not in vis_after:
        return False
    if action.get("type") == "attack":
        tid = action.get("target_id")
        target_before = before.pieces.get(tid)
        if target_before is not None and target_before.color != viewer_color and tid not in vis_before:
            return False
    return True


class HostSession:
    """Authoritative host session. UI thread calls poll()/tick()."""
    def __init__(self, state: GameState, game_name: str, tcp_port: int = 0):
        self.state = state
        self.game_name = game_name
        self.requested_tcp_port = int(tcp_port)
        self.game_id = uuid.uuid4().hex[:12]
        self.queue: queue.Queue = queue.Queue()
        self.server: Optional[socket.socket] = None
        self.peer: Optional[_TCPPeer] = None
        self.discovery: Optional[DiscoveryHost] = None
        self.tcp_port = 0
        self.running = False
        self.connected = False
        self.disconnected_at: Optional[float] = None
        self.session_config = {
            "board_size": list(state.board_size) if isinstance(state.board_size, tuple) else state.board_size,
            "mode": state.game_mode,
            "fog_of_war": state.fog_enabled,
        }
        self.action_id = 0
        self.last_applied = (state.turn_number, 0)
        self.accepted_request_ids: List[str] = []
        self.visual_action_queue: List[Dict[str, Any]] = []
        self.server_thread: Optional[threading.Thread] = None
        self.initial_state_snapshot = state_to_dict(state, viewer_color=None)
        self.rematch_local = False
        self.rematch_remote = False

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if not (0 <= self.requested_tcp_port <= 65535):
            raise ValueError("TCP port must be between 0 and 65535")
        server.bind(("0.0.0.0", self.requested_tcp_port))
        server.listen(1)
        server.settimeout(0.5)
        self.server = server
        self.tcp_port = server.getsockname()[1]
        self.running = True
        self.discovery = DiscoveryHost(self.game_name, self.state.game_mode, self.state.board_size, self.tcp_port, self.game_id)
        self.discovery.start()
        self.server_thread = threading.Thread(target=self._accept_loop, name="hpbc-host", daemon=True)
        self.server_thread.start()

    def _accept_loop(self) -> None:
        while self.running and self.server is not None:
            try:
                sock, _addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.peer is not None and not self.peer.closed.is_set():
                try:
                    send_frame(sock, {"type": "error", "protocol_version": PROTOCOL_VERSION, "message": "Game already has a player"})
                except Exception:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            sock.settimeout(None)
            self.peer = _TCPPeer(sock, self.queue)
            # Send the initial handshake immediately from the accept thread.
            # Раньше welcome ждал следующего HostSession.poll(), что было
            # лишней гонкой между сетевым reader-потоком и UI-циклом: при
            # нагрузке клиент мог подключиться, но получить welcome заметно
            # позже. Отправка сразу после accept делает протокол детерминированным.
            try:
                self._send_welcome()
            except Exception:
                try:
                    self.peer.close()
                except Exception:
                    pass
            self.queue.put({"_event": "connected"})
            # Keep the listening socket alive so a disconnected opponent can reconnect.
            continue

    def send(self, message: Dict[str, Any]) -> bool:
        return self.peer is not None and self.peer.send(message)

    def _send_snapshot(self) -> None:
        if self.peer is None or self.peer.closed.is_set():
            return
        snap = make_snapshot(self.state, "black")
        self.send({
            "type": "snapshot",
            "protocol_version": PROTOCOL_VERSION,
            "session_config": self.session_config,
            **snap,
        })

    def _send_welcome(self) -> None:
        self.send({
            "type": "welcome",
            "protocol_version": PROTOCOL_VERSION,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "player_color": "black",
            "game_id_ref": self.game_id,
            "rules_version": str(PROTOCOL_VERSION),
            "session_config": self.session_config,
            "state": "CONNECTED",
        })
        self._send_snapshot()

    def _valid_action(self, action: Dict[str, Any]) -> bool:
        if self.state.phase not in ("king_stage", "lobby_stage"):
            return False
        if self.state.turn_color != "black":
            return False
        if action.get("type") in ("pass",):
            return False
        piece = self.state.pieces.get(action.get("piece_id"))
        if piece is None or not piece.alive() or piece.color != "black":
            return False
        target = canonical_action(action)
        for legal in actions_mod.get_legal_actions(self.state, piece):
            if canonical_action(legal) == target:
                return True
        return False

    def _accept_action(self, action: Dict[str, Any], request_id: str) -> None:
        if request_id in self.accepted_request_ids:
            return
        if not self._valid_action(action):
            self.send({"type": "action_rejected", "protocol_version": PROTOCOL_VERSION, "request_id": request_id, "reason": "Illegal action"})
            return
        before_state = self.state.clone_light()
        piece = self.state.pieces.get(action.get("piece_id"))
        if not actions_mod.apply_action(self.state, action):
            self.send({"type": "action_rejected", "protocol_version": PROTOCOL_VERSION, "request_id": request_id, "reason": "Action failed"})
            return
        if piece.type == "king" and self.state.phase == "king_stage":
            turn_system.end_king_stage(self.state)
        self.accepted_request_ids.append(request_id)
        self.accepted_request_ids = self.accepted_request_ids[-64:]
        self.action_id += 1
        self.last_applied = (self.state.turn_number, self.action_id)
        self.state.last_event = f"Чёрные: {action.get('type')}"
        self.visual_action_queue.append((canonical_action(action), before_state))
        result = self.state.check_victory()
        if result:
            self.state.phase = "game_over"
            self.state.winner = result
        safe_action = canonical_action(action) if action_visible_to_view(before_state, self.state, action, "black") else None
        self.send({
            "type": "action_applied",
            "protocol_version": PROTOCOL_VERSION,
            "turn_id": self.state.turn_number,
            "action_id": self.action_id,
            "request_id": request_id,
            "action": safe_action,
            "state_hash": state_hash(self.state, "black"),
        })
        self._send_snapshot()

    def _accept_remote_setup(self, pieces: List[Dict[str, Any]]) -> bool:
        try:
            expected = config.get_army_composition(self.state.board_size[0] if isinstance(self.state.board_size, (tuple,list)) else self.state.board_size)
            counts = {t: 0 for t in expected}
            cells = set()
            for raw in pieces:
                t = raw["type"]; c = int(raw["col"]); r = int(raw["row"])
                if t not in counts or counts[t] >= expected[t] or (c, r) in cells or not rules.in_own_half("black", c, r):
                    return False
                counts[t] += 1; cells.add((c, r))
            if counts != expected:
                return False
            for pid in [pid for pid,p in self.state.pieces.items() if p.color == "black"]:
                del self.state.pieces[pid]
            for raw in pieces:
                self.state.add_piece(raw["type"], "black", int(raw["col"]), int(raw["row"]))
            self.state.phase = "king_stage"
            turn_system.start_turn(self.state, "white")
            self.initial_state_snapshot = state_to_dict(self.state, "white")
            return True
        except Exception:
            return False

    def _end_king_stage(self) -> None:
        if self.state.phase == "king_stage" and self.state.turn_color == "black":
            turn_system.end_king_stage(self.state)
            self._send_snapshot()

    def _end_turn(self) -> None:
        if self.state.phase == "game_over":
            return
        if self.state.turn_color != "black":
            return
        turn_system.end_turn(self.state)
        self.action_id = 0
        self.last_applied = (self.state.turn_number, 0)
        self._send_snapshot()

    def poll(self) -> None:
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            event = msg.get("_event")
            if event == "connected":
                self.connected = True
                self.disconnected_at = None
                # welcome/snapshot were already sent atomically at accept time;
                # this event only updates the authoritative connection flag.
                continue
            if event == "disconnected":
                self.connected = False
                self.disconnected_at = time.monotonic()
                if self.state.phase not in ("waiting_for_player", "game_over"):
                    self.state.phase = "game_over"
                    self.state.winner = "white"
                    self.state.last_event = "Соперник отключился — белые победили"
                    self._send_snapshot()
                continue
            if event == "error":
                self.connected = False
                self.disconnected_at = time.monotonic()
                continue
            if msg.get("protocol_version") != PROTOCOL_VERSION:
                self.send({"type": "error", "protocol_version": PROTOCOL_VERSION, "message": "Incompatible game version"})
                continue
            if msg.get("type") == "action_request":
                self._accept_action(msg.get("action") or {}, msg.get("request_id", uuid.uuid4().hex))
            elif msg.get("type") == "command":
                command = msg.get("command")
                if command == "end_king_stage":
                    self._end_king_stage()
                elif command == "end_turn":
                    self._end_turn()
                elif command == "resync_request":
                    self._send_snapshot()
                elif command == "setup_ready":
                    payload = msg.get("payload") or {}
                    if self._accept_remote_setup(payload.get("pieces") or []):
                        self._send_snapshot()
                elif command == "rematch_ready":
                    self.rematch_remote = True
                    self._maybe_start_rematch()
                elif command == "surrender":
                    self.state.phase = "game_over"
                    self.state.winner = "white"
                    self.state.last_event = "Чёрные сдались"
                    self._send_snapshot()
                elif command == "exit_room":
                    self.close()

    def tick(self, dt: float) -> None:
        if not self.running:
            return
        self.poll()
        if self.state.phase == "game_over":
            return
        if not self.connected:
            return
        # Host owns ALL timers, including the local White turn.
        if self.state.turn_color == "black":
            if self.state.phase == "king_stage":
                self.state.king_stage_timer -= dt
                if self.state.king_stage_timer <= 0:
                    self._end_king_stage()
            elif self.state.phase == "lobby_stage":
                self.state.main_timer -= dt
                if self.state.main_timer <= 0:
                    self._end_turn()
        elif self.state.turn_color == "white":
            if self.state.phase == "king_stage":
                self.state.king_stage_timer -= dt
                if self.state.king_stage_timer <= 0:
                    turn_system.end_king_stage(self.state)
                    self._send_snapshot()
            elif self.state.phase == "lobby_stage":
                self.state.main_timer -= dt
                if self.state.main_timer <= 0:
                    turn_system.end_turn(self.state)
                    self.action_id = 0
                    self._send_snapshot()

    def notify_local_state_changed(self, action: Optional[Dict[str, Any]] = None, before_state: Optional[GameState] = None) -> None:
        if self.peer is None or self.peer.closed.is_set():
            return
        if action is not None:
            self.action_id += 1
            self.last_applied = (self.state.turn_number, self.action_id)
            safe_action = canonical_action(action)
            if before_state is not None and not action_visible_to_view(before_state, self.state, action, "black"):
                safe_action = None
            self.send({
                "type": "action_applied",
                "protocol_version": PROTOCOL_VERSION,
                "turn_id": self.state.turn_number,
                "action_id": self.action_id,
                "action": safe_action,
                "state_hash": state_hash(self.state, "black"),
            })
        self._send_snapshot()

    def request_local_rematch(self) -> None:
        self.rematch_local = True
        self._maybe_start_rematch()

    def _maybe_start_rematch(self) -> None:
        if not (self.rematch_local and self.rematch_remote) or not self.connected:
            return
        self.state = state_from_dict(self.initial_state_snapshot)
        self.state.phase = "king_stage"
        self.state.winner = None
        turn_system.start_turn(self.state, "white")
        self.action_id = 0
        self.last_applied = (self.state.turn_number, 0)
        self.accepted_request_ids.clear()
        self.rematch_local = False
        self.rematch_remote = False
        self._send_snapshot()

    def pop_visual_actions(self) -> List[Any]:
        out = list(self.visual_action_queue)
        self.visual_action_queue.clear()
        return out

    def force_game_over(self, winner: str) -> None:
        self.state.phase = "game_over"
        self.state.winner = winner
        self._send_snapshot()

    def close(self) -> None:
        self.running = False
        if self.discovery:
            self.discovery.stop()
        if self.peer:
            self.peer.close()
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass


class ClientSession:
    """Remote black client. Host is authoritative; local state is a view."""
    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.peer: Optional[_TCPPeer] = None
        self.server_info: Optional[Dict[str, Any]] = None
        self.running = False
        self.connected = False
        self.disconnected_at: Optional[float] = None
        self.last_action_key = (0, 0)
        self.game_id: Optional[str] = None
        self.player_color = "black"
        self.session_config: Dict[str, Any] = {}
        self.pending_request: Dict[str, Dict[str, Any]] = {}
        self.last_error = ""

    def connect(self, game: Dict[str, Any], timeout: float = 2.5) -> None:
        self.server_info = dict(game)
        host = game.get("host")
        port = int(game.get("port", DEFAULT_TCP_PORT))
        # Reconnect must retire the previous peer first. Otherwise its reader
        # thread can still enqueue a late ``disconnected`` event after the new
        # connection has already become active.
        old_peer = self.peer
        if old_peer is not None:
            old_peer.close()
            if old_peer.reader.is_alive() and old_peer.reader is not threading.current_thread():
                old_peer.reader.join(timeout=0.15)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(None)
        self.peer = _TCPPeer(sock, self.queue)
        self.running = True
        self.connected = True
        self.disconnected_at = None

    def connect_by_ip(self, host: str, port: int = DEFAULT_TCP_PORT) -> None:
        self.connect({"host": host, "port": int(port), "name": "Direct IP"})

    def send(self, msg: Dict[str, Any]) -> bool:
        return self.peer is not None and self.peer.send(msg)

    def request_action(self, action: Dict[str, Any]) -> Optional[str]:
        if not self.connected:
            return None
        request_id = uuid.uuid4().hex
        self.pending_request[request_id] = canonical_action(action)
        ok = self.send({
            "type": "action_request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "action": canonical_action(action),
        })
        if not ok:
            self.pending_request.pop(request_id, None)
            return None
        return request_id

    def command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        msg = {"type": "command", "protocol_version": PROTOCOL_VERSION, "command": command}
        if payload is not None:
            msg["payload"] = payload
        return self.send(msg)

    def reconnect(self) -> bool:
        if self.server_info is None:
            return False
        try:
            self.connect(self.server_info, timeout=2.0)
            self.command("resync_request")
            return True
        except OSError:
            return False

    def poll(self) -> List[Dict[str, Any]]:
        events = []
        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            event = msg.get("_event")
            if event == "connected":
                self.connected = True
                continue
            if event == "disconnected":
                # Ignore stale disconnects emitted by an old peer after a
                # reconnect. Only the current peer may change connection state.
                current_closed = self.peer is None or self.peer.closed.is_set()
                if current_closed:
                    self.connected = False
                    self.disconnected_at = time.monotonic()
                    events.append(msg)
                continue
            if event == "error":
                self.connected = False
                self.last_error = msg.get("error", "Network error")
                events.append(msg)
                continue
            pver = msg.get("protocol_version")
            if pver != PROTOCOL_VERSION and msg.get("type") != "error":
                self.last_error = "Incompatible game version"
                events.append({"type": "error", "message": self.last_error})
                continue
            if msg.get("type") == "welcome":
                self.game_id = msg.get("game_id")
                self.player_color = msg.get("player_color", "black")
                self.session_config = dict(msg.get("session_config", {}))
            events.append(msg)
        return events

    def close(self) -> None:
        self.running = False
        if self.peer:
            self.peer.close()


def get_local_ipv4() -> str:
    """Best-effort LAN IP for UI; does not perform external network traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
