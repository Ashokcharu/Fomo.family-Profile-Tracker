from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from app.database import Base, SessionLocal, engine
from app.features.followed_users.service import add_followed_user

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


BASE_URL = "https://fomo.family"
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "followers.db"
JSON_PATH = DATA_DIR / "followers.json"
BROWSER_DIR = DATA_DIR / "browser_profile"
CDP_URL = "http://127.0.0.1:9222"
PROFILE_LINK_SELECTOR = 'a[href*="/profile/"], a[href*="/user/"], a[href*="/trader/"]'
USERNAME_PATTERN = re.compile(r"(?:^|\s)@([A-Za-z0-9_.-]{2,128})")
MAX_SCROLLS = 500
PAGE_WAIT_MS = 1200


def normalize_profile_url(href: str, base_url: str) -> str | None:
    url = urljoin(base_url, href)
    parsed = urlparse(url)
    base_host = (urlparse(base_url).hostname or "").lower()
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"fomo.family", "www.fomo.family"}:
        return None
    if base_host not in {"fomo.family", "www.fomo.family"}:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() not in {"profile", "user", "trader"}:
        return None

    return f"https://fomo.family/{'/'.join(path_parts)}"


def normalize_profile_page_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or host not in {"fomo.family", "www.fomo.family"}:
        raise ValueError("Profile URL must be an HTTPS fomo.family URL.")
    if len(path_parts) != 2 or path_parts[0].lower() != "profile":
        raise ValueError("Profile URL must use the /profile/<username> route.")
    return value


def make_profile_record(
    link: dict[str, str | None],
    base_url: str,
    collected_at: str | None = None,
) -> dict[str, str | None] | None:
    profile_url = normalize_profile_url(link.get("href") or "", base_url)
    if profile_url is None:
        return None

    visible_text = (link.get("text") or "").strip()[:4000]
    username = (link.get("username") or "").strip().lstrip("@")
    if not username:
        match = USERNAME_PATTERN.search(visible_text)
        if match:
            username = match.group(1)
    if not username:
        username = unquote(urlparse(profile_url).path.rstrip("/").split("/")[-1])

    display_name = (link.get("display_name") or "").strip()
    if not display_name:
        display_name = next(
            (line.strip() for line in visible_text.splitlines() if line.strip() and line.strip().lstrip("@") != username),
            "",
        )

    profile_id = (link.get("profile_id") or "").strip() or None
    return {
        "profile_id": profile_id,
        "profile_url": profile_url,
        "username": username or None,
        "display_name": display_name or None,
        "profile_text": visible_text or None,
        "collected_at": collected_at or datetime.now(timezone.utc).isoformat(),
    }


def profile_probe_succeeded(final_url: str, visible_text: str) -> bool:
    path = urlparse(final_url).path.rstrip("/")
    text = visible_text.strip().casefold()
    is_profile_route = path.startswith(("/profile/", "/user/", "/trader/"))
    is_marketing_shell = "where traders become legends" in text
    return is_profile_route and bool(text) and not is_marketing_shell


