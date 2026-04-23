import requests
from typing import List, Dict, Any, Optional


class LibreNMSClient:

    def __init__(self, base_url: str, token: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

        self.headers = {
            "X-Auth-Token": self.token,
            "Content-Type": "application/json"
        }


    # --------------------- Request helper --------------------- #
    def _request(self, endpoint: str) -> Optional[Any]:
        url = f"{self.base_url}{endpoint}"

        try:
            r = requests.get(url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None
        

    # --------------------- Request snmp details --------------------- #  
    def get_device_snmp_details(self, hostname: str):
        """Get SNMP details LibreNMS uses for this device."""
        res = self._request(f"/api/v0/devices/{hostname}")
        if not res:
            return None
        device = res.get("devices", [None])[0]
        if not device:
            return None
        return {
            "community":  device.get("community"),
            "snmpver":    device.get("snmpver"),
            "transport":  device.get("transport"),
            "port":       device.get("port"),
            "sysObjectID": device.get("sysObjectID"),
        }
    

    # --------------------- Request trunk ports --------------------- #  
    def get_trunk_ports(self, hostname: str):
        """Returns set of port names that are trunk/uplink ports."""
        res = self._request(f"/api/v0/devices/{hostname}/ports")
        if not res:
            return set()
        trunk_ports = set()
        for p in res.get("ports", []):
            ifname = p.get("ifName")
            if not ifname:
                continue
            # A port is a trunk if it has multiple VLANs or is explicitly tagged
            if p.get("ifTrunk") == "Trunk" or p.get("ifVlan") == 0:
                trunk_ports.add(ifname)
        return trunk_ports


    # --------------------- List devices --------------------- #
    def list_devices(self) -> Optional[List[Dict[str, Any]]]:
        res = self._request("/api/v0/devices")

        if not res:
            return None

        return res.get("devices")


    # --------------------- Get device by hostname --------------------- #
    def get_device(self, hostname: str) -> Optional[Dict[str, Any]]:
        res = self._request(f"/api/v0/devices/{hostname}")

        if not res:
            return None

        return res.get("devices", [None])[0]


    # --------------------- Get device ports --------------------- #
    def get_device_ports(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}/ports")

        if not res:
            return None

        return res.get("ports")


    # --------------------- Get device VLANs --------------------- #
    def get_device_vlans(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}/vlans")

        if not res:
            return None

        return res.get("vlans")
    


































    # --------------------- Get device FDB --------------------- #
    def get_device_fdb(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}/fdb")

        if not res:
            return []

        fdb_entries = []

        for entry in res.get("ports_fdb", []):

            fdb_entries.append({
                "mac": entry.get("mac_address"),
                "vlan": entry.get("vlan_id"),
                "port_id": entry.get("port_id")
            })

        return fdb_entries
    

    # --------------------- Get device port map --------------------- #
    def get_port_map(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}/ports")

        if not res:
            return {}

        port_map = {}

        for p in res.get("ports", []):

            port_map[p["port_id"]] = p.get("ifName")

        return port_map
    

    # --------------------- Get device FDB with ports --------------------- #
    def get_device_fdb_with_ports(self, hostname: str):
        fdb = self.get_device_fdb(hostname)
        port_map = self.get_port_map(hostname)

        for entry in fdb:

            port_id = entry["port_id"]

            entry["port"] = port_map.get(port_id, "unknown")

        return fdb