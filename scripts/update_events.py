#!/usr/bin/env python3
"""
Everyday Compass - What's On updater.

Safety rules:
- Official sources only.
- Each source is isolated: failure/zero usable results falls back to that source's previous records.
- Expire only records with a reliably parsed finite end date before today (Singapore time).
- Permanent / Daily / Ongoing / unknown-end records are preserved.
- Deduplicate and prefer richer records.
- Never replace a valid non-empty feed with an empty feed.
- --dry-run never writes events.json.

This deliberately uses conservative extraction. Sites change; when a parser cannot confidently
extract events it preserves the previous valid records instead of guessing.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, re, sys
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "events.json"
SGT = ZoneInfo("Asia/Singapore")
UA = "EverydayCompass-WhatsOn/1.0 (+official-source-refresh)"

SOURCES = {
    "sccc": {
        "name": "Singapore Chinese Cultural Centre",
        "url": "https://singaporeccc.org.sg/events/",
        "host": "singaporeccc.org.sg",
    },
    "gardens": {
        "name": "Gardens by the Bay",
        "url": "https://www.gardensbythebay.com.sg/en/things-to-do/calendar-of-events.html",
        "host": "gardensbythebay.com.sg",
    },
    "esplanade": {
        "name": "Esplanade",
        "url": "https://www.esplanade.com/whats-on",
        "host": "esplanade.com",
    },
    "national_museum": {
        "name": "National Museum of Singapore",
        "url": "https://www.nationalmuseum.nhb.gov.sg/whats-on/exhibition/exhibitions",
        "host": "nationalmuseum.nhb.gov.sg",
    },
    "national_gallery": {
        "name": "National Gallery Singapore",
        "url": "https://www.nationalgallery.sg/sg/en/whats-on.html",
        "host": "nationalgallery.sg",
    },
    "artscience": {
        "name": "ArtScience Museum",
        "url": "https://www.marinabaysands.com/museum/whats-on.html",
        "host": "marinabaysands.com",
    },
    "ura": {
        "name": "Urban Redevelopment Authority",
        "url": "https://www.ura.gov.sg/get-involved/events-and-exhibitions/",
        "host": "ura.gov.sg",
    },
    "nlb": {
        "name": "National Library Board",
        "url": "https://exhibitions.nlb.gov.sg/current-exhibitions/",
        "host": "exhibitions.nlb.gov.sg",
    },
}

DATE_RANGE = re.compile(
    r"(?P<start>\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?)\s*(?:to|until|till|[–—-])\s*"
    r"(?P<end>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.I
)
ISO_RANGE = re.compile(r"(?P<start>\d{4}-\d{2}-\d{2})\s*(?:to|–|—|-)\s*(?P<end>\d{4}-\d{2}-\d{2})", re.I)

MONTH_FIRST_RANGE = re.compile(
    r"(?P<start>[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\s*(?:to|[–—-])\s*"
    r"(?P<end>[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", re.I
)
SHORT_DATE_RANGE = re.compile(
    r"(?<!\d)\d{1,2}\s*[–—-]\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}", re.I
)
SINGLE_DATE = re.compile(
    r"\b(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", re.I
)

def extract_period(text):
    text = norm(text)
    text = re.sub(r"\b(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?),?\s+(?=\d)", "", text, flags=re.I)
    text = re.sub(r"([A-Za-z]{3,9})\s*[’'](\d{2})\b", r"\1 20\2", text)
    for pattern in (MONTH_FIRST_RANGE, DATE_RANGE, ISO_RANGE, SHORT_DATE_RANGE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    for word in ("Permanent", "Ongoing"):
        if re.search(r"\b" + word + r"\b", text, re.I):
            return word
    match = SINGLE_DATE.search(text)
    return match.group(0) if match else ""

def official_link(url, key):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if key == "eventbrite":
        return parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in ("eventbrite.sg", "eventbrite.com"))
    allowed = SOURCES[key]["host"]
    return parsed.scheme == "https" and (host == allowed or host.endswith("." + allowed))


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def load_previous(path=OUT):
    if not path.exists():
        return {"updated_at": None, "events": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("Existing events.json is invalid")
    return data

def source_key(e):
    return e.get("source_key") or ""

def previous_for(previous, key):
    return [copy.deepcopy(e) for e in previous.get("events", []) if source_key(e) == key]

def parse_date(s, default_year=None):
    s = norm(s).replace(",", "")
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    if default_year:
        for fmt in ("%d %b", "%d %B"):
            try:
                d = datetime.strptime(s, fmt)
                return date(default_year, d.month, d.day)
            except ValueError:
                pass
    return None

def finite_end_date(period):
    p = norm(period)
    if not p or re.search(r"\b(permanent|ongoing|daily|every\b|various dates|from\s+\d)", p, re.I):
        return None
    m = MONTH_FIRST_RANGE.search(p)
    if m:
        return parse_date(m.group("end"))
    m = ISO_RANGE.search(p)
    if m:
        return parse_date(m.group("end"))
    m = DATE_RANGE.search(p)
    if m:
        end = parse_date(m.group("end"))
        return end
    if SINGLE_DATE.fullmatch(p):
        return parse_date(p)
    multiple = re.fullmatch(r"\d{1,2}\s*&\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", p)
    if multiple:
        return parse_date(multiple.group(1))
    # single exact dated event
    m = re.fullmatch(r"(?:[A-Za-z]{3},?\s*)?(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", p, re.I)
    return parse_date(m.group(1)) if m else None

def is_expired(e, today):
    end = finite_end_date(e.get("period", ""))
    return bool(end and end < today)

def status_for(period, today):
    end = finite_end_date(period)
    if end and end < today:
        return "Expired", ""
    # Parse start where possible
    m = MONTH_FIRST_RANGE.search(norm(period)) or DATE_RANGE.search(norm(period)) or ISO_RANGE.search(norm(period))
    if m:
        end_d = parse_date(m.group("end"))
        start_d = parse_date(m.group("start"), end_d.year if end_d else today.year)
        if start_d and start_d > today:
            return "Upcoming", "upcoming"
        if end_d and 0 <= (end_d - today).days <= 14:
            return "Ending Soon", "ending"
    single = parse_date(norm(period))
    if single and single > today:
        return "Upcoming", "upcoming"
    if single and 0 <= (single - today).days <= 14:
        return "Ending Soon", "ending"
    return "Ongoing", ""

def richness(e):
    fields = ("title","category","overview","period","time","venue","mrt","adult","senior","child","link")
    return sum(bool(norm(str(e.get(k,"")))) for k in fields) + len(e.get("tags", []))/10

def event_identity(e):
    title = re.sub(r"[\W_]+", " ", norm(e.get("title","")).casefold()).strip()
    venue = re.sub(r"[\W_]+", " ", norm(e.get("venue","")).casefold()).strip()
    return (title, venue)

def dedupe(events):
    best = {}
    for e in events:
        k = event_identity(e)
        if not k[0]:
            continue
        if k not in best or richness(e) > richness(best[k]):
            best[k] = e
    return list(best.values())

def clean_event(e, key, today):
    x = copy.deepcopy(e)
    x["source_key"] = key
    x["source"] = SOURCES[key]["name"]
    x["title"] = norm(x.get("title"))
    x["category"] = norm(x.get("category")) or "Event"
    x["overview"] = norm(x.get("overview"))
    x["why"] = norm(x.get("why")) or "See the official organiser page for the latest visitor information."
    x["period"] = norm(x.get("period"))
    x["time"] = norm(x.get("time")) or "Check official information"
    x["venue"] = norm(x.get("venue")) or SOURCES[key]["name"]
    x["mrt"] = norm(x.get("mrt")) or "Check directions"
    x["duration"] = norm(x.get("duration")) or "Flexible"
    x["adult"] = norm(x.get("adult")) or "Check official information"
    x["senior"] = norm(x.get("senior")) or "Check official information"
    x["child"] = norm(x.get("child")) or "Check official information"
    x["link"] = norm(x.get("link")) or SOURCES[key]["url"]
    status, cls = status_for(x["period"], today)
    x["status"], x["statusClass"] = status, cls
    tags = set(x.get("tags") or [])
    tags.add("wheelchair") if "wheelchair" in (x["overview"]+" "+x["venue"]).lower() else None
    if status == "Upcoming": tags.add("upcoming")
    else: tags.add("ongoing")
    if status == "Ending Soon": tags.add("ending")
    x["tags"] = sorted(tags)
    return x

def jsonld_events(soup, key, today):
    out = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(node.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                if "@graph" in obj and isinstance(obj["@graph"], list):
                    stack.extend(obj["@graph"])
                typ = obj.get("@type")
                types = typ if isinstance(typ, list) else [typ]
                if "Event" in types:
                    loc = obj.get("location", {})
                    venue = loc.get("name","") if isinstance(loc, dict) else ""
                    start, end = obj.get("startDate",""), obj.get("endDate","")
                    period = ""
                    try:
                        sd = datetime.fromisoformat(str(start).replace("Z","+00:00")).date() if start else None
                        ed = datetime.fromisoformat(str(end).replace("Z","+00:00")).date() if end else sd
                        if sd and ed:
                            period = f"{sd.isoformat()} to {ed.isoformat()}"
                    except Exception:
                        pass
                    offers = obj.get("offers", {})
                    price = ""
                    if isinstance(offers, dict) and offers.get("price") is not None:
                        price = str(offers.get("price"))
                    if obj.get("isAccessibleForFree") is True or price in ("0", "0.0", "0.00"):
                        price = "Free"
                    out.append(clean_event({
                        "title": obj.get("name",""),
                        "category": "Event",
                        "overview": BeautifulSoup(str(obj.get("description","")), "html.parser").get_text(" "),
                        "period": period,
                        "time": "Check official information",
                        "venue": venue,
                        "adult": price or "Check official information",
                        "link": obj.get("url","") or SOURCES[key]["url"],
                    }, key, today))
            elif isinstance(obj, list):
                stack.extend(obj)
    return [e for e in out if e["title"]]

def text_blocks(soup):
    # Select the smallest usable card, including sites using compound class names.
    selectors = "article, [class*='card'], [class*='event'], [class*='listing'], .item, li"
    blocks = []
    seen = set()
    candidates = list(soup.select(selectors))
    # Some listings have no semantic card classes. Walk a bounded number of
    # ancestors from each event heading instead of treating the whole page as an event.
    for heading in soup.select("h2, h3, h4, h5"):
        candidates.extend(list(heading.parents)[:4])
    for el in candidates:
        if id(el) in seen or el.name in ("html", "body", "script", "style", "nav", "footer") or el.find_parent(["nav", "footer"]):
            continue
        seen.add(id(el))
        if len(el.find_all(["h1", "h2", "h3", "h4", "h5"])) > 1:
            continue
        txt = norm(el.get_text(" ", strip=True))
        if 25 <= len(txt) <= 2500:
            blocks.append((el, txt))
    return blocks

def source_specific_events(soup, key, today):
    """Extract only cards with a title, explicit dates and an official event link."""
    out = []
    seen = set()
    for el, txt in text_blocks(soup):
        # The museum also promotes touring exhibitions held overseas.
        if key == "national_museum" and re.search(r"\b(?:Canterbury Museum|Christchurch,?\s+New Zealand)\b", txt, re.I):
            continue
        period = extract_period(txt)
        if not period:
            continue
        heading = el.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        if not heading:
            heading = el.find("a")
        title = norm(heading.get_text(" ", strip=True) if heading else "")
        if not title or len(title) > 180:
            continue
        lk = heading if heading.name == "a" and heading.get("href") else heading.find("a", href=True)
        if not lk:
            parent_link = heading.find_parent("a", href=True)
            lk = parent_link if parent_link and (parent_link is el or parent_link in el.descendants) else None
        if not lk:
            continue
        link = urljoin(SOURCES[key]["url"], lk["href"])
        if not official_link(link, key):
            if key == "visit_singapore":
                link = SOURCES[key].get("listing_url", SOURCES[key]["url"])
            else:
                continue
        if link.split("#")[0].rstrip("/") == SOURCES[key]["url"].split("#")[0].rstrip("/"):
            continue
        ident = (title.lower(), period.lower())
        if ident in seen:
            continue
        seen.add(ident)
        out.append(clean_event({
            "title": title,
            "category": "Event / Exhibition",
            "overview": txt[:500],
            "period": period,
            "time": "Check official information",
            "venue": SOURCES[key]["name"],
            "link": link,
        }, key, today))
    return out

def section_events(soup, key, today):
    """NLB's current-exhibitions page uses headings followed by prose, not cards."""
    if key != "nlb":
        return []
    events = []
    for heading in soup.find_all("h2"):
        title = norm(heading.get_text(" ", strip=True))
        if not title or "exhibitions" == title.lower().strip():
            continue
        nodes = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in ("h1", "h2", "hr"):
                break
            nodes.append(str(sibling))
        section = BeautifulSoup("".join(nodes), "html.parser")
        text = norm(section.get_text(" ", strip=True))
        period = extract_period(text)
        if not period or not re.search(r"\bexhibition\b", text, re.I):
            continue
        identifier = heading.get("id")
        link = SOURCES[key]["url"] + ("#" + identifier if identifier else "")
        paragraphs = [norm(p.get_text(" ", strip=True)) for p in section.find_all("p")]
        overview = next((p for p in paragraphs if len(p) >= 70), "")
        if not overview:
            continue
        venue = next((norm(li.get_text(" ", strip=True)) for li in section.find_all("li")
                      if re.search(r"\b(?:Level|Library|Building)\b", li.get_text(" "), re.I)), "")
        events.append(clean_event({"title":title, "period":period, "overview":overview[:700],
                                   "venue":venue, "category":"Exhibition", "link":link}, key, today))
    return events


