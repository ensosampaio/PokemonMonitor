"""
LigaPokemon Price Monitor
=======================
Tracks the lowest available price for Pokemon TCG cards on LigaPokemon.com.br
and sends a Discord alert when a card rises or drops by more than the configured threshold.

Usage:
    python monitor.py                         # run the price check
    python monitor.py --add "Mega Emboar ex (273/217)"    # start tracking a card (include set number!)
    python monitor.py --remove "Mega Emboar ex (273/217)" # stop tracking a card
    python monitor.py --list                  # show tracked cards + prices
    python monitor.py --test                  # send a test Discord message
    python monitor.py --debug "Mega Emboar ex (273/217)"  # test price extraction
    python monitor.py --reset                 # wipe stored prices
"""

import asyncio
import sqlite3
import json
import argparse
import logging
import random
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE     = BASE_DIR / "prices.db"
LOG_FILE    = BASE_DIR / "monitor.log"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.json not found next to monitor.py")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                card        TEXT PRIMARY KEY,
                last_price  REAL,
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
    return conn

def get_stored_price(conn: sqlite3.Connection, card: str) -> float | None:
    row = conn.execute("SELECT last_price FROM prices WHERE card = ?", (card,)).fetchone()
    return row[0] if row else None

def save_price(conn: sqlite3.Connection, card: str, price: float) -> None:
    with conn:
        conn.execute("""
            INSERT INTO prices (card, last_price, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(card) DO UPDATE SET
                last_price = excluded.last_price,
                updated_at = excluded.updated_at
        """, (card, price))

