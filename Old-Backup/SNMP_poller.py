import asyncio
from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
    walk_cmd
)


async def snmp_walk(ip, community, oid, timeout=15, retries=3):
    results = []
    transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=retries)
    engine = SnmpEngine()

    async for (errorIndication, errorStatus, errorIndex, varBindTable) in walk_cmd(
        engine,
        CommunityData(community, mpModel=1),
        transport,
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
        lexicographicMode=False
    ):
        if errorIndication:
            print(f"{ip} SNMP error:", errorIndication)
            break
        elif errorStatus:
            print(f"{ip} SNMP error:", errorStatus.prettyPrint())
            break
        else:
            for varBind in varBindTable:
                oid_val, value = varBind
                results.append((str(oid_val), value))

    return results


def decode_mac_from_oid(oid: str):
    """Extract MAC from Q-BRIDGE OID — last 6 octets."""
    parts = oid.split('.')
    if len(parts) < 6:
        return None
    try:
        return ':'.join(f"{int(p):02x}" for p in parts[-6:])
    except ValueError:
        return None


async def get_mac_table(ip, community="public", vlan=5):
    """
    Q-BRIDGE per-VLAN MAC table fetch (Nortel/scoped style).
    OID scoped to specific VLAN: ...17.7.1.2.2.1.2.<vlan>
    """
    OID_MAC_TO_BRIDGE   = "1.3.6.1.2.1.17.7.1.2.2.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"

    mac_entries, bridge_entries, name_entries = await asyncio.gather(
        snmp_walk(ip, community, f"{OID_MAC_TO_BRIDGE}.{vlan}"),
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
                "mac":        mac,
                "port_name":  if_name,
                "bridge_port": b_port,
                "if_idx":     if_idx,
                "vlan":       vlan
            })
        except (ValueError, TypeError):
            continue

    print(f"--- Debug for {ip} VLAN {vlan} ---")
    print(f"MAC Entries: {len(mac_entries)} | Bridge: {len(bridge_entries)} | Interfaces: {len(name_entries)}")
    return results


async def get_mac_table_bridge_mib(ip, community="public"):
    """
    Classic BRIDGE-MIB MAC table fetch (HP/universal style).
    Single walk returns all MACs — no VLAN scoping needed.
    VLAN info extracted from Q-BRIDGE OID where available.
    """
    OID_MAC_TO_PORT     = "1.3.6.1.2.1.17.4.3.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"
    OID_MAC_TO_VLAN     = "1.3.6.1.2.1.17.7.1.2.2.1.2"  # best-effort VLAN info

    mac_entries, bridge_entries, name_entries, vlan_entries = await asyncio.gather(
        snmp_walk(ip, community, OID_MAC_TO_PORT),
        snmp_walk(ip, community, OID_BRIDGE_TO_IFIDX),
        snmp_walk(ip, community, OID_IFIDX_TO_NAME),
        snmp_walk(ip, community, OID_MAC_TO_VLAN),
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

    # Extract MAC -> VLAN from Q-BRIDGE OID
    # Format: ...1.2.2.1.2.<vlan>.<6 mac octets>
    mac_to_vlan = {}
    for oid, _ in vlan_entries:
        parts = oid.split('.')
        if len(parts) < 7:
            continue
        try:
            mac = ':'.join(f"{int(p):02x}" for p in parts[-6:])
            mac_to_vlan[mac] = int(parts[-7])
        except (ValueError, IndexError):
            continue

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
                "mac":        mac,
                "port_name":  if_name,
                "bridge_port": b_port,
                "if_idx":     if_idx,
                "vlan":       mac_to_vlan.get(mac, 0)  # 0 = unknown
            })
        except (ValueError, TypeError):
            continue

    print(f"--- Debug for {ip} (BRIDGE-MIB) ---")
    print(f"MAC Entries: {len(mac_entries)} | Bridge: {len(bridge_entries)} | Interfaces: {len(name_entries)} | VLANs: {len(mac_to_vlan)}")
    return results