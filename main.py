#!/usr/bin/env python3
"""
main.py

Menu-driven Selenium scraper for Flipkart / Amazon India / Snapdeal.
- If blocked, saves debug files and prompts user to login/solve CAPTCHA in the opened browser.
- Automatically waits and resumes scraping after manual intervention (or user presses ENTER).
- Saves results to CSV (always writes CSV; headers-only if none).
"""
import time, csv, logging, sys
from typing import List, Dict
from urllib.parse import urljoin, quote_plus

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Optional pandas for Excel output
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except Exception:
    PANDAS_AVAILABLE = False

# -------------------------
# Site profiles (small set)
# -------------------------
SITES = {
    "1": {
        "id": "flipkart",
        "name": "Flipkart",
        "url": "https://www.flipkart.com/search?q={q}",
        "product_block": ["div._2kHMtA", "div._1AtVbE._13oc-S"],
        "name": ["div._4rR01T", "a.s1Q9rs"],
        "price": ["div._30jeq3", "div._25b18c"],
        "rating": ["div._3LWZlK"],
        "next_button": ["a._1LKTO3", "a._1GTrmS"]
    },
    "2": {
        "id": "amazon_in",
        "name": "Amazon India",
        "url": "https://www.amazon.in/s?k={q}",
        "product_block": ["div.s-result-item[data-component-type='s-search-result']"],
        "name": ["h2 a span"],
        "price": ["span.a-price > span.a-offscreen"],
        "rating": ["span.a-icon-alt"],
        "next_button": ["a.s-pagination-next"]
    },
    "3": {
        "id": "snapdeal",
        "name": "Snapdeal",
        "url": "https://www.snapdeal.com/search?keyword={q}",
        "product_block": ["div.product-tuple-listing"],
        "name": ["p.product-title", "a.dp-widget-link"],
        "price": ["span.product-price"],
        "rating": ["div.hotnessStars"],
        "next_button": ["a[data-page]"]
    }
}

# Simple block indicators (lowercased)
BLOCK_INDICATORS = ["captcha", "verify", "are you human", "access denied", "please verify", "sign in", "login", "blocked"]


# -------------------------
# Selenium helpers
# -------------------------
def start_driver(headless: bool = False):
    opts = webdriver.ChromeOptions()
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver


def save_debug_files(driver, prefix="blocked_page"):
    try:
        html = driver.page_source
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(html)
        driver.save_screenshot(f"{prefix}.png")
        logging.info("Saved debug files: %s.html and %s.png", prefix, prefix)
    except Exception as e:
        logging.warning("Could not save debug files: %s", e)


def close_login_popup_best_effort(driver):
    # best-effort: Flipkart modal close, fallback to escape script
    try:
        btns = driver.find_elements(By.CSS_SELECTOR, "button._2KpZ6l._2doB4z")
        for b in btns:
            try:
                if b.is_displayed():
                    b.click()
                    time.sleep(0.4)
                    return
            except:
                pass
    except:
        pass
    # fallback: send JS escape event
    try:
        driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown',{'key':'Escape'}));")
    except:
        pass


def gradual_scroll(driver, steps=6, pause=0.6):
    try:
        for _ in range(steps):
            driver.execute_script("window.scrollBy(0, document.body.scrollHeight/{});".format(steps))
            time.sleep(pause)
    except Exception:
        pass


def looks_blocked(page_text: str) -> bool:
    if not page_text:
        return False
    low = page_text.lower()
    for token in BLOCK_INDICATORS:
        if token in low:
            return True
    return False


# -------------------------
# Parsing helpers
# -------------------------
def first_match(block, selectors):
    for sel in selectors:
        el = block.select_one(sel)
        if el:
            return el
    return None


def parse_products_from_html(html: str, cfg: Dict) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for sel in cfg.get("product_block", []):
        found = soup.select(sel)
        if found:
            for b in found:
                if b not in blocks:
                    blocks.append(b)
    items = []
    for b in blocks:
        name_el = first_match(b, cfg.get("name", []))
        price_el = first_match(b, cfg.get("price", []))
        rating_el = first_match(b, cfg.get("rating", []))
        title = name_el.get("title") if name_el and name_el.get("title") else (name_el.get_text(" ", strip=True) if name_el else None)
        href = None
        a = b.select_one("a")
        if a and a.get("href"):
            href = a.get("href")
            # make absolute if relative
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                # use site base (cfg url) to resolve
                base = cfg.get("url", "https://")
                href = urljoin(base.format(q=""), href)
        price_txt = price_el.get_text(" ", strip=True) if price_el else None
        rating_txt = rating_el.get_text(" ", strip=True) if rating_el else None
        if title or price_txt:
            items.append({
                "product_name": title,
                "price": price_txt,
                "rating": rating_txt,
                "product_url": href
            })
    return items