# ── Discord ───────────────────────────────────────────────────────────────────
async def send_discord_async(webhook_url: str, card: str, old_price: float,
                             new_price: float, card_url: str) -> bool:
    diff = new_price - old_price
    rose = diff > 0
    embed = {
        "title": f"{'🚨 Preço Subiu' if rose else '💸 Preço Caiu'} — {card}",
        "url":   card_url,
        "color": 0xFF4444 if rose else 0x57F287,
        "fields": [
            {"name": "Preço anterior", "value": f"R$ {old_price:.2f}", "inline": True},
            {"name": "Preço atual",    "value": f"R$ {new_price:.2f}", "inline": True},
            {"name": "Variação",       "value": f"{'⬆️' if rose else '⬇️'} R$ {abs(diff):.2f}", "inline": True},
        ],
        "footer": {"text": "LigaPokemon Price Monitor"},
    }

    def _post():
        return requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)

    try:
        r = await asyncio.to_thread(_post)
        if r.ok:
            log.info(f"Discord alert sent for {card}.")
            return True
        log.warning(f"Discord error {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"Discord request failed: {e}")
    return False

# ── Scraper ───────────────────────────────────────────────────────────────────
def build_url(card_name: str) -> str:
    return f"https://www.ligapokemon.com.br/?view=cards/card&card={quote_plus(card_name)}"

def extract_prices(text: str) -> list[float]:
    """Finds all Brazilian-formatted prices in a string and converts them to floats."""
    matches = re.findall(r"R\$\s*([\d.,]+)", text)
    valid_prices = []
    for m in matches:
        clean = m.replace(".", "").replace(",", ".")
        try:
            val = float(clean)
            if val > 0:
                valid_prices.append(val)
        except ValueError:
            continue
    return valid_prices

async def fetch_lowest_price(page, card_name: str) -> float | None:
    log.info(f"Fetching: {build_url(card_name)}")

    try:
        await page.goto(build_url(card_name), wait_until="domcontentloaded", timeout=20_000)

        # LigaPokemon uses the same platform as LigaMagic.
        # The market summary box (#container-price-mkp-card) pre-computes
        # min/avg/max prices — we just read the minimum directly.
        summary_row = page.locator("#container-price-mkp-card")
        await summary_row.wait_for(state="visible", timeout=10_000)
        row_text = await summary_row.inner_text()
        extracted = extract_prices(row_text)

        if extracted:
            lowest = min(extracted)
            log.info(f"  Lowest price found: R$ {lowest:.2f} (via Market Summary)")
            return lowest

    except PlaywrightTimeout:
        log.warning(f"  Timeout: Market Summary box not found for '{card_name}'.")
        log.warning(f"  Try running: python monitor.py --debug \"{card_name}\"")
    except Exception as e:
        log.warning(f"  Price scan failed: {e}")

    log.error(f"  Could not extract any valid price for '{card_name}'.")
    return None

# ── Core monitor loop ─────────────────────────────────────────────────────────
async def run_monitor():
    cfg         = load_config()
    conn        = init_db()
    webhook_url = cfg["discord"]["webhook_url"]
    min_diff    = float(cfg.get("min_price_increase", 5.0))
    cards       = cfg.get("cards", [])

    if not cards:
        log.warning("No cards tracked yet. Use: python monitor.py --add \"Card Name\"")
        return

    log.info(f"Starting monitor — {len(cards)} card(s), threshold R$ {min_diff:.2f}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def block_resources(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_resources)

        for card in cards:
            current_price = await fetch_lowest_price(page, card)

            if current_price is None:
                log.warning(f"Skipping '{card}' — price unavailable.")
            else:
                stored = get_stored_price(conn, card)
                if stored is None:
                    save_price(conn, card, current_price)
                    log.info(f"  [{card}] First record: R$ {current_price:.2f} (saved)")
                else:
                    diff = current_price - stored
                    log.info(
                        f"  [{card}] Stored: R$ {stored:.2f} | "
                        f"Current: R$ {current_price:.2f} | Diff: R$ {diff:+.2f}"
                    )
                    if abs(diff) >= min_diff:
                        sent = await send_discord_async(webhook_url, card, stored, current_price, build_url(card))
                        if sent:
                            save_price(conn, card, current_price)

            delay = random.uniform(2, 5)
            log.info(f"  Waiting {delay:.1f}s…")
            await asyncio.sleep(delay)

        await browser.close()

    conn.close()
    log.info("Monitor run complete.")

# ── CLI commands ──────────────────────────────────────────────────────────────
def cmd_list() -> None:
    cfg, conn = load_config(), init_db()
    cards = cfg.get("cards", [])
    if not cards:
        print("\n  No cards tracked yet. Use: python monitor.py --add \"Card Name\"\n")
        return

    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT card, last_price, updated_at FROM prices").fetchall()}

    print(f"\n  {'#':<4} {'Card':<40} {'Last Price':>10}  {'Last Checked'}")
    print("  " + "─" * 72)
    for i, card in enumerate(sorted(cards), 1):
        if card in rows:
            print(f"  {i:<4} {card:<40} R$ {rows[card][0]:>7.2f}  {rows[card][1]}")
        else:
            print(f"  {i:<4} {card:<40} {'(not checked yet)':>10}")
    print(f"\n  {len(cards)} card(s) tracked.\n")

def cmd_add(card_name: str) -> None:
    cfg = load_config()
    cards = cfg.setdefault("cards", [])
    if any(c.lower() == card_name.lower() for c in cards):
        print(f"  ⚠️   '{card_name}' is already in your list.")
        return
    cards.append(card_name)
    save_config(cfg)
    print(f"  ✅  '{card_name}' added! Run monitor.py to fetch its price.")

def cmd_remove(card_name: str) -> None:
    cfg = load_config()
    cards = cfg.get("cards", [])
    match = next((c for c in cards if c.lower() == card_name.lower()), None)
    if not match:
        print(f"  ⚠️   '{card_name}' not found. Use --list to see tracked cards.")
        return
    cards.remove(match)
    save_config(cfg)
    with init_db() as conn:
        conn.execute("DELETE FROM prices WHERE card = ?", (match,))
    print(f"  🗑️   '{match}' removed from tracking.")

def cmd_test() -> None:
    cfg, conn = load_config(), init_db()
    webhook_url = cfg["discord"]["webhook_url"]
    cards = cfg.get("cards", [])
    if not cards:
        print("  No cards tracked yet. Use: python monitor.py --add \"Card Name\"")
        return

    stored = {r[0]: r[1] for r in conn.execute("SELECT card, last_price FROM prices").fetchall()}
    print(f"  Sending test message for {len(cards)} card(s)…")

    for card in cards:
        price = stored.get(card)
        price_str = f"R$ {price:.2f}" if price is not None else "not fetched yet"
        embed = {
            "title": "🤖 Teste do Bot — LigaPokemon Monitor",
            "color": 0xFFCB05,  # Pokémon yellow
            "fields": [
                {"name": "Carta", "value": card,      "inline": True},
                {"name": "Preço", "value": price_str, "inline": True},
            ],
            "footer": {"text": "Se você está vendo isso, o bot está funcionando! ✅"},
        }
        try:
            r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
            if r.ok:
                print(f"  ✅  Sent: {card} — {price_str}")
            else:
                print(f"  ❌  Failed for {card}: {r.status_code} {r.text}")
        except Exception as e:
            print(f"  ❌  Error for {card}: {e}")

def cmd_reset() -> None:
    DB_FILE.unlink(missing_ok=True)
    print("  Price database reset.")

async def cmd_debug(card_name: str) -> None:
    """
    Loads the card page and prints what the market summary box contains,
    plus a broader scan so the selector can be fixed if needed.
    """
    print(f"\n  🔍 Debug: {card_name}")
    print(f"  URL: {build_url(card_name)}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def block_resources(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_resources)

        try:
            await page.goto(build_url(card_name), wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  ❌ Page load failed: {e}")
            await browser.close()
            return

        # ── Primary selector (same as LigaMagic) ─────────────────────────
        print("  ── Primary selector ──")
        try:
            el = page.locator("#container-price-mkp-card")
            await el.wait_for(state="visible", timeout=5000)
            text = await el.inner_text()
            prices = extract_prices(text)
            print(f"  #container-price-mkp-card")
            print(f"  text:   {repr(text)}")
            print(f"  prices: {prices}")
            if prices:
                print(f"  → min: R$ {min(prices):.2f}")
        except Exception as e:
            print(f"  #container-price-mkp-card → not found: {e}")

        print()

        # ── Fallback selectors ────────────────────────────────────────────
        print("  ── Fallback selectors ──")
        fallbacks = [
            ".price-mkp .min .price",
            ".price-mkp",
            "div.price",
            "[class*='price']",
            "[class*='preco']",
        ]
        for sel in fallbacks:
            try:
                texts = await page.locator(sel).all_inner_texts()
                prices = []
                for t in texts:
                    prices.extend(extract_prices(t))
                if prices:
                    prices.sort()
                    print(f"  {sel}")
                    print(f"  prices: {prices}  → min: R$ {min(prices):.2f}\n")
                else:
                    print(f"  {sel}  → no prices found\n")
            except Exception as e:
                print(f"  {sel}  → error: {e}\n")

        # ── Parent classes of div.price elements ─────────────────────────
        print("  ── Parent classes of div.price elements ──")
        try:
            all_divs = await page.locator("div.price").all()
            for i, el in enumerate(all_divs[:10]):
                try:
                    t = await el.inner_text()
                    found = extract_prices(t)
                    if not found:
                        continue
                    parent = await el.evaluate("el => el.parentElement?.className || ''")
                    gp     = await el.evaluate("el => el.parentElement?.parentElement?.className || ''")
                    print(f"  [{i+1}] prices={found}  parent={parent!r}  grandparent={gp!r}")
                except Exception:
                    continue
        except Exception as e:
            print(f"  error: {e}")

        await browser.close()
    print("\n  Debug complete.\n")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LigaPokemon Price Monitor",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "examples:\n"
            "  python monitor.py                         # run the price check\n"
            "  python monitor.py --add \"Charizard ex (223/165)\"    # start tracking a card\n"
            "  python monitor.py --remove \"Charizard ex (223/165)\" # stop tracking a card\n"
            "  python monitor.py --list                  # show all tracked cards\n"
            "  python monitor.py --test                  # send a test Discord message\n"
            "  python monitor.py --debug \"Charizard ex (223/165)\"  # diagnose price extraction\n"
            "  python monitor.py --reset                 # wipe stored prices\n"
        )
    )
    parser.add_argument("--add",    metavar="CARD", help="Add a card to tracking")
    parser.add_argument("--remove", metavar="CARD", help="Remove a card from tracking")
    parser.add_argument("--list",   action="store_true", help="Show tracked cards and stored prices")
    parser.add_argument("--test",   action="store_true", help="Send a test Discord message")
    parser.add_argument("--debug",  metavar="CARD",    help="Diagnose price extraction for a card")
    parser.add_argument("--reset",  action="store_true", help="Clear the price database")
    args = parser.parse_args()

    if args.add:       cmd_add(args.add)
    elif args.remove:  cmd_remove(args.remove)
    elif args.list:    cmd_list()
    elif args.test:    cmd_test()
    elif args.debug:   asyncio.run(cmd_debug(args.debug))
    elif args.reset:   cmd_reset()
    else:              asyncio.run(run_monitor())