def initialize_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS followers (
                profile_url TEXT PRIMARY KEY,
                profile_id TEXT,
                username TEXT,
                display_name TEXT,
                profile_text TEXT,
                collected_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(followers)")}
        if "profile_id" not in columns:
            connection.execute("ALTER TABLE followers ADD COLUMN profile_id TEXT")


def save_profiles(profiles: list[dict[str, str | None]]) -> None:
    initialize_db()
    with sqlite3.connect(DB_PATH) as connection:
        connection.executemany(
            """
            INSERT INTO followers (
                profile_url, profile_id, username, display_name,
                profile_text, collected_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_url) DO UPDATE SET
                profile_id = excluded.profile_id,
                username = excluded.username,
                display_name = excluded.display_name,
                profile_text = excluded.profile_text,
                collected_at = excluded.collected_at
            """,
            [
                (
                    profile["profile_url"],
                    profile["profile_id"],
                    profile["username"],
                    profile["display_name"],
                    profile["profile_text"],
                    profile["collected_at"],
                )
                for profile in profiles
            ],
        )
    JSON_PATH.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def sync_profiles_to_app(profiles: list[dict[str, str | None]]) -> int:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    imported = 0
    with SessionLocal() as session:
        for profile in profiles:
            username = profile["username"]
            if not username:
                continue
            add_followed_user(
                session,
                username,
                platform_identifier=profile["profile_id"],
                profile_url=profile["profile_url"],
            )
            imported += 1
    return imported


async def collect_profile_links(page) -> list[dict[str, str | None]]:
    dialogs = page.locator('[role="dialog"]')
    if await dialogs.count() == 0:
        raise RuntimeError("Open your profile's Following dialog before collecting.")

    dialog = dialogs.last
    following_is_selected = await dialog.locator("button").evaluate_all(
        """buttons => buttons.some(button =>
            /^Following\\s*\\(\\d+\\)$/.test((button.innerText || '').trim()) &&
            button.className.includes('border-accent-primary'))"""
    )
    if not following_is_selected:
        raise RuntimeError("Select the Following tab in the open profile dialog before collecting.")

    scroll_containers = dialog.locator(".overflow-y-auto")
    if await scroll_containers.count() == 0:
        raise RuntimeError("Could not find the scrollable Following list in the open dialog.")
    scroll_container = scroll_containers.first
    await scroll_container.evaluate("element => { element.scrollTop = 0; }")
    await page.wait_for_timeout(PAGE_WAIT_MS)
    links: dict[str, dict[str, str | None]] = {}
    previous_count = -1
    stable_bottom_rounds = 0

    for _ in range(MAX_SCROLLS):
        raw_links = await dialog.locator(PROFILE_LINK_SELECTOR).evaluate_all(
            """elements => elements.map(element => {
                const result = {
                    href: element.getAttribute('href'),
                    text: element.innerText || '',
                    username: element.getAttribute('data-username'),
                    display_name: element.getAttribute('data-display-name'),
                    profile_id: element.getAttribute('data-user-id') || element.getAttribute('data-profile-id')
                };
                let parent = element;
                for (let depth = 0; parent && depth < 7; depth += 1, parent = parent.parentElement) {
                    result.username ||= parent.getAttribute('data-username');
                    result.display_name ||= parent.getAttribute('data-display-name');
                    result.profile_id ||= parent.getAttribute('data-user-id') || parent.getAttribute('data-profile-id');
                    const parentText = (parent.innerText || '').trim();
                    if (!/@[A-Za-z0-9_.-]+/.test(result.text) && /@[A-Za-z0-9_.-]+/.test(parentText)) {
                        result.text = parentText;
                    }
                }
                return result;
            })"""
        )
        for raw in raw_links:
            try:
                profile = make_profile_record(raw, page.url)
                if profile:
                    links[profile["profile_url"]] = profile
            except Exception as exc:
                print(f"Skipping a profile link: {exc}")

        count = len(links)
        position = await scroll_container.evaluate(
            "element => ({top: element.scrollTop, height: element.scrollHeight, client: element.clientHeight})"
        )
        at_bottom = position["top"] + position["client"] >= position["height"] - 2
        if at_bottom and count == previous_count:
            stable_bottom_rounds += 1
        else:
            stable_bottom_rounds = 0
        if stable_bottom_rounds >= 3:
            break

        previous_count = count
        await scroll_container.evaluate(
            "element => { element.scrollTop = Math.min(element.scrollTop + element.clientHeight * 0.8, element.scrollHeight); }"
        )
        await page.wait_for_timeout(PAGE_WAIT_MS)

    return list(links.values())


async def probe_url(url: str) -> int:
    if async_playwright is None:
        raise RuntimeError("Install the scraper extra and Chromium before using this script.")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            channel="chrome",
        )
        page = await browser.new_page()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(PAGE_WAIT_MS)
        link_count = await page.locator(PROFILE_LINK_SELECTOR).count()
        visible_text = (await page.locator("body").inner_text())[:1000]
        result = {
            "status_code": response.status if response else None,
            "requested_url": url,
            "final_url": page.url,
            "title": await page.title(),
            "profile_links": link_count,
            "visible_text": visible_text,
        }
        await browser.close()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if profile_probe_succeeded(result["final_url"], visible_text) else 2