def gardens_events(soup, key, today):
    if key != "gardens":
        return []
    events = []
    for card in soup.select("a.programme-tile[href]"):
        heading = card.select_one(".title h6")
        dates = card.select_one(".gbb-event")
        if not heading or not dates:
            continue
        title = norm(heading.get_text(" ", strip=True))
        period = extract_period(dates.get_text(" ", strip=True))
        link = urljoin(SOURCES[key]["url"], card["href"])
        if not title or not period or not official_link(link, key):
            continue
        def field(selector):
            el = card.select_one(selector)
            return norm(el.get_text(" ", strip=True)) if el else ""
        e = clean_event({"title":title, "period":period, "link":link,
                         "overview":field(".detail"), "time":field(".gbb-hours"),
                         "venue":field(".gbb-location"), "category":"Event / Exhibition"}, key, today)
        if not is_expired(e, today):
            events.append(e)
    return events


def parse_source_html(html, key, today):
    soup = BeautifulSoup(html, "html.parser")
    events = jsonld_events(soup, key, today)
    if not events:
        events = source_specific_events(soup, key, today) + section_events(soup, key, today) + gardens_events(soup, key, today)
    return dedupe([e for e in events if not is_expired(e, today)])


def esplanade_detail_links(listing_html):
    """Return official Esplanade event/detail links found on a listing/festival page."""
    soup = BeautifulSoup(listing_html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(SOURCES["esplanade"]["url"], a["href"]).split("#")[0].split("?")[0]
        if not official_link(href, "esplanade") or not urlparse(href).path.startswith("/whats-on/"):
            continue
        if href.rstrip("/") in {
            SOURCES["esplanade"]["url"].rstrip("/"),
            "https://www.esplanade.com/whats-on/festivals-and-series",
        }:
            continue
        if href not in links:
            links.append(href)
    return links

def parse_esplanade_detail(html, url, today):
    """Extract richer fields from an Esplanade event detail page conservatively."""
    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" ", strip=True))
    h1 = soup.find("h1")
    title = norm(h1.get_text(" ", strip=True) if h1 else "")
    if not title:
        return None

    # Prefer JSON-LD if present, then enrich from visible detail-page text.
    candidates = jsonld_events(soup, "esplanade", today)
    event = candidates[0] if candidates else clean_event({
        "title": title, "category": "Event", "overview": "", "period": "",
        "time": "Check official information", "venue": "Esplanade – Theatres on the Bay",
        "link": url,
    }, "esplanade", today)
    event["title"] = title
    event["link"] = url

    # Esplanade detail pages visibly expose compact date, venue, duration, Free/Tickets from.
    date_patterns = [
        r"\b\d{1,2}\s*&\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        r"\b\d{1,2}\s*[–—-]\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    ]
    if not event.get("period"):
        for pat in date_patterns:
            m = re.search(pat, text)
            if m:
                event["period"] = m.group(0)
                break

    # Venue names on current Esplanade pages.
    venues = re.findall(
        r"\b(?:Esplanade (?:Concert Hall|Theatre|Concourse|Recital Studio|Annexe Studio)|"
        r"Singtel Waterfront Theatre at Esplanade|DBS Foundation Outdoor Theatre at Esplanade|"
        r"Chillout Stage \(Esplanade Concourse\)|Arena \(DBS Foundation Outdoor Theatre at Esplanade\))\b",
        text, re.I)
    if venues:
        event["venue"] = norm(venues[0])

    dm = re.search(r"\b(\d+\s*hrs?(?:\s*\d+\s*mins?)?|\d+\s*mins?)\b", text, re.I)
    if dm:
        event["duration"] = norm(dm.group(1))

    if re.search(r"\bFree\b", text[:1200], re.I):
        event["adult"] = event["senior"] = event["child"] = "Free"
        event["tags"] = sorted(set(event.get("tags", [])) | {"free"})
    else:
        tm = re.search(r"Tickets from\s*\$([0-9]+(?:\.[0-9]{1,2})?)", text, re.I)
        if tm:
            event["adult"] = f"From ${tm.group(1)}"
            # Do not invent concession prices.
            event["senior"] = "Check official information"
            event["child"] = "Check official information"

    # Synopsis: first substantial paragraph after a Synopsis heading, if available.
    syn = soup.find(lambda tag: tag.name in ("h2","h3") and "synopsis" in tag.get_text(" ",strip=True).lower())
    if syn:
        nxt = syn.find_next("p")
        if nxt:
            desc = norm(nxt.get_text(" ", strip=True))
            if len(desc) >= 40:
                event["overview"] = desc[:700]

    # Time: collect visible HH:MM AM/PM occurrences, unique.
    times=[]
    for m in re.finditer(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", text, re.I):
        v=norm(m.group(0))
        if v.lower() not in [x.lower() for x in times]:
            times.append(v)
    if times:
        event["time"]=", ".join(times[:6])

    event = clean_event(event, "esplanade", today)
    # Category/festival pages can contain unrelated prices and dates in navigation.
    # Require event-specific detail before allowing them into a production feed.
    if not event["overview"] or not event["period"] or not venues:
        return None
    return None if is_expired(event, today) else event

def fetch_esplanade_two_stage(listing_html, today, fetcher, max_details=80):
    """Follow listing -> festival/category -> performance, with a bounded request budget."""
    pending = esplanade_detail_links(listing_html)
    seen = set()
    events, errors = [], []
    while pending and len(seen) < max_details:
        batch = [u for u in dict.fromkeys(pending) if u not in seen][:max_details-len(seen)]
        pending = []
        if not batch:
            break
        seen.update(batch)
        def read(url):
            try:
                return url, fetcher(url), None
            except Exception as exc:
                return url, "", str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read, batch))
        for url, detail, error in results:
            if error:
                errors.append((url, error))
                continue
            event = parse_esplanade_detail(detail, url, today)
            if event:
                events.append(event)
            else:
                # Landing pages are discovery pages, not performances.
                pending.extend(u for u in esplanade_detail_links(detail) if u not in seen)
    return dedupe(events), errors

