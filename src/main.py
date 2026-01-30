import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, cast
from zoneinfo import ZoneInfo

from gcsa.event import Event
from gcsa.google_calendar import GoogleCalendar
from google.oauth2.service_account import Credentials
from playwright.async_api import BrowserContext, Locator, async_playwright
from pydantic import BaseModel, ConfigDict

FOODCOOP_URL = "https://members.foodcoop.com"


def get_google_credentials() -> Credentials:
    """Get Google credentials from environment variable or file.

    In Modal, credentials are passed as a JSON string in GOOGLE_CREDENTIALS_JSON.
    For local development, credentials are read from credentials.json file.
    """
    scopes = ["https://www.googleapis.com/auth/calendar"]

    if creds_json := os.getenv("GOOGLE_CREDENTIALS_JSON"):
        return Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=scopes,
        )
    return Credentials.from_service_account_file(
        Path("credentials.json"),
        scopes=scopes,
    )


async def authenticate_into_foodcoop(browser_context: BrowserContext):
    username = os.getenv("FOODCOOP_USERNAME")
    password = os.getenv("FOODCOOP_PASSWORD")
    assert username, "FOODCOOP_USERNAME is not set"
    assert password, "FOODCOOP_PASSWORD is not set"

    login_url = f"{FOODCOOP_URL}/services/login"
    home_url = f"{FOODCOOP_URL}/services/home"

    page = await browser_context.new_page()

    login_start = time.perf_counter()

    await page.goto(login_url, wait_until="domcontentloaded")

    await page.get_by_role("textbox", name="Member Number or Email").fill(username)
    await page.get_by_role("textbox", name="Password").fill(password)
    await page.get_by_role("button", name="Log In").click()

    if not page.url.startswith(home_url):
        raise Exception(
            f"Authentication failed. Expected to be redirected to {home_url} but was at {page.url}."
        )

    print(f"Login completed in {time.perf_counter() - login_start:.2f}s")


class FoodCoopShiftKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_time: datetime
    label: str


