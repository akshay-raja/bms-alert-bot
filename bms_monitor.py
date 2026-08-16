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
    message_lines = [f"Hi {user_name}, matching seats/shows found!"]
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


def extract_theaters_and_pills(page, target_theaters, availability_filter, preferred_times):
    """
    Extracts theater elements and individual showtime pills matching user criteria.
    Returns DOM handle indices so Playwright can click them directly.
    """
    script = """
    () => {
        const timeRegex = /\\b(\\d{1,2}:\\d{2}\\s*(?:AM|PM))\\b/i;
        const matchedItems = [];

        // Locate all theater cards/containers
        const allContainers = document.querySelectorAll('li, div[class*="listing"], div[class*="Venue"], div[class*="venue"], div[class*="Cinema"], div[class*="cinema"]');
        const validTheaterContainers = [];

        for (const c of allContainers) {
            const heading = c.querySelector('a[href*="/cinemas/"], .venue-name, a.name, [class*="venueName"], [class*="cinema-name"]');
            if (heading && heading.innerText.trim()) {
                // Ensure container has showtime pills inside
                if (c.innerText.match(timeRegex)) {
                    validTheaterContainers.push({ container: c, name: heading.innerText.trim() });
                }
            }
        }

        // De-duplicate containers (keep innermost specific container)
        const filteredContainers = validTheaterContainers.filter((item, idx) => {
            return !validTheaterContainers.some((other, oIdx) => idx !== oIdx && item.container.contains(other.container));
        });

        filteredContainers.forEach((t, tIndex) => {
            const pills = Array.from(t.container.querySelectorAll('a, button, div, span')).filter(el => {
                const text = el.innerText ? el.innerText.trim() : '';
                return text.match(timeRegex) && text.length < 35 && el.children.length <= 4;
            });

            // De-duplicate pills inside this container
            const uniquePills = pills.filter((p, pIdx) => {
                return !pills.some((other, oIdx) => pIdx !== oIdx && p.contains(other));
            });

            uniquePills.forEach((p, pIndex) => {
                const text = p.innerText.trim();
                const timeMatch = text.match(timeRegex);
                if (!timeMatch) return;

                const style = window.getComputedStyle(p);
                const parentStyle = p.parentElement ? window.getComputedStyle(p.parentElement) : style;

                const isBlocked = p.classList.contains('disabled') || 
                                  p.classList.contains('_disabled') || 
                                  p.getAttribute('aria-disabled') === 'true' ||
                                  style.pointerEvents === 'none' ||
                                  style.cursor === 'not-allowed';

                let status = "disabled";
                const colorString = [
                    style.borderColor, 
                    style.borderLeftColor, 
                    style.color, 
                    style.backgroundColor,
                    parentStyle.borderColor,
                    parentStyle.borderLeftColor
                ].join(' ');

                const rgbMatches = Array.from(colorString.matchAll(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/g));
                for (const m of rgbMatches) {
                    const r = parseInt(m[1]), g = parseInt(m[2]), b = parseInt(m[3]);
                    if (Math.abs(r - g) < 25 && Math.abs(g - b) < 25) continue;

                    if (g > 100 && g > r + 15 && g > b + 15) {
                        status = "available"; // Green
                        break;
                    }
                    if (r > 150 && g > 60 && b < 110) {
                        status = "filling_fast"; // Orange
                        break;
                    }
                }

                if (status === "disabled" && !isBlocked) {
                    status = "available";
                }

                // Tag element with unique locator attribute for Playwright click
                const uniqueId = `bms-target-pill-${tIndex}-${pIndex}`;
                p.setAttribute('data-bms-id', uniqueId);

                matchedItems.push({
                    theater: t.name,
                    time: timeMatch[1].toUpperCase(),
                    status: status,
                    selector: `[data-bms-id="${uniqueId}"]`
                });
            });
        });

        return matchedItems;
    }
    """
    try:
        raw_items = page.evaluate(script)
    except Exception as e:
        print(f"    [!] Extraction error: {e}")
        return []

    matched = []
    for item in raw_items:
        venue_name = item["theater"]
        is_target_theater = any(t.lower() in venue_name.lower() for t in target_theaters)
        if not is_target_theater:
            continue

        time_cat = get_time_category(item["time"])
        print(f"  [FOUND] {venue_name} | Show: {item['time']:<8} | Status: {item['status']:<12} | Slot: {time_cat}")

        if availability_filter != "both" and item["status"] != availability_filter:
            continue

        if preferred_times and time_cat not in preferred_times and time_cat != "all":
            continue

        if item["status"] in ["available", "filling_fast"]:
            matched.append(item)

    return matched


def scan_seat_layout(page, preferred_row_categories):
    """
    Evaluates current seat layout page, handles terms/quantity popups,
    and categorizes available (green/orange) seats into screen-relative zones.
    """
    try:
        page.wait_for_timeout(3500)

        # 1. Accept Terms & Conditions modal if present
        try:
            accept_btn = page.locator("button:has-text('Accept'), div:has-text('Accept'), button:has-text('Proceed')")
            if accept_btn.first.is_visible(timeout=1500):
                accept_btn.first.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        # 2. Handle 1-2 Ticket Quantity Selector popup if present
        try:
            select_seats_btn = page.locator("button:has-text('Select Seats'), div:has-text('Select Seats')")
            if select_seats_btn.first.is_visible(timeout=1500):
                select_seats_btn.first.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        script = """
        () => {
            // Find all row labels
            const rowLabels = Array.from(document.querySelectorAll('div[class*="row-name"], td[class*="row-name"], .seat-row-name, span[class*="row"], .seat-row, div[class*="Row"]'))
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

        # Rows from screen perspective (bottom to top)
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

    for date_str in target_dates:
        full_date_url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"\n[{time.strftime('%X')}] Scanning showtimes for {formatted_date} ({date_str})...")

        page = context.new_page()
        try:
            page.goto(full_date_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(3500)
        except Exception as e:
            print(f"  [!] Failed to load {full_date_url}: {e}")
            page.close()
            continue

        matched_shows = extract_theaters_and_pills(page, theaters, avail_filter, pref_times)
        if not matched_shows:
            print("  [-] No shows matching availability or time preferences on this date.")
            page.close()
            continue

        for show in matched_shows:
            theater_name = show["theater"]
            show_time = show["time"]
            show_status = show["status"]
            selector = show["selector"]

            print(f"\n  -> [CLICKING SHOW] {theater_name} @ {show_time} to inspect seat layout...")
            
            # Click the showtime pill to navigate to seat selection layout
            try:
                page.click(selector, timeout=5000)
                page.wait_for_timeout(3500)
            except Exception as e:
                print(f"     [!] Could not click showtime pill ({e}). Continuing...")
                continue

            seat_layout_url = page.url
            matched_seats = scan_seat_layout(page, pref_rows)

            if matched_seats:
                print(f"     [🎉 SEATS MATCHED] Rows: {list(matched_seats.keys())}")
                final_alert_items.append((
                    date_str,
                    theater_name,
                    show_time,
                    show_status,
                    matched_seats,
                    seat_layout_url
                ))
            else:
                print("     [-] No matching seats open in selected row zones.")

            # Return back to main date view for next show iteration
            page.goto(full_date_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

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
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
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