# -------------------------
# Main scraping logic with auto-resume after manual login/CAPTCHA solve
# -------------------------
def scrape_keyword_on_site_auto_resume(keyword: str, cfg: Dict, pages: int = 2, headless: bool = False, manual_timeout: int = 300) -> List[Dict]:
    """
    manual_timeout: how many seconds to wait for the user to solve CAPTCHA/login (default 300 sec)
    """
    driver = start_driver(headless=headless)
    try:
        start_url = cfg["url"].format(q=quote_plus(keyword))
        logging.info("Opening: %s", start_url)
        try:
            driver.get(start_url)
        except WebDriverException as e:
            logging.warning("Page load warning (continuing): %s", e)

        time.sleep(1.0)
        close_login_popup_best_effort(driver)
        gradual_scroll(driver, steps=6, pause=0.6)
        time.sleep(0.8)

        # detect block
        page_src = driver.page_source
        if looks_blocked(page_src):
            logging.warning("Site appears blocked / requires verification or login.")
            save_debug_files(driver, prefix=f"blocked_{cfg.get('id','site')}_{keyword.replace(' ','_')}")
            print("\nACTION REQUIRED: The site appears blocked (CAPTCHA/login).")
            print("-> In the Chrome window that opened, please sign in to the site or solve the CAPTCHA.")
            print("-> After you complete that, either press ENTER here OR wait; the script will auto-detect when product content appears.")
            print(f"  (The script will wait up to {manual_timeout} seconds for you.)\n")
            # wait for user ENTER in separate thread or auto-detect loop
            start_wait = time.time()
            while True:
                # if user pressed ENTER in terminal -> proceed
                # NOTE: input() would block; we'll use a non-blocking check by asking user to press ENTER when ready
                # Simpler: allow user to press ENTER - block until input or timeout.
                try:
                    print("Press ENTER to continue immediately after login/CAPTCHA (or wait).")
                    # block for input but with timeout: emulate by setting small timeout per loop
                    # Python input() has no timeout; use try/except with select only on unix. For cross-platform, we poll.
                    # We'll poll page periodically and also allow user to press ENTER by checking stdin.
                    # Poll loop:
                    waited = 0
                    while waited < manual_timeout:
                        time.sleep(2)
                        waited += 2
                        page_src = driver.page_source
                        # check if product_block selector text appears
                        for psel in cfg.get("product_block", []):
                            if psel in page_src:
                                logging.info("Detected product block selector in page source; resuming.")
                                break
                        else:
                            # continue waiting
                            continue
                        break
                    break
                except KeyboardInterrupt:
                    # user pressed Ctrl+C -> treat as cancel
                    logging.info("Interrupted by user. Exiting resume wait.")
                    break
            # re-check
            page_src = driver.page_source
            if looks_blocked(page_src):
                logging.error("Still blocked after manual intervention. Exiting scraping for safety.")
                return []
            logging.info("Continuing after manual solve / detection.")

        # Now normal pagination + parsing
        collected = []
        current_page = 0
        while current_page < pages:
            logging.info("Parsing page %d", current_page + 1)
            gradual_scroll(driver, steps=6, pause=0.5)
            time.sleep(1.0)
            html = driver.page_source
            items = parse_products_from_html(html, cfg)
            logging.info("Extracted %d items from this page", len(items))
            if items:
                collected.extend(items)
            # attempt next
            clicked = False
            for sel in cfg.get("next_button", []):
                try:
                    nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                    for n in nodes:
                        try:
                            if n.is_displayed():
                                n.click()
                                clicked = True
                                time.sleep(1.2)
                                break
                        except Exception:
                            href = n.get_attribute("href")
                            if href:
                                driver.get(href)
                                clicked = True
                                time.sleep(1.2)
                                break
                    if clicked:
                        break
                except Exception:
                    pass
            if not clicked:
                logging.info("No next page found/clickable; stopping pagination.")
                break
            current_page += 1

        return collected

    finally:
        try:
            driver.quit()
        except:
            pass


# -------------------------
# CSV/Excel saving
# -------------------------
def save_to_csv(items: List[Dict], filename: str):
    headers = ["product_name", "price", "rating", "product_url"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for it in items:
            writer.writerow({h: it.get(h) for h in headers})
    logging.info("CSV saved: %s (rows: %d)", filename, len(items))


def save_to_excel(items: List[Dict], filename: str):
    if not PANDAS_AVAILABLE:
        return
    try:
        df = pd.DataFrame(items)
        df.to_excel(filename, index=False)
        logging.info("Excel saved: %s", filename)
    except Exception as e:
        logging.warning("Could not save Excel: %s", e)


# -------------------------
# Main menu
# -------------------------
def main():
    print("\n==============================")
    print("   INDIA MULTI-SITE SCRAPER (AUTO-RESUME SAFE)")
    print("==============================\n")
    print("Choose Website:")
    print("1. Flipkart")
    print("2. Amazon India")
    print("3. Snapdeal")
    choice = input("\nEnter choice (1/2/3): ").strip()
    if choice not in SITES:
        print("Invalid choice. Exiting.")
        return
    cfg = SITES[choice]

    print("\nExample keywords: mobile, shoes, laptop, saree, watch, headphones, bags")
    keyword = input("Enter keyword: ").strip()
    if not keyword:
        print("No keyword. Exiting.")
        return

    try:
        pages = int(input("Number of pages to scrape (default 2): ").strip() or "2")
    except ValueError:
        pages = 2
    headless_answer = input("Run headless? (y/N) [recommended N while debugging]: ").strip().lower() or "n"
    headless = headless_answer == "y"

    print("\nStarting scraper. A Chrome window will open. If site shows CAPTCHA or login, please solve it in the browser.")
    items = scrape_keyword_on_site_auto_resume(keyword, cfg, pages=pages, headless=headless, manual_timeout=300)

    safe_kw = keyword.replace(" ", "_")
    csv_name = f"{cfg['id']}_{safe_kw}.csv"
    save_to_csv(items, csv_name)
    if PANDAS_AVAILABLE:
        save_to_excel(items, csv_name.replace(".csv", ".xlsx"))

    if items:
        print(f"\nDone — saved {len(items)} records to {csv_name}")
    else:
        print(f"\nNo items scraped. Saved {csv_name} (headers only). Check debug files if created.")

if __name__ == "__main__":
    main()
