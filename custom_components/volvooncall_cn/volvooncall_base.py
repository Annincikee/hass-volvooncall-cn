import logging

from datetime import timedelta, timezone
from urllib.parse import urljoin
import asyncio
import time
import json
import re
from datetime import datetime
import math

from aiohttp import ClientSession, ClientTimeout, ClientSession
from aiohttp.hdrs import METH_GET, METH_POST

import hashlib
import hmac
import urllib.parse
from urllib.parse import urlparse
from datetime import datetime


_LOGGER = logging.getLogger(__name__)

DIGITALVOLVO_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    "X-Ca-Version": "1.0",
    "x-sdk-content-sha256": "UNSIGNED-PAYLOAD",
    "version": "5.53.1",
    "Accept": "application/json; charset=utf-8",
}

DIGITALVOLVO_URL = "https://apigateway.digitalvolvo.com"

TIMEOUT = timedelta(seconds=10)
MAX_RETRIES = 3
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 30

# The pile list carries the live order id, so it must not outlive a poll cycle.
CHARGE_PILE_CACHE_TTL = 15
CHARGE_ORDER_CACHE_TTL = 30 * 60


class VolvoAPIError(Exception):
    def __init__(self, message):
        self.message = message


def redact_sensitive(value):
    """Return a log-safe string without credentials or vehicle identifiers."""
    text = str(value)
    sensitive_keys = (
        "authorization|password|refreshToken|X-Token|phone|phoneNumber|vin|"
        "vinCode|deviceid|uuid|connectorId|orderNo|tradeNo|memberId|latitude|"
        "longitude"
    )
    text = re.sub(
        rf"((?:{sensitive_keys})=)[^&\s'\"]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+)[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(X-Token['\"]?\s*[:=]\s*['\"]?)[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"((?:{sensitive_keys})['\"]?\s*[:=]\s*['\"]?)"
        r"[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "<redacted-phone>",
        text,
    )
    text = re.sub(
        r"(?<![A-HJ-NPR-Z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9]))"
        r"(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*\d)"
        r"[A-HJ-NPR-Z0-9]{17}",
        "<redacted-vin>",
        text,
        flags=re.IGNORECASE,
    )
    return text


def vehicle_log_ref(vin):
    """Return a stable, non-reversible reference suitable for log messages."""
    digest = hashlib.sha256(str(vin).encode("utf-8")).hexdigest()[:10]
    return f"vehicle-{digest}"


class VehicleBaseAPI:
    def __init__(self, session, username, password):
        self._session: ClientSession = session
        self._username = username
        self._password = password

        self._refresh_token = ""
        self._digitalvolvo_access_token = ""
        self._digitalvolvo_x_token = ""
        self._vocapi_access_token = ""
        self._access_token_expire_at = 0
        self._charge_piles_cache = {}
        self._charge_order_cache = {}

    async def _request_digitalvolvo(
        self, method, url, headers, *, max_attempts=MAX_RETRIES, **kwargs
    ):
        for i in range(max_attempts):
            try:
                final_headers = {}
                for k in DIGITALVOLVO_HEADERS:
                    final_headers[k] = DIGITALVOLVO_HEADERS[k]

                for k in headers:
                    final_headers[k] = headers[k]

                if self._digitalvolvo_access_token:
                    final_headers["authorization"] = "Bearer " + self._digitalvolvo_access_token

                if self._digitalvolvo_x_token:
                    final_headers["X-Token"] = self._digitalvolvo_x_token

                sign = sign_request(url, method, kwargs.get('body', None))
                final_headers["x-sdk-date"] = sign['x-sdk-date']
                final_headers["v587sign"] = sign['v587sign']
                final_headers["User-Agent"] = 'vca-android'

                async with self._session.request(
                        method,
                        url,
                        headers=final_headers,
                        timeout=ClientTimeout(total=TIMEOUT.seconds),
                        **kwargs
                ) as response:
                    response.raise_for_status()
                    res = await response.json(loads=json_loads)

                    # Some endpoints (e.g. plugAndCharge) omit "success" and
                    # only report failure via a non-200 "code".
                    if not res.get("success", res.get("code") == 200):
                        raise VolvoAPIError(res.get("errMsg") or res.get("msg"))

                    return res
            except VolvoAPIError:
                # Business rejections are deterministic and must not be replayed.
                raise
            except Exception as error:
                _LOGGER.warning(
                    "Failure when communicating with the server (%s): %s",
                    type(error).__name__,
                    redact_sensitive(error),
                )
                if i < max_attempts - 1:  # Don't delay on last attempt
                    await asyncio.sleep(2**i)  # Exponential backoff
                else:
                    raise

    async def digitalvolvo_get(self, url, headers):
        """Perform a query to the online service."""
        return await self._request_digitalvolvo(METH_GET, url, headers)

    async def digitalvolvo_post(
        self, url, headers, data, *, max_attempts=MAX_RETRIES
    ):
        """Perform a query to the online service."""
        return await self._request_digitalvolvo(
            METH_POST,
            url,
            headers,
            max_attempts=max_attempts,
            json=data,
        )

    async def login(self):
        now = int(time.time())

        # A live session is kept fresh by update_token() using the refresh
        # token. Only fall back to a full password login when there is no
        # usable session to refresh (first auth, or a fully expired token
        # whose refresh token is assumed dead as well).
        if self._refresh_token and (self._access_token_expire_at - now) > 0:
            return

        url = urljoin(DIGITALVOLVO_URL, "/app/iam/api/v1/auth")
        result = await self.digitalvolvo_post(url, {}, {
            "authType": "password",
            "password": self._password,
            "phoneNumber": "0086" + self._username
        })

        if not result:
            raise VolvoAPIError("Login returned no response")

        if not result.get("success"):
            raise VolvoAPIError(
                f"Login rejected by server: {result.get('errMsg') or result.get('msg')}"
            )

        if not result.get("data", {}).get("globalAccessToken"):
            raise VolvoAPIError("Login succeeded but no globalAccessToken was returned")

        if not result["data"].get("accessToken"):
            raise VolvoAPIError("Login succeeded but no accessToken was returned")

        self._refresh_token = result["data"]["refreshToken"]
        self._vocapi_access_token = result["data"]["globalAccessToken"]
        self._digitalvolvo_access_token = result["data"]["accessToken"]
        self._digitalvolvo_x_token = result["data"]["jwtToken"]
        now = int(time.time())
        self._access_token_expire_at = now + int(result["data"]["expiresIn"])

    async def update_token(self):
        now = int(time.time())

        # No session yet (first poll, or a prior refresh cleared it) — a full
        # password login is the only way to establish one.
        if not self._refresh_token:
            await self.login()
            return

        # Access token still has comfortable headroom; nothing to refresh.
        if (self._access_token_expire_at - now) >= 60 * 10:
            return

        url = urljoin(DIGITALVOLVO_URL, "/app/iam/api/v1/refreshToken?refreshToken=" + self._refresh_token)

        try:
            result = await self.digitalvolvo_get(url, {})
            self._refresh_token = result["data"]["refreshToken"]
            self._vocapi_access_token = result["data"]["globalAccessToken"]
            self._digitalvolvo_access_token = result["data"]["accessToken"]
            self._digitalvolvo_x_token = result["data"]["jwtToken"]
            self._access_token_expire_at = now + int(result["data"]["expiresIn"])
        except Exception as err:
            # The refresh token has itself expired or been revoked; drop it and
            # recover with a full password login.
            _LOGGER.warning(
                "Token refresh failed, re-authenticating with password: %s",
                redact_sensitive(err),
            )
            self._refresh_token = ""
            self._access_token_expire_at = 0
            await self.login()

    async def get_vehicles(self):
        url = urljoin(DIGITALVOLVO_URL, "/app/account/vehicles/api/v1/owner/listBindCar")
        result = await self.digitalvolvo_get(url, {})
        if not result:
            return []
        if result.get('success', False):
            return result.get('data', [])

        return []

    async def get_vehicles_vins(self):
        data = await self.get_vehicles()
        vins = {}
        for k in data:
            vinCode = k["vinCode"]
            vins[vinCode] = k

        return vins

    def _invalidate_charge_piles(self, vin=None):
        """Drop cached pile entries so the next read re-fetches the order id."""
        self._charge_piles_cache.pop(vin or "", None)

    def _invalidate_charge_orders(self, connector_id=None):
        """Drop cached order history so a just-ended session shows up at once."""
        if connector_id:
            self._charge_order_cache.pop(connector_id, None)
        else:
            self._charge_order_cache.clear()

    async def get_charge_piles(
        self, vin=None, series_code=None, *, force_refresh=False
    ):
        """Return the home charging piles linked to the current account.

        The app always scopes this to a VIN, and the returned pile carries the
        active order (tradeNo) that charging telemetry is keyed on.
        """
        key = vin or ""
        now = time.time()
        cached = self._charge_piles_cache.get(key)
        if not force_refresh and cached and now < cached[1]:
            return cached[0]

        query = {"phone": self._username}
        if vin:
            query["vin"] = vin
        if series_code:
            query["seriesCode"] = series_code
        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/charge-pile/api/v1/api/brandPile/getPileList?"
            + urllib.parse.urlencode(query),
        )
        result = await self.digitalvolvo_get(url, {})
        piles = result.get("data", {}).get("brandPileList", []) if result else []

        self._charge_piles_cache[key] = (piles, now + CHARGE_PILE_CACHE_TTL)
        return piles

    async def get_active_trade_no(
        self, vin, series_code=None, *, force_refresh=False
    ):
        """Return the order id of the pile's running session, if any."""
        piles = await self.get_charge_piles(
            vin, series_code, force_refresh=force_refresh
        )
        return next(
            (pile.get("tradeNo") for pile in piles if pile.get("tradeNo")),
            None,
        )

    async def get_charge_pile_status(self, vin, series_code=None):
        """Return the linked home pile plus telemetry for its running session.

        The status endpoint only answers for a live order, so it is skipped
        when the pile reports none; the pile entry itself still carries
        connector state, plug-and-charge and session totals.
        """
        piles = await self.get_charge_piles(vin, series_code)
        if not piles:
            return None

        pile = next(
            (item for item in piles if item.get("tradeNo")),
            piles[0],
        )
        trade_no = pile.get("tradeNo")
        if not trade_no:
            return {"pile": pile, "status": {}}

        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/charge-pile/api/v1/api/brandHomePile/status",
        )
        result = await self.digitalvolvo_post(
            url,
            {},
            {
                "tradeNo": trade_no,
                "vinCode": vin,
            },
        )
        return {
            "pile": pile,
            "status": (result.get("data") if result else None) or {},
        }

    async def start_charge_pile(self, vin, series_code=None):
        """Start a charging session on the linked home pile."""
        piles = await self.get_charge_piles(
            vin, series_code, force_refresh=True
        )
        if not piles:
            raise VolvoAPIError("No linked charge pile")
        if any(pile.get("tradeNo") for pile in piles):
            raise VolvoAPIError("A home-charge session is already active")

        pile = piles[0]
        connector_id = pile.get("connectorId")
        member_id = pile.get("memberId")
        if not connector_id or not member_id:
            raise VolvoAPIError("Charge pile is missing control identifiers")
        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/charge-pile/api/v1/api/brandHomePile/start",
        )
        result = await self.digitalvolvo_post(
            url,
            {},
            {
                "connectorId": connector_id,
                "vinCode": vin,
                "phone": self._username,
                "memberId": member_id,
            },
            max_attempts=1,
        )
        self._invalidate_charge_piles(vin)
        self._invalidate_charge_orders(connector_id)
        data = result.get("data") if result else None
        if not data or not data.get("startChargeSeq"):
            raise VolvoAPIError("Charge start returned no session identifier")
        return data

    async def stop_charge_pile(
        self, start_charge_seq, vin=None, series_code=None
    ):
        """Stop the charging session identified by start_charge_seq."""
        piles = await self.get_charge_piles(vin, series_code)
        if not piles:
            raise VolvoAPIError("No linked charge pile")

        pile = next(
            (
                item
                for item in piles
                if item.get("tradeNo") == start_charge_seq
            ),
            next(
                (item for item in piles if item.get("tradeNo")),
                piles[0],
            ),
        )
        connector_id = pile.get("connectorId")
        if not start_charge_seq or not connector_id:
            raise VolvoAPIError("Charge stop is missing control identifiers")
        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/charge-pile/api/v1/api/brandHomePile/stop",
        )
        result = await self.digitalvolvo_post(
            url,
            {},
            {
                "startChargeSeq": start_charge_seq,
                "connectorID": connector_id,
                "versions": "1",
            },
            max_attempts=1,
        )
        self._invalidate_charge_piles(vin)
        self._invalidate_charge_orders(connector_id)
        return result.get("data") if result else None

    async def get_charge_order_list(
        self, connector_id=None, vin=None, series_code=None
    ):
        """Return the charging session history for a linked home pile.

        Defaults to the first linked pile when connector_id is omitted.
        """
        if connector_id is None:
            piles = await self.get_charge_piles(vin, series_code)
            connector_id = piles[0].get("connectorId", "") if piles else ""
        if not connector_id:
            return []

        now = time.time()
        cached = self._charge_order_cache.get(connector_id)
        if cached and now < cached[1]:
            return cached[0]
        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/charge-pile/api/v1/api/brandHomePile/queryList",
        )
        result = await self.digitalvolvo_post(
            url,
            {},
            {
                "tradeNo": None,
                "orderNo": None,
                "phone": None,
                "memberId": None,
                "vin": None,
                "serviceProvider": None,
                "stationId": None,
                "stationName": None,
                "connectorId": connector_id,
                "startupType": None,
                "startTime": None,
                "endTime": None,
                "mainStatus": None,
            },
        )
        orders = (result.get("data") if result else None) or []
        self._charge_order_cache[connector_id] = (
            orders,
            now + CHARGE_ORDER_CACHE_TTL,
        )
        return orders

    async def set_plug_and_charge(
        self, enabled: bool, vin=None, series_code=None
    ):
        """Toggle plug-and-charge (auto start on connect) for the home pile."""
        piles = await self.get_charge_piles(
            vin, series_code, force_refresh=True
        )
        if not piles:
            raise VolvoAPIError("No linked charge pile")

        equipment_id = piles[0].get("equipmentId")
        if not equipment_id:
            raise VolvoAPIError("Charge pile is missing its equipment id")
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/plugAndCharge")
        result = await self.digitalvolvo_post(
            url,
            {},
            {
                "enabled": 1 if enabled else 0,
                "equipmentIdList": [equipment_id],
            },
        )
        self._invalidate_charge_piles(vin)
        return result

    async def sign_in(self, vin=None, series_code=None):
        """Perform the app's daily member check-in (App 签到)."""
        piles = await self.get_charge_piles(vin, series_code)
        if not piles:
            raise VolvoAPIError("No linked charge-pile; member id unavailable for sign-in")

        member_id = piles[0].get("memberId")
        url = urljoin(DIGITALVOLVO_URL, "/app/app/newSign/signIn")
        result = await self.digitalvolvo_post(url, {}, {"memberId": member_id})
        return result.get("data") if result else None


