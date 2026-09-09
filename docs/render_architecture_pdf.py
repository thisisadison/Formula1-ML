"""Render docs/architecture.html to docs/Formula1-ML-Architecture.pdf.

Chromium's print engine, not a PDF library: the document is written as HTML
with CSS paged-media rules (@page, page-break-inside), so the thing that
lays out the pages has to be a browser.

    pip install playwright && playwright install chromium
    python docs/render_architecture_pdf.py

Edit architecture.html and re-run this -- the PDF is a build artifact, and
regenerating it is the only supported way to change it.
"""

import os

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "architecture.html")
OUT = os.path.join(HERE, "Formula1-ML-Architecture.pdf")

# Escape hatch for machines that already have a Chromium and no Playwright
# download (CI images, sandboxes where the pinned build doesn't match).
CHROMIUM = os.environ.get("CHROMIUM_PATH")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        **({"executable_path": CHROMIUM} if CHROMIUM else {}))
    page = browser.new_page()
    page.goto(f"file://{SRC}")
    page.wait_for_timeout(300)   # let webfonts/layout settle before printing
    page.pdf(
        path=OUT,
        format="A4",
        print_background=True,
        # The document sets its own margins via @page; zero here so they
        # aren't applied twice.
        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
    )
    browser.close()

print("wrote", OUT)
