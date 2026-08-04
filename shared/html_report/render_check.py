"""Deploy gate: load a rendered report in a real browser and decide whether it
may ship.

Run the same check at every stage the file passes through, because each stage
breaks the page in a different way:

- ``local``  — the freshly rendered file. Catches anchor misses, wrong chart
  counts, unexplained data gaps, charts landing outside their section.
- ``site``   — the copy after Site externalisation. Catches inline scripts that
  the externaliser stripped or rewrote, and sibling assets (sharded K-line
  JSON, images) whose relative paths did not survive the copy.
- ``online`` — the deployed URL. Catches CSP blocking inline scripts, a stale
  cached build, and assets that 404 in production.

A static parse cannot substitute for this. Almost everything worth checking —
where a chart ended up, whether a hook ran at all — exists only in the DOM
after the page's own scripts have run.

Exit codes are meant to be branched on:

- ``0`` gate passed, deployment may proceed
- ``1`` gate failed, do not deploy and do not clean up
- ``2`` checks passed but only in ``--static-only`` mode, which is a smoke test
  and not a deploy gate

Usage::

    python3 -m html_report.render_check --target reports/report_20260803.html \\
        --stage local --out reports/render_check_20260803_local.json

For ``site`` and ``online``, pass the ``build_id`` from that local audit as
``--expect-build``. Browser mode requires the Playwright Python package and a
Chromium installed by ``python3 -m playwright install chromium``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

STAGES = ("local", "site", "online")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_STATIC_ONLY = 2

# Console noise that says nothing about whether the report rendered. Font CDN
# failures are expected on an offline machine and must not red-light a deploy.
_CONSOLE_IGNORE = re.compile(
    r"fonts\.(googleapis|gstatic)\.com|favicon\.ico|Failed to load resource: net::ERR_INTERNET_DISCONNECTED"
)


def to_url(target: str) -> str:
    parsed = urlparse(target)
    if parsed.scheme in ("http", "https", "file"):
        return target
    path = Path(target).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"target not found: {path}")
    return path.as_uri()


def check_static(target: str) -> Dict[str, Any]:
    """Parse-only smoke test for when a browser is unavailable.

    It can tell that the manifest and the section anchors were written, and
    that each declared hook has a script block. It cannot tell where anything
    ended up on the page, which is why it never counts as a gate pass.
    """
    path = Path(target).expanduser()
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: List[Dict[str, Any]] = []

    match = re.search(
        r'<script id="render-manifest" type="application/json">(.*?)</script>', text, re.DOTALL
    )
    manifest: Optional[Dict[str, Any]] = None
    if not match:
        problems.append({"scope": "manifest", "message": "no render-manifest in the page — it was built without a section contract"})
    else:
        try:
            manifest = json.loads(match.group(1).replace("<\\/", "</"))
        except json.JSONDecodeError as exc:
            problems.append({"scope": "manifest", "message": f"render-manifest is not valid JSON: {exc}"})

    if manifest:
        for key in manifest.get("sections", []):
            if f'data-sec="{key}"' not in text:
                problems.append({"scope": f"section:{key}", "message": "section anchor missing from the served HTML"})
        for hook in manifest.get("hooks", []):
            name = hook.get("name", "")
            root = name.split(".")[0]
            if f"'hook:{root}'" not in text and f'"hook:{root}"' not in text:
                problems.append({"scope": f"hook:{name}", "message": "no script block found for this hook"})

    return {
        "mode": "static-only",
        "status": "error" if problems else "ok",
        "contract_version": (manifest or {}).get("contract_version"),
        "build_id": (manifest or {}).get("build_id"),
        "problems": problems,
        "detail": {},
        "console_errors": [],
    }


def check_browser(target: str, timeout_ms: int) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright  # imported lazily: only this path needs it
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "browser gate requires Playwright; install it with `python3 -m pip install playwright`, "
            "then install Chromium with `python3 -m playwright install chromium`"
        ) from exc

    url = to_url(target)
    console_errors: List[str] = []
    page_errors: List[str] = []
    failed_requests: List[str] = []
    http_errors: List[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda msg: (
            console_errors.append(msg.text)
            if msg.type == "error" and not _CONSOLE_IGNORE.search(msg.text)
            else None
        ))
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("requestfailed", lambda req: (
            failed_requests.append(f"{req.url} — {(req.failure or '')}")
            if not _CONSOLE_IGNORE.search(req.url)
            else None
        ))
        # Playwright considers HTTP 4xx/5xx responses successful at the
        # transport layer, so they never emit ``requestfailed``. Watch the
        # response stream as well or a missing image/asset can pass the gate.
        page.on("response", lambda response: (
            http_errors.append(f"HTTP {response.status} — {response.url}")
            if response.status >= 400 and not _CONSOLE_IGNORE.search(response.url)
            else None
        ))

        started = time.time()
        page.goto(url, wait_until="load", timeout=timeout_ms)
        status_error: Optional[str] = None
        try:
            page.wait_for_function(
                "() => document.documentElement.hasAttribute('data-render-status')",
                timeout=timeout_ms,
            )
        except Exception:
            # No attestation at all. This is the case a naive "look for errors"
            # checker would wave through: scripts stripped, nothing ran,
            # nothing complained.
            status_error = (
                "page never wrote data-render-status — its scripts did not run "
                "(stripped by the Site externaliser or blocked by CSP?), or it was "
                "built without a section contract"
            )

        report: Dict[str, Any] = page.evaluate(
            "() => { const el = document.getElementById('render-report');"
            " if (!el) return null; try { return JSON.parse(el.textContent); } catch (e) { return null; } }"
        )
        dom_sections: Dict[str, bool] = page.evaluate(
            "() => { const el = document.getElementById('render-manifest');"
            " if (!el) return {}; let m; try { m = JSON.parse(el.textContent); } catch (e) { return {}; }"
            " const out = {};"
            " (m.sections || []).forEach(k => { out[k] = Boolean(document.querySelector('[data-sec=\"' + k + '\"]')); });"
            " return out; }"
        )
        elapsed_ms = int((time.time() - started) * 1000)
        browser.close()

    problems: List[Dict[str, Any]] = []
    if status_error:
        problems.append({"scope": "attestation", "message": status_error})
    if report is None and not status_error:
        problems.append({"scope": "attestation", "message": "render-report block is missing or unparseable"})
    if report:
        problems.extend(report.get("problems") or [])
    # Verified independently of the page's own finalizer, so a tampered or
    # partially-executed runtime cannot vouch for itself.
    for key, present in (dom_sections or {}).items():
        if not present:
            problems.append({"scope": f"section:{key}", "message": "section anchor not found in the live DOM"})
    for message in page_errors:
        problems.append({"scope": "pageerror", "message": message})
    for message in console_errors:
        problems.append({"scope": "console", "message": message})
    for message in failed_requests:
        problems.append({"scope": "request", "message": f"request failed: {message}"})
    for message in http_errors:
        problems.append({"scope": "response", "message": message})

    return {
        "mode": "browser",
        "status": "error" if problems else "ok",
        "contract_version": (report or {}).get("contract_version"),
        "build_id": (report or {}).get("build_id"),
        "problems": problems,
        "detail": (report or {}).get("detail") or {},
        "console_errors": console_errors,
        "http_errors": http_errors,
        "load_ms": elapsed_ms,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a rendered report satisfies its render contract.")
    parser.add_argument("--target", required=True, help="Local HTML path or http(s) URL.")
    parser.add_argument("--stage", required=True, choices=STAGES,
                        help="Which pipeline stage this run covers: local file, Site-externalised copy, or the deployed URL.")
    parser.add_argument("--out", default=None, help="Audit JSON path. Defaults to <target>.render-check-<stage>.json for local files.")
    parser.add_argument("--timeout", type=int, default=30000, help="Per-step browser timeout in ms.")
    parser.add_argument("--expect-contract", default=None, help="Fail unless the page reports this contract version.")
    parser.add_argument("--expect-build", default=None,
                        help="Fail unless the page reports this build ID. Required for site/online so stale copies cannot pass.")
    parser.add_argument("--static-only", action="store_true",
                        help="Parse the HTML instead of loading it in a browser. Smoke test only — exits 2 on success, "
                             "because placement and runtime failures are invisible to it.")
    args = parser.parse_args(argv)

    if args.static_only and urlparse(args.target).scheme in ("http", "https"):
        print("error: --static-only needs a local file, not a URL", file=sys.stderr)
        return EXIT_FAIL
    if args.stage in ("site", "online") and not args.expect_build:
        parser.error(
            f"--stage {args.stage} requires --expect-build from the local gate audit; "
            "without it a stale copy could pass"
        )

    try:
        result = check_static(args.target) if args.static_only else check_browser(args.target, args.timeout)
    except Exception as exc:  # noqa: BLE001 — CLI boundary: a crashed check is a failed check
        result = {
            "mode": "static-only" if args.static_only else "browser",
            "status": "error",
            "problems": [{"scope": "checker", "message": f"{type(exc).__name__}: {exc}"}],
            "detail": {},
            "console_errors": [],
        }

    if args.expect_contract and result.get("contract_version") != args.expect_contract:
        result["status"] = "error"
        result.setdefault("problems", []).append({
            "scope": "contract",
            "message": f"expected contract {args.expect_contract}, page reports {result.get('contract_version')!r}",
        })
    if args.expect_build and result.get("build_id") != args.expect_build:
        result["status"] = "error"
        result.setdefault("problems", []).append({
            "scope": "build",
            "message": f"expected build {args.expect_build}, page reports {result.get('build_id')!r}",
        })

    passed = result["status"] == "ok"
    gate_pass = passed and result["mode"] == "browser"
    audit = {
        "stage": args.stage,
        "target": args.target,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "gate_pass": gate_pass,
        **result,
    }

    out_path = Path(args.out) if args.out else None
    if out_path is None and urlparse(args.target).scheme not in ("http", "https"):
        target = Path(args.target)
        out_path = target.with_name(f"{target.stem}.render-check-{args.stage}.json")
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    problems = result.get("problems") or []
    header = f"[{args.stage}] {result['mode']} check: {result['status'].upper()}"
    print(header, file=sys.stderr)
    for item in problems[:30]:
        print(f"  - {item.get('scope')}: {item.get('message')}", file=sys.stderr)
    if len(problems) > 30:
        print(f"  … and {len(problems) - 30} more", file=sys.stderr)
    if out_path is not None:
        print(f"  audit: {out_path}", file=sys.stderr)

    if not passed:
        return EXIT_FAIL
    return EXIT_PASS if gate_pass else EXIT_STATIC_ONLY


if __name__ == "__main__":
    raise SystemExit(main())
