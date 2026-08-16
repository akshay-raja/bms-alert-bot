import math
import re
import time
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
CONFIG_API_URL = "https://api.npoint.io/803e335a91e27ade1511"
DEFAULT_HARDCODED_TOPIC = "akshay-bms-alert-160826"

DEFAULT_TRACKERS = [
    {
        "id": "akshay",
        "user_name": "Akshay",
        "movie_url": "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815",
        "theaters": ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe", "Rakki"],
        "ntfy_topic": "akshay-bms-alert-160826",
        "days_ahead": 3,
        "availability_filter": "both",
        "preferred_times": ["morning", "afternoon", "evening", "night"],
        "preferred_rows": ["upper_middle", "near_top"]
    }
]


def load_trackers():
    try:
        res = requests.get(CONFIG_API_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and "trackers" in data:
                return data["trackers"]
            elif isinstance(data, list):
                return data
    except Exception as e:
        print(f"[!] Warning: Remote config fetch failed ({e}). Using default tracker.")
    return DEFAULT_TRACKERS


def get_target_dates(days_ahead):
    """Generates target dates in Indian Standard Time (IST)."""
    dates = []
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    for i in range(int(days_ahead) + 1):
        target_day = ist_now + timedelta(days=i)
        dates.append((target_day.strftime("%Y%m%d"), target_day.strftime("%d"), target_day.strftime("%b").upper()))
    return dates


def get_time_category(time_str):
    clean_time = time_str.upper().strip()
    match = re.search(r'(\d{1,2}):(\d{2})\s*([AP]M)', clean_time)
    if not match:
        return "all"

    hours = int(match.group(1))
    minutes = int(match.group(2))
    period = match.group(3)

    if period == "PM" and hours != 12:
        hours += 12
    elif period == "AM" and hours == 12:
        hours = 0

    total_mins = hours * 60 + minutes

    if total_mins <= 720:             # Up to 12:00 PM
        return "morning"
    elif 720 < total_mins <= 960:     # 12:01 PM to 04:00 PM
        return "afternoon"
    elif 960 < total_mins <= 1140:    # 04:01 PM to 07:00 PM
        return "evening"
    else:                             # 07:01 PM to 11:59 PM
        return "night"


def send_ntfy_alert(topic, movie_url, user_name, results):
    message_lines = [f"Hi {user_name}, matching shows/seats found!"]
    first_direct_url = None

    for date_str, theater_name, show_time, status, seat_dict, seat_url in results:
        if not first_direct_url:
            first_direct_url = seat_url
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        message_lines.append(f"\n[{formatted_date}] {theater_name}")
        message_lines.append(f"Show: {show_time} ({status.replace('_', ' ').title()})")
        
        if seat_dict:
            for row, seats in seat_dict.items():
                message_lines.append(f"  Row {row}: {', '.join(seats)}")
        else:
            message_lines.append("  (Show is open for booking)")

    message_lines.append("\nTap to open seat selection directly on BookMyShow.")
    message = "\n".join(message_lines)

    target_topic = topic if topic else DEFAULT_HARDCODED_TOPIC

    headers = {
        "Title": f"🎟️ BMS Seats Alert: {user_name}!",
        "Priority": "urgent",
        "Tags": "ticket,movie_camera",
        "Click": first_direct_url or movie_url,
    }

    try:
        response = requests.post(
            f"https://ntfy.sh/{target_topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if response.status_code == 200:
            print(f"  [✓] Notification delivered to '{target_topic}'!")
        else:
            print(f"  [!] ntfy delivery error: {response.status_code}")
    except Exception as exc:
        print(f"  [!] Exception delivering ntfy alert: {exc}")


def switch_date_tab_if_needed(page, day_number, month_short):
    """Clicks the date pill on the page if BMS loaded the default date."""
    try:
        # Try locating date button (e.g., text containing '17' and 'AUG')
        date_locator = page.locator(f"xpath=//*[contains(text(), '{day_number}') and contains(text(), '{month_short}')]").first
        if date_locator.is_visible(timeout=2000):
            date_locator.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass


def extract_all_active_shows(page, target_theaters, availability_filter, preferred_times):
    """
    Direct Anchor & Context Scanner:
    Inspects all clickable booking links on the page, determines their parent theater,
    time slot, and availability state.
    """
    script = """
    () => {
        const timeRegex = /(\\d{1,2}:\\d{2}\\s*(?:AM|PM))/i;
        const results = [];

        // Find all booking links that lead to seat layouts or session purchasing
        const links = Array.from(document.querySelectorAll('a[href*="/seat-layout/"], a[href*="/buytickets/"], a[class*="showtime"], a[class*="pill"], div[class*="showtime"] a'));
        
        links.forEach(a => {
            const text = a.innerText.trim();
            const timeMatch = text.match(timeRegex);
            if (!timeMatch) return;

            const timeStr = timeMatch[1].toUpperCase();

            // Locate parent theater by walking up the DOM tree
            let current = a.parentElement;
            let theaterName = "";

            while (current && current !== document.body) {
                const nameEl = current.querySelector('a[href*="/cinemas/"], .venue-name, a.name, [class*="venueName"], [class*="cinema-name"]');
                if (nameEl && nameEl.innerText.trim()) {
                    theaterName = nameEl.innerText.trim();
                    break;
                }
                current = current.parentElement;
            }

            if (!theaterName) return;

            // Determine availability color status
            const style = window.getComputedStyle(a);
            const isBlocked = a.classList.contains('disabled') || 
                              a.classList.contains('_disabled') || 
                              a.getAttribute('aria-disabled') === 'true';

            let status = "available";
            const colorString = [style.borderColor, style.borderLeftColor, style.color].join(' ');
            const rgbMatches = Array.from(colorString.matchAll(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/g));
            
            for (const m of rgbMatches) {
                const r = parseInt(m[1]), g = parseInt(m[2]), b = parseInt(m[3]);
                if (r > 150 && g > 60 && b < 110) {
                    status = "filling_fast";
                    break;
                }
            }

            if (!isBlocked) {
                results.push({
                    theater: theaterName,
                    time: timeStr,
                    status: status,
                    href: a.href || a.getAttribute('href') || ''
                });
            }
        });

        return results;
    }
    """
    try:
        raw_shows = page.evaluate(script)
    except Exception as e:
        print(f"    [!] Error during show extraction: {e}")
        raw_shows = []

    print(f"    [DOM SCAN] Total raw active show links found on page: {len(raw_shows)}")

    matched = []
    for show in raw_shows:
        venue_name = show["theater"]
        is_target_theater = any(t.lower() in venue_name.lower() for t in target_theaters)
        if not is_target_theater:
            continue

        time_cat = get_time_category(show["time"])
        print(f"    -> [THEATER MATCH] {venue_name} | Time: {show['time']} ({time_cat}) | Status: {show['status']}")

        if availability_filter != "both" and show["status"] != availability_filter:
            continue

        if preferred_times and time_cat not in preferred_times and time_cat != "all":
            continue

        matched.append(show)

    return matched


def scan_seat_layout(page, seat_url, preferred_row_categories):
    """Opens the direct seat layout link and checks for available rows."""
    try:
        page.goto(seat_url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(3000)

        # Handle BMS popup modals
        try:
            btn = page.locator("button:has-text('Accept'), div:has-text('Accept'), button:has-text('Select Seats')").first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        script = """
        () => {
            const rowLabels = Array.from(document.querySelectorAll('div[class*="row-name"], td[class*="row-name"], .seat-row-name, span[class*="row"]'))
                                  .map(el => el.innerText.trim())
                                  .filter(txt => txt.length > 0 && txt.length <= 3 && /^[A-Z0-9]+$/.test(txt));

            const orderedRows = [...new Set(rowLabels)];
            const seatElements = document.querySelectorAll('a[class*="_available"], div[class*="_available"], a[class*="seat"]:not([class*="blocked"]):not([class*="sold"]), .seat-available');
            
            const results = {};
            seatElements.forEach(s => {
                const id = s.innerText.trim() || s.getAttribute('data-seat-number') || '';
                const row = s.getAttribute('data-row-name') || id.replace(/[0-9]/g, '').trim();
                const num = id.replace(/[^0-9]/g, '').trim();
                
                if (row && num) {
                    if (!results[row]) results[row] = [];
                    results[row].push(num);
                }
            });

            return { orderedRows, availableSeatsByRow: results };
        }
        """
        data = page.evaluate(script)
        ordered_rows = data.get("orderedRows", [])
        seats_by_row = data.get("availableSeatsByRow", {})

        if not ordered_rows:
            ordered_rows = list(seats_by_row.keys())

        total_rows = len(ordered_rows)
        if total_rows == 0:
            return seats_by_row

        rows_from_screen = list(reversed(ordered_rows))

        near_screen_end = max(1, math.ceil(total_rows * 0.20))
        lower_middle_end = max(near_screen_end + 1, math.ceil(total_rows * 0.40))
        upper_middle_end = max(lower_middle_end + 1, math.ceil(total_rows * 0.70))

        zone_rows = {
            "near_screen": set(rows_from_screen[:near_screen_end]),
            "lower_middle": set(rows_from_screen[near_screen_end:lower_middle_end]),
            "upper_middle": set(rows_from_screen[lower_middle_end:upper_middle_end]),
            "near_top": set(rows_from_screen[upper_middle_end:])
        }

        target_rows = set()
        for pref in preferred_row_categories:
            target_rows.update(zone_rows.get(pref, set()))

        matched_seats = {}
        for row_name, seat_list in seats_by_row.items():
            if row_name in target_rows and seat_list:
                matched_seats[row_name] = seat_list

        return matched_seats

    except Exception as exc:
        print(f"    [!] Error inspecting seat layout: {exc}")
        return {}


def process_tracker(context, tracker):
    user_name = tracker.get("user_name", tracker.get("id", "User"))
    movie_url = tracker.get("movie_url", "").strip()
    theaters = tracker.get("theaters", [])
    topic = tracker.get("ntfy_topic", DEFAULT_HARDCODED_TOPIC).strip()
    days_ahead = tracker.get("days_ahead", 3)
    avail_filter = tracker.get("availability_filter", "both")
    pref_times = tracker.get("preferred_times", ["morning", "afternoon", "evening", "night"])
    pref_rows = tracker.get("preferred_rows", ["upper_middle", "near_top"])

    if not movie_url or not theaters:
        return

    print(f"\n==========================================")
    print(f"Watcher: {user_name} | ntfy Topic: {topic}")
    print(f"Theaters: {', '.join(theaters)}")
    print(f"Times: {', '.join(pref_times)} | Status: {avail_filter.upper()}")
    print(f"==========================================")

    target_dates = get_target_dates(days_ahead)
    final_alert_items = []

    for date_str, day_num, month_short in target_dates:
        full_date_url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"\n[{time.strftime('%X')}] Scanning showtimes for {formatted_date} ({date_str})...")

        page = context.new_page()
        try:
            page.goto(full_date_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(3000)
            switch_date_tab_if_needed(page, day_num, month_short)
        except Exception as e:
            print(f"  [!] Failed to load {full_date_url}: {e}")
            page.close()
            continue

        page_title = page.title()
        print(f"    [PAGE TITLE] {page_title}")

        matched_shows = extract_all_active_shows(page, theaters, avail_filter, pref_times)
        if not matched_shows:
            print("  [-] No matching showtimes matching availability/time preferences.")
            page.close()
            continue

        for show in matched_shows:
            theater_name = show["theater"]
            show_time = show["time"]
            show_status = show["status"]
            seat_url = show["href"]

            if not seat_url.startswith("http"):
                seat_url = f"https://in.bookmyshow.com{seat_url}"

            print(f"  -> Inspecting seat layout for {theater_name} @ {show_time}...")
            matched_seats = scan_seat_layout(page, seat_url, pref_rows)

            if matched_seats:
                print(f"     [🎉 SEATS MATCHED] Rows: {list(matched_seats.keys())}")
                final_alert_items.append((
                    date_str,
                    theater_name,
                    show_time,
                    show_status,
                    matched_seats,
                    seat_url
                ))
            else:
                print("     [-] No matching seats in selected distance zones.")

        page.close()

    if final_alert_items:
        print(f"\n[🚀] Delivering instant push alert to {user_name}...")
        send_ntfy_alert(topic, movie_url, user_name, final_alert_items)


def main():
    trackers = load_trackers()
    print(f"=== Starting Granular BMS Seat & Showtime Scanner ===")
    print(f"Active Watchers: {len(trackers)}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )

        for tracker in trackers:
            process_tracker(context, tracker)

        browser.close()

    print("\n=== Scan Finished ===")


if __name__ == "__main__":
    main()
