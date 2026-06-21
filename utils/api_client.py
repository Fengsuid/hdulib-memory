import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from utils.config import ConfigManager
from utils.console import logger


class LibraryAPIClient:
    """Read-only client for imported or authenticated library history."""

    MAX_HISTORY_PAGES = 200

    def __init__(
        self,
        config_manager: ConfigManager,
        session_cookies: dict[str, str] | None = None,
    ):
        self.config = config_manager.config
        self.session: httpx.AsyncClient | None = None
        self.uid: str | None = None
        self._session_cookies = session_cookies or {}

    async def __aenter__(self):
        self.session = httpx.AsyncClient(
            timeout=30.0,
            headers=self.config.init_headers,
            cookies=self._session_cookies,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.aclose()

    async def request(self, method: str, url: str, **kwargs) -> dict:
        if not self.session:
            raise RuntimeError("HTTP session is not initialized")

        if not url:
            raise ValueError("URL is required")

        logger.debug(f"Making {method.upper()} request to: {url}")
        try:
            response = await self.session.request(method.upper(), url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Request failed: {e}")
            raise

    async def get_booking_history(
        self,
        include_no_seat: bool = False,
        detail_limit: int | None = None,
    ) -> list[dict]:
        """Fetch the current user's historical appointment records."""
        if not self.uid and not self._session_cookies:
            raise RuntimeError("User not logged in")

        history = await self._fetch_booking_list("myBookingList")
        if include_no_seat:
            history.extend(await self._fetch_booking_list("myNoSeatBookingList"))

        detail_items = history
        if detail_limit is not None and detail_limit >= 0:
            detail_items = history[:detail_limit]

        for item in detail_items:
            booking_id = self._extract_booking_id(item)
            if not booking_id:
                continue

            detail = await self.get_booking_info(booking_id)
            if detail:
                item["detail"] = detail

        return history

    async def _fetch_booking_list(self, endpoint_name: str) -> list[dict]:
        endpoint = f"{self.config.base_url}/Seat/Index/{endpoint_name}"
        request_candidates = [
            ("get", {}),
            ("post", {}),
            ("post", {"page": "1"}),
            ("get", {"page": "1"}),
            ("post", {"page": "1", "pageSize": "1000"}),
            ("get", {"page": "1", "pageSize": "1000"}),
        ]

        last_error: Exception | None = None
        for method, payload in request_candidates:
            try:
                response = await self._request_history_page(
                    method=method,
                    url=endpoint,
                    payload=payload,
                )
            except Exception as exc:
                last_error = exc
                continue

            items = self._extract_booking_items(response)
            if items is not None:
                return await self._collect_history_pages(response, items)
            if self._is_auth_redirect(response):
                raise RuntimeError(
                    "Session is not authenticated. Log in again and export fresh data."
                )

        if last_error is not None:
            raise last_error
        return []

    async def _request_history_page(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
    ) -> dict:
        payload = payload or {}
        if method == "post":
            return await self.request(
                "post",
                self._with_lab_json(url),
                data=payload,
            )
        return await self.request(
            "get",
            self._with_lab_json(url, payload),
        )

    async def _collect_history_pages(
        self,
        first_response: dict,
        first_items: list[dict],
    ) -> list[dict]:
        items = list(first_items)
        seen_urls: set[str] = set()
        next_url = self._extract_next_url(first_response)
        page_count = 1

        while next_url and page_count < self.MAX_HISTORY_PAGES:
            url = self._absolute_history_url(next_url)
            if url in seen_urls:
                break
            seen_urls.add(url)

            response = await self._request_history_page(
                method="get",
                url=url,
            )
            if self._is_auth_redirect(response):
                raise RuntimeError(
                    "Session is not authenticated. Log in again and export fresh data."
                )

            page_items = self._extract_booking_items(response)
            if page_items is None:
                break

            items.extend(page_items)
            next_url = self._extract_next_url(response)
            page_count += 1

        return self._dedupe_booking_items(items)

    def _is_auth_redirect(self, response: dict) -> bool:
        if not isinstance(response, dict):
            return False
        href = str(response.get("href", ""))
        return response.get("ui_type") == "com.Redirect" and "hduCASLogin" in href

    async def get_booking_info(self, booking_id: str) -> dict:
        endpoint = f"{self.config.base_url}/Seat/Index/bookingInfo"
        response = await self.request(
            "get",
            endpoint,
            params={
                "LAB_JSON": "1",
                "bookingId": booking_id,
            },
        )
        return self._extract_booking_detail(response)

    def _with_lab_json(self, url: str, extra_params: dict | None = None) -> str:
        absolute_url = self._absolute_history_url(url)
        parsed = urlparse(absolute_url)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(query_items)
        query.setdefault("LAB_JSON", "1")
        if extra_params:
            query.update({str(key): str(value) for key, value in extra_params.items()})
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _absolute_history_url(self, url: str) -> str:
        return urljoin(self.config.base_url, url)

    def _extract_booking_items(self, response: dict) -> list[dict] | None:
        if not isinstance(response, dict):
            return None

        explicit_items = self._search_named_booking_items(response)
        if explicit_items is not None:
            return explicit_items

        candidates = [
            response.get("DATA"),
            response.get("data"),
            response.get("content"),
            response,
        ]
        for candidate in candidates:
            items = self._search_booking_items(candidate)
            if items is not None:
                return items
        return None

    def _search_named_booking_items(self, payload) -> list[dict] | None:
        if isinstance(payload, dict):
            for key in ("items", "defaultItems", "list", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    if not value or any(
                        self._looks_like_booking_item(item)
                        for item in value
                        if isinstance(item, dict)
                    ):
                        return value
            for value in payload.values():
                found = self._search_named_booking_items(value)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = self._search_named_booking_items(item)
                if found is not None:
                    return found
        return None

    def _search_booking_items(self, payload) -> list[dict] | None:
        if isinstance(payload, list):
            if payload and all(isinstance(item, dict) for item in payload):
                if any(self._looks_like_booking_item(item) for item in payload):
                    return payload
            for item in payload:
                found = self._search_booking_items(item)
                if found is not None:
                    return found

        if isinstance(payload, dict):
            if self._looks_like_booking_item(payload):
                return [payload]
            for value in payload.values():
                found = self._search_booking_items(value)
                if found is not None:
                    return found

        return None

    def _looks_like_booking_item(self, item: dict) -> bool:
        keys = {
            "booking",
            "seat",
            "space",
            "begin_time",
            "duration",
            "id",
            "bookingId",
            "roomName",
            "seatNum",
            "orderTime",
        }
        item_keys = set(item.keys())
        return len(keys & item_keys) >= 2

    def _extract_next_url(self, response: dict) -> str | None:
        if not isinstance(response, dict):
            return None
        for key in (
            "nextUrl",
            "defaultNextUrl",
            "next_url",
            "default_next_url",
            "next",
        ):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in response.values():
            if isinstance(value, dict):
                found = self._extract_next_url(value)
                if found:
                    return found
        return None

    def _dedupe_booking_items(self, items: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in items:
            booking_id = self._extract_booking_id(item)
            if booking_id:
                key = f"booking:{booking_id}"
            else:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _extract_booking_detail(self, response: dict) -> dict:
        if not isinstance(response, dict):
            return {}

        candidates = [
            response.get("DATA"),
            response.get("data"),
            response.get("content"),
            response,
        ]
        for candidate in candidates:
            detail = self._search_booking_detail(candidate)
            if detail:
                return detail
        return {}

    def _search_booking_detail(self, payload) -> dict:
        if isinstance(payload, dict):
            booking = payload.get("booking")
            if isinstance(booking, dict):
                return payload
            for value in payload.values():
                detail = self._search_booking_detail(value)
                if detail:
                    return detail
        elif isinstance(payload, list):
            for item in payload:
                detail = self._search_booking_detail(item)
                if detail:
                    return detail
        return {}

    def _extract_booking_id(self, item: dict) -> str | None:
        if not isinstance(item, dict):
            return None

        direct_keys = ["bookingId", "booking_id", "id"]
        for key in direct_keys:
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value)

        booking = item.get("booking")
        if isinstance(booking, dict):
            for key in direct_keys:
                value = booking.get(key)
                if value is not None and str(value).strip():
                    return str(value)

        for value in item.values():
            if not isinstance(value, str):
                continue
            match = re.search(r"bookingId=(\d+)", value)
            if match:
                return match.group(1)

        return None
