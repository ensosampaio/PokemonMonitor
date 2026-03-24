# 🎴 LigaPokemon Price Monitor

Monitors Pokemon TCG card prices on **LigaPokemon.com.br** and sends a **Discord alert** whenever a tracked card's lowest price changes by more than your configured threshold.

---

## 📦 Requirements

- Python 3.10+
- A Discord server where you can create a webhook

---

## ⚙️ Installation

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 🤖 Discord Webhook Setup

1. Open Discord and go to the channel where you want alerts
2. Click the **⚙️ gear icon** next to the channel → **Integrations** → **Webhooks**
3. Click **New Webhook**, give it a name (e.g. "Pokemon Monitor"), then **Copy Webhook URL**
4. Paste it into `config.json`

---

## ▶️ Usage

```bash
python monitor.py                                    # run the price check
python monitor.py --add "Charizard ex (223/165)"     # start tracking a card
python monitor.py --remove "Charizard ex (223/165)"  # stop tracking a card
python monitor.py --list                             # show tracked cards + prices
python monitor.py --test                             # send a test Discord message
python monitor.py --debug "Charizard ex (223/165)"   # diagnose price extraction
python monitor.py --reset                            # wipe stored prices
```

> ⚠️ **Card names must include the set number**, e.g. `"Mega Emboar ex (273/217)"` or `"Charizard ex (223/165)"`.
> This is how LigaPokemon identifies specific cards. Without the set number, the search may return no results.

The **first run** only records baseline prices — alerts start from the second run onward.

---

## 🔔 Alert Types

| Alert | Color | Trigger |
|-------|-------|---------|
| 🚨 Preço Subiu | 🔴 Red | Price rose by R$ 5+ |
| 💸 Preço Caiu | 🟢 Green | Price dropped by R$ 5+ |

---

## ⚠️ First-run selector check

Since LigaPokemon uses the same platform as LigaMagic, the scraper should work out of the box. If a card returns `None`, run:

```bash
python monitor.py --debug "Card Name"
```

And share the output to get the selector fixed.

---

## ⏰ Scheduling

### Windows — Task Scheduler
- Program: `python`
- Arguments: `C:\path\to\pokemon_monitor\monitor.py`
- Repeat every **12 hours**

### Linux / Mac — Cron
```
0 */12 * * * cd /path/to/pokemon_monitor && python monitor.py
```
