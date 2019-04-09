"""Python client library for the Genius Hub API.

see: https://my.geniushub.co.uk/docs
"""
import asyncio
from hashlib import sha256
import logging

import aiohttp

from .const import (
    API_STATUS_ERROR,
    DEFAULT_INTERVAL_V1, DEFAULT_INTERVAL_V3,
    DEFAULT_TIMEOUT_V1, DEFAULT_TIMEOUT_V3,
    ITYPE_TO_TYPE, IMODE_TO_MODE,
    LEVEL_TO_TEXT, DESCRIPTION_TO_TEXT,
    zone_types, zone_modes, kit_types)

HTTP_OK = 200  # cheaper than: from http import HTTPStatus.OK

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.WARNING)


def _convert_zone(input) -> dict:
    """Convert v3 zone dict/json to v1 schema."""
    result = {}
    result['id'] = input['iID']
    result['type'] = ITYPE_TO_TYPE[input['iType']]
    result['name'] = input['strName']

    if input['iType'] in [zone_types.ControlSP, zone_types.TPI]:
        result['temperature'] = input['fPV']
        result['setpoint'] = input['fSP']

    if input['iType'] == zone_types.OnOffTimer:
        result['setpoint'] = input['fSP'] != 0

    result['mode'] = IMODE_TO_MODE[input['iMode']]

    # l = parseInt(i.iFlagExpectedKit) & e.equipmentTypes.Kit_PIR
    if input['iFlagExpectedKit'] & kit_types.PIR:
        # = parseInt(i.iMode) === e.zoneModes.Mode_Footprint
        u = input['iMode'] == zone_modes.Footprint
        # = null != (s = i.zoneReactive) ? s.bTriggerOn : void 0,
        d = input['objFootprint']['objReactive']['bTriggerOn']
        # = parseInt(i.iActivity) || 0,
        # c = input['iActivity'] | 0
        # o = t.isInFootprintNightMode(i)
        o = input['objFootprint']['bIsNight']
        # u && l && d && !o ? True : False
        result['occupied'] = u and d and not o

    if input['iType'] in [zone_types.OnOffTimer,
                          zone_types.ControlSP,
                          zone_types.TPI]:
        result['override'] = {}
        result['override']['duration'] = input['iBoostTimeRemaining']
        if input['iType'] == zone_types.OnOffTimer:
            result['override']['setpoint'] = (input['fBoostSP'] != 0)
        else:
            result['override']['setpoint'] = input['fBoostSP']

        result['schedule'] = {}

    return result


def _convert_device(input) -> dict:
    """Convert v3 device dict/json to v1 schema."""

    # if device['addr'] not in ['1', 'WeatherData']:
    result = {}
    result['id'] = input['addr']
    node = input['childNodes']['_cfg']['childValues']
    if node:
        result['type'] = node['name']['val']
        result['sku'] = node['sku']['val']
    else:
        result['type'] = None

    tmp = input['childValues']['location']['val']
    if tmp:
        result['assignedZones'] = [{'name': tmp}]
    else:
        result['assignedZones'] = [{'name': None}]

    result['state'] = {}

    return result


def _extract_zones_from_zones(input) -> list:
    """Extract zones from /v3/data_manager JSON."""

    return input


def _extract_devices_from_data_manager(input) -> list:
    """Extract devices from /v3/data_manager JSON."""

    _LOGGER.error("input = %s", input)
    result = []
    for k1, v1 in input['childNodes'].items():
        if k1 != 'WeatherData':  # or: k1 not in ['1', 'WeatherData']
            for k2, device in v1['childNodes'].items():
                if device['addr'] != '1':
                    result.append(_convert_device(device))

    return result


def _extract_devices_from_zones(input) -> list:
    """Extract devices from /v3/zones JSON."""

    result = []
    for zone in input:
        if 'nodes' in zone:
            for node in zone['nodes']:
                if node['addr'] not in ['1', 'WeatherData']:
                    result.append(node)

    return result


