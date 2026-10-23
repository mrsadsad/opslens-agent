"""Optional offline browser smoke test: pip install playwright; uses installed Edge."""

from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opslens.providers import DemoProvider
from opslens.server import create_server
from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "runs/team-ui"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        server = create_server([root / "opslens/fixtures/inc001.json"],
                               Path(temporary) / "runs.sqlite3", Path(temporary) / "output", DemoProvider, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}")
                assert page.locator("#policy option").evaluate_all(
                    "options => options.map(option => option.value)") == ["", "react", "investigator", "multi-agent"]
                page.locator("#policy").select_option("multi-agent")
                page.locator("#start").click()
                expect(page.locator("#report-status")).to_have_text("已提交", timeout=15000)
                assert page.locator("#agents details").count() == 4
                assert "11 / 12" in page.locator("#steps-label").inner_text()
                page.screenshot(path=str(output / "desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(output / "mobile.png"), full_page=True)
                assert not errors, errors
                browser.close()
                print("Team UI: completed, four roles visible, total budget displayed, mobile fits, no JavaScript errors")
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()


if __name__ == "__main__":
    main()
