import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from curl_cffi import requests

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
        print(f"[!] Remote config fetch failed ({e}). Using fallback tracker.")
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

    if total_mins <= 720:
        return "morning"
    elif 720 < total_mins <= 960:
        return "afternoon"
    elif 960 < total_mins <= 1140:
        return "evening"
    else:
        return "night"


def send_ntfy_alert(topic, movie_url, user_name, results):
    message_lines = [f"Hi {user_name}, matching shows found!"]
    first_direct_url = None

    for date_str, theater_name, show_time, status, seat_url in results:
        if not first_direct_url:
            first_direct_url = seat_url
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        message_lines.append(f"\n[{formatted_date}] {theater_name}")
        message_lines.append(f"Show: {show_time} ({status.replace('_', ' ').title()})")

    message_lines.append("\nTap notification to open BookMyShow immediately.")
    message = "\n".join(message_lines)

    target_topic = topic if topic else DEFAULT_HARDCODED_TOPIC

    headers = {
        "Title": f"🎟️ BMS Ticket Alert: {user_name}!",
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
            print(f"  [!] ntfy delivery status: {response.status_code}")
    except Exception as exc:
        print(f"  [!] Exception delivering ntfy alert: {exc}")


def fetch_page_stealth(session, url):
    """Fetches URL by impersonating Chrome 124 TLS & HTTP/2 fingerprints."""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    try:
        response = session.get(url, headers=headers, impersonate="chrome124", timeout=20)
        return response
    except Exception as e:
        print(f"    [!] Fetch error: {e}")
        return None


def parse_bms_shows(html_text, target_theaters, availability_filter, preferred_times):
    """
    Parses BookMyShow server-rendered state and venue markup directly from HTML.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    time_regex = re.compile(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', re.IGNORECASE)
    matched = []

    # 1. Parse JSON state (__NEXT_DATA__) if available
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            data = json.loads(next_script.string)
            venues = data.get("props", {}).get("pageProps", {}).get("initialData", {}).get("venues", [])
            for v in venues:
                v_name = v.get("VenueName", "")
                if not any(t.lower() in v_name.lower() for t in target_theaters):
                    continue

                for s in v.get("ShowTimes", []):
                    show_time = s.get("ShowTime", "")
                    avail_status = s.get("AvailStatus", "1")
                    status = "available" if avail_status == "1" else "filling_fast"
                    time_cat = get_time_category(show_time)

                    if availability_filter != "both" and status != availability_filter:
                        continue
                    if preferred_times and time_cat not in preferred_times and time_cat != "all":
                        continue

                    session_url = s.get("SessionUrl", "")
                    matched.append({
                        "theater": v_name,
                        "time": show_time,
                        "status": status,
                        "url": f"https://in.bookmyshow.com{session_url}" if session_url.startswith("/") else session_url
                    })
        except Exception:
            pass

    if matched:
        return matched

    # 2. DOM Tree Search Fallback
    venue_containers = soup.find_all(["li", "div"], class_=lambda c: c and any(k in c.lower() for k in ["venue", "cinema", "listing", "list"]))
    
    for vc in venue_containers:
        title_el = vc.find(["a", "span", "div"], href=lambda h: h and "/cinemas/" in h) or vc.find(class_=lambda c: c and "name" in c.lower())
        if not title_el:
            continue

        theater_name = title_el.get_text(strip=True)
        if not any(t.lower() in theater_name.lower() for t in target_theaters):
            continue

        pills = vc.find_all(["a", "div", "button"], href=lambda h: h and ("/seat-layout/" in h or "/buytickets/" in h))
        if not pills:
            pills = [el for el in vc.find_all(["a", "div", "span"]) if time_regex.search(el.get_text(strip=True))]

        for p in pills:
            text = p.get_text(strip=True)
            match = time_regex.search(text)
            if not match:
                continue

            show_time = match.group(1).upper()
            classes = " ".join(p.get("class", []))
            if "disabled" in classes or "_disabled" in classes:
                continue

            status = "filling_fast" if any(k in classes for k in ["filling", "fast", "orange", "yellow"]) else "available"
            time_cat = get_time_category(show_time)

            if availability_filter != "both" and status != availability_filter:
                continue
            if preferred_times and time_cat not in preferred_times and time_cat != "all":
                continue

            href = p.get("href", "")
            seat_url = f"https://in.bookmyshow.com{href}" if href.startswith("/") else href

            matched.append({
                "theater": theater_name,
                "time": show_time,
                "status": status,
                "url": seat_url
            })

    return matched


def process_tracker(session, tracker):
    user_name = tracker.get("user_name", tracker.get("id", "User"))
    movie_url = tracker.get("movie_url", "").strip()
    theaters = tracker.get("theaters", [])
    topic = tracker.get("ntfy_topic", DEFAULT_HARDCODED_TOPIC).strip()
    days_ahead = tracker.get("days_ahead", 3)
    avail_filter = tracker.get("availability_filter", "both")
    pref_times = tracker.get("preferred_times", ["morning", "afternoon", "evening", "night"])

    if not movie_url or not theaters:
        return

    print(f"\n==========================================")
    print(f"Watcher: {user_name} | Channel: {topic}")
    print(f"Theaters: {', '.join(theaters)}")
    print(f"Slots: {', '.join(pref_times)} | Status: {avail_filter.upper()}")
    print(f"==========================================")

    target_dates = get_target_dates(days_ahead)
    final_alert_items = []

    for date_str in target_dates:
        full_date_url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"\n[{time.strftime('%X')}] Scanning showtimes for {formatted_date} ({date_str})...")

        res = fetch_page_stealth(session, full_date_url)
        if not res or res.status_code != 200:
            print(f"  [!] HTTP {res.status_code if res else 'Failed'} fetching {full_date_url}")
            continue

        # Verify Cloudflare status
        if "Attention Required! | Cloudflare" in res.text or "Just a moment..." in res.text:
            print("  [!] Cloudflare challenge encountered.")
            continue

        print(f"  [✓] Page fetched successfully (Size: {len(res.text)} bytes)")

        matched_shows = parse_bms_shows(res.text, theaters, avail_filter, pref_times)
        if not matched_shows:
            print("  [-] No matching shows open for this date.")
            continue

        for show in matched_shows:
            theater_name = show["theater"]
            show_time = show["time"]
            show_status = show["status"]
            seat_url = show["url"]

            print(f"  -> [🎉 MATCH CONFIRMED] {theater_name} @ {show_time} ({show_status.upper()})")
            final_alert_items.append((
                date_str,
                theater_name,
                show_time,
                show_status,
                seat_url or full_date_url
            ))

    if final_alert_items:
        print(f"\n[🚀] Delivering instant push notification to {user_name}...")
        send_ntfy_alert(topic, movie_url, user_name, final_alert_items)


def main():
    trackers = load_trackers()
    print(f"=== Starting Granular BMS Stealth Scanner ===")
    print(f"Total Watchers: {len(trackers)}")

    session = requests.Session()

    for tracker in trackers:
        process_tracker(session, tracker)

    print("\n=== Scan Finished Successfully ===")


if __name__ == "__main__":
    main()
