import os
import sys
import json
import time
import logging
import asyncio
import threading
import builtins
import nest_asyncio
nest_asyncio.apply()

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, '..', 'Frontend', 'templates'),
            static_folder=os.path.join(BASE_DIR,   '..', 'Frontend', 'static'))

from LibreNMS_client import LibreNMSClient
from SNMP_poller import (
    get_mac_table_bridge_mib,
    get_mac_table_qbridge_all_vlans,
    snmp_walk,
)
from storage import (
    init_db, save_host, save_mib_mode, get_mib_mode,
    save_community, get_community, load_all_communities,
    save_fdb_entries, prune_old_fdb, get_flaps, save_flap_events,
    save_topology, get_topology, get_all_hosts,
    get_all_flap_events, get_fdb_summary, clear_flap_events,
)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Configuration  (override via environment variables)
# ------------------------------------------------------------------ #

LIBRENMS_URL    = os.environ.get("LIBRENMS_URL",  "")            # Insert the IP of the LibreNMS server
LIBRENMS_TOKEN  = os.environ.get("LIBRENMS_TOKEN", "")           # Insert the API token for LibreNMS
DB_PATH         = os.environ.get("LIBRENMS_DB",    "LibreNMS_data.db")

POLL_ROUNDS     = 5
POLL_INTERVAL   = 10   # seconds between rounds
FDB_KEEP_ROUNDS = POLL_ROUNDS   # rounds of FDB history retained per device
FLAP_MIN_MOVES  = 2

# sysObjectID prefix -> MIB polling mode
SYSOID_TO_MIB = {
    "1.3.6.1.4.1.11.":    "bridge",    # HP / Aruba
    "1.3.6.1.4.1.9.":     "qbridge",   # Cisco
    "1.3.6.1.4.1.45.":    "qbridge",   # Nortel
    "1.3.6.1.4.1.52642.": "qbridge",   # Fiberstore
    "1.3.6.1.4.1.2636.":  "qbridge",   # Juniper
    "1.3.6.1.4.1.6027.":  "bridge",    # Dell Force10
    "1.3.6.1.4.1.674.":   "bridge",    # Dell PowerConnect / OS10
    "1.3.6.1.4.1.2011.":  "qbridge",   # Huawei VRP
}

# In-memory SNMP community cache — loaded from DB at the start of every run
COMMUNITY_MAP: dict[str, str] = {}

# ------------------------------------------------------------------ #
#  Flask + SocketIO
# ------------------------------------------------------------------ #

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ------------------------------------------------------------------ #
#  Web console — forward print() to the browser via SocketIO
# ------------------------------------------------------------------ #

_real_print = builtins.print

def _web_print(*args, **kwargs):
    msg = " ".join(map(str, args))
    _real_print(msg, **{k: v for k, v in kwargs.items() if k != "file"})
    try:
        socketio.emit("log", {"data": msg})
    except Exception:
        pass

builtins.print = _web_print

# ------------------------------------------------------------------ #
#  Background task runner
# ------------------------------------------------------------------ #

def _run_in_thread(coro):
    """Run an async coroutine in a fresh background thread + event loop."""
    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        except Exception as e:
            socketio.emit("log", {"data": f"ERROR: {e}"})
            log.exception("Background task failed")
        finally:
            loop.close()
            socketio.emit("done", {})
    threading.Thread(target=target, daemon=True).start()

# ------------------------------------------------------------------ #
#  MIB detection
# ------------------------------------------------------------------ #

def _mib_from_sysoid(sysoid):
    if not sysoid:
        return None
    sysoid = sysoid.lstrip(".")
    if not sysoid.endswith("."):
        sysoid += "."
    for prefix, mode in SYSOID_TO_MIB.items():
        if sysoid.startswith(prefix):
            return mode
    return None


async def _detect_mib_mode(ip, community, vlans, client):
    # 1. sysObjectID first
    details = client.get_device_snmp_details(ip)
    if details:
        mode = _mib_from_sysoid(details.get("sysObjectID") or "")
        if mode:
            log.info("[%s] MIB mode from sysObjectID: %s", ip, mode)
            return mode

    # 2. Q-BRIDGE scoped
    results = await asyncio.gather(*[
        snmp_walk(ip, community, f"1.3.6.1.2.1.17.7.1.2.2.1.2.{v}", timeout=3, retries=1)
        for v in vlans[:3]
    ])
    if any(results):
        return "qbridge"

    # 3. Q-BRIDGE unscoped
    if await snmp_walk(ip, community, "1.3.6.1.2.1.17.7.1.2.2.1.2", timeout=3, retries=1):
        return "qbridge_unscoped"

    # 4. BRIDGE-MIB
    if await snmp_walk(ip, community, "1.3.6.1.2.1.17.4.3.1.2", timeout=3, retries=1):
        return "bridge"

    return None