async def collect_following_list(cdp_url: str | None = None, profile_url: str | None = None) -> int:
    if async_playwright is None:
        raise RuntimeError("Install with `pip install -e '.[scraper]'` and run `playwright install chromium`.")

    async with async_playwright() as playwright:
        if cdp_url:
            try:
                browser = await playwright.chromium.connect_over_cdp(cdp_url)
                if not browser.contexts:
                    raise RuntimeError("Chrome has no open browser context.")
                context = browser.contexts[0]
            except Exception as exc:
                raise RuntimeError(
                    f"Chrome is not listening at {cdp_url}. In another terminal, run:\n"
                    'google-chrome --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 '
                    '--user-data-dir="$HOME/.cache/fomo-following" https://fomo.family\n'
                    "Keep that Chrome window open, sign in, open Following, then retry this command."
                ) from exc
        else:
            BROWSER_DIR.mkdir(parents=True, exist_ok=True)
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_DIR),
                headless=False,
                channel="chrome",
                viewport={"width": 1440, "height": 950},
            )
        try:
            if cdp_url and profile_url:
                page = await context.new_page()
            else:
                page = context.pages[0] if context.pages else await context.new_page()

            if profile_url:
                target_url = normalize_profile_page_url(profile_url)
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                following_button = page.get_by_role(
                    "button",
                    name=re.compile(r"^\d+\s+Following$", re.IGNORECASE),
                ).first
                try:
                    await following_button.wait_for(state="visible", timeout=20000)
                except Exception as exc:
                    raise RuntimeError(
                        f"Profile page did not expose a Following control: {page.url}. Nothing was saved."
                    ) from exc
                await following_button.click()
                dialog = page.locator('[role="dialog"]').last
                await dialog.wait_for(state="visible", timeout=10000)
            elif not cdp_url:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                print("In the Chrome window, log in if needed and open your profile's Following dialog.")
                input("Press ENTER here when the Following list is visible in Chrome... ")
            else:
                print("Use the attached Chrome tab to open the profile's Following dialog.")
                input("Press ENTER here when the Following list is visible in Chrome... ")

            print(f"Collecting visible profile cards from: {page.url}")
            try:
                profiles = await collect_profile_links(page)
            except Exception as exc:
                if type(exc).__name__ == "TargetClosedError":
                    raise RuntimeError(
                        "The Chrome tab or CDP session closed before collection finished. "
                        "Keep Chrome open on the profile's Following dialog and retry; nothing was saved."
                    ) from exc
                raise
            if not profiles:
                print("No profile links were found. Nothing was saved.")
                print("Confirm you are logged in and the Following list is open, then check the site's current link markup.")
                print(f"Visible page text: {(await page.locator('body').inner_text())[:600]}")
                return 2

            save_profiles(profiles)
            imported = sync_profiles_to_app(profiles)
            identified = sum(bool(profile["profile_id"]) for profile in profiles)
            print(f"Collected {len(profiles)} profiles; {identified} exposed a profile ID in visible DOM attributes.")
            print(f"Imported {imported} usernames into the app database.")
            print(f"SQLite: {DB_PATH}")
            print(f"JSON: {JSON_PATH}")
            if identified < len(profiles):
                print("Some unique IDs were not exposed in visible markup and were left blank, not guessed from usernames.")
            return 0
        finally:
            if not cdp_url:
                await context.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Import visible Fomo Family Following-list profiles.")
    parser.add_argument("--probe-url", help="Inspect a public URL without saving anything or using a login session.")
    parser.add_argument(
        "--connect-cdp",
        nargs="?",
        const=CDP_URL,
        metavar="URL",
        help="Attach to a Chrome window you started normally with remote debugging enabled.",
    )
    parser.add_argument(
        "--profile-url",
        help="Open this Fomo profile and import its Following list, e.g. https://fomo.family/profile/name?r=owner.",
    )
    args = parser.parse_args()
    if args.probe_url:
        return await probe_url(args.probe_url)
    return await collect_following_list(args.connect_cdp, args.profile_url)


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except RuntimeError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from None
    raise SystemExit(exit_code)