def json_loads(s):
    return json.loads(s)


x_pi = 3.14159265358979324 * 3000.0 / 180.0
pi = 3.1415926535897932384626  # π
a = 6378245.0  # 长半轴
ee = 0.00669342162296594323  # 扁率


def gcj02towgs84(lng, lat):
    """
    GCJ02(火星坐标系)转GPS84
    :param lng:火星坐标系的经度
    :param lat:火星坐标系纬度
    :return:
    """
    dlat = transformlat(lng - 105.0, lat - 35.0)
    dlng = transformlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglng = lng + dlng
    return [lng * 2 - mglng, lat * 2 - mglat]


def transformlat(lng, lat):
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(math.fabs(lng))
    ret += (20.0 * math.sin(6.0 * lng * pi) + 20.0 * math.sin(2.0 * lng * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * pi) + 40.0 *
            math.sin(lat / 3.0 * pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * pi) + 320 *
            math.sin(lat * pi / 30.0)) * 2.0 / 3.0
    return ret


def transformlng(lng, lat):
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(math.fabs(lng))
    ret += (20.0 * math.sin(6.0 * lng * pi) + 20.0 * math.sin(2.0 * lng * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * pi) + 40.0 * math.sin(lng / 3.0 * pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * pi) + 300.0 * math.sin(lng / 30.0 * pi)) * 2.0 / 3.0
    return ret