DETAIL_PATHS = {
    "eventbrite": re.compile(r"^/e/[^/]+/?$"),
    "allevents": re.compile(r"^/singapore/[^/]+/[^/]+/?$"),
    "nlb_calendar": re.compile(r"^/event/\d+/?$"),
    "national_museum": re.compile(r"^/whats-on/(?:exhibition|programme)/[^/]+(?:/events)?/?$"),
    "visit_singapore": re.compile(r"^/whats-happening/all-happenings/events/[^/]+/?$"),
    "catch_sg": re.compile(r"^/Event/(?:[^/]+/)*[^/]*\d{8,}[^/]*$"),
    "gardens": re.compile(r"^/en/things-to-do/calendar-of-events/[^/]+\.html$"),
    "national_gallery": re.compile(r"^/sg/en/(?:exhibitions|whats-on|programmes)/[^/]+\.html$"),
    "ura": re.compile(r"^/get-involved/events-and-exhibitions/[^/]+/?$"),
    "artscience": re.compile(r"^/museum/(?:exhibitions|events|programmes)/[^/]+\.html$"),
}

def parse_official_detail(html, url, key, today):
    if key in {"eventbrite", "allevents", "nlb_calendar"}:
        import additional_sources
        return additional_sources.parse(html, key, today, SOURCES[key], clean_event, base_url=url)
    soup = BeautifulSoup(html, "html.parser")
    structured = jsonld_events(soup, key, today)
    if structured:
        if key == "catch_sg":
            structured = [e for e in structured if e.get("adult") in ("0", "0.0", "0.00", "Free")]
            for e in structured:
                e["adult"] = e["senior"] = e["child"] = "Free"
        if structured:
            return [e for e in structured if not is_expired(e, today)]
    for node in soup.select("nav, header, footer, aside, script, style"):
        node.decompose()
    main = soup if key == "catch_sg" else soup.find("main") or soup
    h1 = main.find("h1")
    meta_title = soup.find("meta", property="og:title")
    title = norm(h1.get_text(" ", strip=True) if h1 else meta_title.get("content", "") if meta_title else "")
    if key == "catch_sg":
        title = norm(meta_title.get("content", "") if meta_title else soup.title.get_text(" ", strip=True) if soup.title else title)
    title = title.split(" | ")[0]
    if not title or len(title) > 200:
        return []
    text = norm(main.get_text(" ", strip=True))
    # Prefer labelled visitor dates. Never use a publication timestamp as an event date.
    labels = [norm(el.get_text(" ", strip=True)) for el in main.find_all(["li", "p"])
              if re.search(r"\b(?:When|Date):", el.get_text(" "), re.I)]
    period = next((extract_period(s) for s in labels if extract_period(s)), "")
    if key == "catch_sg":
        details = re.search(r"\bEvent Details\b", text, re.I)
        date_scope = text[details.end():] if details else text
        date_field = re.search(r"\bDate\s*:?\s*(?=\d)(.*?)(?=\b(?:Opening Hours|Location|Tickets|Contact|FIND OUT MORE)\b|$)", date_scope, re.I)
        period = extract_period(date_field.group(1)) if date_field else ""
    elif not period:
        match = MONTH_FIRST_RANGE.search(text) or DATE_RANGE.search(text) or ISO_RANGE.search(text) or SHORT_DATE_RANGE.search(text)
        period = match.group(0) if match else ""
    if not period:
        return []
    paragraphs = [norm(p.get_text(" ", strip=True)) for p in main.find_all("p")]
    overview = next((p for p in paragraphs if len(p) >= 80 and not re.search(r"cookie|newsletter|privacy", p, re.I)), "")
    if key == "catch_sg":
        description = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name":"description"})
        if description and len(norm(description.get("content", ""))) >= 40:
            overview = norm(description["content"])
    if not overview:
        return []
    if key == "catch_sg":
        ticket_section = re.search(r"\bTickets\s*(?:&|and)\s*Prices\b(.*?)(?=\b(?:Terms\s*(?:&|and)\s*Conditions|Event Details)\b|$)", text, re.I)
        price_scope = ticket_section.group(1).strip() if ticket_section else text
        free = bool(re.fullmatch(r"Free[.!]?", price_scope, re.I) or re.search(r"\b(?:admission is free|free admission|(?:tickets?\s*(?:&\s*)?prices?|price|admission)\s*(?:is|:|-)?\s*(?:free|\$?0(?:\.00)?))\b", price_scope, re.I))
        if not free:
            return []
    e = clean_event({"title":title, "overview":overview[:700], "period":period,
                     "category":"Event / Exhibition", "link":url}, key, today)
    if key == "catch_sg":
        e["adult"] = e["senior"] = e["child"] = "Free"
        e["tags"] = sorted(set(e["tags"]) | {"free"})
    return [] if is_expired(e, today) else [e]

