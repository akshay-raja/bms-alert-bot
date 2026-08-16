import json
import os
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== DEFAULT CONFIGURATION ====================
DEFAULT_CONFIG = {
    "movie_url": "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815",
    "theaters": ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"],
    "ntfy_topic": "akshay-bms-alert-160826",
    "days_ahead": 3
}

CONFIG_FILE = "config.json"

def load_config():
    """Loads configuration from config.json if present; otherwise uses defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_CONFIG, **data}
        except Exception as e:
            print(f"[!] Warning: Failed to read config.json ({e}). Using defaults.")
    return DEFAULT_CONFIG

CONFIG = load_config()
BASE_MOVIE_URL = CONFIG["movie_url"]
TARGET_THEATERS = CONFIG["theaters"]
NTFY_TOPIC = CONFIG["ntfy_topic"]
DAYS_AHEAD = int(CONFIG.get("days_ahead", 3))
# ===============================================================


def get_target_dates():
    dates = []
    base_date = datetime.now()
    for i in range(DAYS_AHEAD + 1):
        target_day = base_date + timedelta(days=i)
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def send_ntfy_alert(results_by_date):
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
            print("[✓] Notification delivered to ntfy app successfully!")
        else:
            print(f"[!] ntfy request failed with status: {response.status_code}")
    except Exception as exc:
        print(f"[!] Failed to send push notification: {exc}")


def scan_date(page, target_theaters, full_url):
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
    print(f"=== Starting BookMyShow Multi-Date Scan ===")
    print(f"Movie URL: {BASE_MOVIE_URL}")
    print(f"Dates to scan: {', '.join(target_dates)}")
    print(f"Theaters: {', '.join(TARGET_THEATERS)}\n")

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
        print("\n[-] No matches found across today and next days.")


if __name__ == "__main__":
    main()
