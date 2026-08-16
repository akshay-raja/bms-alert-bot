import math
import re
import time
from datetime import datetime, timedelta
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
        print(f"[!] Warning: Remote config fetch failed ({e}). Using default.")
    return DEFAULT_TRACKERS


def get_target_dates(days_ahead):
    dates = []
    base_date = datetime.now()
    for i in range(int(days_ahead) + 1):
        target_day = base_date + timedelta(days=i)
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def get_time_category(time_str):
    clean_time = time_str.upper().strip()
    match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)', clean_time)
    if not match:
        return "all"
    hours, minutes, period = int(match.group(1)), int(match.group(2)), match.group(3)
    if period == "PM" and hours != 12:
        hours += 12
    elif period == "AM" and hours == 12:
        hours = 0

    if 6 <= hours < 12:
        return "morning"
    elif 12 <= hours < 16:
        return "afternoon"
    elif 16 <= hours < 20:
        return "evening"
    else:
        return "night"


def send_ntfy_alert(topic, movie_url, user_name, results):
    message_lines = [f"Hi {user_name}, matching seats found!"]
    first_direct_url = None

    for date_str, theater_name, show_time, status, seat_dict, seat_url in results:
        if not first_direct_url:
            first_direct_url = seat_url
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        message_lines.append(f"\n[{formatted_date}] {theater_name}")
        message_lines.append(f"Show: {show_time} ({status.title()})")
        
        for row, seats in seat_dict.items():
            message_lines.append(f"  Row {row}: {', '.join(seats)}")

    message_lines.append("\nTap notification to open seat layout immediately.")
    message = "\n".join(message_lines)

    target_topic = topic if topic else DEFAULT_HARDCODED_TOPIC

    headers = {
        "Title": f"🎟️ BMS Seats Alert: {user_name}!",
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
                const style = window.getComputedStyle(p);
                const borderLeft = style.borderLeftColor || style.borderColor || '';
                const color = style.color || '';

                const isBlocked = p.classList.contains('disabled') || 
                                  p.classList.contains('_disabled') || 
                                  p.getAttribute('aria-disabled') === 'true' ||
                                  style.pointerEvents === 'none';

                let status = "disabled";
                const rgbMatch = (borderLeft + ' ' + color).match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (rgbMatch) {
                    const [_, r_val, g_val, b_val] = rgbMatch.map(Number);
                    if (g_val > 140 && g_val > r_val + 20) {
                        status = "available";
                    } else if (r_val > 200 && g_val > 100 && b_val < 80) {
                        status = "filling_fast";
                    }
                }

                const href = p.getAttribute('href') || (p.tagName === 'A' ? p.href : '');
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
    except Exception:
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
            if preferred_times and time_cat not in preferred_times:
                continue

            matched.append({
                "theater": venue_name,
                "time": show["time"],
                "status": show["status"],
                "href": show["href"]
            })

    return matched


def scan_seat_layout(page, seat_url, preferred_row_categories):
    try:
        page.goto(seat_url, wait_until="networkidle", timeout=35000)
        page.wait_for_timeout(3500)

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
            return {}

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
        print(f"    [!] Error scanning seat layout: {exc}")
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
    print(f"Checking: {user_name} | Channel: {topic}")
    print(f"Times: {', '.join(pref_times)} | Zones: {', '.join(pref_rows)}")
    print(f"Status Filter: {avail_filter.upper()}")
    print(f"==========================================")

    target_dates = get_target_dates(days_ahead)
    final_alert_items = []

    for date_str in target_dates:
        full_date_url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"\n[{time.strftime('%X')}] Scanning showtimes for {formatted_date}...")

        try:
            page.goto(full_date_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(3000)
        except Exception:
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
            if not seat_url:
                continue

            print(f"  -> Found {theater_name} ({show_time} - {show_status}). Inspecting seats...")
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
                print("     [-] No matching seats open in selected row zones.")

    if final_alert_items:
        print(f"\n[🚀] Delivering instant push notification to {user_name}...")
        send_ntfy_alert(topic, movie_url, user_name, final_alert_items)


def main():
    trackers = load_trackers()
    print(f"=== Starting Granular BMS Seat & Showtime Scanner ===")
    print(f"Total Watchers: {len(trackers)}")

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
