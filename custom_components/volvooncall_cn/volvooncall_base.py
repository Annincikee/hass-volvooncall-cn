import logging

from datetime import timedelta, timezone
from urllib.parse import urljoin
import asyncio
import time
import json
import re
from datetime import datetime
import math

from aiohttp import ClientResponseError, ClientSession, ClientTimeout
from aiohttp.hdrs import METH_GET, METH_POST

import hashlib
import hmac
import urllib.parse
from urllib.parse import urlparse


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
# Backoff between transport retries. Kept small on purpose: the whole update
# cycle runs under a single timeout while holding the coordinator lock, and
# an exponential 1s/2s/4s ladder pushed the worst case for one REST call past
# what the cycle budget allowed, so a flaky network killed every poll before
# any data landed.
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
# Worst case for one REST call, used to size UPDATE_CYCLE_TIMEOUT.
REQUEST_BUDGET = TIMEOUT.total_seconds() * MAX_RETRIES + sum(
    RETRY_BACKOFF_SECONDS[: MAX_RETRIES - 1]
)
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 30

# The pile list carries the live order id, so it must not outlive a poll cycle.
CHARGE_PILE_CACHE_TTL = 15
CHARGE_ORDER_CACHE_TTL = 30 * 60

# Frequent password logins can trip Volvo's server-side risk control (the
# observed symptom is a long streak of 403s shortly after login), so repeat
# attempts are locally rate limited.
PASSWORD_LOGIN_COOLDOWN = 120

# Refresh the access token once it has less than this much lifetime left.
TOKEN_REFRESH_HEADROOM = 60 * 10

# Volvo answers 403 both for a dead token and while risk control is throttling
# the account. A 403 from the refresh endpoint therefore keeps the refresh
# token for this many consecutive attempts before it is treated as a real
# rejection; discarding it on the first one turned a short risk-control streak
# into password logins and, three of those later, a manual reauthentication.
REFRESH_FORBIDDEN_TOLERANCE = 3

# A gRPC UNAUTHENTICATED only forces a session refresh when the session is at
# least this old. A token the server rejects right after issuing it is not
# fixed by issuing another one, and refreshing every poll would hammer the
# auth endpoint.
GRPC_REAUTH_MIN_SESSION_AGE = 15 * 60

SESSION_REQUIRED_FIELDS = (
    "refreshToken",
    "globalAccessToken",
    "accessToken",
    "jwtToken",
    "expiresIn",
)


class VolvoAPIError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class VolvoApiHttpError(VolvoAPIError):
    """A non-auth HTTP failure."""


class VolvoTransientError(VolvoApiHttpError):
    """A temporary transport/server failure; password auth must not follow."""


class VolvoRateLimitError(VolvoTransientError):
    """The server rate-limited the request and may specify when to retry."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class VolvoAuthExpiredError(VolvoAPIError):
    """The server rejected our access token (401/403) before its expiry."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class VolvoRefreshRejectedError(VolvoAuthExpiredError):
    """The refresh endpoint explicitly rejected the stored refresh token."""


class VolvoRefreshForbiddenError(VolvoAuthExpiredError):
    """The refresh endpoint answered 403; the refresh token is kept for now."""


class VolvoAuthError(VolvoAPIError):
    """The auth endpoint answered but rejected the account credentials."""


class VolvoAuthForbiddenError(VolvoAuthError):
    """The password endpoint answered 403.

    That is how risk control throttles an account as well as how it refuses
    one, so it is weaker evidence of a wrong password than an explicit
    business rejection and is counted against a higher reauth threshold.
    """


class VolvoAuthThrottledError(VolvoAPIError):
    """A password login was suppressed by the local risk-control cooldown."""


