import os
import time
import asyncio

from LibreNMS_client import LibreNMSClient
from SNMP_poller import get_mac_table_bridge_mib, snmp_walk, decode_mac_from_oid
from storage import init_db, save_host, save_vlan, save_fdb_entries, get_flaps, save_flap_events, save_mib_mode, get_mib_mode

LIBRENMS_URL   = os.environ.get("LIBRENMS_URL",   "http://10.104.14.40")
LIBRENMS_TOKEN = os.environ.get("LIBRENMS_TOKEN",  "e2ee4018904eedf72757aa0fb09ee5a0")
DB_PATH        = os.environ.get("LIBRENMS_DB",     "LibreNMS_data.db")

MODE          = "single"
TEST_IP       = "10.104.0.112"
POLL_ROUNDS   = 5
POLL_INTERVAL = 5
FLAP_MIN_MOVES = 2   # how many port moves = a confirmed flap

COMMUNITY_MAP = {}   # populated automatically from LibreNMS at startup

SYSOID_TO_MIB = {
    "1.3.6.1.4.1.11":    "bridge",          # HP/Aruba
    "1.3.6.1.4.1.9":     "qbridge",         # Cisco
    "1.3.6.1.4.1.45":    "qbridge",         # Nortel
    "1.3.6.1.4.1.52642": "fiberstore",      # Fiberstore
    "1.3.6.1.4.1.2636":  "qbridge",         # Juniper
    "1.3.6.1.4.1.6027":  "bridge",          # Dell Force10
}

def mib_from_sysoid(sysoid: str):
    if not sysoid:
        return None
    sysoid = sysoid.lstrip(".")
    for prefix, mode in SYSOID_TO_MIB.items():
        if sysoid.startswith(prefix):
            return mode
    return None


async def detect_mib_mode(ip, community, vlans, client):
    print(f"  [{ip}] Detecting MIB mode...")

    # Step 1: sysObjectID lookup via LibreNMS — no SNMP probing needed
    details = client.get_device_snmp_details(ip)
    if details:
        mode = mib_from_sysoid(details.get("sysObjectID", ""))
        if mode:
            print(f"  [{ip}] Identified via sysObjectID -> {mode}")
            return mode

    # Step 2: SNMP probing fallback
    results = await asyncio.gather(*[
        snmp_walk(ip, community, f"1.3.6.1.2.1.17.7.1.2.2.1.2.{v}") for v in vlans[:3]
    ])
    if sum(len(r) for r in results) > 0:
        return "qbridge"

    if await snmp_walk(ip, community, "1.3.6.1.2.1.17.7.1.2.2.1.2"):
        return "qbridge_unscoped"

    if await snmp_walk(ip, community, "1.3.6.1.2.1.17.4.3.1.2"):
        return "bridge"

    return None


async def fetch_device_info(client, conn, ip):
    device = client.get_device(ip)
    if not device:
        print(f"  Could not fetch device info for {ip}, skipping.")
        return None, [], None

    device_id   = str(device.get("device_id"))
    system_name = device.get("sysName") or device.get("hostname")
    save_host(conn, device_id, system_name, ip, device.get("os"), device.get("hardware"))

    vlans_raw = client.get_device_vlans(ip)
    if not vlans_raw:
        print(f"  No VLANs found for {ip}, skipping.")
        return device_id, [], None

    vlans = [v["vlan_vlan"] for v in vlans_raw if v.get("vlan_vlan")]
    for v in vlans:
        save_vlan(conn, v)

    # Get community from LibreNMS and cache it
    snmp_details = client.get_device_snmp_details(ip)
    community = (
        snmp_details.get("community")
        if snmp_details and snmp_details.get("community")
        else "public"
    )
    COMMUNITY_MAP[ip] = community

    # Use cached MIB mode if available, otherwise detect and save
    mib_mode = get_mib_mode(conn, device_id)
    if mib_mode:
        print(f"  [{ip}] Using cached MIB mode: {mib_mode}")
    else:
        mib_mode = await detect_mib_mode(ip, community, vlans, client)
        if mib_mode:
            save_mib_mode(conn, device_id, mib_mode)
            print(f"  [{ip}] Detected and saved MIB mode: {mib_mode}")
        else:
            print(f"  [{ip}] Could not detect MIB mode, skipping.")
            return device_id, [], None

    print(f"  {system_name} ({ip}) — {len(vlans)} VLANs — mode: {mib_mode}")
    return device_id, vlans, mib_mode