# ------------------------------------------------------------------ #
#  Device setup

async def fetch_device_info(client, conn, ip):
    """
    Fetch device metadata from LibreNMS, resolve SNMP community and MIB mode.
    """
    device = client.get_device(ip)
    if not device:
        log.warning("[%s] Device not found in LibreNMS, skipping.", ip)
        return None, [], None

    device_id   = str(device["device_id"])
    system_name = device.get("sysName") or device.get("hostname")
    save_host(conn, device_id, system_name, ip, device.get("os"), device.get("hardware"))

    vlans = client.get_device_vlans(ip)
    if not vlans:
        log.warning("[%s] No VLANs found, skipping.", ip)
        return device_id, [], None

    snmp = client.get_device_snmp_details(ip)
    if snmp and snmp.get("community"):
        community = snmp["community"]
    else:
        community = get_community(conn, device_id) or "public"
        if community == "public":
            log.warning("[%s] Using default community 'public' (not in LibreNMS or DB).", ip)

    COMMUNITY_MAP[ip] = community
    save_community(conn, device_id, community)

    mib_mode = get_mib_mode(conn, device_id)
    if mib_mode:
        log.info("[%s] Cached MIB mode: %s", ip, mib_mode)
    else:
        mib_mode = await _detect_mib_mode(ip, community, vlans, client)
        if mib_mode:
            save_mib_mode(conn, device_id, mib_mode)
            log.info("[%s] Detected MIB mode: %s", ip, mib_mode)
        else:
            log.warning("[%s] Could not detect MIB mode, skipping.", ip)
            return device_id, [], None

    print(f"  {system_name} ({ip}) — {len(vlans)} VLANs — mode: {mib_mode}")
    return device_id, vlans, mib_mode

# ------------------------------------------------------------------ #
#  Poll one device

async def poll_device(conn, ip, vlans, device_id, mib_mode):
    community = COMMUNITY_MAP.get(ip, "public")
    timestamp = time.time()

    if mib_mode == "qbridge":
        log.info("[%s] Q-BRIDGE — %d VLANs", ip, len(vlans))
        entries = await get_mac_table_qbridge_all_vlans(ip, community, vlans)
    else:
        log.info("[%s] %s — single walk", ip, mib_mode)
        entries = await get_mac_table_bridge_mib(ip, community)

    if entries:
        save_fdb_entries(conn, device_id, entries, timestamp)

    pruned = prune_old_fdb(conn, device_id, keep_rounds=FDB_KEEP_ROUNDS)
    if pruned:
        log.info("[%s] Pruned %d stale FDB row(s).", ip, pruned)

    print(f"  [{ip}] {len(entries)} MAC entries saved")

    flaps = get_flaps(conn, device_id, last_n_rounds=5, min_moves=FLAP_MIN_MOVES)
    if flaps:
        print(f"  [{ip}] !! {len(flaps)} flap(s) detected:")
        for f in flaps:
            print(f"     MAC {f['mac']} VLAN {f['vlan_id']}: "
                  f"{f['from_port']} -> {f['to_port']} "
                  f"(rounds {f['from_round']}→{f['to_round']})")
        save_flap_events(conn, device_id, flaps, timestamp)
    else:
        print(f"  [{ip}] No flaps.")

# ------------------------------------------------------------------ #
#  Topology setup  (setup mode)

async def setup_from_librenms(client, conn):
    print("Setting up DB from LibreNMS API...")
    devices = client.list_devices()
    if not devices:
        print("No devices found.")
        return

    for dev in devices:
        ip          = dev.get("ip") or dev.get("hostname")
        device_id   = str(dev["device_id"])
        system_name = dev.get("sysName") or dev.get("hostname")
        save_host(conn, device_id, system_name, ip, dev.get("os"), dev.get("hardware"))

        snmp = client.get_device_snmp_details(ip)
        if snmp and snmp.get("community"):
            COMMUNITY_MAP[ip] = snmp["community"]
            save_community(conn, device_id, snmp["community"])

        vlans = client.get_device_vlans(ip)
        print(f"  {system_name} ({ip}) — {len(vlans)} VLANs")

    print("\nFetching LLDP topology...")
    for host in get_all_hosts(conn):
        links = client.get_device_links(host["ip_address"])
        if links:
            save_topology(conn, host["hostid"], links)
            print(f"  [{host['ip_address']}] {len(links)} link(s)")
        else:
            print(f"  [{host['ip_address']}] No LLDP links")

# ------------------------------------------------------------------ #
#  JSON export

