"""pygruenbeck_cloud is a Python library to communicate with the Grünbeck Cloud based Water softeners."""  # noqa: E501
from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
import hashlib
import json
from json import JSONDecodeError
import logging
import random
import socket
from types import TracebackType
from typing import Any

import aiohttp
from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    ContentTypeError,
    ServerDisconnectedError,
    WSMsgType,
    WSServerHandshakeError,
)
from aiohttp.typedefs import StrOrURL
from yarl import URL

from .const import (
    API_GET_MG_INFOS_ENDPOINT,
    API_WS_CLIENT_HEADER,
    API_WS_CLIENT_QUERY,
    API_WS_CLIENT_URL,
    API_WS_HOST,
    API_WS_INITIAL_MESSAGE,
    API_WS_REQUEST_TIMEOUT,
    API_WS_SCHEME_WS,
    LOGIN_CODE_CHALLENGE_CHARS,
    PARAM_NAME_ACCESS_TOKEN,
    PARAM_NAME_CODE,
    PARAM_NAME_CODE_CHALLENGE,
    PARAM_NAME_CODE_VERIFIER,
    PARAM_NAME_CONNECTION_ID,
    PARAM_NAME_CSRF_TOKEN,
    PARAM_NAME_DEVICE_ID,
    PARAM_NAME_ENDPOINT,
    PARAM_NAME_PASSWORD,
    PARAM_NAME_POLICY,
    PARAM_NAME_REFRESH_TOKEN,
    PARAM_NAME_TENANT,
    PARAM_NAME_TRANS_ID,
    PARAM_NAME_USERNAME,
    WEB_REQUESTS,
)
from .exceptions import (
    PyGruenbeckCloudConnectionClosedError,
    PyGruenbeckCloudConnectionError,
    PyGruenbeckCloudError,
    PyGruenbeckCloudMissingAuthTokenError,
    PyGruenbeckCloudResponseError,
    PyGruenbeckCloudResponseStatusError,
)
from .models import Device, GruenbeckAuthToken

_LOGGER = logging.getLogger(__name__)