def hmac_sha256(key, msg):
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def hex_encode_sha256_hash(data):
    return hashlib.sha256(data.encode()).hexdigest()


def urlencode(string):
    return urllib.parse.quote(string, safe='')


def find_header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def canonical_request(req, signed_headers):
    payload_hash = find_header(req['headers'], 'x-sdk-content-sha256')
    if payload_hash is None:
        payload_hash = hex_encode_sha256_hash(req['body'] if req['body'] else '')

    canonical_uri = '/'.join(urlencode(p) for p in req['uri'].split('/'))
    if not canonical_uri.endswith('/'):
        canonical_uri += '/'

    query_string = sorted((k, v) for k, v in req['query'].items())
    canonical_query_string = '&'.join(f"{urlencode(k)}={urlencode(v)}" for k, v in query_string)

    canonical_headers = '\n'.join(f"{k}:{v.strip()}" for k in signed_headers for v in [req['headers'].get(k, '')])
    if canonical_headers:
        canonical_headers += '\n'

    return f"{req['method']}\n{canonical_uri}\n{canonical_query_string}\n{canonical_headers}\n{';'.join(signed_headers)}\n{payload_hash}"


def string_to_sign(canonical_req, date_stamp, service='SDK-HMAC-SHA256'):
    return f"{service}\n{date_stamp}\n{hex_encode_sha256_hash(canonical_req)}"


