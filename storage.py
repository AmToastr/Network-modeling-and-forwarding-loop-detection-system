import sqlite3
import time
from collections import defaultdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    hostid      TEXT PRIMARY KEY,
    hostname    TEXT,
    ip_address  TEXT,
    os          TEXT,
    hardware    TEXT
);

CREATE TABLE IF NOT EXISTS device_mib (
    hostid      TEXT PRIMARY KEY,
    mib_mode    TEXT,
    detected_at REAL
);

CREATE TABLE IF NOT EXISTS topology (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    local_hostid     TEXT,
    local_port       TEXT,
    remote_hostid    TEXT,
    remote_port      TEXT,
    discovered_at    REAL,
    UNIQUE(local_hostid, local_port, remote_hostid)
);

CREATE TABLE IF NOT EXISTS fdb (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    hostid    TEXT,
    mac       TEXT,
    vlan_id   INTEGER,
    port      TEXT,
    seen_at   REAL,
    round_id  INTEGER,
    FOREIGN KEY (hostid) REFERENCES hosts(hostid)
);

CREATE TABLE IF NOT EXISTS flap_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hostid       TEXT,
    mac          TEXT,
    vlan_id      INTEGER,
    from_port    TEXT,
    to_port      TEXT,
    detected_at  REAL,
    round_id     INTEGER
);
"""


# ------------------------------------------------------------------ #
#  Init
# ------------------------------------------------------------------ #

def init_db(path: str = "LibreNMS_data.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ------------------------------------------------------------------ #
#  Hosts
# ------------------------------------------------------------------ #

def save_host(conn, hostid, hostname, ip_address, os, hardware):
    conn.execute(
        "INSERT OR REPLACE INTO hosts(hostid, hostname, ip_address, os, hardware) "
        "VALUES(?,?,?,?,?)",
        (hostid, hostname, ip_address, os, hardware)
    )
    conn.commit()

def get_all_hosts(conn):
    rows = conn.execute("SELECT * FROM hosts").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
#  MIB mode cache
# ------------------------------------------------------------------ #

def save_mib_mode(conn, hostid, mib_mode):
    conn.execute(
        "INSERT OR REPLACE INTO device_mib(hostid, mib_mode, detected_at) VALUES(?,?,?)",
        (hostid, mib_mode, time.time())
    )
    conn.commit()

def get_mib_mode(conn, hostid):
    row = conn.execute(
        "SELECT mib_mode FROM device_mib WHERE hostid = ?", (hostid,)
    ).fetchone()
    return row["mib_mode"] if row else None


# ------------------------------------------------------------------ #
#  Topology
# ------------------------------------------------------------------ #

def save_topology(conn, local_hostid, links):
    """Save LLDP links. remote_device_id maps directly to hostid."""
    now = time.time()
    for link in links:
        remote_hostid = link.get("remote_device_id")
        if not remote_hostid:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO topology
               (local_hostid, local_port, remote_hostid, remote_port, discovered_at)
               VALUES(?,?,?,?,?)""",
            (local_hostid, link.get("local_port_id"),
             remote_hostid, link.get("remote_port"), now)
        )
    conn.commit()

def get_topology(conn):
    """Returns all switch-to-switch links with hostnames and IPs."""
    rows = conn.execute("""
        SELECT
            t.local_hostid,  h1.hostname AS local_hostname,  h1.ip_address AS local_ip,
            t.local_port,
            t.remote_hostid, h2.hostname AS remote_hostname, h2.ip_address AS remote_ip,
            t.remote_port
        FROM topology t
        JOIN hosts h1 ON t.local_hostid  = h1.hostid
        JOIN hosts h2 ON t.remote_hostid = h2.hostid
    """).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
#  FDB
# ------------------------------------------------------------------ #

def get_next_round_id(conn, hostid):
    row = conn.execute(
        "SELECT MAX(round_id) FROM fdb WHERE hostid = ?", (hostid,)
    ).fetchone()[0]
    return (row or 0) + 1

def save_fdb_entries(conn, hostid, entries, timestamp):
    round_id = get_next_round_id(conn, hostid)
    conn.executemany(
        "INSERT INTO fdb(hostid, mac, vlan_id, port, seen_at, round_id) "
        "VALUES(?,?,?,?,?,?)",
        [
            (hostid, e["mac"], e["vlan"], e["port_name"].strip(), timestamp, round_id)
            for e in entries
        ]
    )
    conn.commit()

def get_fdb_summary(conn, hostid):
    """MAC count per VLAN for the latest round — used by frontend."""
    rows = conn.execute("""
        SELECT vlan_id, COUNT(DISTINCT mac) as mac_count
        FROM fdb
        WHERE hostid = ?
          AND round_id = (SELECT MAX(round_id) FROM fdb WHERE hostid = ?)
        GROUP BY vlan_id
        ORDER BY mac_count DESC
    """, (hostid, hostid)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
#  Flap detection
# ------------------------------------------------------------------ #

def get_flaps(conn, hostid, last_n_rounds=5, min_moves=2):
    """
    Detect MAC flapping within the last N rounds.
    Only reports MACs that moved ports at least min_moves times.
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

    seen = defaultdict(dict)
    for row in rows:
        seen[(row["mac"], row["vlan_id"])][row["round_id"]] = row["port"]

    flaps = []
    for (mac, vlan_id), round_map in seen.items():
        sorted_rounds = sorted(round_map.keys())
        moves = []
        for i in range(1, len(sorted_rounds)):
            prev_round = sorted_rounds[i - 1]
            curr_round = sorted_rounds[i]
            prev_port  = round_map[prev_round]
            curr_port  = round_map[curr_round]
            if prev_port != curr_port:
                moves.append({
                    "mac":        mac,
                    "vlan_id":    vlan_id,
                    "from_port":  prev_port,
                    "to_port":    curr_port,
                    "from_round": prev_round,
                    "to_round":   curr_round,
                })
        if len(moves) >= min_moves:
            flaps.extend(moves)

    return flaps

def save_flap_events(conn, hostid, flaps, timestamp):
    if not flaps:
        return
    conn.executemany(
        """INSERT INTO flap_events
           (hostid, mac, vlan_id, from_port, to_port, detected_at, round_id)
           VALUES(?,?,?,?,?,?,?)""",
        [(hostid, f["mac"], f["vlan_id"], f["from_port"], f["to_port"],
          timestamp, f["to_round"]) for f in flaps]
    )
    conn.commit()
    print(f"  Saved {len(flaps)} flap event(s) to DB.")

def get_all_flap_events(conn, hostid=None, limit=200):
    """Fetch flap events for frontend — optionally filtered by host."""
    if hostid:
        rows = conn.execute("""
            SELECT f.*, h.hostname, h.ip_address
            FROM flap_events f
            JOIN hosts h ON f.hostid = h.hostid
            WHERE f.hostid = ?
            ORDER BY f.detected_at DESC
            LIMIT ?
        """, (hostid, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT f.*, h.hostname, h.ip_address
            FROM flap_events f
            JOIN hosts h ON f.hostid = h.hostid
            ORDER BY f.detected_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]