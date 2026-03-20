"""
Amazon.in Price Tracker
-----------------------
Tracks product prices and sends Gmail alerts when they drop below your target.

Usage:
    python tracker.py                  # Run once (check prices now)
    python tracker.py --watch          # Watch mode: checks every N hours
    python tracker.py --add            # Interactive: add a new product
    python tracker.py --list           # List all tracked products
    python tracker.py --remove <id>    # Remove a product by ID
"""

import json
import os
import re
import smtplib
import sys
import time
import argparse
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_FILE   = BASE_DIR / "products.json"
LOG_FILE    = BASE_DIR / "tracker.log"

# ── Default config ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "gmail_sender":   "",
    "gmail_password": "",
    "notify_email":   "",
    "check_interval_hours": 6,
    "request_delay_seconds": 3
}

# ── Rotate User-Agents to reduce Amazon blocks ───────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def get_headers() -> dict:
    import random
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


# ════════════════════════════════════════════════════════════════════════════
# Config & data helpers
# ════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    # Environment variables take priority (used in GitHub Actions)
    env_sender   = os.environ.get("GMAIL_SENDER", "")
    env_password = os.environ.get("GMAIL_PASSWORD", "")
    env_notify   = os.environ.get("NOTIFY_EMAIL", "")

    if env_sender and env_password and env_notify:
        return {
            "gmail_sender":           env_sender,
            "gmail_password":         env_password,
            "notify_email":           env_notify,
            "check_interval_hours":   6,
            "request_delay_seconds":  3,
        }

    # Fallback: read from local config.json
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
        print(f"[setup] Created config file: {CONFIG_FILE}")
        print("        Edit it to add your Gmail credentials before running.\n")
    return load_json(CONFIG_FILE)


def load_products() -> list:
    if not DATA_FILE.exists():
        save_json(DATA_FILE, [])
    return load_json(DATA_FILE)


def save_products(products: list):
    save_json(DATA_FILE, products)


def load_json(path: Path) -> any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ════════════════════════════════════════════════════════════════════════════
# Amazon scraping  (retries + better selectors)
# ════════════════════════════════════════════════════════════════════════════

def clean_url(url: str) -> str:
    match = re.search(r"(https://www\.amazon\.in/[^/]+/dp/[A-Z0-9]{10})", url)
    if match:
        return match.group(1)
    match = re.search(r"(https://www\.amazon\.in/dp/[A-Z0-9]{10})", url)
    if match:
        return match.group(1)
    return url.split("?")[0]


def parse_price(soup: BeautifulSoup) -> int | None:
    """Try every known Amazon price selector and return an integer price."""

    # Selector list — ordered from most to least reliable
    price_selectors = [
        {"class": "a-price-whole"},
        {"id": "priceblock_ourprice"},
        {"id": "priceblock_dealprice"},
        {"id": "priceblock_saleprice"},
        {"class": "a-offscreen"},
        {"id": "corePrice_feature_div"},
    ]

    for sel in price_selectors:
        tag = soup.find("span", sel)
        if not tag:
            continue
        raw = (tag.get_text(strip=True)
               .replace(",", "")
               .replace("₹", "")
               .replace("\u20b9", "")
               .strip()
               .rstrip("."))
        digits = re.sub(r"[^\d]", "", raw.split(".")[0])
        if digits:
            return int(digits)

    # Last resort: scan ALL span text for a ₹ pattern
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        m = re.search(r"[₹\u20b9]\s*([\d,]+)", text)
        if m:
            digits = m.group(1).replace(",", "")
            if digits and len(digits) >= 2:
                return int(digits)

    return None


