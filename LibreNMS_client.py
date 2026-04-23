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

    # --------------------- Get device VLANs --------------------- #
    def get_device_vlans(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}/vlans")
        if not res:
            return None
        return res.get("vlans")

    # --------------------- Get SNMP details --------------------- #
    def get_device_snmp_details(self, hostname: str):
        res = self._request(f"/api/v0/devices/{hostname}")
        if not res:
            return None
        device = res.get("devices", [None])[0]
        if not device:
            return None
        return {
            "community":   device.get("community"),
            "snmpver":     device.get("snmpver"),
            "transport":   device.get("transport"),
            "port":        device.get("port"),
            "sysObjectID": device.get("sysObjectID"),
        }

    # --------------------- Get LLDP neighbours --------------------- #
    def get_device_links(self, hostname: str) -> List[Dict[str, Any]]:
        """
        Returns LLDP links for a device.
        Only returns links where remote_device_id > 0 (managed switches).
        """
        res = self._request(f"/api/v0/devices/{hostname}/links")
        if not res:
            return []
        links = []
        for link in res.get("links", []):
            # Skip links to unmanaged devices (APs, phones, etc)
            if not link.get("remote_device_id"):
                continue
            links.append({
                "local_port_id":    link.get("local_port_id"),
                "remote_device_id": str(link.get("remote_device_id")),
                "remote_port":      link.get("remote_port"),
                "remote_hostname":  link.get("remote_hostname"),
            })
        return links