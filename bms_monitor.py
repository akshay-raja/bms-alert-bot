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
        "theaters": ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"],
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
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def get_time_category(time_str):
    """
    Categorizes time string into:
      - morning:   12:00 AM - 12:00 PM
      - afternoon: 12:01 PM - 04:00 PM
      - evening:   04:01 PM - 07:00 PM
      - night:     07:01 PM - 11:59 PM
    """
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

    message_lines.append("\nTap to open BookMyShow immediately.")
    message = "\n".join(message_lines)

    target_topic = topic if topic else DEFAULT_HARDCODED_TOPIC

    headers = {
        "Title": f"BMS Alert: {user_name}!",
        "Priority": "urgent",
        "Tags": "ticket,seat",
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


def extract_matching_shows(page, target_theaters, availability_filter, preferred_times):
    """Deep inspects theater containers and pills for active styles and timeframes."""
    script = """
    () => {
        const venues = [];
        const rows = document.querySelectorAll('li.list, div.listing-info, div[class*="Venue"], div[class*="venue"]');
        
        rows.forEach(r => {
            const nameEl = r.querySelector('a[href*="/cinemas/"], .venue-name, a.name');
            if (!nameEl) return;
            const theater = nameEl.innerText.trim();

            const pills = r.querySelectorAll('a[class*="showtime"], div[class*="showtime"], a[class*="pill"], div[class*="pill"], .showtime-pill');
            const showList = [];

            pills.forEach(p => {
                const text = p.innerText.trim().split('\\n')[0];
                const allContent = (p.className + ' ' + p.innerHTML).toLowerCase();

                const isBlocked = p.classList.contains('disabled') || 
                                  p.classList.contains('_disabled') || 
                                  p.getAttribute('aria-disabled') === 'true';

                let status = "disabled";

                // Check colors across pill and its child elements
                const elements = [p, ...p.querySelectorAll('*')];
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const colorString = [
                        style.borderColor, 
                        style.borderLeftColor, 
                        style.color, 
                        style.backgroundColor
                    ].join(' ');

                    const rgbMatches = colorString.matchAll(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/g);
                    for (const match of rgbMatches) {
                        const r = parseInt(match[1]), g = parseInt(match[2]), b = parseInt(match[3]);
                        
                        // Ignore grey/monochrome tones
                        if (Math.abs(r - g) < 20 && Math.abs(g - b) < 20) continue;

                        // Green tone: Green is significantly higher than Red & Blue
                        if (g > 110 && g > r + 20 && g > b + 20) {
                            status = "available";
                            break;
                        }
                        // Orange / Amber tone: Red is high, Green is moderate, Blue is low
                        if (r > 160 && g > 60 && b < 100) {
                            status = "filling_fast";
                            break;
                        }
                    }
                    if (status !== "disabled") break;
                }

                // Fallback: If non-disabled anchor with an active link
                if (status === "disabled" && !isBlocked && p.tagName === 'A' && p.getAttribute('href')) {
                    status = "available";
                }

                const href = p.getAttribute('href') || p.closest('a')?.getAttribute('href') || '';
                if (!isBlocked && (status === "available" || status === "filling_fast")) {
                    showList.push({
                        time: text,
                        status: status,
                        href: href
                    });
                }
            });

            if (showList.length > 0) {
                venues.push({ theater, shows: showList });
            }
        });
        return venues;
    }
    """
    try:
        found_venues = page.evaluate(script)
    except Exception as e:
        print(f"    [!] DOM extraction error: {e}")
        found_venues = []

    matched = []
    for item in found_venues:
        venue_name = item["theater"]
        if not any(t.lower() in venue_name.lower() for t in target_theaters):
            continue

        for show in item["shows"]:
            if availability_filter != "both" and show["status"] != availability_filter:
                continue

            time_cat = get_time_category(show["time"])
            if preferred_times and time_cat not in preferred_times and time_cat != "all":
                continue

            matched.append({
                "theater": venue_name,
                "time": show["time"],
                "status": show["status"],
                "href": show["href"]
            })

    return matched


def scan_seat_layout(page, seat_url, preferred_row_categories):
    """Loads seat layout, bypasses terms popups, and categorizes row zones."""
    try:
        page.goto(seat_url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(2500)

        # Handle BMS "Terms & Conditions" / "Accept" popups if present
        try:
            accept_btn = page.locator("button:has-text('Accept'), div:has-text('Accept'), button:has-text('Proceed')")
            if accept_btn.first.is_visible(timeout=1500):
                accept_btn.first.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        script = """
        () => {
            const rowLabels = Array.from(document.querySelectorAll('div[class*="row-name"], td[class*="row-name"], .seat-row-name, span[class*="row"]'))
                                  .map(el => el.innerText.trim())
                                  .filter(txt => txt.length > 0 && txt.length <= 3);

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

        # Rows from screen perspective (bottom of page upwards)
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


def process_tracker(page, tracker):
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

    for date_str in target_dates:
        full_date_url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"\n[{time.strftime('%X')}] Scanning showtimes for {formatted_date} ({date_str})...")

        try:
            page.goto(full_date_url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(3500)
        except Exception as e:
            print(f"  [!] Failed to load {full_date_url}: {e}")
            continue

        matched_shows = extract_matching_shows(page, theaters, avail_filter, pref_times)
        if not matched_shows:
            print("  [-] No matching showtimes matching availability/time preferences.")
            continue

        for show in matched_shows:
            theater_name = show["theater"]
            show_time = show["time"]
            show_status = show["status"]
            href = show["href"]

            seat_url = f"https://in.bookmyshow.com{href}" if href.startswith("/") else href

            if seat_url:
                print(f"  -> Found {theater_name} ({show_time} - {show_status}). Checking seats...")
                matched_seats = scan_seat_layout(page, seat_url, pref_rows)
            else:
                matched_seats = {}

            if matched_seats or not href:
                print(f"     [🎉 SEATS/SHOW MATCHED] Rows: {list(matched_seats.keys()) if matched_seats else 'Open'}")
                final_alert_items.append((
                    date_str,
                    theater_name,
                    show_time,
                    show_status,
                    matched_seats,
                    seat_url or full_date_url
                ))

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
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        for tracker in trackers:
            process_tracker(page, tracker)

        browser.close()

    print("\n=== Scan Finished ===")


if __name__ == "__main__":
    main()
