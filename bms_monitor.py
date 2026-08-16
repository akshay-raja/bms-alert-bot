import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
# Base movie booking URL without the date at the end
BASE_MOVIE_URL = "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815"

# List of target theater keywords to monitor
TARGET_THEATERS = ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"]

# Your ntfy topic name
NTFY_TOPIC = "akshay-bms-alert-160826"

# Number of future days to scan (0 = today only, 3 = today + next 3 days)
DAYS_AHEAD = 3
# =======================================================


def get_target_dates():
    """Generates YYYYMMDD date strings for today and the next N days."""
    dates = []
    base_date = datetime.now()
    for i in range(DAYS_AHEAD + 1):
        target_day = base_date + timedelta(days=i)
        dates.append(target_day.strftime("%Y%m%d"))
    return dates


def send_ntfy_alert(results_by_date):
    """Sends consolidated push notification grouped by date via ntfy.sh"""
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
            print("[✓] Notification sent to ntfy app successfully!")
        else:
            print(f"[!] ntfy request failed with status: {response.status_code}")
    except Exception as exc:
        print(f"[!] Failed to send push notification: {exc}")


def scan_date(page, target_theaters, full_url):
    """Loads a single date URL and extracts matching theaters."""
    try:
        page.goto(full_url, wait_until="networkidle", timeout=35000)
        page.wait_for_timeout(3000)

        soup = BeautifulSoup(page.content(), "html.parser")
        found_theaters = set()

        # Extract cinema names from venue links or containers
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
            print(f"[{time.strftime('%X')}] Checking {formatted_date} ({url})...")

            matches = scan_date(page, TARGET_THEATERS, url)

            if matches:
                print(f"  -> [MATCH FOUND] {len(matches)} theater(s): {matches}")
                matched_results.append((date_str, matches, url))
            else:
                print(f"  -> No target theaters open.")

        browser.close()

    # Send consolidated alert if any matches were found across any date
    if matched_results:
        print(f"\n[🎉] Delivering push alert for {len(matched_results)} active date(s)...")
        send_ntfy_alert(matched_results)
    else:
        print("\n[-] No matches found across today and the next 3 days.")


if __name__ == "__main__":
    main()