def fetch_product(url: str, retries: int = 3) -> dict | None:
    """Scrape title and price with retries and random UA rotation."""

    for attempt in range(1, retries + 1):
        try:
            session = requests.Session()
            resp = session.get(url, headers=get_headers(), timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            log(f"  [error] Attempt {attempt}/{retries} — network error: {e}")
            time.sleep(5 * attempt)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Detect if Amazon served a captcha/robot-check page
        page_text = soup.get_text().lower()
        if "enter the characters you see below" in page_text or "robot check" in page_text:
            log(f"  [warn] Attempt {attempt}/{retries} — Amazon served a captcha page. Retrying…")
            time.sleep(10 * attempt)
            continue

        # Title
        title_tag = soup.find("span", id="productTitle") or soup.find("h1", id="title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Product"

        price = parse_price(soup)

        if price is None:
            log(f"  [warn] Attempt {attempt}/{retries} — price not found on page (title: {title[:50]}). Retrying…")
            time.sleep(5 * attempt)
            continue

        # Success
        return {"title": title, "price": price}

    log(f"  [error] All {retries} attempts failed for: {url}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Gmail notification
# ════════════════════════════════════════════════════════════════════════════

def send_alert(config: dict, product: dict, current_price: int):
    sender    = config["gmail_sender"]
    password  = config["gmail_password"]
    recipient = config["notify_email"]

    if not all([sender, password, recipient]):
        log("  [skip] Gmail credentials not set — skipping email.")
        return

    subject = f"🔔 Price Drop Alert: {product['title'][:60]}"

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
      <h2 style="color:#e47911;">🛒 Amazon.in Price Drop Alert</h2>
      <p><b>{product['title']}</b></p>
      <table style="border-collapse:collapse;width:100%;">
        <tr>
          <td style="padding:8px;background:#f5f5f5;"><b>Current Price</b></td>
          <td style="padding:8px;color:#B12704;font-size:1.4em;"><b>₹{current_price:,}</b></td>
        </tr>
        <tr>
          <td style="padding:8px;"><b>Your Target</b></td>
          <td style="padding:8px;">₹{product['target_price']:,}</td>
        </tr>
        <tr>
          <td style="padding:8px;background:#f5f5f5;"><b>Below Target By</b></td>
          <td style="padding:8px;color:#007600;">₹{product['target_price'] - current_price:,}</td>
        </tr>
      </table>
      <br>
      <a href="{product['url']}"
         style="background:#e47911;color:white;padding:12px 24px;
                text-decoration:none;border-radius:4px;display:inline-block;">
        View on Amazon.in
      </a>
      <p style="color:#888;font-size:0.85em;margin-top:24px;">
        Checked at {datetime.now().strftime("%d %b %Y, %I:%M %p")}
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        log(f"  [email] ✅ Alert sent to {recipient}")
    except smtplib.SMTPException as e:
        log(f"  [error] Email failed: {e}")


# ════════════════════════════════════════════════════════════════════════════
# Core check logic
# ════════════════════════════════════════════════════════════════════════════

def check_all(config: dict, products: list) -> list:
    if not products:
        print("No products tracked yet. Run with --add to add one.")
        return products

    delay = config.get("request_delay_seconds", 3)
    log(f"Checking {len(products)} product(s)…")

    for product in products:
        log(f"  → {product['title'][:70]}")
        info = fetch_product(product["url"])

        if info is None:
            log("    Skipping — could not fetch price after retries.")
            continue

        current_price = info["price"]
        last_alerted  = product.get("last_alerted_price")

        # Update history
        entry = {"date": datetime.now().isoformat(), "price": current_price}
        product.setdefault("history", []).append(entry)
        product["last_checked"] = entry["date"]
        product["last_price"]   = current_price

        log(f"    Current: ₹{current_price:,}  |  Target: ₹{product['target_price']:,}  |  Last alerted at: {'₹' + f'{last_alerted:,}' if last_alerted else 'never'}")

        if current_price <= product["target_price"]:
            # Alert only if we haven't alerted yet, OR price has dropped further
            if last_alerted is None or current_price < last_alerted:
                log(f"    ✅ Price at/below target — sending alert…")
                send_alert(config, product, current_price)
                product["last_alerted_price"] = current_price
            else:
                log(f"    ℹ️  Still below target but no further drop since last alert (₹{last_alerted:,}) — skipping.")
        else:
            diff = current_price - product["target_price"]
            log(f"    ⏳ ₹{diff:,} above target — no alert.")
            # Reset last_alerted if price has gone back above target
            if last_alerted is not None:
                log(f"    ↩️  Price recovered above target — resetting alert state.")
                product["last_alerted_price"] = None

        time.sleep(delay)

    save_products(products)
    return products


# ════════════════════════════════════════════════════════════════════════════
# CLI helpers
# ════════════════════════════════════════════════════════════════════════════

def add_product_interactive(products: list):
    print("\n── Add a new product ──────────────────────────────")
    url = input("Amazon.in product URL: ").strip()
    if not url:
        print("No URL entered. Aborting.")
        return

    url = clean_url(url)
    print(f"Cleaned URL: {url}")
    print("Fetching product info…")

    info = fetch_product(url)
    if info is None:
        print("Could not fetch product. Check the URL and try again.")
        return

    print(f"  Title : {info['title']}")
    if info["price"]:
        print(f"  Price : ₹{info['price']:,}")
    else:
        print("  Price : (could not detect)")

    try:
        target = int(input("Target price (₹): ").strip())
    except ValueError:
        print("Invalid price. Aborting.")
        return

    new_id = max((p.get("id", 0) for p in products), default=0) + 1
    product = {
        "id":                  new_id,
        "url":                 url,
        "title":               info["title"],
        "target_price":        target,
        "last_price":          info["price"],
        "last_alerted_price":  None,
        "last_checked":        datetime.now().isoformat(),
        "history":             [{"date": datetime.now().isoformat(), "price": info["price"]}]
                               if info["price"] else [],
    }
    products.append(product)
    save_products(products)
    print(f"\n✅ Added! (ID {new_id}) Tracking «{info['title'][:60]}» @ target ₹{target:,}\n")


def list_products(products: list):
    if not products:
        print("No products tracked yet. Use --add to add one.")
        return
    print(f"\n{'ID':<4} {'Target':>10} {'Last':>10}  Title")
    print("─" * 72)
    for p in products:
        last = f"₹{p['last_price']:,}" if p.get("last_price") else "—"
        print(f"{p['id']:<4} ₹{p['target_price']:>8,} {last:>10}  {p['title'][:48]}")
    print()


def remove_product(products: list, pid: int) -> list:
    before = len(products)
    products = [p for p in products if p["id"] != pid]
    if len(products) < before:
        save_products(products)
        print(f"Removed product ID {pid}.")
    else:
        print(f"No product with ID {pid} found.")
    return products


def watch_mode(config: dict, products: list):
    interval = config.get("check_interval_hours", 6)
    print(f"\n🔄 Watch mode — checking every {interval} hour(s). Ctrl+C to stop.\n")
    while True:
        products = load_products()
        check_all(config, products)
        log(f"Next check in {interval}h…")
        time.sleep(interval * 3600)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Amazon.in Price Tracker")
    parser.add_argument("--add",    action="store_true", help="Add a new product interactively")
    parser.add_argument("--list",   action="store_true", help="List all tracked products")
    parser.add_argument("--remove", type=int, metavar="ID", help="Remove a product by ID")
    parser.add_argument("--watch",  action="store_true", help="Run continuously on interval")
    args = parser.parse_args()

    config   = load_config()
    products = load_products()

    if args.add:
        add_product_interactive(products)
    elif args.list:
        list_products(products)
    elif args.remove:
        products = remove_product(products, args.remove)
    elif args.watch:
        watch_mode(config, products)
    else:
        check_all(config, products)


if __name__ == "__main__":
    main()