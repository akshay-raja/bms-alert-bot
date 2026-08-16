import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
BMS_URL = "https://in.bookmyshow.com/movies/chennai/vishwanath-and-sons/buytickets/ET00489815/20260816"
TARGET_THEATERS = ["PVR", "INOX", "Sathyam", "SPI", "Palazzo", "AGS", "Luxe"]
NTFY_TOPIC = "akshay-bms-alert-160826"
# =======================================================


def send_ntfy_alert(matched_theaters, url):
    """Sends push notification to your phone via ntfy.sh"""
    theater_list_str = "\n".join([f"- {t}" for t in matched_theaters])
    message = f"Bookings opened for:\n{theater_list_str}\n\nTap to open BookMyShow immediately."

    headers = {
        "Title": "BMS Tickets Live!",
        "Priority": "urgent",
        "Tags": "ticket,movie_camera",
        "Click": url,
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


def scan_bookmyshow(playwright, target_theaters, url):
    """Renders page via headless Chromium and searches for target venues."""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    print(f"[{time.strftime('%X')}] Scanning BookMyShow...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(3500)

        soup = BeautifulSoup(page.content(), "html.parser")
        venue_elements = soup.find_all("a", href=lambda h: h and "/cinemas/" in h)

        found_theaters = set()
        for el in venue_elements:
            name = el.get_text(strip=True)
            if name:
                found_theaters.add(name)

        if not found_theaters:
            for item in soup.find_all(["li", "div"], class_=lambda c: c and ("listing" in c.lower() or "venue" in c.lower())):
                text = item.get_text(strip=True)
                if text:
                    found_theaters.add(text)

        matched = []
        for venue in found_theaters:
            for target in target_theaters:
                if target.lower() in venue.lower():
                    matched.append(venue)
                    break

        return list(set(matched))

    except Exception as exc:
        print(f"[!] Page scan encountered an error: {exc}")
        return []
    finally:
        browser.close()


def main():
    print("=== Scanning BookMyShow ===")
    with sync_playwright() as playwright:
        matches = scan_bookmyshow(playwright, TARGET_THEATERS, BMS_URL)
        if matches:
            print(f"[🎉 MATCH FOUND]: {matches}")
            send_ntfy_alert(matches, BMS_URL)
        else:
            print("[-] No target bookings open yet.")


if __name__ == "__main__":
    main()
