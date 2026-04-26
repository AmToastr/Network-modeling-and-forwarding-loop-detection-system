import logging
import asyncio
from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
    walk_cmd,
)

log = logging.getLogger(__name__)

# One shared engine for the process lifetime
_engine = SnmpEngine()


async def snmp_walk(ip, community, oid, timeout=10, retries=2):
    results = []
    try:
        transport = await UdpTransportTarget.create(
            (ip, 161), timeout=timeout, retries=retries
        )
    except Exception as e:
        log.warning("[%s] Transport error: %s", ip, e)
        return results

    async for (err_indication, err_status, _, var_binds) in walk_cmd(
        _engine,
        CommunityData(community, mpModel=1),
        transport,
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False,
    ):
        if err_indication:
            log.warning("[%s] SNMP error: %s", ip, err_indication)
            break
        if err_status:
            log.warning("[%s] SNMP error: %s", ip, err_status.prettyPrint())
            break
        for var_bind in var_binds:
            results.append((str(var_bind[0]), var_bind[1]))

    return results


def _parse_last_int(oid):
    """Return the last OID segment as an int, or None."""
    try:
        return int(oid.rsplit(".", 1)[-1])
    except (ValueError, IndexError):
        return None


def decode_mac_from_oid(oid):
    """Extract MAC address from the last 6 octets of an OID."""
    parts = oid.split(".")
    if len(parts) < 6:
        return None
    try:
        return ":".join(f"{int(p):02x}" for p in parts[-6:])
    except ValueError:
        return None


def _build_port_map(bridge_entries, name_entries):
    """Build bridge-port -> ifIndex and ifIndex -> ifName lookup dicts."""
    bridge_to_ifidx = {}
    for oid, val in bridge_entries:
        key = _parse_last_int(oid)
        if key is None:
            continue
        try:
            bridge_to_ifidx[key] = int(val)
        except (ValueError, TypeError):
            pass

    ifidx_to_name = {}
    for oid, val in name_entries:
        key = _parse_last_int(oid)
        if key is None:
            continue
        ifidx_to_name[key] = str(val).strip()

    return bridge_to_ifidx, ifidx_to_name


def _make_entry(mac, b_port, bridge_to_ifidx, ifidx_to_name, vlan):
    try:
        if_idx  = bridge_to_ifidx.get(b_port)
        if_name = ifidx_to_name.get(if_idx, f"Bridge-Port-{b_port}")
        return {
            "mac":         mac,
            "port_name":   if_name,
            "bridge_port": b_port,
            "if_idx":      if_idx,
            "vlan":        vlan,
        }
    except TypeError:
        return None


# ------------------------------------------------------------------ #
#  BRIDGE-MIB  (HP / bridge / qbridge_unscoped)
# ------------------------------------------------------------------ #

async def get_mac_table_bridge_mib(ip, community="public"):
    OID_MAC_TO_PORT     = "1.3.6.1.2.1.17.4.3.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"
    OID_MAC_TO_VLAN     = "1.3.6.1.2.1.17.7.1.2.2.1.2"

    mac_entries, bridge_entries, name_entries, vlan_entries = await asyncio.gather(
        snmp_walk(ip, community, OID_MAC_TO_PORT),
        snmp_walk(ip, community, OID_BRIDGE_TO_IFIDX),
        snmp_walk(ip, community, OID_IFIDX_TO_NAME),
        snmp_walk(ip, community, OID_MAC_TO_VLAN),
    )

    bridge_to_ifidx, ifidx_to_name = _build_port_map(bridge_entries, name_entries)

    # OID suffix format: <vlan>.<mac[6]>  — vlan is at index [-7]
    mac_to_vlan = {}
    for oid, _ in vlan_entries:
        parts = oid.split(".")
        if len(parts) < 7:
            continue
        try:
            mac = ":".join(f"{int(p):02x}" for p in parts[-6:])
            mac_to_vlan[mac] = int(parts[-7])
        except (ValueError, IndexError):
            continue

    results = []
    for oid, b_port_val in mac_entries:
        mac = decode_mac_from_oid(oid)
        if not mac:
            continue
        try:
            entry = _make_entry(
                mac, int(b_port_val), bridge_to_ifidx, ifidx_to_name,
                mac_to_vlan.get(mac, 0)
            )
            if entry:
                results.append(entry)
        except (ValueError, TypeError):
            continue

    log.debug("[%s] BRIDGE-MIB: %d MACs, %d VLANs mapped",
              ip, len(results), len(mac_to_vlan))
    return results


# ------------------------------------------------------------------ #
#  Q-BRIDGE  (Cisco / Nortel / Juniper) — concurrent per-VLAN walk
# ------------------------------------------------------------------ #

async def get_mac_table_qbridge_all_vlans(ip, community, vlans, concurrency=10):
    OID_MAC_TO_BRIDGE   = "1.3.6.1.2.1.17.7.1.2.2.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"

    bridge_entries, name_entries = await asyncio.gather(
        snmp_walk(ip, community, OID_BRIDGE_TO_IFIDX),
        snmp_walk(ip, community, OID_IFIDX_TO_NAME),
    )
    bridge_to_ifidx, ifidx_to_name = _build_port_map(bridge_entries, name_entries)

    semaphore = asyncio.Semaphore(concurrency)

    async def walk_vlan(vlan):
        async with semaphore:
            mac_entries = await snmp_walk(ip, community, f"{OID_MAC_TO_BRIDGE}.{vlan}")
            results = []
            for oid, b_port_val in mac_entries:
                mac = decode_mac_from_oid(oid)
                if not mac:
                    continue
                try:
                    entry = _make_entry(
                        mac, int(b_port_val), bridge_to_ifidx, ifidx_to_name, vlan
                    )
                    if entry:
                        results.append(entry)
                except (ValueError, TypeError):
                    continue
            if results:
                log.debug("[%s] VLAN %d: %d MACs", ip, vlan, len(results))
            return vlan, results

    all_entries = []
    for _, entries in await asyncio.gather(*[walk_vlan(v) for v in vlans]):
        all_entries.extend(entries)

    return all_entries