def create_signature(string_to_sign, secret_key):
    return hmac_sha256(secret_key, string_to_sign)


def format_auth_header(signature, access_key, signed_headers):
    return f"SDK-HMAC-SHA256 Access={access_key}, SignedHeaders={';'.join(signed_headers)}, Signature={signature}"


def generate_date_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sign_request(url, method, body):
    parsed_url = urlparse(url)
    request = {
        'headers': {
            "x-sdk-content-sha256": "UNSIGNED-PAYLOAD",
            "host": "apigateway.digitalvolvo.com"
        },
        'method': method,
        'body': body,
        'uri': parsed_url.path,
        'host': "apigateway.digitalvolvo.com",
        'query': dict(urllib.parse.parse_qsl(parsed_url.query))
    }
    key = "204114990"
    secret = "bjGqb3TvEEZ8W8QhoyhEH4IenwCnc4JQ"
    date = find_header(request['headers'], 'x-sdk-date')
    if date is None:
        date = generate_date_stamp()
        request['headers']['x-sdk-date'] = date

    if request['method'] not in ['PUT', 'PATCH', 'POST']:
        request['body'] = ''

    canonical_req = canonical_request(request, sorted([k.lower() for k in request['headers']]))
    string_to_sign_val = string_to_sign(canonical_req, date)
    signature = create_signature(string_to_sign_val, secret)

    return {
        'x-sdk-date': request['headers']['x-sdk-date'],
        'v587sign': format_auth_header(signature, key, sorted([k.lower() for k in request['headers']]))
    }