class FoodCoopShift(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: FoodCoopShiftKey
    urls: frozenset[str]

    @staticmethod
    def from_event(event: Event) -> "FoodCoopShift":
        start_time = cast(datetime, event.start)
        label = cast(str, event.summary)
        description = cast(str, event.description)

        urls = [
            line.strip().lstrip("<li>").rstrip("</li>")
            for line in description.splitlines()
            if line.strip().startswith("<li>")
        ]

        shift = FoodCoopShift(
            key=FoodCoopShiftKey(start_time=start_time, label=label),
            urls=frozenset(urls),
        )

        return shift


def get_calendar_page_urls(num_pages: int = 6) -> list[str]:
    today = datetime.now()
    shift_calendar_url = f"{FOODCOOP_URL}/services/shifts"

    return [
        f"{shift_calendar_url}/{shift_page}/0/0/{today.strftime('%Y-%m-%d')}/"
        for shift_page in range(num_pages)
    ]


async def parse_shifts_from_calendar_date_locator(
    shift_day: Locator,
) -> Iterable[FoodCoopShift]:
    day_data = await shift_day.evaluate(
        """
        (day) => {
            const dateText = day.querySelector("p b")?.innerText?.trim() || "";
            const shifts = Array.from(day.querySelectorAll("a.shift:not(.my_shift)")).map(
                (shift) => ({
                    href: shift.getAttribute("href") || "",
                    time: shift.querySelector("b")?.innerText?.trim() || "",
                    label: shift.innerText?.trim() || "",
                })
            );

            return { dateText, shifts };
        }
        """
    )

    date_parts = day_data["dateText"].strip().split()
    assert len(date_parts) >= 2, "Shift date text is missing"
    date = date_parts[-1]

    shifts_for_key: dict[FoodCoopShiftKey, list[str]] = {}
    for shift in day_data["shifts"]:
        url = shift["href"].strip()
        assert url, "Shift url is missing"
        url = f"{FOODCOOP_URL}{url.strip().rstrip('/')}"

        start_time = shift["time"].strip()
        label_text = shift["label"].strip()
        _, label = label_text.lstrip("🥕").split(maxsplit=1)
        shift_name, emoji = label.rsplit(maxsplit=1)

        # Put the emoji in front of the label for easier visual parsing on the calendar
        label = f"{emoji} {shift_name}"

        start_time = datetime.strptime(f"{date} {start_time}", "%m/%d/%Y %I:%M%p")
        start_time = start_time.replace(tzinfo=ZoneInfo("US/Eastern"))

        key = FoodCoopShiftKey(start_time=start_time, label=label)

        shifts_for_key.setdefault(key, []).append(url)

    shifts = [
        FoodCoopShift(key=key, urls=frozenset(urls))
        for key, urls in shifts_for_key.items()
    ]

    return shifts


async def parse_shifts_from_calendar_page(
    browser_context: BrowserContext,
    url: str,
) -> list[FoodCoopShift]:
    page = await browser_context.new_page()
    page_load_start = time.perf_counter()
    await page.goto(url, wait_until="domcontentloaded")
    print(
        f"Loaded calendar page {url} in {time.perf_counter() - page_load_start:.2f}s"
    )

    shifts = []
    parse_start = time.perf_counter()
    for task in asyncio.as_completed(
        [
            parse_shifts_from_calendar_date_locator(shift_day_locator)
            for shift_day_locator in (
                await page.locator(".grid-container div.col").all()
            )
        ]
    ):
        shifts.extend(await task)

    print(
        f"Parsed shifts from page {url} in {time.perf_counter() - parse_start:.2f}s"
    )

    return shifts


async def parse_shifts_from_calendar(
    browser_context: BrowserContext,
) -> list[FoodCoopShift]:
    shifts = []
    for task in asyncio.as_completed(
        [
            parse_shifts_from_calendar_page(browser_context, url)
            for url in get_calendar_page_urls()
        ]
    ):
        shifts.extend(await task)

    return shifts


def create_event_from_shift(shift: FoodCoopShift) -> Event:
    shift_length = timedelta(hours=2, minutes=45)
    location = "Park Slope Food Coop"

    return Event(
        summary=shift.key.label,
        start=shift.key.start_time,
        end=shift.key.start_time + shift_length,
        description="\n".join(
            [
                f"{len(shift.urls)} shift(s) available for {shift.key.label}:",
                "<ul>",
                *(f"<li>{url}</li>" for url in shift.urls),
                "</ul>",
            ]
        ),
        location=location,
    )


def reconcile_shifts_to_google_calendar(shifts: list[FoodCoopShift]):
    calendar_id = "9b8f99f4caf33d2afbd17ac5f64a5113c7e373686247a7126b6a0b96a8cbd462@group.calendar.google.com"

    foodcoop_shift_calendar = GoogleCalendar(
        default_calendar=calendar_id,
        credentials=get_google_credentials(),  # type: ignore
    )

    existing_shifts_for_key: dict[FoodCoopShiftKey, tuple[FoodCoopShift, Event]] = {}
    for event in foodcoop_shift_calendar.get_events():
        existing_shift = FoodCoopShift.from_event(event)

        if existing_shift.key in existing_shifts_for_key:
            foodcoop_shift_calendar.delete_event(event)
        else:
            existing_shifts_for_key[existing_shift.key] = (existing_shift, event)

    parsed_shifts_for_key = {shift.key: shift for shift in shifts}

    print(f"Found {len(existing_shifts_for_key)} shifts in calendar.")
    print(f"Found {len(parsed_shifts_for_key)} shifts in parsed calendar.")

    # Add shifts that don't exist in the calendar
    shifts_to_add = [
        shift for shift in shifts if shift.key not in existing_shifts_for_key
    ]

    print(f"Adding {len(shifts_to_add)} shifts to calendar.")
    for shift in shifts_to_add:
        foodcoop_shift_calendar.add_event(create_event_from_shift(shift))

    # Remove shifts that no longer exist
    events_to_remove = [
        event
        for (shift, event) in existing_shifts_for_key.values()
        if shift.key not in parsed_shifts_for_key
    ]

    print(f"Removing {len(events_to_remove)} shifts to calendar.")
    for event in events_to_remove:
        foodcoop_shift_calendar.delete_event(event)

    # Update shifts that have changed
    shifts_to_update = [
        (parsed_shifts_for_key[shift.key], event)
        for (shift, event) in existing_shifts_for_key.values()
        if shift.key in parsed_shifts_for_key
        and shift.urls != parsed_shifts_for_key[shift.key].urls
    ]

    print(f"Updating {len(shifts_to_update)} shifts to calendar.")
    for shift, event in shifts_to_update:
        event.description = create_event_from_shift(shift).description
        foodcoop_shift_calendar.update_event(event)


async def main():
    start_time = time.time()

    print("Parsing shifts from foodcoop calendar...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        browser_context = await browser.new_context()

        async def block_heavy_resources(route):
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
                return
            await route.continue_()

        await browser_context.route("**/*", block_heavy_resources)

        await authenticate_into_foodcoop(browser_context)

        shifts = await parse_shifts_from_calendar(browser_context)

        await browser.close()

    print(f"Parsed {len(shifts)} shifts in {time.time() - start_time:.2f} seconds.")

    start_time = time.time()

    print("Reconciling shifts to Google Calendar...")

    reconcile_shifts_to_google_calendar(shifts)

    print(
        f"Finished reconciling shifts to calendar in {time.time() - start_time:.2f} seconds."
    )
