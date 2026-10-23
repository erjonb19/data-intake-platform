"""Capture the README's screenshots from a running local stack (Stage 8, S8.3).

    python scripts/screenshots.py OUT_DIR

Drives a separate headless Chrome (its own throwaway profile, never the user's browser)
over the DevTools protocol: logs in where a page needs it, opens the page, waits, and
saves a PNG. Expects the stack from `scripts/up.sh` with the observability profile, the
E2E already run (so there is data to show), and these port-forwards:

  Airflow     http://localhost:8080          (up.sh binds it)
  Grafana     kubectl -n dip-obs port-forward svc/grafana 3000:80
  Marquez UI  kubectl -n dip-obs port-forward svc/marquez-web 3001:3000

Env: AIRFLOW_PASSWORD, GRAFANA_PASSWORD (scripts/creds.sh prints them), CHROME (default:
the usual Windows install path), MARQUEZ_NAMESPACE (the warehouse's OpenLineage
namespace), QUARANTINE_LOG_URL (the Airflow page of an intake task that quarantined files),
and APPROVALS_TEXT, a file holding `dip approvals list` and `show` output as printed (the
approvals queue has no web UI: the CLI is the queue, so that frame is a terminal).
Needs `websocket-client` (pip). Every value shown is synthetic or invented.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = os.environ.get("CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
W, H = 1440, 900


class Page:
    def __init__(self, ws_url: str):
        # No Origin header: Chrome refuses DevTools connections from an unlisted origin.
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0

    def send(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def go(self, url: str, settle: float = 4.0) -> None:
        self.send("Page.navigate", url=url)
        self.wait("document.readyState === 'complete'")
        time.sleep(settle)

    def js(self, expr: str):
        got = self.send("Runtime.evaluate", expression=expr, awaitPromise=True,
                        returnByValue=True)  # fmt: skip
        return got.get("result", {}).get("value")

    def wait(self, expr: str, timeout: float = 60) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.js(f"!!({expr})"):
                return
            time.sleep(0.5)
        raise TimeoutError(expr)

    def fill(self, selector: str, value: str) -> None:
        # Set the value the way React and Angular inputs notice it.
        sel, val = json.dumps(selector), json.dumps(value)
        self.js(
            f"(() => {{ const el = document.querySelector({sel});"
            " const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"
            f" 'value').set; set.call(el, {val});"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )

    def click(self, selector: str) -> None:
        self.js(f"document.querySelector({json.dumps(selector)}).click()")

    def click_text(self, tag: str, text: str) -> None:
        self.js(
            f"[...document.querySelectorAll({json.dumps(tag)})]"
            f".find(b => b.textContent.trim() === {json.dumps(text)})?.click()"
        )

    def shot(self, path: Path) -> None:
        png = self.send("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(png))
        print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


def terminal_page(text: str) -> str:
    """The text as a terminal shows it: monospace, dark, nothing added."""
    import html

    return (
        "<!doctype html><meta charset=utf-8><body style='margin:0;background:#1e1e1e'>"
        "<pre style='margin:0;padding:24px;color:#d4d4d4;font:14px/1.45 Consolas,"
        "monospace;white-space:pre-wrap'>" + html.escape(text) + "</pre>"
    )


def chrome() -> tuple[subprocess.Popen, Page, str]:
    """A headless Chrome with its own throwaway profile and a DevTools port of its own
    choosing (bound to 127.0.0.1, read back from DevToolsActivePort): never another Chrome
    that happens to listen on a fixed port. Returns the process, its page and the profile
    directory (the caller removes it)."""
    profile = tempfile.mkdtemp(prefix="dip-shots-")
    args = [
        CHROME,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        f"--window-size={W},{H}",
        "--hide-scrollbars",
        "--no-first-run",
        "about:blank",
    ]
    proc = subprocess.Popen(args)
    active = Path(profile) / "DevToolsActivePort"
    for _ in range(60):
        try:
            port = int(active.read_text().splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as r:
                tabs = [t for t in json.load(r) if t.get("type") == "page"]
            if tabs:
                page = Page(tabs[0]["webSocketDebuggerUrl"])
                page.send("Page.enable")
                page.send("Emulation.setDeviceMetricsOverride", width=W, height=H,
                          deviceScaleFactor=1, mobile=False)  # fmt: skip
                return proc, page, profile
        except (OSError, ValueError, IndexError):
            pass
        except Exception:
            stop(proc, profile)
            raise
        time.sleep(0.5)
    stop(proc, profile)
    raise RuntimeError("headless Chrome did not start")


def stop(proc: subprocess.Popen, *dirs: str) -> None:
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(10)
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    e = os.environ
    proc, p, profile = chrome()
    scratch = tempfile.mkdtemp(prefix="dip-shots-")
    try:
        # 1. Airflow: the eligibility feed's intake DAG and its runs.
        p.go("http://localhost:8080/auth/login/")
        p.fill("input[name=username]", "admin")
        p.fill("input[name=password]", e["AIRFLOW_PASSWORD"])
        p.js("document.querySelector('form[name=login]').submit()")
        time.sleep(5)
        p.go("http://localhost:8080/dags/intake__healthcare__eligibility", settle=6)
        p.shot(out / "airflow-dag-run.png")

        # 2. Grafana: the Feeds dashboard.
        p.go("http://localhost:3000/login")
        p.fill("input[name=user]", "admin")
        p.fill("input[name=password]", e["GRAFANA_PASSWORD"])
        p.click("button[type=submit]")
        time.sleep(4)
        p.go("http://localhost:3000/d/dip-feeds/feeds?orgId=1&from=now-6h&to=now&kiosk", settle=8)
        p.shot(out / "grafana-feeds.png")

        # 3. Marquez: lineage of gold.member_eligibility back to the landed files.
        p.go(
            "http://localhost:3001/lineage/dataset/"
            + urllib.request.quote(e["MARQUEZ_NAMESPACE"], safe="")
            + "/warehouse.gold.member_eligibility",
            settle=8,
        )
        p.shot(out / "marquez-lineage.png")

        # 4. Quarantined files with their reason codes: the intake task's log in Airflow
        #    (one line per file: status, accepted and rejected rows, reason codes).
        p.go(e["QUARANTINE_LOG_URL"], settle=8)
        p.shot(out / "quarantine-reason.png")

        # 5. The approvals queue: `dip approvals list` and `show`, as printed.
        page = Path(scratch) / "approvals.html"
        page.write_text(terminal_page(Path(e["APPROVALS_TEXT"]).read_text("utf-8")), "utf-8")
        p.go(page.as_uri(), settle=1)
        p.shot(out / "approvals-queue.png")
    finally:
        with contextlib.suppress(Exception):
            p.send("Browser.close")
        stop(proc, profile, scratch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
