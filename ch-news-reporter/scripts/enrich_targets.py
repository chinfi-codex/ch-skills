#!/usr/bin/env python3
"""Fetch deterministic enrichment details for report evidence targets."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from db_adapter import (
    ensure_enrichments_table as db_ensure_enrichments_table,
    get_connection,
    write_enrichment,
)
from prepare_report_data import (
    DEFAULT_DB,
    normalize_url,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
GITHUB_API_BASE = "https://api.github.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch explicit enrichment targets into SQLite."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument(
        "--targets-file",
        help="JSON file containing a target object or a list of target objects. Use '-' for stdin.",
    )
    parser.add_argument(
        "--target-json",
        action="append",
        default=[],
        help="Explicit target JSON object. May be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap across selected enrichment targets.",
    )
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout.")
    parser.add_argument(
        "--readme-max-chars",
        type=int,
        default=12000,
        help="Maximum README characters stored for GitHub targets.",
    )
    parser.add_argument(
        "--website-max-chars",
        type=int,
        default=8000,
        help="Maximum visible text characters stored for website/article targets.",
    )
    parser.add_argument(
        "--no-readme",
        action="store_true",
        help="Skip README fetching for GitHub targets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected targets without fetching or writing.",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146 Safari/537.36"
            )
        }
    )
    return session


def ensure_enrichments_table(con: Any) -> None:
    db_ensure_enrichments_table(con)


def load_json_targets_from_text(text: str, label: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid target JSON in {label}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("targets", payload)
        if isinstance(payload, dict):
            return [payload]
    if isinstance(payload, list):
        targets = [target for target in payload if isinstance(target, dict)]
        if len(targets) != len(payload):
            raise SystemExit(f"{label} must contain only JSON objects")
        return targets
    raise SystemExit(f"{label} must be a target object, a targets object, or a list")


def load_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    if args.targets_file:
        if args.targets_file == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.targets_file).read_text(encoding="utf-8")
        targets.extend(load_json_targets_from_text(text, args.targets_file))
    for idx, target_json in enumerate(args.target_json, start=1):
        targets.extend(load_json_targets_from_text(target_json, f"--target-json #{idx}"))
    if args.limit is not None:
        targets = targets[: max(0, args.limit)]
    return [normalize_target(target) for target in targets]


def normalize_target(target: dict[str, Any]) -> dict[str, Any]:
    item_id = str(target.get("item_id") or "").strip()
    target_type = str(target.get("target_type") or "").strip()
    target_url = normalize_url(str(target.get("target_url") or ""))
    if not item_id:
        raise SystemExit("Each target requires item_id")
    if target_type not in {"github_repo", "product_website", "article_url"}:
        raise SystemExit(
            "Each target requires target_type: github_repo, product_website, or article_url"
        )
    if not target_url:
        raise SystemExit("Each target requires a valid absolute target_url")
    normalized = dict(target)
    normalized["item_id"] = item_id
    normalized["target_type"] = target_type
    normalized["target_url"] = target_url
    return normalized


def stable_enrichment_id(item_id: str, target_type: str, target_url: str) -> str:
    seed = "|".join([item_id, target_type, target_url])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def upsert_enrichment(
    con: Any,
    target: dict[str, Any],
    result: dict[str, Any],
) -> None:
    item_id = str(target["item_id"])
    target_type = str(target["target_type"])
    target_url = str(target["target_url"])
    row_id = stable_enrichment_id(item_id, target_type, target_url)
    fetched_at = now_iso()
    payload = {
        "id": row_id,
        "item_id": item_id,
        "target_type": target_type,
        "target_url": target_url,
        "fetched_at": fetched_at,
        "status": result.get("status") or "ok",
        "title": result.get("title"),
        "text_excerpt": result.get("text_excerpt"),
        "metadata": result.get("metadata") or {},
        "raw": result.get("raw") or {},
    }
    write_enrichment(
        con,
        enrichment_id=row_id,
        item_id=item_id,
        enrichment_type=target_type,
        source=target_url,
        model=None,
        prompt_hash=None,
        result_json=json.dumps(payload, ensure_ascii=False, default=str),
        created_at=fetched_at,
    )


def github_headers(raw: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_api_get(
    session: requests.Session,
    path: str,
    timeout: int,
    raw: bool = False,
    allow_404: bool = False,
) -> Any:
    response = session.get(
        f"{GITHUB_API_BASE}{path}", headers=github_headers(raw=raw), timeout=timeout
    )
    if response.status_code == 404 and allow_404:
        return None
    if response.status_code in {403, 429}:
        raise RuntimeError(f"GitHub API rate limited or forbidden: {response.status_code}")
    response.raise_for_status()
    return response.text if raw else response.json()


def parse_github_repo_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(f"Not a GitHub URL: {url}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub repo URL: {url}")
    return parts[0], parts[1].removesuffix(".git")


def truncate_text(text: str, max_chars: int) -> str:
    clean = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."


def extract_readme_sections(readme: str, max_chars: int) -> dict[str, str]:
    wanted = re.compile(
        r"(install|installation|usage|quick start|getting started|architecture|"
        r"example|examples|api|deploy|setup)",
        re.IGNORECASE,
    )
    headings = list(re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*$", readme))
    sections: dict[str, str] = {}
    for idx, match in enumerate(headings):
        title = match.group(2).strip()
        if not wanted.search(title):
            continue
        start = match.end()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(readme)
        body = readme[start:end].strip()
        if body:
            sections[title] = truncate_text(body, min(1800, max_chars))
        if len(sections) >= 6:
            break
    return sections


def fetch_github_repo(
    session: requests.Session,
    target_url: str,
    timeout: int,
    include_readme: bool,
    readme_max_chars: int,
) -> dict[str, Any]:
    owner, repo_name = parse_github_repo_url(target_url)
    repo = github_api_get(session, f"/repos/{owner}/{repo_name}", timeout)
    languages = github_api_get(
        session, f"/repos/{owner}/{repo_name}/languages", timeout, allow_404=True
    ) or {}
    release = github_api_get(
        session, f"/repos/{owner}/{repo_name}/releases/latest", timeout, allow_404=True
    )
    readme = ""
    readme_sections: dict[str, str] = {}
    if include_readme:
        readme_payload = github_api_get(
            session,
            f"/repos/{owner}/{repo_name}/readme",
            timeout,
            raw=True,
            allow_404=True,
        )
        readme = str(readme_payload or "")
        readme_sections = extract_readme_sections(readme, readme_max_chars)

    license_info = repo.get("license") if isinstance(repo.get("license"), dict) else {}
    owner_info = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    latest_release = None
    if isinstance(release, dict):
        latest_release = {
            "name": release.get("name"),
            "tag_name": release.get("tag_name"),
            "published_at": release.get("published_at"),
            "prerelease": release.get("prerelease"),
        }

    metadata = {
        "owner": owner_info.get("login"),
        "owner_type": owner_info.get("type"),
        "full_name": repo.get("full_name"),
        "description": repo.get("description"),
        "homepage": repo.get("homepage"),
        "topics": repo.get("topics") or [],
        "language": repo.get("language"),
        "languages": languages,
        "stargazers_count": repo.get("stargazers_count"),
        "forks_count": repo.get("forks_count"),
        "watchers_count": repo.get("watchers_count"),
        "open_issues_count": repo.get("open_issues_count"),
        "license": license_info.get("spdx_id") or license_info.get("name"),
        "default_branch": repo.get("default_branch"),
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "archived": repo.get("archived"),
        "latest_release": latest_release,
        "readme_sections": readme_sections,
    }
    return {
        "status": "ok",
        "title": repo.get("full_name") or f"{owner}/{repo_name}",
        "text_excerpt": truncate_text(readme, readme_max_chars) if readme else "",
        "metadata": metadata,
        "raw": {
            "repo": {
                key: repo.get(key)
                for key in (
                    "id",
                    "node_id",
                    "html_url",
                    "description",
                    "homepage",
                    "topics",
                    "stargazers_count",
                    "forks_count",
                    "open_issues_count",
                    "pushed_at",
                )
            },
            "languages": languages,
            "latest_release": latest_release,
            "readme_excerpt_chars": len(readme or ""),
        },
    }


def compact_visible_text(soup: BeautifulSoup, max_chars: int) -> str:
    for node in soup(["script", "style", "noscript", "svg", "canvas"]):
        node.decompose()
    lines: list[str] = []
    seen: set[str] = set()
    for line in soup.get_text("\n", strip=True).splitlines():
        clean = " ".join(line.split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        lines.append(clean)
        if sum(len(part) for part in lines) > max_chars + 1000:
            break
    return truncate_text("\n".join(lines), max_chars)


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"name": name}) or soup.find(
            "meta", attrs={"property": name}
        )
        if node and node.get("content"):
            return str(node["content"]).strip()
    return ""


def extract_key_links(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    keywords = re.compile(
        r"(pricing|price|docs|documentation|about|demo|api|enterprise|"
        r"waitlist|contact|login|sign in|sign up|get started)",
        re.IGNORECASE,
    )
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        text = " ".join(link.get_text(" ", strip=True).split())
        href = str(link.get("href") or "")
        resolved = urljoin(base_url, href)
        signal = f"{text} {href}"
        if not keywords.search(signal) or resolved in seen:
            continue
        seen.add(resolved)
        links.append({"text": text[:80], "url": resolved})
        if len(links) >= 16:
            break
    return links


def extract_ctas(soup: BeautifulSoup) -> list[str]:
    keywords = re.compile(
        r"(get started|start|try|book|demo|sign up|join|waitlist|contact|"
        r"subscribe|download|install)",
        re.IGNORECASE,
    )
    ctas: list[str] = []
    seen: set[str] = set()
    for node in soup.select("button, a"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text or text in seen or not keywords.search(text):
            continue
        seen.add(text)
        ctas.append(text[:80])
        if len(ctas) >= 12:
            break
    return ctas


def fetch_html_summary(
    session: requests.Session,
    target_url: str,
    timeout: int,
    max_chars: int,
) -> dict[str, Any]:
    response = session.get(target_url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text = response.text
    if "html" not in content_type.lower() and "<html" not in text[:500].lower():
        excerpt = truncate_text(text, max_chars)
        return {
            "status": "ok",
            "title": target_url,
            "text_excerpt": excerpt,
            "metadata": {
                "final_url": response.url,
                "status_code": response.status_code,
                "content_type": content_type,
            },
            "raw": {"headers": dict(response.headers), "non_html": True},
        }

    soup = BeautifulSoup(text, "html.parser")
    title = (
        meta_content(soup, "og:title", "twitter:title")
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
        or response.url
    )
    description = meta_content(
        soup, "description", "og:description", "twitter:description"
    )
    h1 = [node.get_text(" ", strip=True) for node in soup.find_all("h1")[:6]]
    h2 = [node.get_text(" ", strip=True) for node in soup.find_all("h2")[:12]]
    metadata = {
        "final_url": response.url,
        "status_code": response.status_code,
        "content_type": content_type,
        "description": description,
        "h1": h1,
        "h2": h2,
        "key_links": extract_key_links(soup, response.url),
        "cta_texts": extract_ctas(soup),
        "canonical": "",
    }
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        metadata["canonical"] = urljoin(response.url, str(canonical["href"]))

    return {
        "status": "ok",
        "title": title,
        "text_excerpt": compact_visible_text(soup, max_chars),
        "metadata": metadata,
        "raw": {
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "last-modified", "etag"}
            },
            "html_chars": len(text),
        },
    }


def fetch_target(
    session: requests.Session,
    target: dict[str, Any],
    include_readme: bool,
    readme_max_chars: int,
    website_max_chars: int,
    timeout: int,
) -> dict[str, Any]:
    target_type = target.get("target_type")
    target_url = str(target.get("target_url") or "")
    if target_type == "github_repo":
        return fetch_github_repo(
            session, target_url, timeout, include_readme, readme_max_chars
        )
    if target_type in {"product_website", "article_url"}:
        return fetch_html_summary(session, target_url, timeout, website_max_chars)
    raise ValueError(f"Unsupported target_type: {target_type}")


def target_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "error",
        "title": None,
        "text_excerpt": "",
        "metadata": {"error": str(exc), "error_type": type(exc).__name__},
        "raw": {},
    }


def main() -> int:
    args = parse_args()
    targets = load_targets(args)
    if not targets:
        raise SystemExit(
            "No targets supplied. Pass --targets-file or one or more --target-json values."
        )
    if args.dry_run:
        print(json.dumps({"targets": targets}, ensure_ascii=False, indent=2))
        return 0

    with get_connection(str(Path(args.db))) as con:
        ensure_enrichments_table(con)
        session = get_session()
        results: list[dict[str, Any]] = []
        for target in targets:
            try:
                result = fetch_target(
                    session,
                    target,
                    not args.no_readme,
                    args.readme_max_chars,
                    args.website_max_chars,
                    args.timeout,
                )
            except Exception as exc:  # Store retryable failures for traceability.
                result = target_error(exc)
            upsert_enrichment(con, target, result)
            con.commit()
            results.append(
                {
                    "item_id": target["item_id"],
                    "target_type": target["target_type"],
                    "target_url": target["target_url"],
                    "status": result.get("status"),
                    "title": result.get("title"),
                }
            )

    print(
        json.dumps(
            {
                "processed": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
