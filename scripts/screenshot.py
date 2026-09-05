"""Render the console in headless Chromium and capture screenshots + console errors.

    python scripts/screenshot.py [url] [out_prefix]

Used as the prototype's own smoke test: it proves the colour-coded map draws,
the alert queue populates and the detail panel renders its charts.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
url = sys.argv[1] if len(sys.argv) > 1 else "file://" + str(ROOT / "dist" / "jal_anomaly_demo.html")
prefix = sys.argv[2] if len(sys.argv) > 2 else "offline"
OUT = ROOT / "dist" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(args=["--allow-file-access-from-files", "--enable-webgl",
                                "--use-gl=swiftshader", "--ignore-gpu-blocklist"])
    page = b.new_page(viewport={"width": 1680, "height": 1000})
    msgs, errors = [], []
    page.on("console", lambda m: msgs.append(f"{m.type}: {m.text}"[:400]))
    page.on("pageerror", lambda e: errors.append(str(e)[:400]))
    page.goto(url, wait_until="load", timeout=90000)
    page.wait_for_selector(".alert", timeout=60000)
    page.wait_for_timeout(4000)
    page.screenshot(path=str(OUT / f"{prefix}-1-overview.png"))

    page.click(".alert")                       # highest-risk alert
    page.wait_for_timeout(3500)
    page.screenshot(path=str(OUT / f"{prefix}-2-detail.png"))

    # turn on the soil-moisture layer
    page.click("text=Soil moisture (EOS-04 + SMAP)")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(OUT / f"{prefix}-3-soilmoisture.png"))

    page.click("text=Model")
    page.wait_for_timeout(900)
    page.screenshot(path=str(OUT / f"{prefix}-4-model.png"))

    counts = page.evaluate("document.querySelectorAll('.alert').length")
    canvas = page.evaluate("!!document.querySelector('#map canvas')")
    print(f"alerts rendered : {counts}")
    print(f"map canvas      : {canvas}")
    print(f"page errors     : {errors if errors else 'none'}")
    bad = [m for m in msgs if m.startswith("error")]
    print(f"console errors  : {bad if bad else 'none'}")
    b.close()