def redact_sensitive(value):
    """Return a log-safe string without credentials or vehicle identifiers."""
    text = str(value)
    sensitive_keys = (
        "authorization|password|refreshToken|accessToken|globalAccessToken|"
        "jwtToken|X-Token|phone|phoneNumber|vin|vinCode|deviceid|uuid|"
        "connectorId|orderNo|tradeNo|memberId|latitude|longitude"
    )
    # Redact quoted values first.  The old token-oriented pattern stopped at
    # the first whitespace and leaked the remainder of values such as
    # ``"accessToken": "secret token with spaces"``.
    text = re.sub(
        rf"((?:{sensitive_keys})['\"]?\s*[:=]\s*)(['\"])"
        r"(?:\\.|(?!\2)[\s\S])*\2",
        lambda match: (
            f"{match.group(1)}{match.group(2)}<redacted>{match.group(2)}"
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(authorization['\"]?\s*[:=]\s*)Bearer\s+[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"((?:{sensitive_keys})['\"]?\s*[:=]\s*)"
        r"[^,&'\"\s}\]]+",
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


def _retry_delay(attempt_index):
    """Return the backoff before the retry that follows attempt_index."""
    if attempt_index < len(RETRY_BACKOFF_SECONDS):
        return RETRY_BACKOFF_SECONDS[attempt_index]
    return RETRY_BACKOFF_SECONDS[-1]


def _retry_after_seconds(headers):
    """Parse Retry-After as delta seconds or an HTTP date."""
    if not headers:
        return None
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        raw_value = headers.get("retry-after")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        try:
            from email.utils import parsedate_to_datetime

            retry_at = parsedate_to_datetime(str(raw_value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


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
        self._last_password_login_at = 0
        self._refresh_forbidden_streak = 0
        # Monotonic time this process obtained the session; None for a
        # session restored from disk, whose real age is unknown.
        self._session_established_monotonic = None
        self._token_listener = None
        self._charge_piles_cache = {}
        self._charge_pile_locks = {}
        self._charge_pile_generations = {}
        self._charge_order_cache = {}
        self._charge_order_locks = {}
        self._charge_order_generations = {}
        self._charge_order_global_generation = 0

    def set_token_listener(self, listener):
        """Register a callback invoked with export_tokens() after each auth."""
        self._token_listener = listener

    def export_tokens(self):
        """Return the session tokens for persistence across restarts."""
        return {
            "refresh_token": self._refresh_token,
            "digitalvolvo_access_token": self._digitalvolvo_access_token,
            "digitalvolvo_x_token": self._digitalvolvo_x_token,
            "vocapi_access_token": self._vocapi_access_token,
            "access_token_expire_at": self._access_token_expire_at,
            # Persisted so the risk-control cooldown survives restarts. A
            # purely in-memory timestamp is reset by every Home Assistant
            # restart and by every ConfigEntryNotReady retry, which rebuilds
            # the API object — exactly the situations that produce a burst
            # of password logins.
            "last_password_login_at": self._last_password_login_at,
        }

    def import_tokens(self, data):
        """Seed the session from persisted tokens to avoid a password login."""
        if not isinstance(data, dict):
            return
        self._refresh_token = data.get("refresh_token") or ""
        self._digitalvolvo_access_token = (
            data.get("digitalvolvo_access_token") or ""
        )
        self._digitalvolvo_x_token = data.get("digitalvolvo_x_token") or ""
        self._vocapi_access_token = data.get("vocapi_access_token") or ""
        try:
            self._access_token_expire_at = int(
                data.get("access_token_expire_at") or 0
            )
        except (TypeError, ValueError):
            self._access_token_expire_at = 0
        try:
            last_login = int(data.get("last_password_login_at") or 0)
        except (TypeError, ValueError):
            last_login = 0
        # A timestamp from the future (clock skew, restored backup) would
        # wedge logins shut, so never extend the cooldown past one window.
        now = int(time.time())
        self._last_password_login_at = min(last_login, now)

    def _discard_refresh_token(self):
        """Drop a refresh token the server rejected, in memory and on disk.

        Clearing only the in-memory copy left the dead token in the auth
        store, so every restart replayed it and burned a guaranteed-failing
        refresh call before falling back to a password login.
        """
        self._refresh_token = ""
        self._access_token_expire_at = 0
        self._refresh_forbidden_streak = 0
        self._notify_token_listener()

    def note_grpc_unauthenticated(self):
        """Mark the session dead after a gRPC UNAUTHENTICATED, if plausible.

        REST 401/403 already invalidates the session, but the gRPC token can
        be rejected on its own; without this every data source kept failing
        until the scheduled refresh window. Returns True when the next auth
        pass will rebuild the session.
        """
        established = self._session_established_monotonic
        if (
            established is not None
            and time.monotonic() - established < GRPC_REAUTH_MIN_SESSION_AGE
        ):
            return False
        self._access_token_expire_at = 0
        return True

    def _notify_token_listener(self):
        if self._token_listener is None:
            return
        try:
            self._token_listener(self.export_tokens())
        except Exception as err:
            _LOGGER.debug("Token listener failed: %s", redact_sensitive(err))

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
                        timeout=ClientTimeout(total=TIMEOUT.total_seconds()),
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
            except ClientResponseError as error:
                # NEVER log these with exc_info or re-raise them as-is: the
                # exception carries the full request URL, and the refreshToken
                # endpoint embeds the token in its query string. Every path
                # below logs and raises a redacted copy only ("from None"
                # detaches the token-bearing original from the chain).
                safe_message = redact_sensitive(error)
                if error.status in (401, 403):
                    # The server revoked this session ahead of its expiry.
                    # Mark the access token dead so the next auth pass rebuilds
                    # the session instead of replaying a rejected token.
                    self._access_token_expire_at = 0
                    _LOGGER.warning(
                        "Authentication rejected by server: %s", safe_message
                    )
                    raise VolvoAuthExpiredError(
                        safe_message, status=error.status
                    ) from None
                if error.status == 429:
                    # Respect server-side rate limiting.  Replaying after the
                    # generic 0.5/1s backoff creates extra traffic and almost
                    # always lands inside the same limit window; the caller can
                    # instead schedule a later coordinator refresh.
                    retry_after = _retry_after_seconds(error.headers)
                    _LOGGER.warning(
                        "Request rate-limited by server%s: %s",
                        (
                            f"; retry after {retry_after:g}s"
                            if retry_after is not None
                            else ""
                        ),
                        safe_message,
                    )
                    raise VolvoRateLimitError(
                        safe_message, retry_after=retry_after
                    ) from None
                if 400 <= error.status < 500 and error.status != 429:
                    # Deterministic client errors do not become successes on
                    # replay; retrying only hammers the API.
                    _LOGGER.warning(
                        "Request rejected by server: %s", safe_message
                    )
                    raise VolvoApiHttpError(safe_message) from None
                _LOGGER.warning(
                    "Transient server error from the API: %s", safe_message
                )
                if i < max_attempts - 1:
                    await asyncio.sleep(_retry_delay(i))
                else:
                    raise VolvoTransientError(safe_message) from None
            except Exception as error:
                _LOGGER.warning(
                    "Failure when communicating with the server (%s): %s",
                    type(error).__name__,
                    redact_sensitive(error),
                )
                if i < max_attempts - 1:  # Don't delay on last attempt
                    await asyncio.sleep(_retry_delay(i))
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

    def _store_session(self, data, now):
        # Validate everything before touching any field. Assigning as we went
        # raised a bare KeyError on an incomplete response and left the
        # session half replaced: new tokens in memory, old expiry, nothing
        # persisted.
        if not isinstance(data, dict):
            raise VolvoAPIError("Auth response carried no session data")
        missing = [
            field for field in SESSION_REQUIRED_FIELDS if not data.get(field)
        ]
        if missing:
            raise VolvoAPIError(
                "Auth response is missing " + ", ".join(missing)
            )
        try:
            expires_in = int(data["expiresIn"])
        except (TypeError, ValueError):
            raise VolvoAPIError(
                "Auth response carried an invalid expiresIn"
            ) from None
        if expires_in <= 0:
            raise VolvoAPIError("Auth response carried an invalid expiresIn")

        self._refresh_token = data["refreshToken"]
        self._vocapi_access_token = data["globalAccessToken"]
        self._digitalvolvo_access_token = data["accessToken"]
        self._digitalvolvo_x_token = data["jwtToken"]
        self._access_token_expire_at = now + expires_in
        self._refresh_forbidden_streak = 0
        self._session_established_monotonic = time.monotonic()
        self._notify_token_listener()

    async def _refresh_access_token(self):
        query = urllib.parse.urlencode({"refreshToken": self._refresh_token})
        url = urljoin(
            DIGITALVOLVO_URL,
            "/app/iam/api/v1/refreshToken?" + query,
        )
        try:
            result = await self.digitalvolvo_get(url, {})
        except VolvoTransientError:
            raise
        except (VolvoAuthExpiredError, VolvoApiHttpError, VolvoAPIError) as err:
            if getattr(err, "status", None) == 403:
                # 403 is also what risk control answers while it throttles
                # the account, so a few of them are not yet proof that the
                # refresh token is dead. Keep it and let the coordinator
                # back off; only a persistent streak escalates.
                self._refresh_forbidden_streak += 1
                if self._refresh_forbidden_streak < REFRESH_FORBIDDEN_TOLERANCE:
                    raise VolvoRefreshForbiddenError(
                        err.message, status=403
                    ) from None
            # Only a response that explicitly rejects the refresh request is
            # evidence that the stored refresh token is unusable.  Timeouts,
            # rate limits and 5xx failures remain retryable and must preserve
            # the session instead of escalating to a password login.
            raise VolvoRefreshRejectedError(err.message) from None
        self._store_session(
            result.get("data") if isinstance(result, dict) else None,
            int(time.time()),
        )

    async def login(self):
        now = int(time.time())

        # A live session is kept fresh by update_token() using the refresh
        # token; nothing to do here.
        if self._refresh_token and (self._access_token_expire_at - now) > 0:
            return

        # An expired session that still has a refresh token (e.g. tokens
        # persisted across a restart, or a session the server revoked early)
        # is recovered through the refresh endpoint first — a password login
        # is the last resort because frequent password logins can trip
        # server-side risk control.
        if self._refresh_token:
            try:
                await self._refresh_access_token()
                return
            except VolvoRefreshRejectedError as err:
                _LOGGER.warning(
                    "Refresh token rejected; re-authenticating with password: %s",
                    redact_sensitive(err),
                )
                self._discard_refresh_token()

        now = int(time.time())
        if (now - self._last_password_login_at) < PASSWORD_LOGIN_COOLDOWN:
            raise VolvoAuthThrottledError(
                "Password login suppressed for up to "
                f"{PASSWORD_LOGIN_COOLDOWN}s to avoid account risk control"
            )
        # Failed attempts count too; hammering the auth endpoint with a
        # rejected password is exactly what risk control punishes. Persist it
        # right away so a restart mid-cooldown cannot reset the window.
        self._last_password_login_at = now
        self._notify_token_listener()

        url = urljoin(DIGITALVOLVO_URL, "/app/iam/api/v1/auth")
        try:
            # Exactly one attempt: the local cooldown above accounts for a
            # single password login, so letting the transport retry would
            # put up to MAX_RETRIES real password posts on the auth endpoint
            # per cooldown window — the very pattern risk control punishes.
            result = await self.digitalvolvo_post(url, {}, {
                "authType": "password",
                "password": self._password,
                "phoneNumber": "0086" + self._username
            }, max_attempts=1)
        except VolvoAuthExpiredError as err:
            # On the password endpoint, 401/403 is a direct rejection rather
            # than an expired bearer session. Classify it so the coordinator's
            # bounded reauth thresholds can take effect; 403 gets its own
            # class because risk control uses it for throttling too.
            error_class = (
                VolvoAuthForbiddenError if err.status == 403 else VolvoAuthError
            )
            raise error_class(
                f"Login rejected by server: {err.message}"
            ) from None
        except VolvoApiHttpError:
            # Other transport-level failures are not a verdict on credentials.
            raise
        except VolvoAPIError as err:
            # The auth endpoint answered and rejected the credentials.
            raise VolvoAuthError(
                f"Login rejected by server: {err.message}"
            ) from None

        if not result:
            raise VolvoAPIError("Login returned no response")

        if not result.get("success"):
            raise VolvoAuthError(
                f"Login rejected by server: {result.get('errMsg') or result.get('msg')}"
            )

        data = result.get("data") or {}
        if not data.get("globalAccessToken"):
            raise VolvoAPIError("Login succeeded but no globalAccessToken was returned")

        if not data.get("accessToken"):
            raise VolvoAPIError("Login succeeded but no accessToken was returned")

        self._store_session(data, int(time.time()))

    async def update_token(self):
        now = int(time.time())

        # No session yet (first poll, or a prior refresh cleared it) — a full
        # password login is the only way to establish one.
        if not self._refresh_token:
            await self.login()
            return

        # Access token still has comfortable headroom; nothing to refresh.
        if (self._access_token_expire_at - now) >= TOKEN_REFRESH_HEADROOM:
            return

        try:
            await self._refresh_access_token()
        except VolvoRefreshRejectedError as err:
            # The refresh token has itself expired or been revoked; drop it and
            # recover with a full password login.
            _LOGGER.warning(
                "Refresh token rejected; re-authenticating with password: %s",
                redact_sensitive(err),
            )
            self._discard_refresh_token()
            await self.login()

    async def get_vehicles(self):
        url = urljoin(DIGITALVOLVO_URL, "/app/account/vehicles/api/v1/owner/listBindCar")
        result = await self.digitalvolvo_get(url, {})
        if not result:
            return []
        if result.get('success', False):
            return result.get('data') or []

        return []

    async def get_vehicles_vins(self):
        data = await self.get_vehicles()
        vins = {}
        for vehicle_info in data:
            if not isinstance(vehicle_info, dict):
                _LOGGER.warning(
                    "Skipping a malformed non-object vehicle-list entry"
                )
                continue
            vin_code = vehicle_info.get("vinCode")
            if not isinstance(vin_code, str) or not vin_code.strip():
                _LOGGER.warning(
                    "Skipping a vehicle-list entry without a valid VIN"
                )
                continue
            vins[vin_code] = vehicle_info

        return vins

    def _invalidate_charge_piles(self, vin=None):
        """Drop cached pile entries so the next read re-fetches the order id."""
        key = vin or ""
        self._charge_pile_generations[key] = (
            self._charge_pile_generations.get(key, 0) + 1
        )
        self._charge_piles_cache.pop(key, None)

    def _invalidate_charge_orders(self, connector_id=None):
        """Drop cached order history so a just-ended session shows up at once."""
        if connector_id:
            self._charge_order_generations[connector_id] = (
                self._charge_order_generations.get(connector_id, 0) + 1
            )
            self._charge_order_cache.pop(connector_id, None)
        else:
            self._charge_order_global_generation += 1
            self._charge_order_cache.clear()

    async def get_charge_piles(
        self, vin=None, series_code=None, *, force_refresh=False
    ):
        """Return the home charging piles linked to the current account.

        The app always scopes this to a VIN, and the returned pile carries the
        active order (tradeNo) that charging telemetry is keyed on.
        """
        key = vin or ""
        now = time.monotonic()
        cached = self._charge_piles_cache.get(key)
        if not force_refresh and cached and now < cached[1]:
            return cached[0]

        lock = self._charge_pile_locks.setdefault(key, asyncio.Lock())
        join_inflight = lock.locked()
        async with lock:
            # A concurrent reader may have populated the cache while this
            # coroutine waited for the per-VIN single-flight lock.  A forced
            # reader may also join that already-fresh in-flight request; it
            # only starts another request when there was nothing to join.
            now = time.monotonic()
            cached = self._charge_piles_cache.get(key)
            if (
                cached
                and now < cached[1]
                and (not force_refresh or join_inflight)
            ):
                return cached[0]

            generation = self._charge_pile_generations.get(key, 0)
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
            piles = (
                (result.get("data") or {}).get("brandPileList") or []
                if result
                else []
            )

            # A control request may have invalidated this VIN while the GET
            # was in flight.  Return its response to its original caller but
            # never let that old response resurrect the invalidated cache.
            if self._charge_pile_generations.get(key, 0) == generation:
                self._charge_piles_cache[key] = (
                    piles,
                    time.monotonic() + CHARGE_PILE_CACHE_TTL,
                )
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

        now = time.monotonic()
        cached = self._charge_order_cache.get(connector_id)
        if cached and now < cached[1]:
            return cached[0]
        lock = self._charge_order_locks.setdefault(
            connector_id, asyncio.Lock()
        )
        async with lock:
            now = time.monotonic()
            cached = self._charge_order_cache.get(connector_id)
            if cached and now < cached[1]:
                return cached[0]

            global_generation = self._charge_order_global_generation
            generation = self._charge_order_generations.get(connector_id, 0)
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
            if (
                self._charge_order_global_generation == global_generation
                and self._charge_order_generations.get(connector_id, 0)
                == generation
            ):
                self._charge_order_cache[connector_id] = (
                    orders,
                    time.monotonic() + CHARGE_ORDER_CACHE_TTL,
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
