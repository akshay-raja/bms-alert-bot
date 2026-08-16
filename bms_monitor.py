import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
CONFIG_API_URL = "https://api.npoint.io/803e335a91e27ade1511"

DEFAULT_TRACKERS = [
    {
        "id": "default-1",
        "user_name": "Akshay",
        "movie_url": "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815",
        "theaters": ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"],
        "ntfy_topic": "akshay-bms-alert-160826",
        "days_ahead": 3
    }
]

def load_trackers():
    """Fetches all active movie trackers from npoint."""
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
    """Generates target dates starting from today."""
    dates = []
    base_date = datetime.now()
    for i in range(int(days_ahead) + 1):
        target_day = base_date + timedelta(days=i)
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def send_ntfy_alert(topic, movie_url, user_name, results_by_date):
    """Dispatches push notification directly to the specific friend's ntfy topic."""
    message_lines = [f"Hi {user_name}, bookings opened for:"]
    first_direct_url = None
    
    for date_str, theaters, url in results_by_date:
        if not first_direct_url:
            first_direct_url = url
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        message_lines.append(f"\n[{formatted_date}]:")
        for t in theaters:
            message_lines.append(f"  - {t}")
            
    message_lines.append(f"\nTap to open BookMyShow.")
    message = "\n".join(message_lines)

    headers = {
        "Title": f"BMS Live: {user_name}'s Movie!",
        "Priority": "urgent",
        "Tags": "ticket,movie_camera",
        "Click": first_direct_url or movie_url,
    }

    try:
        response = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if response.status_code == 200:
            print(f"  [✓] Push alert sent to '{topic}'!")
        else:
            print(f"  [!] ntfy delivery failed for '{topic}': {response.status_code}")
    except Exception as exc:
        print(f"  [!] Exception delivering ntfy alert: {exc}")


def scan_date(page, target_theaters, full_url):
    """Scans a specific date URL for venue matches."""
    try:
        page.goto(full_url, wait_until="networkidle", timeout=35000)
        page.wait_for_timeout(3000)

        soup = BeautifulSoup(page.content(), "html.parser")
        found_theaters = set()

        for el in soup.find_all("a", href=lambda h: h and ("/cinemas/" in h or "/buytickets/" in h)):
            text = el.get_text(strip=True)
            if text and len(text) > 3:
                found_theaters.add(text)

        if not found_theaters:
            for item in soup.find_all(["li", "div", "span"], class_=lambda c: c and ("venue" in c.lower() or "cinema" in c.lower())):
                text = item.get_text(strip=True)
                if text and len(text) > 3:
                    found_theaters.add(text)

        matched = []
        for venue in found_theaters:
            for target in target_theaters:
                if target.lower() in venue.lower():
                    matched.append(venue)
                    break

        return list(set(matched))
    except Exception as exc:
        print(f"    [!] Error scanning {full_url}: {exc}")
        return []


def process_tracker(page, tracker):
    user_name = tracker.get("user_name", "Friend")
    movie_url = tracker.get("movie_url", "").strip()
    theaters = tracker.get("theaters", [])
    topic = tracker.get("ntfy_topic", "").strip()
    days_ahead = tracker.get("days_ahead", 3)

    if not movie_url or not topic or not theaters:
        print(f"[-] Skipping invalid tracker ({user_name})")
        return

    print(f"\n--- Processing: {user_name} (Topic: {topic}) ---")
    print(f"Movie: {movie_url}")
    print(f"Watching for: {', '.join(theaters)}")

    target_dates = get_target_dates(days_ahead)
    matched_results = []

    for date_str in target_dates:
        url = f"{movie_url.rstrip('/')}/{date_str}"
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        print(f"  [{time.strftime('%X')}] Checking {formatted_date}...")

        matches = scan_date(page, theaters, url)
        if matches:
            print(f"    -> [MATCH FOUND] {len(matches)} theater(s): {matches}")
            matched_results.append((date_str, matches, url))
        else:
            print("    -> No target theaters open.")

    if matched_results:
        print(f"  [🎉] Sending notification to {user_name}...")
        send_ntfy_alert(topic, movie_url, user_name, matched_results)


def main():
    trackers = load_trackers()
    print(f"=== Starting Multi-User BMS Scanner ===")
    print(f"Active Trackers Found: {len(trackers)}")

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

    print("\n=== All Tracker Scans Finished ===")


if __name__ == "__main__":
    main()