class GeniusHubClient(object):
    def __init__(self, hub_id, username=None, password=None, session=None):
        _LOGGER.debug("GeniusHubClient(hub_id=%s)", hub_id)

        if True:  # debug is True:
            _LOGGER.setLevel(logging.DEBUG)
            _LOGGER.debug("Debug mode is explicitly enabled.")
        else:
            _LOGGER.debug("Debug mode is not explicitly enabled "
                          "(but may be enabled elsewhere).")

        # use existing session if provided
        self._session = session if session else aiohttp.ClientSession()

        # if no credentials, then hub_id is a token for v1 API
        self._api_v1 = not (username or password)
        if self._api_v1:
            self._auth = None
            self._url_base = 'https://my.geniushub.co.uk/v1/'
            self._headers = {'authorization': "Bearer " + hub_id}
            self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_V1)
            self._poll_interval = DEFAULT_INTERVAL_V1
        else:  # using API ver3
            hash = sha256()
            hash.update((username + password).encode('utf-8'))
            self._auth = aiohttp.BasicAuth(
                login=username, password=hash.hexdigest())
            self._url_base = 'http://{}:1223/v3/'.format(hub_id)
            self._headers = {"Connection": "close"}
            self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_V3)
            self._poll_interval = DEFAULT_INTERVAL_V3

        self._verbose = False
        self._hub_id = hub_id[:20] + "..." if len(hub_id) > 20 else hub_id

        self.hub = GeniusHub(self, hub_id)

    @property
    def verbose(self) -> int:
        return self._verbose

    @verbose.setter
    def verbose(self, value):
        self._verbose = 0 if value is None else value


class GeniusObject(object):
    def __init__(self, client, hub=None, zone=None, device=None, data={}):
        self.__dict__.update(data)

        self._client = client
        self._api_v1 = client._api_v1

        if isinstance(self, GeniusHub):
            self.zone_objs = []
            self.zone_by_id = {}
            self.zone_by_name = {}

            self.device_objs = []
            self.device_by_id = {}

        elif isinstance(self, GeniusZone):
            self._hub = hub

            self.device_objs = []
            self.device_by_id = {}

        elif isinstance(self, GeniusDevice):
            self._hub = hub
            self._zone = zone

    def _without_keys(self, dict_obj, keys) -> dict:
        _info = dict(dict_obj)
        _info = {k: v for k, v in _info.items() if k[:1] != '_'}
        _info = {k: v for k, v in _info.items() if k not in keys}
        return _info

    async def _handle_assetion(self, error):
        _LOGGER.debug("_handle_assetion(error=%s)", error)

    async def _request(self, type, url, data=None):
        _LOGGER.debug("_request(type=%s, url='%s')", type, url)

        http_method = {
            "GET": self._client._session.get,
            "PATCH": self._client._session.patch,
            "POST": self._client._session.post,
            "PUT": self._client._session.put,
        }.get(type)

        # concurrent.futures._base.TimeoutError
        async with http_method(
            self._client._url_base + url,
            json=data,
            headers=self._client._headers,
            auth=self._client._auth,
            timeout=self._client._timeout
        ) as response:
            assert response.status == HTTP_OK, response.text
            return await response.json(content_type=None)

    @staticmethod
    def LookupStatusError(status):
        return API_STATUS_ERROR.get(status, str(status) + " Unknown status")


