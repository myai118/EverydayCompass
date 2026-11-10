#!/usr/bin/env python3
"""
Everyday Compass - What's On refresh, version 2 (wrapper around update_events.py).

This file does NOT change update_events.py. It borrows its readers and safety rules, and adds:
  1. Reads the source list from sources.json (up to 20 sources). Falls back to the built-in 8.
  2. Records the date each source was LAST CHECKED SUCCESSFULLY (kept when a source fails).
  3. Flags the run when EVERY source returned zero events, so the workflow can fail and email you.
  4. Fixes the end-date reader so periods like "12 - 27 Sep 2026" are understood, and removes
     finished events even when sources fail.
  5. A simple reader for NEW sources: it understands the standard "Event" data that many official
     websites publish inside their pages (JSON-LD). Sites without it return zero events (safe).

Safety kept from update_events.py: a failed source keeps its previous events, an empty feed is
never written, and --dry-run never writes events.json.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_events as ue  # noqa: E402
import additional_sources as additional

ROOT = ue.ROOT
SOURCES_FILE = ROOT / "sources.json"
MAX_SOURCES = 20
BUILTIN = copy.deepcopy(ue.SOURCES)

_orig_finite_end_date = ue.finite_end_date
_orig_status_for = ue.status_for
_orig_parse_source_html = ue.parse_source_html
_orig_verified_empty_source = ue.verified_empty_source

EXCLUDE = re.compile(r"\b(permanent|ongoing|daily|every\b|various dates|from\s+\d)", re.I)
# "12 - 27 Sep 2026": first date has no month. update_events.py cannot read this form.
SHORT_RANGE = re.compile(
    r"(?<!\d)(?P<d1>\d{1,2})\s*[\u2013\u2014-]\s*(?P<end>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
)


# ---------------------------------------------------------------- dates / expiry
def finite_end_date_v2(period):
    """Same as the original, plus the 'd - d Mon yyyy' form. Unknown or open-ended -> None (kept)."""
    d = _orig_finite_end_date(period)
    if d:
        return d
    p = ue.norm(period)
    if not p or EXCLUDE.search(p):
        return None
    m = SHORT_RANGE.search(p)
    return ue.parse_date(m.group("end")) if m else None


def status_for_v2(period, today):
    status, cls = _orig_status_for(period, today)
    p = ue.norm(period)
    if status == "Ongoing" and p and not ue.DATE_RANGE.search(p) and not EXCLUDE.search(p):
        m = SHORT_RANGE.search(p)
        end_d = ue.parse_date(m.group("end")) if m else None
        if end_d:
            try:
                start_d = date(end_d.year, end_d.month, int(m.group("d1")))
            except ValueError:
                start_d = None
            if start_d and start_d > today:
                return "Upcoming", "upcoming"
            if 0 <= (end_d - today).days <= 14:
                return "Ending Soon", "ending"
    return status, cls


# ---------------------------------------------------------------- sources.json
def load_sources(path=None):
    """Return (sources_dict, warnings). Never raises: problems fall back to the built-in sources."""
    path = Path(path) if path else SOURCES_FILE
    warnings = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(BUILTIN), ["sources.json not found - using the built-in sources"]
    except Exception as exc:  # unreadable / invalid JSON
        return copy.deepcopy(BUILTIN), [f"sources.json unreadable ({str(exc)[:80]}) - using the built-in sources"]
    items = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return copy.deepcopy(BUILTIN), ["sources.json has no 'sources' list - using the built-in sources"]

    out = {}
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            warnings.append(f"source {i}: not an object - skipped")
            continue
        if it.get("enabled", True) is False:
            continue
        if len(out) >= MAX_SOURCES:
            warnings.append(f"more than {MAX_SOURCES} enabled sources - extra ones ignored")
            break
        url = str(it.get("url", "")).strip()
        u = urlparse(url)
        if u.scheme != "https" or not u.hostname or "." not in u.hostname:
            warnings.append(f"source {i}: web address must start with https:// - skipped")
            continue
        name = ue.norm(it.get("name")) or u.hostname
        key = re.sub(r"[^a-z0-9_]+", "_", str(it.get("id") or name).lower()).strip("_") or f"src{i}"
        base, n = key, 2
        while key in out:
            key, n = f"{base}_{n}", n + 1
        host = u.hostname.lower()
        host = host[4:] if host.startswith("www.") else host
        b = BUILTIN.get(key)
        if b and host.endswith(b["host"]):
            host = b["host"]
        out[key] = {"name": name, "url": url, "host": host}
        listing = str(it.get("listing_url") or "").strip()
        lu = urlparse(listing)
        if lu.scheme == "https" and ((lu.hostname or "").lower() == host or (lu.hostname or "").lower().endswith("." + host)):
            out[key]["listing_url"] = listing
    if not out:
        warnings.append("no usable enabled sources in sources.json - using the built-in sources")
        return copy.deepcopy(BUILTIN), warnings
    return out, warnings


# ---------------------------------------------------------------- reader for NEW sources
def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "itemListElement", "item", "subEvent"):
            if k in node:
                yield from _walk(node[k])


def _is_event(n):
    t = n.get("@type")
    ts = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.endswith("Event") for x in ts)


def _to_date(v):
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y-%m-%d").date()
    except ValueError:
        return None


def _fmt(d):
    return f"{d.day} {d:%b %Y}"


def _is_free(n):
    if n.get("isAccessibleForFree") is True:
        return True
    offers = n.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    for o in offers:
        if isinstance(o, dict) and str(o.get("price", "")).strip() in ("0", "0.0", "0.00"):
            return True
    return False


def generic_parse(html, key, today):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    cfg = ue.SOURCES[key]
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        for n in _walk(data):
            if not _is_event(n):
                continue
            title = ue.norm(n.get("name"))
            start = _to_date(n.get("startDate"))
            end = _to_date(n.get("endDate")) or start
            if not title or not start or end < today:
                continue
            loc = n.get("location")
            venue = ue.norm(loc.get("name") if isinstance(loc, dict) else loc)
            link = urljoin(cfg["url"], str(n.get("url") or ""))
            lu = urlparse(link)
            if lu.scheme != "https" or not (lu.hostname or "").lower().endswith(cfg["host"]):
                link = cfg["url"]  # official sources only
            desc = BeautifulSoup(str(n.get("description") or ""), "html.parser").get_text(" ")
            free = _is_free(n)
            raw = {
                "title": title,
                "category": "Event",
                "overview": ue.norm(desc)[:400],
                "period": _fmt(start) if end == start else f"{_fmt(start)} \u2013 {_fmt(end)}",
                "venue": venue,
                "link": link,
                "adult": "Free" if free else "",
                "senior": "Free" if free else "",
                "child": "Free" if free else "",
                "tags": ["ongoing"] + (["free"] if free else []),
            }
            x = ue.clean_event(raw, key, today) or raw
            if isinstance(x, dict):
                x.setdefault("tags", raw["tags"])
                found.append(x)
    return found


def verified_empty_source_v2(html, key, today):
    if _orig_verified_empty_source(html, key, today):
        return True
    if key not in {"visit_singapore", "catch_sg", "science_centre"}:
        return False
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = ue.norm(soup.title.get_text(" ", strip=True) if soup.title else "")
    text = ue.norm(soup.get_text(" ", strip=True))
    # These public pages are readable but do not expose extractable event
    # records in their server HTML. A reachable, non-challenge page is still
    # a successful source check with zero extracted records.
    if key == "visit_singapore":
        return bool(title and len(text) > 200)
    return bool(title and len(text) > 500 and not re.search(
        r"verify.{0,20}(?:human|robot)|access denied|checking your browser|temporarily unavailable",
        text, re.I
    ))


def parse_source_html_v2(html, key, today):
    if key == "science_centre":
        return ue.parse_official_detail(html, ue.SOURCES[key]["url"], key, today)
    if key in additional.KEYS:
        return ue.dedupe(additional.parse(html, key, today, ue.SOURCES[key], ue.clean_event))
    structured = generic_parse(html, key, today)
    # New sources may expose a valid public calendar without JSON-LD event records.
    # Treat a clearly loaded official page with an explicit empty calendar as a
    # successful zero-event read; unreadable/challenge pages still fail safely.
    if not structured and key not in BUILTIN:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        page_title = ue.norm(soup.title.get_text(" ", strip=True) if soup.title else "")
        page_text = ue.norm(soup.get_text(" ", strip=True))
        if page_title and len(page_text) > 500 and not re.search(
            r"verify.{0,20}(?:human|robot)|access denied|checking your browser|temporarily unavailable",
            page_text, re.I
        ):
            if key in {"srcmvcxqoj2", "srcmw5leuft"}:
                return []
    if key == "catch_sg":
        return [e for e in structured if e.get("adult") == "Free"]
    if key == "catch_sg":
        return [e for e in structured if e.get("adult") == "Free"]
    if key in BUILTIN:
        return ue.dedupe(structured + _orig_parse_source_html(html, key, today))
    from bs4 import BeautifulSoup
    cards = ue.source_specific_events(BeautifulSoup(html, "html.parser"), key, today)
    return ue.dedupe(structured + [e for e in cards if not ue.is_expired(e, today)])


@contextlib.contextmanager
def patched(sources):
    """Temporarily swap in the v2 behaviour; always restored afterwards."""
    names = ("SOURCES", "finite_end_date", "status_for", "parse_source_html", "verified_empty_source")
    saved = {k: getattr(ue, k) for k in names}
    ue.SOURCES = sources
    ue.finite_end_date = finite_end_date_v2
    ue.status_for = status_for_v2
    ue.parse_source_html = parse_source_html_v2
    ue.verified_empty_source = verified_empty_source_v2
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(ue, k, v)


# ---------------------------------------------------------------- the refresh
def run(previous, sources, today=None, now=None, fetcher=None, source_keys=None):
    now = now or datetime.now(ue.SGT)
    today = today or now.date()
    active_keys = set(sources) if source_keys is None else set(source_keys)

    # Events from sources you removed or switched off are dropped. Hand-added events
    # (no source_key) are kept.
    kept = [e for e in previous.get("events", []) if not ue.source_key(e) or ue.source_key(e) in sources]
    prev = dict(previous)
    prev["events"] = kept

    kwargs = {"today": today}
    if fetcher is not None:
        kwargs["fetcher"] = fetcher
    with patched(sources):
        if source_keys is not None:
            kwargs["source_keys"] = active_keys
        payload = ue.update(prev, **kwargs)
        # Safety net: finished events never stay, even when every source failed.
        payload["events"] = [e for e in payload["events"] if not ue.is_expired(e, today)]

    # Track each event's first date in the feed. Older events without history stay unknown.
    def title_key(event):
        return re.sub(r"[\W_]+", " ", ue.norm(event.get("title")).casefold()).strip()

    previous_titles = {}
    for old_event in previous.get("events", []):
        if not isinstance(old_event, dict):
            continue
        key = title_key(old_event)
        if key and (key not in previous_titles or not previous_titles[key]):
            previous_titles[key] = old_event.get("date_added")
    for event in payload["events"]:
        if not isinstance(event, dict):
            continue
        key = title_key(event)
        if event.get("date_added"):
            continue
        if key in previous_titles:
            event["date_added"] = previous_titles[key]
        else:
            event["date_added"] = now.date().isoformat()

    old = {}
    for s in previous.get("sources", []) or []:
        if isinstance(s, dict) and s.get("id"):
            old[s["id"]] = s
    rows = []
    for key, cfg in sources.items():
        r = payload["source_report"].get(key, {})
        prior = old.get(key, {})
        deferred = key not in active_keys
        ok = r.get("status") == "fresh"
        if deferred:
            # A staged recovery run must not replace the already-published
            # normal-source result with a misleading "deferred" warning.
            # Carry the previous stage's status, count, timestamp and error
            # forward; only the selected recovery source is recalculated.
            row_status = prior.get("status") if prior.get("status") in {"ok", "fallback"} else "deferred"
            row_count = prior.get("count", 0)
            row_last = prior.get("last_success")
            row_error = prior.get("error", "")
            row_review = bool(prior.get("needs_reader_review"))
        else:
            row_status = "ok" if ok else "fallback"
            row_count = r.get("count", 0)
            row_last = now.isoformat(timespec="seconds") if ok else prior.get("last_success")
            row_error = "" if ok else str(r.get("error", ""))[:180]
            row_review = not ok and "zero usable" in str(r.get("error", "")).lower()
        rows.append({
            "id": key,
            "name": cfg["name"],
            "url": cfg["url"],
            "status": row_status,
            "count": row_count,
            "last_success": row_last,
            "error": row_error,
            "needs_reader_review": row_review,
        })
    payload["sources"] = rows
    # Evaluate the final merged feed, including results carried forward from
    # the earlier stage. This prevents an ArtScience-only retry from declaring
    # the whole refresh failed when normal sources succeeded.
    payload["all_sources_failed"] = not any(r["status"] == "ok" for r in rows)
    return payload


def write_github_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-artscience", action="store_true",
                    help="Refresh all sources except the slow ArtScience recovery source")
    ap.add_argument("--only-artscience", action="store_true",
                    help="Refresh only the ArtScience recovery source")
    ap.add_argument("--output", default=str(ue.OUT))
    ap.add_argument("--sources", default=str(SOURCES_FILE))
    args = ap.parse_args(argv)

    path = Path(args.output)
    previous = ue.load_previous(path)
    sources, warnings = load_sources(args.sources)
    for w in warnings:
        print("WARNING:", w)
    print(f"Using {len(sources)} source(s): " + ", ".join(sources))

    if args.skip_artscience and args.only_artscience:
        raise SystemExit("Choose only one of --skip-artscience and --only-artscience")
    if args.only_artscience:
        active_keys = {"artscience"}
    elif args.skip_artscience:
        active_keys = set(sources) - {"artscience"}
    else:
        active_keys = set(sources)

    payload = run(previous, sources, source_keys=active_keys)
    for row in payload["sources"]:
        line = f"  [{row['status'].upper():8}] {row['name']}: {row['count']} event(s)"
        if row["error"]:
            line += f" - {row['error']}"
        print(line)

    reviews = [row for row in payload["sources"] if row.get("needs_reader_review")]
    if not args.dry_run:
        write_github_output("reader_review", "true" if reviews else "false")
        write_github_output("reader_review_details", json.dumps(reviews, ensure_ascii=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and reviews:
        import html
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Source reader enhancement review\n\n")
            fh.write("These pages returned no usable events. They may have no current events or need a custom reader; review before enhancing.\n\n")
            for row in reviews:
                fh.write(f"- {html.escape(row['name'])}: {html.escape(row['url'])} — {html.escape(row['error'])}\n")
    if payload["all_sources_failed"]:
        print("WARNING: every source returned zero events. Old events were kept (finished ones removed).")

    if args.dry_run:
        print(f"DRY RUN: {len(payload['events'])} events; {path} not modified")
        return 0
    if not payload["events"]:
        raise RuntimeError("Safety stop: refusing to create an empty production feed")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_github_output("all_sources_failed", "true" if payload["all_sources_failed"] else "false")
    print(f"Wrote {len(payload['events'])} events to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
