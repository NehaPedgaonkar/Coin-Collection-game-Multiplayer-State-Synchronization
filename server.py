#!/usr/bin/env python3
"""
server.py
Authoritative Python WebSocket server for Coin Collector.

Features:
- asyncio + websockets
- authoritative server tick (20 Hz)
- snapshot broadcast (10 Hz)
- artificial one-way latency (200 ms) applied to both incoming and outgoing messages
- lobby auto-start with 2 players
- coin spawning and authoritative pickup validation
- includes lastProcessedInputSeq per player in snapshots for client reconciliation
"""

import asyncio
import json
import random
import time
import logging
import signal
from collections import defaultdict

import websockets

# ----------------------------
# Configuration
# ----------------------------
PORT = 8080
TICK_RATE = 20                # simulation ticks per second
SNAPSHOT_RATE = 10            # snapshots per second
ARTIFICIAL_LATENCY_MS = 200   # one-way latency in milliseconds
LOBBY_SIZE = 2
MAP_W, MAP_H = 800, 600
PLAYER_SPEED = 160.0          # px/s
PICKUP_RADIUS = 24.0          # px
INITIAL_COINS = 5
MAX_COINS = 6

# ----------------------------
# Server state
# ----------------------------
next_player_id = 1
players = {}    # pid -> dict: {id, ws, x,y,vx,vy,score,inputs,input_history,lastProcessedInputSeq}
coins = {}      # cid -> {id,x,y}
next_coin_id = 1

game_running = False
_last_tick = time.time()

# For graceful shutdown
STOP = False

# Setup logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')


# ----------------------------
# Utility helpers
# ----------------------------
def now_ms():
    return int(time.time() * 1000)


def spawn_coin():
    global next_coin_id
    cid = next_coin_id
    next_coin_id += 1
    coins[cid] = {
        "id": cid,
        "x": random.random() * (MAP_W - 40) + 20,
        "y": random.random() * (MAP_H - 40) + 20,
    }


async def delayed_send(ws, obj):
    """Simulate artificial outgoing latency (one-way)."""
    await asyncio.sleep(ARTIFICIAL_LATENCY_MS / 1000.0)
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        # client may have disconnected
        pass


async def handle_incoming_with_delay(raw, handler):
    """Simulate artificial incoming latency by delaying processing."""
    await asyncio.sleep(ARTIFICIAL_LATENCY_MS / 1000.0)
    try:
        await handler(raw)
    except Exception:
        logging.exception("Error handling incoming message")


# ----------------------------
# Simulation
# ----------------------------
def server_tick(dt):
    """Advance authoritative physics by dt (seconds)."""
    for p in list(players.values()):
        inp = p.get("inputs", {})
        ax = 0.0
        ay = 0.0
        if inp.get("up"):    ay -= 1.0
        if inp.get("down"):  ay += 1.0
        if inp.get("left"):  ax -= 1.0
        if inp.get("right"): ax += 1.0

        length = (ax * ax + ay * ay) ** 0.5 or 1.0
        p["vx"] = (ax / length) * PLAYER_SPEED
        p["vy"] = (ay / length) * PLAYER_SPEED

        p["x"] += p["vx"] * dt
        p["y"] += p["vy"] * dt

        # clamp
        p["x"] = max(0, min(MAP_W, p["x"]))
        p["y"] = max(0, min(MAP_H, p["y"]))

    # coin pickups (authoritative)
    removed = []
    for cid, coin in list(coins.items()):
        for pid, p in list(players.items()):
            dx = p["x"] - coin["x"]
            dy = p["y"] - coin["y"]
            if dx * dx + dy * dy <= PICKUP_RADIUS * PICKUP_RADIUS:
                p["score"] = p.get("score", 0) + 1
                removed.append(cid)
                logging.info(f"Player {pid} picked coin {cid} (score={p['score']})")
                # broadcast pickup event (will also be visible in snapshots)
                coro = broadcast({"type": "pickup", "serverTime": now_ms(), "playerId": pid, "coinId": cid})
                # schedule broadcast without awaiting (it will honor outgoing delay)
                asyncio.create_task(coro)
                break
    for cid in removed:
        coins.pop(cid, None)


async def broadcast(obj):
    """Send obj (dict) to all connected players using delayed_send to simulate outgoing latency."""
    tasks = []
    for p in players.values():
        ws = p.get("ws")
        if ws:
            tasks.append(asyncio.create_task(delayed_send(ws, obj)))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def broadcast_snapshot():
    """Create a snapshot object and broadcast to all players."""
    snapshot = {
        "type": "snapshot",
        "serverTime": now_ms(),
        "players": [],
        "coins": list(coins.values()),
    }
    for p in players.values():
        snapshot["players"].append({
            "id": p["id"],
            "x": p["x"],
            "y": p["y"],
            "vx": p["vx"],
            "vy": p["vy"],
            "score": p.get("score", 0),
            "lastProcessedInputSeq": p.get("lastProcessedInputSeq", 0),
        })
    await broadcast(snapshot)