class PyGruenbeckCloud:
    """Class for communicate with the Grünbeck cloud."""

    session: ClientSession | None = None
    _close_session: bool = False
    _ws_session: ClientSession | None = None
    _ws_client: ClientWebSocketResponse | None = None
    _auth_token: GruenbeckAuthToken | None = None
    _device: Device | None = None
    logger: logging.Logger = logging.getLogger(__name__)

    def __init__(self, username: str, password: str) -> None:
        """Initialize PyGruenbeckCloud Class."""
        self._username = username
        self._password = password

    @staticmethod
    def _placeholder_to_values_dict(
        const: dict[str, str], values: dict[str, str]
    ) -> dict[str, str]:
        """Convert placeholder from dict to value in dict."""
        result = {}
        for key, value in const.items():
            result[key] = value.format(**values)

        return result

    @staticmethod
    def _placeholder_to_values_str(const: str, values: dict[str, str]) -> str:
        """Convert placeholder from str to values in dict."""
        return const.format(**values)

    @staticmethod
    def _extract_from_html_response(
        response: str, search_str: str, sep: str = ","
    ) -> str:
        """Retrieve str from HTML response."""
        start = response.index(search_str) + len(search_str) + 3
        end = response.index(sep, start) - 1

        return response[start:end]

    @staticmethod
    async def _get_code_challenge() -> list[str]:
        """Get Grünbeck Cloud API Code Challenge."""
        challenge_hash = ""
        result = ""

        while (
            challenge_hash == ""
            or "+" in challenge_hash
            or "/" in challenge_hash
            or "=" in challenge_hash
            or "+" in result
            or "/" in result
        ):
            result = "".join(
                random.choice(LOGIN_CODE_CHALLENGE_CHARS) for _ in range(64)
            )
            result = base64.b64encode(result.encode()).decode().rstrip("=")
            hash_object = hashlib.sha256(result.encode())
            challenge_hash = base64.b64encode(hash_object.digest()).decode()[:-1]

        return [result, challenge_hash]

    async def login(self) -> bool:
        """Login to Grünbeck Cloud."""
        code_verifier, code_challenge = await self._get_code_challenge()

        auth_data = await self._login_step1(code_challenge)

        if not await self._login_step2(auth_data):
            self.logger.error("Unable to login")
            return False

        code = await self._login_step3(auth_data)

        response = await self._login_step4(auth_data, code, code_verifier)

        self._auth_token = GruenbeckAuthToken(
            access_token=response["access_token"],
            refresh_token=response["refresh_token"],
            not_before=datetime.fromtimestamp(response["not_before"]),
            expires_on=datetime.fromtimestamp(response["expires_on"]),
            expires_in=response["expires_in"],
            tenant=auth_data["tenant"],
        )

        return True

    async def _login_step1(self, code_challenge: str) -> dict[str, str]:
        scheme = WEB_REQUESTS["login_step_1"]["scheme"]
        host = WEB_REQUESTS["login_step_1"]["host"]
        use_cookies = WEB_REQUESTS["login_step_1"]["use_cookies"]

        headers = WEB_REQUESTS["login_step_1"]["headers"]
        path = WEB_REQUESTS["login_step_1"]["path"]
        method = WEB_REQUESTS["login_step_1"]["method"]
        data = WEB_REQUESTS["login_step_1"]["data"]

        query = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_1"]["query_params"],
            {PARAM_NAME_CODE_CHALLENGE: code_challenge},
        )

        # If we already have cookies, we will get a 302 and our code_challenge will not
        # match, that's why we need to clear our cookies
        if self.session and self.session.cookie_jar:
            self.session.cookie_jar.clear()

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )
        if not isinstance(response, str):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        return {
            "csrf_token": self._extract_from_html_response(
                response=response, search_str="csrf"
            ),
            "transId": self._extract_from_html_response(
                response=response, search_str="transId"
            ),
            "policy": self._extract_from_html_response(
                response=response, search_str="policy"
            ),
            "tenant": self._extract_from_html_response(
                response=response, search_str="tenant"
            ),
        }

    async def _login_step2(self, auth_data: dict[str, str]) -> bool:
        scheme = WEB_REQUESTS["login_step_2"]["scheme"]
        host = WEB_REQUESTS["login_step_2"]["host"]
        use_cookies = WEB_REQUESTS["login_step_2"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_2"]["headers"],
            {PARAM_NAME_CSRF_TOKEN: auth_data["csrf_token"]},
        )

        path = self._placeholder_to_values_str(
            WEB_REQUESTS["login_step_2"]["path"],
            {PARAM_NAME_TENANT: auth_data["tenant"]},
        )

        data = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_2"]["data"],
            {
                PARAM_NAME_USERNAME: self._username,
                PARAM_NAME_PASSWORD: self._password,
            },
        )

        method = WEB_REQUESTS["login_step_2"]["method"]

        query = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_2"]["query_params"],
            {
                PARAM_NAME_TRANS_ID: auth_data["transId"],
                PARAM_NAME_POLICY: auth_data["policy"],
            },
        )

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )

        if "status" in response:
            parsed_response = {}
            if isinstance(response, str):
                parsed_response = json.loads(response)
            elif isinstance(response, dict):
                parsed_response = response
            else:
                msg = f"Incorrect response from {url}"
                raise PyGruenbeckCloudResponseError(msg)

            if parsed_response["status"] == "200":
                return True

        return False

    async def _login_step3(self, auth_data: dict[str, str]) -> str:
        scheme = WEB_REQUESTS["login_step_3"]["scheme"]
        host = WEB_REQUESTS["login_step_3"]["host"]
        use_cookies = WEB_REQUESTS["login_step_3"]["use_cookies"]

        headers = WEB_REQUESTS["login_step_3"]["headers"]
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["login_step_3"]["path"],
            {PARAM_NAME_TENANT: auth_data["tenant"]},
        )
        method = WEB_REQUESTS["login_step_3"]["method"]
        data = WEB_REQUESTS["login_step_3"]["data"]

        query = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_3"]["query_params"],
            {
                PARAM_NAME_CSRF_TOKEN: auth_data["csrf_token"],
                PARAM_NAME_TRANS_ID: auth_data["transId"],
                PARAM_NAME_POLICY: auth_data["policy"],
            },
        )

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        # @TODO - expected_status_code and allow_redirects can also come from CONST!
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            expected_status_code=aiohttp.http.HTTPStatus.FOUND,
            allow_redirects=False,
            use_cookies=use_cookies,
        )

        if not isinstance(response, str):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        start = response.index("code%3d") + 7
        end = response.index(">here") - 1
        return response[start:end]

    async def _login_step4(
        self, auth_data: dict[str, str], code: str, code_verifier: str
    ) -> dict[str, Any]:
        scheme = WEB_REQUESTS["login_step_4"]["scheme"]
        host = WEB_REQUESTS["login_step_4"]["host"]
        use_cookies = WEB_REQUESTS["login_step_4"]["use_cookies"]

        headers = WEB_REQUESTS["login_step_4"]["headers"]
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["login_step_4"]["path"],
            {PARAM_NAME_TENANT: auth_data["tenant"]},
        )
        method = WEB_REQUESTS["login_step_4"]["method"]
        data = self._placeholder_to_values_dict(
            WEB_REQUESTS["login_step_4"]["data"],
            {PARAM_NAME_CODE: code, PARAM_NAME_CODE_VERIFIER: code_verifier},
        )
        query = WEB_REQUESTS["login_step_4"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )

        if not isinstance(response, dict):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        return response

    async def _refresh_web_token(self) -> bool:
        """Refresh Web Access Token."""
        if not isinstance(self._auth_token, GruenbeckAuthToken):
            msg = "Cannot refresh, missing Auth Token."
            raise PyGruenbeckCloudMissingAuthTokenError(msg)

        scheme = WEB_REQUESTS["web_token_refresh"]["scheme"]
        host = WEB_REQUESTS["web_token_refresh"]["host"]
        use_cookies = WEB_REQUESTS["web_token_refresh"]["use_cookies"]

        headers = WEB_REQUESTS["web_token_refresh"]["headers"]
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["web_token_refresh"]["path"],
            {PARAM_NAME_TENANT: self._auth_token.tenant},
        )
        method = WEB_REQUESTS["web_token_refresh"]["method"]
        data = self._placeholder_to_values_dict(
            WEB_REQUESTS["web_token_refresh"]["data"],
            {
                PARAM_NAME_REFRESH_TOKEN: self._auth_token.refresh_token,
            },
        )
        query = WEB_REQUESTS["web_token_refresh"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )
        if not isinstance(response, dict):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        # @TODO - Check response if token is expired!

        self._auth_token.access_token = response["access_token"]
        self._auth_token.refresh_token = response["refresh_token"]
        self._auth_token.not_before = datetime.fromtimestamp(response["not_before"])
        self._auth_token.expires_on = datetime.fromtimestamp(response["expires_on"])
        self._auth_token.expires_in = response["expires_in"]

        return True

    async def get_devices(self) -> list[Device]:
        """Get Devices from Cloud."""
        devices: list[Device] = []

        token = await self._get_web_access_token()

        scheme = WEB_REQUESTS["get_devices"]["scheme"]
        host = WEB_REQUESTS["get_devices"]["host"]
        use_cookies = WEB_REQUESTS["get_devices"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["get_devices"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = WEB_REQUESTS["get_devices"]["path"]
        method = WEB_REQUESTS["get_devices"]["method"]
        data = WEB_REQUESTS["get_devices"]["data"]
        query = WEB_REQUESTS["get_devices"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )

        if not isinstance(response, list):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        for device in response:
            if "soft" in device["id"]:
                devices.append(Device.from_json(device))

        return devices

    @property
    def device(self) -> Device | None:
        """Return current device."""
        return self._device

    async def set_device(self, device: Device) -> None:
        """Async setter for device."""
        self._device = device

        # Set logger for Device
        self._device.logger = self.logger
        try:
            self._device = await self.get_device_infos()
        except PyGruenbeckCloudResponseError as ex:
            msg = "Unable to get device infos"
            raise PyGruenbeckCloudError(msg) from ex

    async def set_device_from_id(self, device_id: str) -> bool:
        """Set device from given device ID."""
        devices = await self.get_devices()

        for device in devices:
            if device.id == device_id:
                await self.set_device(device)
                return True

        return False

    async def get_device_infos(self) -> Device:
        """Retrieve information for device."""
        if self.device is None:
            msg = "You need to select a device first"
            raise PyGruenbeckCloudError(msg)

        data = await self._get_device_infos_request(
            self.device, API_GET_MG_INFOS_ENDPOINT
        )

        if data.get("id") != self.device.id:
            msg = f"Got invalid device id {data.get('id')}, expected {self.device.id}"
            raise PyGruenbeckCloudResponseError(msg)

        return self.device.update_from_json(data)

    #
    # async def get_device_infos_parameters(self, device: Device):
    #     data = await self._get_device_infos_request(
    #         device, API_GET_MG_INFOS_ENDPOINT_PARAMETERS
    #     )
    #
    # async def get_device_infos_salt_measurements(self, device: Device):
    #     data = await self._get_device_infos_request(
    #         device, API_GET_MG_INFOS_ENDPOINT_SALT_MEASUREMENTS
    #     )
    #
    # async def get_device_infos_water_measurements(self, device: Device):
    #     data = await self._get_device_infos_request(
    #         device, API_GET_MG_INFOS_ENDPOINT_WATER_MEASUREMENTS
    #     )

    async def _get_device_infos_request(
        self, device: Device, endpoint: str = ""
    ) -> dict[str, str]:
        """Get Device Infos from API."""
        token = await self._get_web_access_token()

        scheme = WEB_REQUESTS["get_device_infos_request"]["scheme"]
        host = WEB_REQUESTS["get_device_infos_request"]["host"]
        use_cookies = WEB_REQUESTS["get_device_infos_request"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["get_device_infos_request"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["get_device_infos_request"]["path"],
            {
                PARAM_NAME_DEVICE_ID: device.id,
                PARAM_NAME_ENDPOINT: endpoint,
            },
        )
        method = WEB_REQUESTS["get_device_infos_request"]["method"]
        data = WEB_REQUESTS["get_device_infos_request"]["data"]
        query = WEB_REQUESTS["get_device_infos_request"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )
        if not isinstance(response, dict):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        return response

    async def enter_sd(
        self,
    ) -> None:
        """Send enter SD for WS."""
        if self.device is None:
            msg = "You need to select an device first"
            raise PyGruenbeckCloudError(msg)

        device = self.device

        token = await self._get_web_access_token()

        scheme = WEB_REQUESTS["enter_sd"]["scheme"]
        host = WEB_REQUESTS["enter_sd"]["host"]
        use_cookies = WEB_REQUESTS["enter_sd"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["enter_sd"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["enter_sd"]["path"],
            {PARAM_NAME_DEVICE_ID: device.id},
        )
        method = WEB_REQUESTS["enter_sd"]["method"]
        data = WEB_REQUESTS["enter_sd"]["data"]
        query = WEB_REQUESTS["enter_sd"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        # @TODO - expected_status_code and allow_redirects can also come from CONST!
        await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            expected_status_code=202,
            use_cookies=use_cookies,
        )

    async def refresh_sd(self) -> None:
        """Send refresh SD for WS."""
        if self.device is None:
            msg = "You need to select an device first"
            raise PyGruenbeckCloudError(msg)

        device = self.device

        token = await self._get_web_access_token()

        scheme = WEB_REQUESTS["refresh_sd"]["scheme"]
        host = WEB_REQUESTS["refresh_sd"]["host"]
        use_cookies = WEB_REQUESTS["refresh_sd"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["refresh_sd"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["refresh_sd"]["path"],
            {PARAM_NAME_DEVICE_ID: device.id},
        )
        method = WEB_REQUESTS["refresh_sd"]["method"]
        data = WEB_REQUESTS["refresh_sd"]["data"]
        query = WEB_REQUESTS["refresh_sd"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        # @TODO - expected_status_code and allow_redirects can also come from CONST!
        await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            expected_status_code=202,
            use_cookies=use_cookies,
        )

        # Reset ping counter after refreshing
        self.device.ping_counter = 0

    async def leave_sd(self) -> None:
        """Send leave SD for WS."""
        if self.device is None:
            msg = "You need to select an device first"
            raise PyGruenbeckCloudError(msg)

        device = self.device

        token = await self._get_web_access_token()

        scheme = WEB_REQUESTS["leave_sd"]["scheme"]
        host = WEB_REQUESTS["leave_sd"]["host"]
        use_cookies = WEB_REQUESTS["leave_sd"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["leave_sd"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = self._placeholder_to_values_str(
            WEB_REQUESTS["leave_sd"]["path"],
            {PARAM_NAME_DEVICE_ID: device.id},
        )
        method = WEB_REQUESTS["leave_sd"]["method"]
        data = WEB_REQUESTS["leave_sd"]["data"]
        query = WEB_REQUESTS["leave_sd"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        # @TODO - expected_status_code and allow_redirects can also come from CONST!
        await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            expected_status_code=202,
            use_cookies=use_cookies,
        )

    async def _http_request(
        self,
        headers: dict,
        url: StrOrURL,
        data: Any = None,
        expected_status_code: int = aiohttp.http.HTTPStatus.OK,
        method: str = aiohttp.hdrs.METH_GET,
        allow_redirects: bool = False,
        use_cookies: bool = False,
    ) -> str | dict[Any, Any] | list[Any]:
        """Execute HTTP request."""
        if self.session is None:
            self.session = ClientSession()
            self._close_session = True

        try:
            self.logger.debug("Requesting URL %s with method %s", url, method)
            async with self.session.request(
                method=method,
                url=url,
                headers=headers,
                allow_redirects=allow_redirects,
                data=data,
            ) as resp:
                if resp.status != expected_status_code:
                    error = (
                        f"Response status code for {url} is {resp.status},"
                        f" we expected {expected_status_code}."
                    )
                    self.logger.error(error)

                    raise PyGruenbeckCloudResponseStatusError(error)
                try:
                    response = await resp.json()
                except ContentTypeError:
                    response = await resp.text()

                if use_cookies:
                    self.session.cookie_jar.update_cookies(resp.cookies)

                self.logger.debug(
                    "Response from URL %s with status %d was %s",
                    url,
                    resp.status,
                    response,
                )

                if (
                    not isinstance(response, str)
                    and not isinstance(response, dict)
                    and not isinstance(response, list)
                ):
                    msg = f"Response from URL {url} has incorrect type {type(response)}"
                    raise PyGruenbeckCloudResponseError(msg)

                return response
        except (ClientConnectorError, ServerDisconnectedError) as ex:
            self.logger.error("%s", ex)
            raise PyGruenbeckCloudConnectionError(ex) from ex

    @property
    def connected(self) -> bool:
        """Return if we are connected to WebSocket."""
        return self._ws_client is not None and not self._ws_client.closed

    async def connect(self) -> None:
        """Connect to the WebSocket."""
        if self.connected:
            return

        ws_access_token, ws_connection_id = await self._get_ws_tokens()

        query = self._placeholder_to_values_dict(
            API_WS_CLIENT_QUERY,
            {
                PARAM_NAME_CONNECTION_ID: ws_connection_id,
                PARAM_NAME_ACCESS_TOKEN: ws_access_token,
            },
        )
        url = URL.build(
            scheme=API_WS_SCHEME_WS,
            host=API_WS_HOST,
            path=API_WS_CLIENT_URL,
            query=query,
        )

        if self._ws_session is None:
            self._ws_session = ClientSession(
                timeout=ClientTimeout(total=API_WS_REQUEST_TIMEOUT)
            )

        try:
            self._ws_client = await self._ws_session.ws_connect(
                url=url, headers=API_WS_CLIENT_HEADER, heartbeat=30
            )
            # Send initial Message
            await self._ws_client.send_str(API_WS_INITIAL_MESSAGE)
            await self.enter_sd()
            await self.refresh_sd()
        except (
            WSServerHandshakeError,
            ClientConnectionError,
            socket.gaierror,
        ) as ex:
            raise PyGruenbeckCloudConnectionError(ex) from ex

    async def listen(self, callback: Callable[[Device], None]) -> None:
        """Listen for WebSocket messages."""
        if not self._ws_client or not self.connected:
            msg = "We are not connected to WebSocket"
            raise PyGruenbeckCloudConnectionError(msg)

        while not self._ws_client.closed:
            ws_msg = await self._ws_client.receive()
            self.logger.debug("WebSocket Message received: %s", ws_msg.data)

            if ws_msg.type == WSMsgType.ERROR:
                raise PyGruenbeckCloudConnectionError(self._ws_client.exception())

            if ws_msg.type == WSMsgType.TEXT:
                try:
                    # There is a "%1E = Record Separator" char at the end of the string!
                    response = json.loads(ws_msg.data.strip())

                    if response:
                        device = self.device.update_from_response(data=response)  # type: ignore[union-attr]  # noqa: E501
                        callback(device)
                    else:
                        self.logger.debug("Skipping empty response: %s", response)
                except JSONDecodeError:
                    self.logger.debug(
                        "Skipping invalid JSON response: %s", ws_msg.data.strip()
                    )

            if ws_msg.type == WSMsgType.BINARY:
                msg = "WebSocket response is binary type"
                raise PyGruenbeckCloudResponseError(msg)

            if ws_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                msg = "WebSocket connection has been closed"
                raise PyGruenbeckCloudConnectionClosedError(msg)

    async def disconnect(self) -> None:
        """Close open connections."""
        if not self._ws_session or not self.connected:
            return

        await self.leave_sd()
        await self._ws_session.close()

    async def _get_ws_tokens(self) -> list[str]:
        """Get new WebSocket tokens."""
        web_access_token = await self._get_web_access_token()

        _, ws_access_token = await self._start_ws_negotiation(
            access_token=web_access_token
        )
        ws_connection_id = await self._get_ws_connection_id(
            ws_access_token=ws_access_token
        )

        return [ws_access_token, ws_connection_id]

    async def _start_ws_negotiation(self, access_token: str) -> list[str]:
        """Start WebSocket connection negotiation."""
        scheme = WEB_REQUESTS["start_ws_negotiation"]["scheme"]
        host = WEB_REQUESTS["start_ws_negotiation"]["host"]
        use_cookies = WEB_REQUESTS["start_ws_negotiation"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["start_ws_negotiation"]["headers"],
            {PARAM_NAME_ACCESS_TOKEN: access_token},
        )
        path = WEB_REQUESTS["start_ws_negotiation"]["path"]
        method = WEB_REQUESTS["start_ws_negotiation"]["method"]
        data = WEB_REQUESTS["start_ws_negotiation"]["data"]

        query = WEB_REQUESTS["start_ws_negotiation"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )

        if not isinstance(response, dict):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        return [response["url"], response["accessToken"]]

    async def _get_ws_connection_id(self, ws_access_token: str) -> str:
        """Get WebSocket Connection ID."""
        scheme = WEB_REQUESTS["get_ws_connection_id"]["scheme"]
        host = WEB_REQUESTS["get_ws_connection_id"]["host"]
        use_cookies = WEB_REQUESTS["get_ws_connection_id"]["use_cookies"]

        headers = self._placeholder_to_values_dict(
            WEB_REQUESTS["get_ws_connection_id"]["headers"],
            {PARAM_NAME_ACCESS_TOKEN: ws_access_token},
        )
        path = WEB_REQUESTS["get_ws_connection_id"]["path"]
        method = WEB_REQUESTS["get_ws_connection_id"]["method"]
        data = WEB_REQUESTS["get_ws_connection_id"]["data"]

        query = WEB_REQUESTS["get_ws_connection_id"]["query_params"]

        url = URL.build(scheme=scheme, host=host, path=path, query=query)
        response = await self._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            use_cookies=use_cookies,
        )

        if not isinstance(response, dict):
            msg = f"Incorrect response from {url}"
            raise PyGruenbeckCloudResponseError(msg)

        return response["connectionId"]  # type: ignore[no-any-return]

    async def _get_web_access_token(self) -> str:
        """Get current WebSocket token."""
        if not isinstance(self._auth_token, GruenbeckAuthToken):
            await self.login()

        # Refreshes the token if needed
        if not self._auth_token.is_expired():  # type: ignore[union-attr]
            return self._auth_token.access_token  # type: ignore[union-attr]

        refresh = await self._refresh_web_token()
        if not refresh:
            self.logger.info("Unable to refresh token, need to relogin.")
            await self.login()

        return self._auth_token.access_token  # type: ignore[union-attr]

    async def close(self) -> None:
        """Close all connections."""
        await self.disconnect()

        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self) -> PyGruenbeckCloud:
        """Start PyGruenbeckCloud class from context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop PyGruenbeckCloud class from context manager."""
        await self.close()
