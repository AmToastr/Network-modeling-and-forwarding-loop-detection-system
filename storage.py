import sqlite3
import time
from collections import defaultdict
from typing import List, Dict, Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    hostid TEXT PRIMARY KEY,
    hostname TEXT,
    ip_address TEXT,
    os TEXT,
    hardware TEXT
);

CREATE TABLE IF NOT EXISTS vlans (
    vlan_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vlan_id INTEGER UNIQUE
);

CREATE TABLE IF NOT EXISTS fdb (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostid TEXT,
    mac TEXT,
    vlan_id INTEGER,
    port TEXT,
    seen_at REAL,
    round_id INTEGER,
    FOREIGN KEY (hostid) REFERENCES hosts(hostid)
);

CREATE TABLE IF NOT EXISTS flap_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostid TEXT,
    mac TEXT,
    vlan_id INTEGER,
    from_port TEXT,
    to_port TEXT,
    detected_at REAL,
    round_id INTEGER
);
"""

def init_db(path: str = "LibreNMS_client.db"):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def save_host(conn, hostid: str, hostname: str, ip_address: str, os: str, hardware: str):
    conn.execute("INSERT OR REPLACE INTO hosts(hostid, hostname, ip_address, os, hardware) VALUES(?, ?, ?, ?, ?)", (hostid, hostname, ip_address, os, hardware))
    conn.commit()

def save_vlan(conn, vlan_id: int):
    conn.execute("INSERT OR IGNORE INTO vlans(vlan_id) VALUES(?)", (vlan_id,))
    conn.commit()
def get_next_round_id(conn, hostid: str) -> int:
    row = conn.execute(
        "SELECT MAX(round_id) FROM fdb WHERE hostid = ?", (hostid,)
    ).fetchone()[0]
    return (row or 0) + 1


def save_fdb_entries(conn, hostid: str, entries: list, timestamp: float):
    round_id = get_next_round_id(conn, hostid)
    conn.executemany(
        "INSERT INTO fdb(hostid, mac, vlan_id, port, seen_at, round_id) VALUES(?,?,?,?,?,?)",
        [
            (hostid, e["mac"], e["vlan"], e["port_name"].strip(), timestamp, round_id)
            for e in entries
        ]
    )
    conn.commit()
    return round_id


def get_flaps(conn, hostid: str, last_n_rounds: int = 5):
    """
    Compare each round against the previous one.
    A flap is when the same MAC+VLAN appears on a different port.
    """
    max_round = conn.execute(
        "SELECT MAX(round_id) FROM fdb WHERE hostid = ?", (hostid,)
    ).fetchone()[0]

    if not max_round or max_round < 2:
        return []

    min_round = max(1, max_round - last_n_rounds)

    rows = conn.execute("""
        SELECT mac, vlan_id, port, round_id
        FROM fdb
        WHERE hostid = ? AND round_id >= ?
        ORDER BY mac, vlan_id, round_id
    """, (hostid, min_round)).fetchall()

    # Group by (mac, vlan) -> {round_id: port}
    seen = defaultdict(dict)
    for mac, vlan_id, port, round_id in rows:
        seen[(mac, vlan_id)][round_id] = port

    flaps = []
    for (mac, vlan_id), round_map in seen.items():
        sorted_rounds = sorted(round_map.keys())
        for i in range(1, len(sorted_rounds)):
            prev_round = sorted_rounds[i - 1]
            curr_round = sorted_rounds[i]
            prev_port  = round_map[prev_round]
            curr_port  = round_map[curr_round]
            if prev_port != curr_port:
                flaps.append({
                    "mac":       mac,
                    "vlan_id":   vlan_id,
                    "from_port": prev_port,
                    "to_port":   curr_port,
                    "from_round": prev_round,
                    "to_round":   curr_round,
                })

    return flaps

def save_flap_events(conn, hostid: str, flaps: list, timestamp: float):
    if not flaps:
        return
    conn.executemany(
        """INSERT INTO flap_events(hostid, mac, vlan_id, from_port, to_port, detected_at, round_id)
           VALUES(?,?,?,?,?,?,?)""",
        [
            (hostid, f["mac"], f["vlan_id"], f["from_port"], f["to_port"], timestamp, f["to_round"])
            for f in flaps
        ]
    )
    conn.commit()
    print(f"  Saved {len(flaps)} flap event(s) to DB.")