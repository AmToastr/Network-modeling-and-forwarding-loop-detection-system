import logging
import sqlite3
import time
from collections import defaultdict

log = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS device_communities (
    hostid      TEXT PRIMARY KEY,
    community   TEXT NOT NULL,
    updated_at  REAL NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_fdb_host_round ON fdb(hostid, round_id);

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

def init_db(path="LibreNMS_data.db"):
    conn = sqlite3.connect(path, check_same_thread=False)
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
        (hostid, hostname, ip_address, os, hardware),
    )
    conn.commit()


def get_all_hosts(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM hosts").fetchall()]


# ------------------------------------------------------------------ #
#  MIB mode cache
# ------------------------------------------------------------------ #

def save_mib_mode(conn, hostid, mib_mode):
    conn.execute(
        "INSERT OR REPLACE INTO device_mib(hostid, mib_mode, detected_at) VALUES(?,?,?)",
        (hostid, mib_mode, time.time()),
    )
    conn.commit()


def get_mib_mode(conn, hostid):
    row = conn.execute(
        "SELECT mib_mode FROM device_mib WHERE hostid = ?", (hostid,)
    ).fetchone()
    return row["mib_mode"] if row else None


# ------------------------------------------------------------------ #
#  SNMP community persistence  (Step 2)
# ------------------------------------------------------------------ #

def save_community(conn, hostid, community):
    """Persist an SNMP community string so it survives server restarts."""
    conn.execute(
        "INSERT OR REPLACE INTO device_communities(hostid, community, updated_at) "
        "VALUES(?,?,?)",
        (hostid, community, time.time()),
    )
    conn.commit()


def get_community(conn, hostid):
    """Return the stored community for a host, or None if unknown."""
    row = conn.execute(
        "SELECT community FROM device_communities WHERE hostid = ?", (hostid,)
    ).fetchone()
    return row["community"] if row else None


def load_all_communities(conn):
    """
    Return a dict mapping ip_address -> community for every host that has
    both a stored community and a known IP.  Used to warm COMMUNITY_MAP on
    startup without hitting LibreNMS again.
    """
    rows = conn.execute("""
        SELECT h.ip_address, dc.community
        FROM device_communities dc
        JOIN hosts h ON dc.hostid = h.hostid
        WHERE h.ip_address IS NOT NULL
    """).fetchall()
    return {r["ip_address"]: r["community"] for r in rows}


# ------------------------------------------------------------------ #
#  Topology
# ------------------------------------------------------------------ #

def save_topology(conn, local_hostid, links):
    """Upsert LLDP links. remote_device_id is the LibreNMS device_id (= hostid)."""
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
             remote_hostid, link.get("remote_port"), now),
        )
    conn.commit()


def get_topology(conn):
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

def _next_round_id(conn, hostid):
    row = conn.execute(
        "SELECT MAX(round_id) FROM fdb WHERE hostid = ?", (hostid,)
    ).fetchone()[0]
    return (row or 0) + 1


def save_fdb_entries(conn, hostid, entries, timestamp):
    round_id = _next_round_id(conn, hostid)
    conn.executemany(
        "INSERT INTO fdb(hostid, mac, vlan_id, port, seen_at, round_id) "
        "VALUES(?,?,?,?,?,?)",
        [
            (hostid, e["mac"], e["vlan"], e["port_name"].strip(), timestamp, round_id)
            for e in entries
        ],
    )
    conn.commit()


def prune_old_fdb(conn, hostid, keep_rounds=20):
    """
    Delete FDB rows older than the most recent `keep_rounds` rounds for a
    given host.  Call this after every poll round to prevent unbounded growth.
    Returns the number of rows deleted.
    """
    max_round = conn.execute(
        "SELECT MAX(round_id) FROM fdb WHERE hostid = ?", (hostid,)
    ).fetchone()[0]

    if not max_round or max_round <= keep_rounds:
        return 0  # not enough history yet — nothing to prune

    cutoff = max_round - keep_rounds
    cursor = conn.execute(
        "DELETE FROM fdb WHERE hostid = ? AND round_id <= ?", (hostid, cutoff)
    )
    conn.commit()
    deleted = cursor.rowcount
    if deleted:
        log.debug("[%s] Pruned %d old FDB row(s) (rounds <= %d)", hostid, deleted, cutoff)
    return deleted


def get_fdb_summary(conn, hostid):
    """MAC count per VLAN for the latest round."""
    rows = conn.execute("""
        SELECT vlan_id, COUNT(DISTINCT mac) AS mac_count
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
    Returns only MACs that changed ports at least min_moves times.
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

    # { (mac, vlan_id) : { round_id: port } }
    seen = defaultdict(dict)
    for row in rows:
        seen[(row["mac"], row["vlan_id"])][row["round_id"]] = row["port"]

    flaps = []
    for (mac, vlan_id), round_map in seen.items():
        moves = []
        for prev_r, curr_r in zip(sorted(round_map), sorted(round_map)[1:]):
            if round_map[prev_r] != round_map[curr_r]:
                moves.append({
                    "mac":        mac,
                    "vlan_id":    vlan_id,
                    "from_port":  round_map[prev_r],
                    "to_port":    round_map[curr_r],
                    "from_round": prev_r,
                    "to_round":   curr_r,
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
          timestamp, f["to_round"]) for f in flaps],
    )
    conn.commit()
    log.info("Saved %d flap event(s) for host %s", len(flaps), hostid)


def get_all_flap_events(conn, hostid=None, limit=200):
    base = """
        SELECT f.*, h.hostname, h.ip_address
        FROM flap_events f
        JOIN hosts h ON f.hostid = h.hostid
        {where}
        ORDER BY f.detected_at DESC
        LIMIT ?
    """
    if hostid:
        rows = conn.execute(
            base.format(where="WHERE f.hostid = ?"), (hostid, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            base.format(where=""), (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_flap_events(conn, hostid=None):
    """
    Delete flap events.  If hostid is given, only that host's events are
    removed; otherwise ALL flap events are cleared.
    Returns the number of rows deleted.
    """
    if hostid:
        cursor = conn.execute("DELETE FROM flap_events WHERE hostid = ?", (hostid,))
    else:
        cursor = conn.execute("DELETE FROM flap_events")
    conn.commit()
    deleted = cursor.rowcount
    log.info("Cleared %d flap event(s) (hostid=%s)", deleted, hostid or "ALL")
    return deleted