class GeniusHub(GeniusObject):
    # connection.post("/v3/system/reboot", { username: e, password: t,json: {}} )
    # connection.get("/v3/auth/test", { username: e, password: t, timeout: n })

    def __init__(self, client, hub_id):
        _LOGGER.debug("GeniusHub(hub=%s)", hub_id[:20] + "...")
        super().__init__(client, data={'id': hub_id[:20] + "..."})

        self._info = {}  # a dict of attrs
        self._zones = []  # a list of dicts
        self._devices = []  # a list of dicts
        self._issues = []  # a list of dicts

        self._issues_raw = self._devices_raw = self._zones_raw = None

    async def update(self, force_refresh=False):
        """Update the Hub with its latest state data."""
        _LOGGER.debug("Hub(%s).update()", self.id)

        def _populate_zone(zone_dict):
            hub = self  # for now, only Hubs invoke this method

            id_no = zone_dict['id']
            try:  # does the hub already know about this device?
                zone = hub.zone_by_id[id_no]
            except KeyError:
                _LOGGER.debug("Creating a Zone (hub=%s, zone=%s)", hub.id, zone_dict['id'])
                zone = GeniusZone(self._client, hub, zone_dict)
                hub.zone_objs.append(zone)
                hub.zone_by_id[zone.id] = zone
                hub.zone_by_name[zone.name] = zone
            else:
                _LOGGER.debug("Found a Zone (hub=%s, zone=%s)", hub.id, zone_dict['id'])

        def _populate_device(device_dict, parent=None):
            if isinstance(self, GeniusHub):
                hub = self

                name = device_dict['assignedZones'][0]['name']  # or parent if None?
                zone = hub.zone_by_name[name] if name else None
                # print("zone:", zone.id if zone else None, "device:", device_dict['id'])
            else:
                hub = self.hub
                zone = self

            idx = device_dict['id']
            try:  # does the Hub already know about this device?
                device = hub.device_by_id[idx]
            except KeyError:
                _LOGGER.debug("Creating a Device (hub=%s, device=%s)", hub.id, device_dict['id'])
                device = GeniusDevice(self, hub, zone, device_dict)
                hub.device_objs.append(device)
                hub.device_by_id[device.id] = device
            else:
                _LOGGER.debug("Found a Device (hub=%s, device=%s)",
                              hub.id, device_dict['id'])

            if zone:
                try:  # does the (parent) Zone already know about this device?
                    device = zone.device_by_id[idx]
                except KeyError:
                    _LOGGER.debug("Adding a Device (zone=%s, device=%s)", zone.id, device_dict['id'])
                    zone.device_objs.append(device)
                    zone.device_by_id[device.id] = device
                else:
                    _LOGGER.debug("Found a Device (zone=%s, device=%s)", zone.id, device_dict['id'])

            if isinstance(self, GeniusZone):  # TODO: this code may be redundant
                print("LOOK FOR THIS IN THE LIBRARY")
                try:  # does the zone already know about this device?
                    device = self.device_by_id[idx]
                except KeyError:
                    self.device_objs.append(device)
                    self.device_by_id[device.id] = device

        for zone in await self._get_zones:
            _populate_zone(zone)
        for device in await self._get_devices:
            _populate_device(device)

        _LOGGER.debug("Hub(%s) len(hub.zone_objs)", self.id, len(self.zone_objs))
        _LOGGER.debug("Hub(%s) len(hub.device_objs)", self.id, len(self.device_objs))

    @property
    def info(self) -> dict:
        """Return all information for the hub."""
        _LOGGER.debug("Hub(%s).info", self.id)

        def _convert_to_v1(input) -> dict:
            """Convert v3 output to v1 schema."""
            output = dict(input)
            output['schedule'] = {}
            output['schedule']['timer'] = {}
            output['schedule']['footprint'] = {}
            return output

        keys = ['device_objs', 'device_by_id', 'zone_objs', 'zone_by_id', 'zone_by_name']
        info = self._without_keys(self.__dict__, keys)

        _LOGGER.debug("Hub(%s).info = %s", self.id, info)
        return info

    @property
    async def version(self) -> dict:
        """Return the current software version(s) of the system.

          This is a v1 API only.
        """
        if self._api_v1:
            url = 'version'
            self._version = await self._request("GET", url)
        else:
            self._version = {
                'hubSoftwareVersion': 'unable to determine via v3 API'
            }

        _LOGGER.debug("self._version = %s", self._version)
        return self._version

    @property
    async def _get_zones(self) -> list:
        """Return a list of zones included in the system.

          This is a v1 API: GET /zones
        """
        # getAllZonesData = x.get("/v3/zones", {username: e, password: t})
        url = 'zones'
        raw_json = await self._request("GET", url)
        raw_json = raw_json if self._api_v1 else raw_json['data']

        if self._api_v1:
            self._zones = raw_json
        else:
            self._zones = []
            for zone in _extract_zones_from_zones(raw_json):
                self._zones.append(_convert_zone(zone))

        self._zones_raw = raw_json
        self._zones.sort(key=lambda s: int(s['id']))

        _LOGGER.debug("GeniusHub.zones = %s", self._zones)
        return self._zones_raw

    @property
    async def zones(self) -> list:
        """Return a list of Zones known to the Hub.

          v1/zones/summary: id, name
          v1/zones: id, name, type, mode, temperature, setpoint, occupied,
          override, schedule
        """
        _LOGGER.debug("Hub().zones: len(self.zone_objs) = %s", len(self.zone_objs))

        if not self._zones:
            # await self._get_zones
            self._zones = []
            for zone in self.zone_objs:
                self._zones.append(zone.info)

        _LOGGER.debug("Hub().zones: self._devices = %s", self._zones)
        return self._zones

    @property
    async def _get_devices(self) -> list:
        """Return a list of devices included in the system.

          This is a v1 API: GET /devices
        """
        # getDeviceList = x.get("/v3/data_manager", {username: e, password: t})

        if self._api_v1:
            url = 'devices' if self._api_v1 else 'zones'  # or: 'data_manager'
            raw_json = await self._request("GET", url)
            raw_json = raw_json if self._api_v1 else raw_json['data']
        else:
            # WORKAROUND: There's a aiohttp.ServerDisconnectedError on 2nd HTTP
            # method (get v3/zones x2 or get v3/zones & get /data_manager) if
            # it is done the v1 way (above) for v3
            raw_json = self._zones_raw if self._zones_raw else await self._get_zones

        if self._api_v1:
            self._devices = raw_json
        else:
            self._devices = []
            # r device in _extract_devices_from_data_manager(raw_json):
            for device in _extract_devices_from_zones(raw_json):
                self._devices.append(_convert_device(device))

        self._devices_raw = raw_json
        self._devices.sort(key=lambda s: s['id'])

        _LOGGER.debug("GeniusHub.devices = %s", self._devices)
        return self._devices_raw

    @property
    async def devices(self) -> list:
        """Return a list of Devices known to the Hub.

          v1/devices/summary: id, type
          v1/devices: id, type, assignedZones, state
        """
        _LOGGER.debug("Hub().devices: len(self.device_objs) = %s", len(self.device_objs))

        if not self._devices:
            # await self._get_devices
            self._devices = []
            for device in self.device_objs:
                self._devices.append(device.info)

        _LOGGER.debug("Hub().devices: self._devices = %s", self._devices)
        return self._devices

    @property
    async def _get_issues(self) -> list:
        """Return a list of currently identified issues with the system.

          This is a v1 API: GET /issues
        """
        def _convert_to_v1(input) -> list:
            """Convert v3 output to v1 schema."""
            output = []
            for zone in input['data']:
                for issue in zone['lstIssues']:
                    message = DESCRIPTION_TO_TEXT[issue['id']]

                    tmp = {}
                    tmp['description'] = message.format(zone['strName'])
                    tmp['level'] = LEVEL_TO_TEXT[issue['level']]

                    output.append(tmp)

            return output

        # url = 'issues' if self._api_v1 else 'zones'
        raw_json = await self._request("GET", 'issues')

        self._issues = raw_json if self._api_v1 else _convert_to_v1(raw_json)

        _LOGGER.debug("GeniusHub.issues = %s", self._issues)
        return raw_json if self._client._verbose else self._issues

    @property
    async def issues(self) -> list:
        """Return a list of Issues known to the Hub."""

        if not self._issues:
            await self._get_issues
        return self._issues


