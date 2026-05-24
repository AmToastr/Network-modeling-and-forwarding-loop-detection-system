import logging
import requests

log = logging.getLogger(__name__)


class LibreNMSClient:

    def __init__(self, base_url, token, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self.session  = requests.Session()
        self.session.headers.update({
            "X-Auth-Token": token,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------ #
    #  Request helper

    def _get(self, endpoint):
        url = f"{self.base_url}{endpoint}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning("HTTP %s for %s", e.response.status_code, url)
        except requests.RequestException as e:
            log.warning("Request failed for %s: %s", url, e)
        return None

    # ------------------------------------------------------------------ #
    #  Public methods

    def list_devices(self):
        res = self._get("/api/v0/devices")
        return res.get("devices", []) if res else []

    def get_device(self, ip):
        res = self._get(f"/api/v0/devices/{ip}")
        if not res:
            return None
        devices = res.get("devices") or []
        return devices[0] if devices else None

    def get_device_vlans(self, ip):
        """Returns a flat list of VLAN IDs for the device."""
        res = self._get(f"/api/v0/devices/{ip}/vlans")
        if not res:
            return []
        return [v["vlan_vlan"] for v in res.get("vlans", []) if v.get("vlan_vlan")]

    def get_device_snmp_details(self, ip):
        device = self.get_device(ip)
        if not device:
            return None
        return {
            "community":   device.get("community"),
            "snmpver":     device.get("snmpver"),
            "transport":   device.get("transport"),
            "port":        device.get("port"),
            "sysObjectID": device.get("sysObjectID"),
        }

    def get_device_links(self, ip):
        """LLDP neighbours — only managed devices (remote_device_id set)."""
        res = self._get(f"/api/v0/devices/{ip}/links")
        if not res:
            return []
        return [
            {
                "local_port_id":    link.get("local_port_id"),
                "remote_device_id": str(link["remote_device_id"]),
                "remote_port":      link.get("remote_port"),
                "remote_hostname":  link.get("remote_hostname"),
            }
            for link in res.get("links", [])
            if link.get("remote_device_id")
        ]