def fetch_official_details(listing_html, key, today, fetcher, limit=12):
    if key in {"eventbrite", "allevents", "nlb_calendar"}:
        limit = 60
    pattern = DETAIL_PATHS.get(key)
    if not pattern:
        return []
    soup = BeautifulSoup(listing_html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        url = urljoin(SOURCES[key]["url"], a["href"]).split("#")[0].split("?")[0]
        if official_link(url, key) and pattern.fullmatch(urlparse(url).path) and url not in links:
            links.append(url)
    def read(url):
        try:
            events = parse_official_detail(fetcher(url), url, key, today)
            if not events and key in ("catch_sg", "nlb_calendar") and fetcher is fetch:
                events = parse_official_detail(render_source(url), url, key, today)
            return events
        except Exception as exc:
            print(f"Detail unavailable {url}: {str(exc)[:160]}", flush=True)
            return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dedupe([e for result in pool.map(read, links[:limit]) for e in result])


def fetch(url, timeout=30):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-SG,en;q=0.9", "Accept": "text/html,application/xhtml+xml"}, timeout=timeout)
    r.raise_for_status()
    if r.status_code != 200 or not r.content:
        raise ValueError(f"Official source returned HTTP {r.status_code} with no usable page; keeping previous events")
    soup = BeautifulSoup(r.text, "html.parser")
    page_text = norm(soup.get_text(" ", strip=True))
    if "verify that you're not a robot" in page_text.lower() or "enable javascript and then reload" in page_text.lower():
        raise ValueError("Official source requires browser verification; keeping previous events")
    print(f"Fetched {r.url}: HTTP {r.status_code}, {len(r.content)} bytes, "
          f"{len(text_blocks(soup))} candidate cards; title={norm(soup.title.get_text() if soup.title else '')[:100]}", flush=True)
    return r.text

def render_source(url):
    """Read a public JavaScript listing; never interact with verification challenges."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        # Use Chromium's normal protocol negotiation for Marina Bay Sands.
        # Keep the existing compatibility setting for the other working sources.
        args = [] if (urlparse(url).hostname or "").endswith("marinabaysands.com") else ["--disable-http2"]
        browser = p.chromium.launch(headless=True, args=args)
        try:
            page = browser.new_page(locale="en-SG")
            if any(name in (urlparse(url).hostname or "") for name in ("onepa.gov.sg", "nparks.gov.sg")):
                page.on("response", lambda response: print(
                    f"Browser resource {response.status}: {response.url[:260]}",
                    flush=True) if any(word in response.url.lower() for word in ("api", "event", "search", "graphql")) else None)
            # Optional resources must not prevent reading an already loaded document.
            response = page.goto(url, wait_until="commit", timeout=45000)
            page.locator("body").wait_for(state="attached", timeout=20000)
            if response and response.status >= 400:
                raise ValueError(f"Official browser page returned HTTP {response.status}")
            def check_verification():
                text = page.locator("body").inner_text(timeout=10000).lower()
                if any(s in text for s in ("verify that you're not a robot", "verify you are human", "checking your browser")):
                    raise ValueError("Official source requires browser verification; keeping previous events")
            check_verification()
            # Allow the public listing's JavaScript to fill event cards.
            try:
                if "catch.sg" in url and "/Event/" in urlparse(url).path:
                    page.wait_for_function("() => document.body.innerText.includes('Event Details')", timeout=20000)
                elif (urlparse(url).hostname or "").endswith("onepa.gov.sg"):
                    page.wait_for_function("""() => [...document.querySelectorAll('a[href]')].filter(a =>
                        /\/events\/(?!search|neighbourhood-events$|arts-and-culture$|active-ageing$)/i.test(a.getAttribute('href') || '')).length > 2""", timeout=25000)
                elif (urlparse(url).hostname or "").endswith("nparks.gov.sg"):
                    page.wait_for_function("""() => document.querySelectorAll('article, [class*="event"], [class*="card"], [class*="listing"]').length > 20""", timeout=25000)
                elif any(host in (urlparse(url).hostname or "") for host in ("eventbrite", "allevents", "libcal")):
                    page.wait_for_function("""() => [...document.querySelectorAll('a[href]')].some(a =>
                        /\\/e\\/|\\/singapore\\/[^/]+\\/|\\/event\\/\\d+/.test(a.getAttribute('href') || ''))""", timeout=25000)
                else:
                    page.wait_for_function("""() => [...document.querySelectorAll('a[href]')].some(a =>
                        /calendar-of-events\\/.+|museum\\/(exhibitions|events|programmes)\\/.+|\\/Event\\/.*\\d{8}|whats-happening\\/.+/.test(a.getAttribute('href') || ''))""",
                        timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            check_verification()
            html = page.content()
            print(f"Rendered public listing {page.url}: {len(html)} characters", flush=True)
            return html
        finally:
            browser.close()


# Public official alternatives only. A challenge page is never a successful read.
OFFICIAL_LISTING_ALTERNATIVES = {
    "artscience": ["https://www.marinabaysands.com/museum.html"],
    "nparks": ["https://juronglakegardens.nparks.gov.sg/things-to-do/upcoming-events/", "https://sbg.nparks.gov.sg/events/"],
}
BROWSER_SOURCES = {"gardens", "artscience", "visit_singapore", "catch_sg", "national_museum", "eventbrite", "allevents", "nlb_calendar", "onepa", "nparks"}


def read_source_events(cfg, key, today, fetcher):
    urls = list(dict.fromkeys([cfg.get("listing_url", cfg["url"])]
                             + OFFICIAL_LISTING_ALTERNATIVES.get(key, [])))
    errors = []

    def extract(html):
        events = parse_source_html(html, key, today)
        # Read official detail links before rendering the JavaScript listing.
        return events or fetch_official_details(html, key, today, fetcher)

    for url in urls:
        try:
            fresh = extract(fetcher(url))
            if fresh:
                return fresh
            errors.append(f"{url}: zero usable events")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print(f"Official listing unavailable: {errors[-1]}", flush=True)
    if fetcher is fetch and key in BROWSER_SOURCES:
        for url in urls:
            try:
                fresh = extract(render_source(url))
                if fresh:
                    return fresh
                errors.append(f"{url}: zero usable events after browser read")
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                print(f"Official browser read unavailable: {errors[-1]}", flush=True)
    reason = errors[-1] if errors else "zero usable events"
    raise ValueError(reason + "; preserving previous source records")


def update(previous, today=None, fetcher=fetch):
    today = today or datetime.now(SGT).date()
    all_events = []
    report = {}
    for key, cfg in SOURCES.items():
        prev = previous_for(previous, key)
        try:
            if key == "esplanade":
                html = fetcher(cfg.get("listing_url", cfg["url"]))
                fresh, detail_errors = fetch_esplanade_two_stage(html, today, fetcher)
                # If no detail links/results are usable, fall back to conservative listing parser.
                if not fresh:
                    fresh = parse_source_html(html, key, today)
                if not fresh:
                    raise ValueError("zero usable Esplanade events; preserving previous source records")
                all_events.extend(fresh)
                report[key] = {"status":"fresh", "count":len(fresh),
                               "detail_errors":len(detail_errors)}
            else:
                fresh = read_source_events(cfg, key, today, fetcher)
                all_events.extend(fresh)
                report[key] = {"status":"fresh", "count":len(fresh)}
        except Exception as exc:
            fallback = [e for e in prev if not is_expired(e, today)]
            all_events.extend(fallback)
            report[key] = {"status":"fallback", "count":len(fallback), "error":str(exc)[:180]}

    merged = dedupe(all_events)
    # Preserve unknown/legacy-source records, subject only to reliable expiry.
    known = set(SOURCES)
    legacy = [copy.deepcopy(e) for e in previous.get("events", [])
              if source_key(e) not in known and not is_expired(e, today)]
    merged = dedupe(merged + legacy)

    if not merged and previous.get("events"):
        raise RuntimeError("Safety stop: refusing to replace a non-empty valid feed with an empty feed")

    payload = {
        "updated_at": datetime.now(SGT).isoformat(timespec="seconds"),
        "events": sorted(merged, key=lambda e: (e.get("status")=="Upcoming", e.get("title","").lower())),
        "source_report": report,
    }
    return payload

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args(argv)
    path = Path(args.output)
    previous = load_previous(path)
    payload = update(previous)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print(json.dumps(payload["source_report"], indent=2))
        print(f"DRY RUN: {len(payload['events'])} events; {path} not modified")
        return 0
    if not payload["events"]:
        raise RuntimeError("Safety stop: refusing to create an empty production feed")
    path.write_text(encoded, encoding="utf-8")
    print(f"Wrote {len(payload['events'])} events to {path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