class GeniusZone(GeniusObject):
    def __init__(self, client, hub, zone_dict):
        _LOGGER.debug("GeniusZone(hub=%s, zone=%s)",
                      hub.id, zone_dict['id'])
        super().__init__(client, data=zone_dict)

        self._info = {}
        self._devices = []
        self._issues = []

        self._issues_raw = self._devices_raw = None

    @property
    def info(self) -> dict:
        """Return all information for a zone."""
        _LOGGER.debug("Zone(%s).info", self.id)

        keys = ['device_objs', 'device_by_id']
        info = self._without_keys(self.__dict__, keys)

        _LOGGER.debug("Zone(%s).info = %s", self.id, info)
        return info

    @property
    async def _get_devices(self) -> list:
        """Return information for devices assigned to a zone.

          This is a v1 API: GET /zones/{zoneId}devices
        """
        url = 'zones/{}/devices'
        self._devices = await self._request("GET", url.format(self.id))

        for device in await self.devices:
            _populate_device(device, parent=self)

        _LOGGER.debug("self.devices = %s", self._devices)
        return self._devices

    @property
    async def devices(self) -> list:
        """Return information for devices assigned to a zone.

          This is a v1 API: GET /zones/{zoneId}devices
        """
        _LOGGER.debug("Zone(%s).devices: len(self.device_objs) = %s", self.id, len(self.device_objs))

        if not self._devices:
            # await self._get_devices
            self._devices = []
            for device in self.device_objs:
                self._devices.append(device.info)

        _LOGGER.debug("Zone(%s).devices: self._devices = %s", self.id, self._devices)
        return self._devices

    async def set_mode(self, mode):
        """Set the mode of the zone.

          mode is in {'off', 'timer', footprint', 'override'}
        """
        _LOGGER.debug("set_mode(%s): mode=%s", self.id, mode)

        if self._api_v1:
            url = 'zones/{}/mode'
            await self._request("PUT", url.format(self.id), data=mode)
        else:
            # 'off'       'data': {'iMode': 1}}
            # 'footprint' 'data': {'iMode': 4}}
            # 'timer'     'data': {'iMode': 2}}
            url = 'zone/{}'
            data = {'iMode': mode}
            await self._request("PATCH", url.format(self.id), data=data)

        _LOGGER.debug("set_mode(%s): done.", self.id)                            # TODO: remove this line

    async def set_override(self, duration, setpoint):
        """Set the zone to override to a certain temperature.

          duration is in seconds
          setpoint is in degrees Celsius
        """
        _LOGGER.debug("set_override_temp(%s): duration=%s, setpoint=%s", self.id, duration, setpoint)

        if self._api_v1:
            url = 'zones/{}/override'
            data = {'duration': duration, 'setpoint': setpoint}
            await self._request("POST", url.format(self.id), data=data)
        else:
            # 'override'  'data': {'iMode': 16, 'iBoostTimeRemaining': 3600, 'fBoostSP': temp}}
            url = 'zone/{}'
            data = {'iMode': 16,
                    'iBoostTimeRemaining': duration,
                    'fBoostSP': setpoint}
            await self._request("PATCH", url.format(self.id), data=data)

        _LOGGER.debug("set_override_temp(%s): done.", self.id)                   # TODO: remove this line

    async def update(self):
        """Update the Zone with its latest state data."""
        _LOGGER.debug("Zone(%s).update()", self.id)

        url = 'zones/{}'
        data = await self._request("GET", url.format(self.id))
        self.__dict__.update(data)


class GeniusDevice(GeniusObject):
    def __init__(self, client, hub, zone, device_dict):
        _LOGGER.debug("GeniusZone(hub=%s, zone=%s,device=%s)",
                      hub.id, zone, device_dict['id'])
        super().__init__(client, data=device_dict)

        self._info = {}
        self._issues = []

        self._issues_raw = None

    @property
    def info(self) -> dict:
        """Return all information for a device."""
        _LOGGER.debug("Device(%s).info: type = %s", self.id, type(self))

        keys = []
        info = self._without_keys(self.__dict__, keys)
        _LOGGER.debug("Device(%s).info = %s", self.id, info)
        return info

    @property
    async def location(self) -> dict:  # aka assignedZones
        raise NotImplementedError()

    async def update(self):
        """Update the Device with its latest state data."""
        _LOGGER.debug("Device(%s).update()", self.id)

        url = 'devices/{}'
        data = await self._request("GET", url.format(self.id))
        self.__dict__.update(data)
