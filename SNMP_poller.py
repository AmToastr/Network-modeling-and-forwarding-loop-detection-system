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

async def snmp_walk(ip, community, oid, timeout=10, retries=2):
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
                oid_val, value = varBind   # ObjectType supports tuple unpacking
                results.append((str(oid_val), value))

    return results




def decode_mac_from_oid(oid: str):
    if oid == "unknown": return None
    parts = oid.split('.')
    # In Q-BRIDGE, the MAC is the last 6 parts. 
    # The part before that is the VLAN ID.
    if len(parts) < 6: return None
    
    try:
        mac_parts = parts[-6:]
        return ':'.join(f"{int(p):02x}" for p in mac_parts)
    except ValueError:
        return None

async def get_mac_table(ip, community="public", vlan=5):
    OID_MAC_TO_BRIDGE   = "1.3.6.1.2.1.17.7.1.2.2.1.2"
    OID_BRIDGE_TO_IFIDX = "1.3.6.1.2.1.17.1.4.1.2"
    OID_IFIDX_TO_NAME   = "1.3.6.1.2.1.2.2.1.2"

    mac_oid = f"{OID_MAC_TO_BRIDGE}.{vlan}"  # scope to VLAN in OID

    mac_entries, bridge_entries, name_entries = await asyncio.gather(
        snmp_walk(ip, community, mac_oid),
        snmp_walk(ip, community, OID_BRIDGE_TO_IFIDX),
        snmp_walk(ip, community, OID_IFIDX_TO_NAME)
    )

    bridge_to_ifidx = {}
    for oid, val in bridge_entries:
        try:
            b_port = int(oid.split('.')[-1])
            bridge_to_ifidx[b_port] = int(val)
        except (ValueError, IndexError):
            continue

    ifidx_to_name = {}
    for oid, val in name_entries:
        try:
            if_idx = int(oid.split('.')[-1])
            ifidx_to_name[if_idx] = str(val)
        except (ValueError, IndexError):
            continue

    results = []
    for oid, b_port_val in mac_entries:
        mac = decode_mac_from_oid(oid)
        if not mac:
            continue
        try:
            b_port = int(b_port_val)
            if_idx = bridge_to_ifidx.get(b_port)
            if_name = ifidx_to_name.get(if_idx, f"Bridge-Port-{b_port}")
            results.append({
                "mac": mac,
                "port_name": if_name,
                "bridge_port": b_port,
                "if_idx": if_idx,
                "vlan": vlan
            })
        except (ValueError, TypeError):
            continue

    print(f"--- Debug for {ip} VLAN {vlan} ---")
    print(f"MAC Entries found: {len(mac_entries)}")
    print(f"Bridge Mapping found: {len(bridge_entries)}")
    print(f"Interface Names found: {len(name_entries)}")
    return results


async def get_interfaces(ip, community="public"):
    IFNAME_OID = "1.3.6.1.2.1.2.2.1.2"
    entries = await snmp_walk(ip, community, IFNAME_OID)

    interfaces = {}
    for oid_str, value in entries:
        # Get the last part of the OID string (the index)
        try:
            index = int(oid_str.split('.')[-1])
            interfaces[index] = str(value)
        except (ValueError, IndexError):
            continue

    return interfaces






async def get_mac_table_with_ports(ip, community="public", vlan=5):
    mac_table = await get_mac_table(ip, community, vlan)
    interfaces = await get_interfaces(ip, community)

    for entry in mac_table:
        idx = entry["if_idx"]
        entry["port"] = interfaces.get(idx, "unknown")

    return mac_table
