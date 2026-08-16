import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
CONFIG_API_URL = "https://api.npoint.io/803e335a91e27ade1511"

DEFAULT_CONFIG = {
    "movie_url": "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815",
    "theaters": ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"],
    "ntfy_topic": "akshay-bms-alert-160826",
    "days_ahead": 3
}

def load_remote_config():
    """Fetches configuration from npoint; falls back to defaults on failure."""
    try:
        res = requests.get(CONFIG_API_URL, timeout=10)
        if res.status_code == 200:
            return {**DEFAULT_CONFIG, **res.json()}
    except Exception as e:
        print(f"[!] Warning: Remote config fetch failed ({e}). Using fallback defaults.")
    return DEFAULT_CONFIG

CONFIG = load_remote_config()
BASE_MOVIE_URL = CONFIG.get("movie_url", DEFAULT_CONFIG["movie_url"])
TARGET_THEATERS = CONFIG.get("theaters", DEFAULT_CONFIG["theaters"])
NTFY_TOPIC = CONFIG.get("ntfy_topic", DEFAULT_CONFIG["ntfy_topic"])
DAYS_AHEAD = int(CONFIG.get("days_ahead", 3))
# =======================================================


def get_target_dates():
    """Generates YYYYMMDD date strings starting today through days_ahead."""
    dates = []
    base_date = datetime.now()
    for i in range(DAYS_AHEAD + 1):
        target_day = base_date + timedelta(days=i)
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def send_ntfy_alert(results_by_date):
    """Dispatches a push notification with direct link to ntfy."""
    message_lines = ["Bookings opened for:"]
    first_direct_url = None
    
    for date_str, theaters, url in results_by_date:
        if not first_direct_url:
            first_direct_url = url
        formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
        message_lines.append(f"\n[{formatted_date}]:")
        for t in theaters:
            message_lines.append(f"  - {t}")
            
    message_lines.append(f"\nTap notification to open BookMyShow.")
    message = "\n".join(message_lines)

    headers = {
        "Title": "BMS Tickets Live!",
        "Priority": "urgent",
        "Tags": "ticket,movie_camera",
        "Click": first_direct_url or BASE_MOVIE_URL,
    }

    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if response.status_code == 200:
            print("[✓] Push alert delivered to ntfy successfully!")
        else:
            print(f"[!] ntfy request failed with status: {response.status_code}")
    except Exception as exc:
        print(f"[!] Failed to send notification: {exc}")


def scan_date(page, target_theaters, full_url):
    """Scans a single movie date page for target cinema listings."""
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


def main():
    target_dates = get_target_dates()
    print(f"=== Starting BookMyShow Scan ===")
    print(f"Target URL: {BASE_MOVIE_URL}")
    print(f"Dates to scan: {', '.join(target_dates)}")
    print(f"Tracking theaters: {', '.join(TARGET_THEATERS)}")
    print(f"ntfy topic: {NTFY_TOPIC}\n")

    matched_results = []
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

        for date_str in target_dates:
            url = f"{BASE_MOVIE_URL.rstrip('/')}/{date_str}"
            formatted_date = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b (%a)")
            print(f"[{time.strftime('%X')}] Checking {formatted_date}...")

            matches = scan_date(page, TARGET_THEATERS, url)
            if matches:
                print(f"  -> [MATCH FOUND] {len(matches)} theater(s): {matches}")
                matched_results.append((date_str, matches, url))
            else:
                print(f"  -> No target theaters open.")

        browser.close()

    if matched_results:
        print(f"\n[🎉] Delivering push alert...")
        send_ntfy_alert(matched_results)
    else:
        print("\n[-] No matches found across requested dates.")


if __name__ == "__main__":
    main()
