import os
import time
import asyncio

from LibreNMS_client import LibreNMSClient
from SNMP_poller import get_mac_table
from storage import init_db, save_host, save_vlan, save_fdb_entries, get_flaps, save_flap_events

LIBRENMS_URL   = os.environ.get("LIBRENMS_URL",   "http://10.104.14.40")
LIBRENMS_TOKEN = os.environ.get("LIBRENMS_TOKEN",  "")
DB_PATH        = os.environ.get("LIBRENMS_DB",     "LibreNMS_data.db")

# --- Mode ---
# "single" : test against one switch
# "network": poll all switches from LibreNMS
MODE = "single"

# --- Single switch config (used when MODE = "single") ---
TEST_IP = "10.104.0.103"

# --- Polling config ---
POLL_ROUNDS   = 20
POLL_INTERVAL = 30

# --- Community strings per switch IP ---
COMMUNITY_MAP = {
    "10.104.0.103": "private",
    # "10.104.0.x":  "public1",
    # add more as you expand
}


async def poll_vlan(ip, community, vlan):
    try:
        entries = await get_mac_table(ip, community, vlan=vlan)
        return vlan, entries
    except Exception as e:
        print(f"  SNMP error {ip} VLAN {vlan}: {e}")
        return vlan, []


async def poll_device(conn, ip, vlans, device_id):
    community = COMMUNITY_MAP.get(ip, "public")
    timestamp = time.time()

    tasks = [poll_vlan(ip, community, v) for v in vlans]
    results = await asyncio.gather(*tasks)

    total = 0
    for vlan, entries in results:
        if entries:
            save_fdb_entries(conn, device_id, entries, timestamp)
            total += len(entries)

    print(f"  [{ip}] Saved {total} MAC entries across {len(vlans)} VLANs")

    flaps = get_flaps(conn, device_id, last_n_rounds=5)
    if flaps:
        print(f"  [{ip}] !! {len(flaps)} flap(s) detected:")
        for f in flaps:
            print(f"     MAC {f['mac']} VLAN {f['vlan_id']}: "
                  f"{f['from_port']} -> {f['to_port']} "
                  f"(round {f['from_round']} -> {f['to_round']})")
        save_flap_events(conn, device_id, flaps, timestamp)
    else:
        print(f"  [{ip}] No flaps detected.")


def fetch_device_info(client, conn, ip):
    """Fetch device info from LibreNMS, save to DB, return (device_id, vlans)."""
    device = client.get_device(ip)
    if not device:
        print(f"  Could not fetch device info for {ip}, skipping.")
        return None, []

    device_id   = str(device.get("device_id"))
    system_name = device.get("sysName") or device.get("hostname")
    hardware    = device.get("hardware")
    os_name     = device.get("os")
    save_host(conn, device_id, system_name, ip, os_name, hardware)

    vlans_raw = client.get_device_vlans(ip)
    if not vlans_raw:
        print(f"  No VLANs found for {ip}, skipping.")
        return device_id, []

    vlans = [v["vlan_vlan"] for v in vlans_raw if v.get("vlan_vlan")]
    for v in vlans:
        save_vlan(conn, v)

    print(f"  {system_name} ({ip}) — {len(vlans)} VLANs: {vlans}")
    return device_id, vlans


async def main():
    if not LIBRENMS_TOKEN:
        print("LIBRENMS_TOKEN not set; exiting.")
        return

    client = LibreNMSClient(LIBRENMS_URL, token=LIBRENMS_TOKEN)
    conn   = init_db(DB_PATH)

    # --- Build device list based on mode ---
    # device_map: { ip -> (device_id, vlans) }
    device_map = {}

    if MODE == "single":
        print(f"Mode: SINGLE — testing {TEST_IP}\n")
        device_id, vlans = fetch_device_info(client, conn, TEST_IP)
        if device_id and vlans:
            device_map[TEST_IP] = (device_id, vlans)

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
            if ip not in COMMUNITY_MAP:
                print(f"  Skipping {ip} — no community string in COMMUNITY_MAP")
                continue
            device_id, vlans = fetch_device_info(client, conn, ip)
            if device_id and vlans:
                device_map[ip] = (device_id, vlans)

    if not device_map:
        print("No devices to poll, exiting.")
        return

    print(f"\nPolling {len(device_map)} device(s) — "
          f"{POLL_ROUNDS} rounds every {POLL_INTERVAL}s\n")

    # --- Polling loop ---
    for i in range(POLL_ROUNDS):
        print(f"--- Round {i+1}/{POLL_ROUNDS} ---")

        # All devices polled concurrently per round
        round_tasks = [
            poll_device(conn, ip, vlans, device_id)
            for ip, (device_id, vlans) in device_map.items()
        ]
        await asyncio.gather(*round_tasks)

        if i < POLL_ROUNDS - 1:
            print(f"  Waiting {POLL_INTERVAL}s...\n")
            await asyncio.sleep(POLL_INTERVAL)

    # --- Summary ---
    print("\n=== Run Complete ===")
    print(f"Devices polled : {len(device_map)}")
    print(f"Total rounds   : {POLL_ROUNDS}")
    print(f"FDB entries    : {conn.execute('SELECT COUNT(*) FROM fdb').fetchone()[0]}")
    print(f"Flap events    : {conn.execute('SELECT COUNT(*) FROM flap_events').fetchone()[0]}")


asyncio.run(main())