# ----------------------------
# Main async loops
# ----------------------------
async def simulation_loop():
    global _last_tick
    logging.info("Simulation loop started")
    while not STOP:
        now = time.time()
        dt = now - _last_tick
        _last_tick = now
        if game_running:
            server_tick(dt)
        await asyncio.sleep(1.0 / TICK_RATE)


async def snapshot_loop():
    logging.info("Snapshot loop started")
    while not STOP:
        if game_running:
            await broadcast_snapshot()
        await asyncio.sleep(1.0 / SNAPSHOT_RATE)


async def coin_spawner_loop():
    while not STOP:
        if game_running and len(coins) < MAX_COINS:
            spawn_coin()
        await asyncio.sleep(1.5)


# ----------------------------
# WebSocket connection handler
# ----------------------------
async def handle_connection(connection):
    ws = connection
    path = getattr(connection, "path", "/")  # safe fallback

    global next_player_id, game_running

    pid = next_player_id
    next_player_id += 1

    # initial player state
    players[pid] = {
        "id": pid,
        "ws": ws,
        "x": random.random() * MAP_W,
        "y": random.random() * MAP_H,
        "vx": 0.0,
        "vy": 0.0,
        "score": 0,
        "inputs": {},
        "input_history": [],
        "lastProcessedInputSeq": 0,
        "connectedAt": now_ms(),
    }

    logging.info(f"Player connected {pid}")

    # send welcome packet (outgoing latency will be applied)
    welcome = {"type": "welcome", "playerId": pid, "map": {"w": MAP_W, "h": MAP_H}, "serverTime": now_ms()}
    asyncio.create_task(delayed_send(ws, welcome))

    # send lobby update to everyone
    await send_lobby_update()

    # check whether to auto-start
    if not game_running and len(players) >= LOBBY_SIZE:
        game_running = True
        # reset/initialize players
        for p in players.values():
            p["x"] = random.random() * MAP_W
            p["y"] = random.random() * MAP_H
            p["vx"] = 0.0
            p["vy"] = 0.0
            p["score"] = 0
            p["inputs"] = {}
            p["input_history"].clear()
            p["lastProcessedInputSeq"] = 0
        # initial coins
        for _ in range(INITIAL_COINS):
            spawn_coin()
        logging.info("Lobby filled. Starting game.")

    try:
        async for message in ws:
            # simulate incoming delay before processing
            asyncio.create_task(handle_incoming_with_delay(message, lambda raw: process_client_message(pid, raw)))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # cleanup
        logging.info(f"Player disconnected {pid}")
        players.pop(pid, None)
        await send_lobby_update()
        if len(players) < LOBBY_SIZE:
            game_running = False
            logging.info("Not enough players — game stopped. Waiting in lobby.")


async def send_lobby_update():
    lobby = {"type": "lobby", "players": [{"id": p["id"]} for p in players.values()]}
    await broadcast(lobby)


async def process_client_message(pid, raw):
    """
    Handle messages from client for player pid.
    Called after artificial incoming delay.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return

    p = players.get(pid)
    if not p:
        return

    if data.get("type") == "input":
        # update last processed seq for that player (server side)
        seq = int(data.get("seq", 0))
        inputs = data.get("inputs", {})
        # sanitize inputs to booleans
        safe_inputs = {
            "up": bool(inputs.get("up", False)),
            "down": bool(inputs.get("down", False)),
            "left": bool(inputs.get("left", False)),
            "right": bool(inputs.get("right", False)),
        }
        p["inputs"] = safe_inputs
        p["lastProcessedInputSeq"] = seq
        # store simple input history (for optional rewind/replay)
        p["input_history"].append({"seq": seq, "inputs": safe_inputs, "receivedAt": now_ms()})
        if len(p["input_history"]) > 200:
            p["input_history"].pop(0)
        # (do not accept any client-reported pickups or scores)
    else:
        # ignore other client messages
        pass


# ----------------------------
# Graceful shutdown handler
# ----------------------------
def setup_signal_handlers(loop):
    def _stop(signum, frame):
        global STOP
        STOP = True
        logging.info("Shutting down...")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


# ----------------------------
# Entrypoint
# ----------------------------
async def main():
    setup_signal_handlers(asyncio.get_event_loop())
    # start loops
    tasks = [
        asyncio.create_task(simulation_loop()),
        asyncio.create_task(snapshot_loop()),
        asyncio.create_task(coin_spawner_loop()),
    ]
    ws_server = await websockets.serve(handle_connection, "0.0.0.0", PORT, process_request=None)
    logging.info(f"Authoritative server listening on ws://localhost:{PORT}")
    try:
        # keep running until STOP
        while not STOP:
            await asyncio.sleep(0.2)
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