def export_json(conn, path="data.json"):
    hosts = get_all_hosts(conn)
    data  = {
        "generated_at": time.time(),
        "hosts":         hosts,
        "topology":      get_topology(conn),
        "flap_events":   get_all_flap_events(conn),
        "fdb_summaries": {h["hostid"]: get_fdb_summary(conn, h["hostid"]) for h in hosts},
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nExported frontend data -> {path}")

# ------------------------------------------------------------------ #
#  Polling loop

async def run_poll_rounds(conn, device_map, poll_rounds, poll_interval):
    print(f"\nPolling {len(device_map)} device(s) — {poll_rounds} rounds every {poll_interval}s\n")

    for i in range(poll_rounds):
        print(f"--- Round {i + 1}/{poll_rounds} ---")
        await asyncio.gather(*[
            poll_device(conn, ip, vlans, device_id, mib_mode)
            for ip, (device_id, vlans, mib_mode) in device_map.items()
        ])

        try:
            socketio.emit("round_progress", {"current": i + 1, "total": poll_rounds})
            export_json(conn)
            socketio.emit("reload_data", {})
        except Exception:
            pass

        if i < poll_rounds - 1:
            print(f"  Waiting {poll_interval}s...\n")
            await asyncio.sleep(poll_interval)

# ------------------------------------------------------------------ #
#  Mode coroutines

async def _run_setup():
    client = LibreNMSClient(LIBRENMS_URL, token=LIBRENMS_TOKEN)
    conn   = init_db(DB_PATH)
    await setup_from_librenms(client, conn)
    export_json(conn)


async def _run_single(ip, poll_rounds, poll_interval):
    client = LibreNMSClient(LIBRENMS_URL, token=LIBRENMS_TOKEN)
    conn   = init_db(DB_PATH)
    COMMUNITY_MAP.update(load_all_communities(conn))

    device_id, vlans, mib_mode = await fetch_device_info(client, conn, ip)
    if not device_id or not vlans:
        print(f"Could not prepare device {ip}")
        return

    await run_poll_rounds(conn, {ip: (device_id, vlans, mib_mode)}, poll_rounds, poll_interval)
    export_json(conn)


async def _run_network(poll_rounds, poll_interval):
    client = LibreNMSClient(LIBRENMS_URL, token=LIBRENMS_TOKEN)
    conn   = init_db(DB_PATH)
    COMMUNITY_MAP.update(load_all_communities(conn))

    device_map = {}
    for dev in client.list_devices():
        ip = dev.get("ip") or dev.get("hostname")
        if not ip:
            continue
        device_id, vlans, mib_mode = await fetch_device_info(client, conn, ip)
        if device_id and vlans:
            device_map[ip] = (device_id, vlans, mib_mode)

    if not device_map:
        print("No devices to poll.")
        return

    await run_poll_rounds(conn, device_map, poll_rounds, poll_interval)
    export_json(conn)

# ------------------------------------------------------------------ #
#  Routes

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    try:
        with open("data.json") as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({"hosts": [], "topology": [], "flap_events": [], "fdb_summaries": {}})


@app.route("/api/config")
def api_config():
    return jsonify({"poll_rounds": POLL_ROUNDS, "poll_interval": POLL_INTERVAL})


@app.route("/api/flaps", methods=["DELETE"])
def api_clear_flaps():
    body    = request.get_json(silent=True) or {}
    hostid  = (body.get("hostid") or "").strip() or None
    conn    = init_db(DB_PATH)
    deleted = clear_flap_events(conn, hostid=hostid)
    export_json(conn)
    label = f"host {hostid}" if hostid else "all hosts"
    return jsonify({"status": "cleared", "deleted": deleted, "scope": label})


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    ip   = (body.get("ip") or "").strip()

    try:
        poll_rounds = max(1, min(int(body.get("poll_rounds") or POLL_ROUNDS), 100))
    except (ValueError, TypeError):
        poll_rounds = POLL_ROUNDS

    try:
        poll_interval = max(5, min(int(body.get("poll_interval") or POLL_INTERVAL), 3600))
    except (ValueError, TypeError):
        poll_interval = POLL_INTERVAL

    if mode == "setup":
        _run_in_thread(_run_setup())
    elif mode == "single":
        if not ip:
            return jsonify({"error": "IP required for single mode"}), 400
        _run_in_thread(_run_single(ip, poll_rounds, poll_interval))
    elif mode == "network":
        _run_in_thread(_run_network(poll_rounds, poll_interval))
    else:
        return jsonify({"error": f"Unknown mode: {mode!r}"}), 400

    return jsonify({"status": "started", "poll_rounds": poll_rounds, "poll_interval": poll_interval})


# ------------------------------------------------------------------ #
#  Entry point

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    socketio.run(app, debug=False, port=5000)