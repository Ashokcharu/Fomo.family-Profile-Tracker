from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.features.dismissed_tokens.service import active_dismissed_token_ids
from app.integrations.fomo_family.activity import fetch_fomo_buy_market_caps, parse_buy_swap, parse_profile_identity, persist_fomo_buys
from app.features.followed_users.service import sync_followed_profiles
from app.models import Chain, FollowedTrader, Token

logger = logging.getLogger(__name__)
FOMO_ORIGIN = "https://fomo.family"
FOMO_API_HOST = "prod-api.fomo.family"
FOMO_TOKEN_BATCH_SIZE = 100
FOMO_DB_WRITE_BATCH_SIZE = 100
FOMO_NETWORK_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "bnb": "56",
    "bnb_chain": "56",
    "base": "8453",
    "robinhood": "4663",
    "robinhood_chain": "4663",
    "monad": "143",
    "solana": "1399811149",
}


class FomoFamilyActivityMonitor:
    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        browser_profile_dir: str = "~/.cache/fomo-following",
        profile_handle: str = "",
        profile_url: str = "",
        following_sync_interval_seconds: int = 43_200,
        poll_interval_seconds: int = 300,
        batch_size: int = 25,
        page_timeout_seconds: int = 20,
    ) -> None:
        self.cdp_url = cdp_url
        self.browser_profile_dir = Path(browser_profile_dir).expanduser()
        self.profile_handle = profile_handle.strip()
        self.profile_url = profile_url.strip()
        self.following_sync_interval_seconds = max(60, int(following_sync_interval_seconds))
        self.poll_interval_seconds = max(30, poll_interval_seconds)
        self.batch_size = max(1, batch_size)
        self.page_timeout_seconds = max(5, page_timeout_seconds)
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright = None
        self._browser = None
        self._page = None
        self._chrome_process: subprocess.Popen | None = None
        self._status = "starting"
        self._status_message: str | None = None
        self._current_username: str | None = None

    def status_snapshot(self) -> dict[str, str | None]:
        return {
            "state": self._status,
            "message": self._status_message,
            "current_username": self._current_username,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run(), name="fomo-family-activity-monitor")

    def request_sync(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake_event.set)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._page is not None and not self._page.is_closed():
            await self._page.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._chrome_process is not None and self._chrome_process.poll() is None:
            self._chrome_process.terminate()
            try:
                await asyncio.to_thread(self._chrome_process.wait, 5)
            except subprocess.TimeoutExpired:
                self._chrome_process.kill()
                await asyncio.to_thread(self._chrome_process.wait)
        self._status = "stopped"

    async def _run(self) -> None:
        next_metadata_refresh = 0.0
        next_following_refresh = 0.0
        try:
            while not self._stop_event.is_set():
                if self._page is None or self._page.is_closed():
                    self._status = "connecting"
                    self._status_message = None
                    try:
                        await self._connect_browser()
                        logger.info("Fomo Family monitor connected to Chrome at %s", self.cdp_url)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._status = "unavailable"
                        self._status_message = str(exc)
                        logger.warning("Fomo Family monitor could not connect to Chrome; retrying: %s", exc)
                        await self._wait_or_stop(15)
                        continue

                loop = asyncio.get_running_loop()
                if loop.time() >= next_metadata_refresh:
                    try:
                        refreshed = await self._refresh_tracked_token_metadata()
                        logger.info("Refreshed Fomo metadata for %d tracked tokens", refreshed)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Fomo token metadata refresh failed")
                    next_metadata_refresh = loop.time() + self.poll_interval_seconds

                if self.profile_url and loop.time() >= next_following_refresh:
                    try:
                        result = await self._refresh_profile_following()
                        logger.info(
                            "Synced Fomo following list: %d added, %d updated, %d identity conflicts",
                            result["added"],
                            result["updated"],
                            result["conflicts"],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Fomo profile following sync failed")
                    next_following_refresh = loop.time() + self.following_sync_interval_seconds

                traders = await asyncio.to_thread(_list_traders, self.poll_interval_seconds)
                if traders:
                    self._status = "syncing"
                    self._status_message = None
                    batch = traders[: self.batch_size]
                    browser_lost = False
                    for index, trader in enumerate(batch):
                        if self._stop_event.is_set():
                            break
                        self._current_username = trader["username"]
                        identity = None
                        try:
                            identity, swaps = await self._fetch_trader(
                                trader["username"],
                            )
                            inserted = await asyncio.to_thread(
                                _save_trader_activity,
                                trader["username"],
                                identity,
                                swaps,
                                None,
                            )
                            logger.info(
                                "Fomo Family sync for %s: %s new buys",
                                trader["username"],
                                inserted,
                            )
                            self._current_username = None
                        except Exception as exc:
                            if _is_browser_closed(exc):
                                logger.warning("Chrome disconnected during Fomo sync; reconnecting")
                                await self._disconnect_browser()
                                self._current_username = None
                                browser_lost = True
                                break
                            await asyncio.to_thread(
                                _save_trader_activity,
                                trader["username"],
                                None,
                                {"swaps": []},
                                str(exc),
                            )
                            self._current_username = None
                            logger.warning("Fomo Family sync failed for %s: %s", trader["username"], exc)
                        await asyncio.sleep(0.2)
                    if browser_lost:
                        continue
                else:
                    self._status = "waiting"
                    self._status_message = "No followed traders are due for sync."
                    await self._wait_or_stop(self.poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status = "unavailable"
            self._status_message = str(exc)
            self._current_username = None
            logger.exception("Fomo Family monitor could not start")

    async def _connect_browser(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception:
            self._start_chrome()
            for _ in range(60):
                if self._stop_event.is_set():
                    raise asyncio.CancelledError
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            if self._browser is None:
                await self._disconnect_browser()
                exit_code = self._chrome_process.poll() if self._chrome_process is not None else None
                raise RuntimeError(f"Chrome did not open {self.cdp_url} (process exit code: {exit_code})")

        if not self._browser.contexts:
            raise RuntimeError("Chrome has no active browser context")
        context = self._browser.contexts[0]
        self._page = await context.new_page()
        await self._page.goto(FOMO_ORIGIN, wait_until="domcontentloaded", timeout=30000)

    async def _disconnect_browser(self) -> None:
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    await self._page.close()
            except Exception:
                pass
            self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._browser = None
        if self._chrome_process is not None and self._chrome_process.poll() is not None:
            self._chrome_process = None

    async def _wait_or_stop(self, timeout: int) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            self._wake_event.clear()
        except asyncio.TimeoutError:
            pass

    def _start_chrome(self) -> None:
        if self._chrome_process is not None and self._chrome_process.poll() is None:
            return
        executable = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if executable is None:
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
        if executable is None:
            raise RuntimeError("Google Chrome or Chromium is required for Fomo monitoring")

        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        port = urlparse(self.cdp_url).port or 9222
        self._chrome_process = subprocess.Popen(
            [
                executable,
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.browser_profile_dir}",
                FOMO_ORIGIN,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Opened Chrome for Fomo Family monitoring")

    async def _fetch_trader(
        self,
        username: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        if self._page is None:
            raise RuntimeError("Fomo monitor page is not available")

        profile_payload: dict[str, Any] | None = None
        swaps_by_profile_id: dict[str, dict[str, Any]] = {}
        profile_ready = asyncio.Event()
        swaps_ready = asyncio.Event()
        pending: set[asyncio.Task] = set()
        expected_path = f"/v2/users/userHandle/{quote(username, safe='')}".casefold()

        async def capture(response) -> None:
            nonlocal profile_payload
            parsed_url = urlparse(response.url)
            if parsed_url.hostname != FOMO_API_HOST:
                return
            try:
                payload = await response.json()
            except Exception:
                return

            path = parsed_url.path
            if path.casefold() == expected_path:
                profile_payload = payload
                profile_ready.set()
                identity = parse_profile_identity(payload, username)
                if identity and identity["profile_id"] in swaps_by_profile_id:
                    swaps_ready.set()
                return

            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[0:2] == ["v2", "users"] and parts[3] == "swaps":
                swaps_by_profile_id[parts[2]] = payload
                identity = parse_profile_identity(profile_payload or {}, username)
                if identity and identity["profile_id"] == parts[2]:
                    swaps_ready.set()

        def on_response(response) -> None:
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        self._page.on("response", on_response)
        referral = f"?r={quote(self.profile_handle)}" if self.profile_handle else ""
        profile_url = f"{FOMO_ORIGIN}/profile/{quote(username, safe='')}{referral}"
        try:
            await self._page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.wait_for(profile_ready.wait(), timeout=self.page_timeout_seconds)
            identity = parse_profile_identity(profile_payload or {}, username)
            if identity is None:
                raise RuntimeError("Fomo profile response did not contain the expected username and ID")
            if not swaps_ready.is_set():
                await asyncio.wait_for(swaps_ready.wait(), timeout=self.page_timeout_seconds)
            swaps_payload = swaps_by_profile_id.get(identity["profile_id"], {})
            response = swaps_payload.get("responseObject", {})
            swaps = response.get("swaps", []) if isinstance(response, dict) else []
            if not isinstance(swaps, list):
                swaps = []
            token_supplies, token_metadata = await self._fetch_token_supplies(swaps)
            return identity, {
                "swaps": swaps,
                "has_next_page": bool(response.get("hasNextPage")),
                "token_supplies": token_supplies,
                "token_metadata": token_metadata,
            }
        finally:
            self._page.remove_listener("response", on_response)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _fetch_token_supplies(
        self,
        swaps: list[dict[str, Any]],
        tracked_token_ids: list[str] | None = None,
    ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], dict[str, Any]]]:
        if self._page is None:
            return {}, {}
        token_ids = list(dict.fromkeys([
            f"{parsed['token_address']}:{parsed['network_id']}"
            for swap in swaps
            if (parsed := parse_buy_swap(swap)) is not None
        ] + (tracked_token_ids or [])))
        if not token_ids:
            return {}, {}
        token_rows = []
        for start in range(0, len(token_ids), FOMO_TOKEN_BATCH_SIZE):
            batch = token_ids[start:start + FOMO_TOKEN_BATCH_SIZE]
            try:
                rows = await self._page.evaluate(
                    """async (tokenIds) => {
                    const response = await fetch('https://prod-api.fomo.family/proxy/filterTokens', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'content-type': 'application/json',
                            'x-supported-chains': '1,56,143,4663,5042,8453,1399811149',
                            'app-language': 'en',
                        },
                        body: JSON.stringify(tokenIds),
                    });
                    if (!response.ok) throw new Error(`filterTokens request failed: ${response.status}`);
                    const payload = await response.json();
                    const rows = payload.responseObject;
                    if (!payload.success) throw new Error('filterTokens response was unsuccessful');
                    return Array.isArray(rows) ? rows : [];
                }""",
                    batch,
                )
                if isinstance(rows, list):
                    token_rows.extend(rows)
            except Exception:
                logger.exception("Fomo token metadata lookup failed for batch starting at %d", start)

        supplies = {}
        token_metadata = {}
        for row in token_rows:
            if not isinstance(row, dict):
                continue
            token = row.get("token") if isinstance(row.get("token"), dict) else row
            token_info = token.get("info") if isinstance(token.get("info"), dict) else {}
            address = token.get("address") or token.get("contractAddress") or row.get("address")
            network_id = token.get("networkId") or token.get("network_id") or row.get("networkId") or row.get("network_id")
            supply = token_info.get("circulatingSupply") or token.get("circulatingSupply") or row.get("circulatingSupply")
            if isinstance(address, str) and network_id is not None:
                holder_count = next((
                    value for source in (token_info, token, row)
                    for key in ("holderCount", "holder_count", "holdersCount", "holders_count", "holders", "totalHolders")
                    if (value := source.get(key)) is not None
                ), None)
                try:
                    holder_count = int(holder_count) if holder_count is not None else None
                except (TypeError, ValueError):
                    holder_count = None
                image_url = next((
                    value for source in (token_info, token, row)
                    for key in ("imageUrl", "image_url", "logoURI", "logoUrl", "image")
                    if isinstance((value := source.get(key)), str) and value.startswith(("http://", "https://"))
                ), None)
                metadata = {
                    "holder_count": holder_count,
                    "name": token_info.get("name") or token.get("name") or row.get("name"),
                    "symbol": token_info.get("symbol") or token.get("symbol") or row.get("symbol"),
                    "image_url": image_url,
                }
                quote_sources = [token_info, token, row]
                for source in tuple(quote_sources):
                    quote_sources.extend(
                        nested for key in ("marketData", "market_data", "metrics", "priceInfo", "price_info")
                        if isinstance((nested := source.get(key)), dict)
                    )
                for field, keys in {
                    "market_cap": ("marketCapUsd", "market_cap_usd", "marketCap", "market_cap"),
                    "price": ("priceUsd", "price_usd", "usdPrice", "price"),
                    "liquidity": ("liquidityUsd", "liquidity_usd", "liquidity"),
                    "volume": ("volume24hUsd", "volume_24h_usd", "volume24h", "volume_24h", "volume"),
                }.items():
                    value = _fomo_quote_value(quote_sources, keys)
                    if value is not None:
                        metadata[field] = value
                token_metadata[(str(network_id), address.lower())] = {
                    key: value for key, value in metadata.items() if value is not None
                }
            try:
                supply = float(supply)
            except (TypeError, ValueError):
                continue
            if isinstance(address, str) and network_id is not None and supply > 0:
                supplies[(str(network_id), address.lower())] = supply
        return supplies, token_metadata

    async def _refresh_tracked_token_metadata(self) -> int:
        tracked_token_ids = await asyncio.to_thread(_list_tracked_token_ids)
        if not tracked_token_ids:
            return 0
        _, token_metadata = await self._fetch_token_supplies([], tracked_token_ids)
        if not token_metadata:
            return 0
        return await asyncio.to_thread(_save_token_metadata, token_metadata)

    async def _refresh_profile_following(self) -> dict[str, int]:
        if self._page is None:
            return {"added": 0, "updated": 0, "conflicts": 0}
        profiles = await self._collect_profile_following()
        return await asyncio.to_thread(_save_followed_profiles, profiles)

    async def _collect_profile_following(self) -> list[dict[str, str | None]]:
        parsed_profile_url = urlparse(self.profile_url)
        path_parts = [part for part in parsed_profile_url.path.split("/") if part]
        if (
            parsed_profile_url.scheme != "https"
            or (parsed_profile_url.hostname or "").lower() not in {"fomo.family", "www.fomo.family"}
            or len(path_parts) != 2
            or path_parts[0].lower() != "profile"
        ):
            raise ValueError("FOMO_FAMILY_PROFILE_URL must be an HTTPS fomo.family/profile/<username> URL")

        await self._page.goto(self.profile_url, wait_until="domcontentloaded", timeout=30000)
        following_button = self._page.get_by_role(
            "button",
            name=re.compile(r"^\d+\s+Following$", re.IGNORECASE),
        ).first
        await following_button.wait_for(state="visible", timeout=self.page_timeout_seconds * 1000)
        await following_button.click()

        dialog = self._page.locator('[role="dialog"]').last
        await dialog.wait_for(state="visible", timeout=10000)
        scroll_containers = dialog.locator(".overflow-y-auto")
        if await scroll_containers.count() == 0:
            raise RuntimeError("Fomo Following dialog has no scrollable list")
        scroll_container = scroll_containers.first
        await scroll_container.evaluate("element => { element.scrollTop = 0; }")

        profiles: dict[str, dict[str, str | None]] = {}
        previous_count = -1
        stable_bottom_rounds = 0
        for _ in range(500):
            cards = await dialog.locator(
                'a[href*="/profile/"], a[href*="/user/"], a[href*="/trader/"]'
            ).evaluate_all(
                """elements => elements.map(element => {
                    const result = {
                        href: element.getAttribute('href'),
                        text: element.innerText || '',
                        username: element.getAttribute('data-username'),
                        profile_id: element.getAttribute('data-user-id') || element.getAttribute('data-profile-id')
                    };
                    let parent = element;
                    for (let depth = 0; parent && depth < 7; depth += 1, parent = parent.parentElement) {
                        result.username ||= parent.getAttribute('data-username');
                        result.profile_id ||= parent.getAttribute('data-user-id') || parent.getAttribute('data-profile-id');
                        const parentText = (parent.innerText || '').trim();
                        if (!/@[A-Za-z0-9_.-]+/.test(result.text) && /@[A-Za-z0-9_.-]+/.test(parentText)) {
                            result.text = parentText;
                        }
                    }
                    return result;
                })"""
            )
            for card in cards:
                profile = _profile_from_following_card(card)
                if profile:
                    key = profile["profile_id"] or (profile["username"] or "").casefold()
                    profiles[key] = profile

            position = await scroll_container.evaluate(
                "element => ({top: element.scrollTop, height: element.scrollHeight, client: element.clientHeight})"
            )
            at_bottom = position["top"] + position["client"] >= position["height"] - 2
            if at_bottom and len(profiles) == previous_count:
                stable_bottom_rounds += 1
            else:
                stable_bottom_rounds = 0
            if stable_bottom_rounds >= 3:
                break

            previous_count = len(profiles)
            await scroll_container.evaluate(
                "element => { element.scrollTop = Math.min(element.scrollTop + element.clientHeight * 0.8, element.scrollHeight); }"
            )
            await self._page.wait_for_timeout(800)

        if not profiles:
            raise RuntimeError("No profiles were found in the Fomo Following list; no changes were saved")
        return list(profiles.values())


def _list_traders(poll_interval_seconds: int = 300) -> list[dict[str, Any]]:
    due_before = datetime.now(timezone.utc) - timedelta(seconds=poll_interval_seconds)
    with SessionLocal() as session:
        rows = (
            session.query(FollowedTrader)
            .filter(
                FollowedTrader.tracking_enabled.is_(True),
                or_(
                    FollowedTrader.last_synced_at.is_(None),
                    FollowedTrader.last_synced_at <= due_before,
                ),
            )
            .order_by(FollowedTrader.last_synced_at.asc().nullsfirst(), FollowedTrader.id.desc())
            .all()
        )
        return [{"id": row.id, "username": row.username} for row in rows]


def _list_tracked_token_ids() -> list[str]:
    with SessionLocal() as session:
        dismissed_ids = active_dismissed_token_ids(session)
        rows = (
            session.query(Token.contract_address, Chain.chain_id, Chain.chain_slug)
            .join(Chain, Token.chain_id == Chain.id)
            .filter(~Token.id.in_(dismissed_ids)) if dismissed_ids else
            session.query(Token.contract_address, Chain.chain_id, Chain.chain_slug)
            .join(Chain, Token.chain_id == Chain.id)
        )
        rows = (
            rows
            .order_by(Token.id)
            .all()
        )
    return [
        f"{address}:{network_id or FOMO_NETWORK_IDS.get(chain_slug.lower())}"
        for address, network_id, chain_slug in rows
        if network_id or FOMO_NETWORK_IDS.get(chain_slug.lower())
    ]


def _profile_from_following_card(card: dict[str, Any]) -> dict[str, str | None] | None:
    href = card.get("href")
    if not isinstance(href, str) or not href.strip():
        return None
    parsed = urlparse(urljoin(FOMO_ORIGIN, href))
    parts = [part for part in parsed.path.split("/") if part]
    if (
        (parsed.hostname or "").lower() not in {"fomo.family", "www.fomo.family"}
        or len(parts) != 2
        or parts[0].lower() not in {"profile", "user", "trader"}
    ):
        return None

    username = card.get("username")
    username = username.strip().lstrip("@") if isinstance(username, str) else ""
    if not username:
        text = card.get("text") if isinstance(card.get("text"), str) else ""
        handle = re.search(r"@([A-Za-z0-9_.-]{2,128})", text)
        username = handle.group(1) if handle else unquote(parts[1])
    if not username:
        return None

    profile_id = card.get("profile_id")
    profile_id = str(profile_id).strip() if profile_id is not None else ""
    return {
        "profile_id": profile_id or None,
        "username": username,
        "profile_url": f"{FOMO_ORIGIN}/profile/{quote(username, safe='')}",
    }


def _save_followed_profiles(profiles: list[dict[str, str | None]]) -> dict[str, int]:
    totals = {"added": 0, "updated": 0, "conflicts": 0}
    for start in range(0, len(profiles), FOMO_DB_WRITE_BATCH_SIZE):
        with SessionLocal() as session:
            result = sync_followed_profiles(
                session,
                profiles[start:start + FOMO_DB_WRITE_BATCH_SIZE],
            )
        for key in totals:
            totals[key] += result[key]
    return totals


def _save_token_metadata(token_metadata: dict[tuple[str, str], dict[str, Any]]) -> int:
    from app.integrations.fomo_family.activity import persist_fomo_token_metadata

    items = list(token_metadata.items())
    for start in range(0, len(items), FOMO_DB_WRITE_BATCH_SIZE):
        with SessionLocal() as session:
            persist_fomo_token_metadata(
                session,
                dict(items[start:start + FOMO_DB_WRITE_BATCH_SIZE]),
            )
            session.commit()
    return len(token_metadata)


def _fomo_quote_value(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, dict):
                value = next((value.get(nested_key) for nested_key in ("usd", "value", "h24", "24h") if value.get(nested_key) is not None), None)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return number
    return None


def _is_browser_closed(error: Exception) -> bool:
    return type(error).__name__ == "TargetClosedError" or "Target page, context or browser has been closed" in str(error)


def _save_trader_activity(
    username: str,
    identity: dict[str, str] | None,
    activity: dict[str, Any],
    error: str | None,
) -> int:
    settings = get_settings()
    activity = activity if isinstance(activity, dict) else {}
    swaps = activity.get("swaps", []) if identity and not error else []
    market_caps = fetch_fomo_buy_market_caps(
        swaps,
        settings.dexscreener_api_url,
        activity.get("token_supplies"),
    )
    with SessionLocal() as session:
        trader = session.query(FollowedTrader).filter(FollowedTrader.username == username).first()
        if trader is None:
            return 0

        from datetime import datetime, timezone

        trader.last_synced_at = datetime.now(timezone.utc)
        trader.sync_error = error[:255] if error else None
        if identity:
            trader.platform_identifier = identity["profile_id"]
            trader.username = identity["username"]
            trader.profile_url = f"{FOMO_ORIGIN}/profile/{quote(identity['username'], safe='')}"
        inserted = 0
        if identity and not error:
            from app.integrations.fomo_family.activity import persist_fomo_token_metadata

            token_metadata = activity.get("token_metadata", {})
            persist_fomo_token_metadata(session, token_metadata)
            inserted = persist_fomo_buys(
                session,
                trader,
                identity["wallet_address"],
                swaps,
                discovery_threshold_usd=settings.discovery_threshold_usd,
                market_caps=market_caps,
                token_metadata=token_metadata,
            )
        session.commit()
        return inserted