async def get_mac_table_qbridge_all_vlans(ip, community, vlans):
    """Fetch bridge/interface tables once, then walk all VLANs concurrently."""
    OID_MAC_TO_BRIDGE   = "1.3.6.1.2.1.17.7.1.2.2.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"

    bridge_entries, name_entries = await asyncio.gather(
        snmp_walk(ip, community, OID_BRIDGE_TO_IFIDX),
        snmp_walk(ip, community, OID_IFIDX_TO_NAME)
    )

    bridge_to_ifidx = {}
    for oid, val in bridge_entries:
        try:
            bridge_to_ifidx[int(oid.split('.')[-1])] = int(val)
        except (ValueError, IndexError):
            continue

    ifidx_to_name = {}
    for oid, val in name_entries:
        try:
            ifidx_to_name[int(oid.split('.')[-1])] = str(val).strip()
        except (ValueError, IndexError):
            continue

    semaphore = asyncio.Semaphore(5)

    async def walk_vlan(vlan):
        async with semaphore:
            mac_entries = await snmp_walk(ip, community, f"{OID_MAC_TO_BRIDGE}.{vlan}")
            results = []
            for oid, b_port_val in mac_entries:
                mac = decode_mac_from_oid(oid)
                if not mac:
                    continue
                try:
                    b_port  = int(b_port_val)
                    if_idx  = bridge_to_ifidx.get(b_port)
                    if_name = ifidx_to_name.get(if_idx, f"Bridge-Port-{b_port}")
                    results.append({
                        "mac":         mac,
                        "port_name":   if_name,
                        "bridge_port": b_port,
                        "if_idx":      if_idx,
                        "vlan":        vlan
                    })
                except (ValueError, TypeError):
                    continue
            return vlan, results

    all_entries = []
    for vlan, entries in await asyncio.gather(*[walk_vlan(v) for v in vlans]):
        if entries:
            print(f"  [{ip}] VLAN {vlan}: {len(entries)} MACs")
        all_entries.extend(entries)

    return all_entries


async def poll_device(conn, ip, vlans, device_id, mib_mode):
    community = COMMUNITY_MAP.get(ip, "public")
    timestamp = time.time()

    if mib_mode == "qbridge":
        print(f"  [{ip}] Q-BRIDGE — polling {len(vlans)} VLANs concurrently")
        entries = await get_mac_table_qbridge_all_vlans(ip, community, vlans)
    else:
        print(f"  [{ip}] {mib_mode} — single walk")
        entries = await get_mac_table_bridge_mib(ip, community)

    if entries:
        save_fdb_entries(conn, device_id, entries, timestamp)
    print(f"  [{ip}] Saved {len(entries)} MAC entries")

    flaps = get_flaps(conn, device_id, last_n_rounds=5, min_moves=FLAP_MIN_MOVES)
    if flaps:
        print(f"  [{ip}] !! {len(flaps)} flap(s) detected:")
        for f in flaps:
            print(f"     MAC {f['mac']} VLAN {f['vlan_id']}: "
                  f"{f['from_port']} -> {f['to_port']} "
                  f"(round {f['from_round']} -> {f['to_round']})")
        save_flap_events(conn, device_id, flaps, timestamp)
    else:
        print(f"  [{ip}] No flaps detected.")

async def main():
    if not LIBRENMS_TOKEN:
        print("LIBRENMS_TOKEN not set; exiting.")
        return

    client = LibreNMSClient(LIBRENMS_URL, token=LIBRENMS_TOKEN)
    conn   = init_db(DB_PATH)
    device_map = {}

    if MODE == "single":
        print(f"Mode: SINGLE — testing {TEST_IP}\n")
        device_id, vlans, mib_mode = await fetch_device_info(client, conn, TEST_IP)
        if device_id and vlans:
            device_map[TEST_IP] = (device_id, vlans, mib_mode)

    elif MODE == "network":
        print("Mode: NETWORK — polling all LibreNMS devices\n")
        devices = client.list_devices()
        if not devices:
            print("No devices found (or API error).")
            return
        for dev in devices:
            ip = dev.get("ip") or dev.get("hostname")
            if not ip:
                continue
            device_id, vlans, mib_mode = await fetch_device_info(client, conn, ip)
            if device_id and vlans:
                device_map[ip] = (device_id, vlans, mib_mode)

    if not device_map:
        print("No devices to poll, exiting.")
        return

    print(f"\nPolling {len(device_map)} device(s) — {POLL_ROUNDS} rounds every {POLL_INTERVAL}s\n")

    for i in range(POLL_ROUNDS):
        print(f"--- Round {i+1}/{POLL_ROUNDS} ---")
        await asyncio.gather(*[
            poll_device(conn, ip, vlans, device_id, mib_mode)
            for ip, (device_id, vlans, mib_mode) in device_map.items()
        ])
        if i < POLL_ROUNDS - 1:
            print(f"  Waiting {POLL_INTERVAL}s...\n")
            await asyncio.sleep(POLL_INTERVAL)

    print("\n=== Run Complete ===")
    print(f"Devices polled : {len(device_map)}")
    print(f"Total rounds   : {POLL_ROUNDS}")
    print(f"FDB entries    : {conn.execute('SELECT COUNT(*) FROM fdb').fetchone()[0]}")
    print(f"Flap events    : {conn.execute('SELECT COUNT(*) FROM flap_events').fetchone()[0]}")

asyncio.run(main())