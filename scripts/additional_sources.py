"""Conservative readers for free-event directories and the official GoLibrary calendar."""
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

SGT = timezone(timedelta(hours=8))
KEYS = {"eventbrite", "allevents", "nlb_calendar", "onepa", "nparks"}

def walk(value):
    if isinstance(value, list):
        for item in value:
            yield from walk(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "itemListElement", "item", "subEvent"):
            if key in value:
                yield from walk(value[key])

def free_status(node):
    offers = node.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    amounts = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            raw = offer.get(key)
            if raw is None:
                continue
            try:
                amounts.append(float(raw))
            except (TypeError, ValueError):
                return None
    if any(value > 0 for value in amounts):
        return False
    if node.get("isAccessibleForFree") is True or amounts and all(value == 0 for value in amounts):
        # A zero lower bound alone does not establish that the event is free.
        if any("lowPrice" in o and "highPrice" not in o and "price" not in o for o in offers if isinstance(o, dict)):
            return None
        return True
    return None

def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(SGT) if result.tzinfo else result.replace(tzinfo=SGT)
    except (TypeError, ValueError):
        return None

def clock(value):
    return f"{value.hour % 12 or 12}:{value.minute:02d}{'am' if value.hour < 12 else 'pm'}"

def allowed(url, key, cfg):
    host = (urlparse(url).hostname or "").lower()
    hosts = ["eventbrite.sg", "eventbrite.com"] if key == "eventbrite" else [cfg["host"]]
    return urlparse(url).scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)

def parse(html, key, today, cfg, clean_event, base_url=None):
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            nodes.extend(walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            pass
    results = []
    for node in nodes:
        kinds = node.get("@type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(isinstance(kind, str) and kind.endswith("Event") for kind in kinds):
            continue
        if str(node.get("eventStatus", "")).endswith("EventCancelled"):
            continue
        start = stamp(node.get("startDate"))
        end = stamp(node.get("endDate")) or start
        if not start or not end or end.date() < today:
            continue
        loc = node.get("location") or {}
        if isinstance(loc, list):
            loc = next((item for item in loc if isinstance(item, dict)), {})
        if not isinstance(loc, dict):
            loc = {"name": str(loc)}
        address = loc.get("address") or {}
        address_text = " ".join(str(v) for v in address.values()) if isinstance(address, dict) else str(address)
        country = address.get("addressCountry", "") if isinstance(address, dict) else ""
        if isinstance(country, dict):
            country = country.get("name", "")
        venue = str(loc.get("name") or "").strip()
        location_text = venue + " " + address_text
        if "OnlineEventAttendanceMode" in str(node.get("eventAttendanceMode", "")) or "VirtualLocation" in str(loc.get("@type", "")):
            continue
        if re.search(r"\b(?:online|zoom|webinar|virtual event)\b", location_text, re.I):
            continue
        if country and str(country).lower() not in ("sg", "singapore"):
            continue
        if key in {"eventbrite", "allevents"} and not country and "singapore" not in location_text.lower():
            continue
        free = free_status(node)
        if free is False or key != "nlb_calendar" and free is not True:
            continue
        title = BeautifulSoup(str(node.get("name") or ""), "html.parser").get_text(" ", strip=True)
        link = urljoin(base_url or cfg["url"], str(node.get("url") or base_url or ""))
        if not title or not allowed(link, key, cfg) or link.rstrip("/") == cfg["url"].rstrip("/"):
            continue
        description = BeautifulSoup(str(node.get("description") or ""), "html.parser").get_text(" ", strip=True)
        timing = clock(start) + (" – " + clock(end) if end != start else "") if re.search(r"T\d{2}:", str(node.get("startDate", ""))) else ""
        event = clean_event({
            "title": title, "overview": description[:700],
            "period": start.date().isoformat() + " to " + end.date().isoformat(),
            "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
            "time": timing, "venue": venue or address_text,
            "adult": "Free" if free is True else "Check official information",
            "category": "Event / Programme", "link": link,
            "tags": ["free"] if free is True else [],
        }, key, today)
        results.append(event)
    if key == "nlb_calendar" and not results and base_url and "/event/" in urlparse(base_url).path:
        event = parse_libcal_detail(soup, base_url, today, clean_event)
        if event:
            results.append(event)
    if key in {"onepa", "nparks"} and not results:
        results.extend(parse_free_cards(soup, key, today, cfg, clean_event))
    print(f"Directory {key}: {len(nodes)} structured objects; {len(results)} verified active in-person records", flush=True)
    return results

def parse_libcal_detail(soup, url, today, clean_event):
    fields = {}
    for term in soup.find_all("dt"):
        definition = term.find_next_sibling("dd")
        if definition:
            fields[term.get_text(" ", strip=True).lower().strip(": ")] = definition.get_text(" ", strip=True)
    def read_date(text):
        text = re.sub(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*", "", text or "")
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%I:%M %p, %A, %B %d, %Y", "%I:%M%p, %A, %B %d, %Y"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                pass
        return None
    start = read_date(fields.get("from") or fields.get("date"))
    end = read_date(fields.get("to")) or start
    title = soup.find("h1")
    venue = fields.get("location", "") or fields.get("branch", "")
    if not start or not end or end < today or not title or not venue or re.search(r"\b(?:online|zoom)\b", venue, re.I):
        return None
    main = soup.find("main") or soup
    body = main.get_text(" ", strip=True)
    free = bool(re.search(r"\b(?:free admission|admission is free|free,? volunteer-led workshop)\b", body, re.I))
    heading = title.get_text(" ", strip=True).replace("In-Person", "").strip()
    paragraphs = [p.get_text(" ", strip=True) for p in main.find_all("p")]
    description = next((p for p in paragraphs if len(p) >= 60), "See the official GoLibrary event page for programme and registration details.")
    return clean_event({"title": heading, "overview": description[:700],
        "period": start.isoformat() + " to " + end.isoformat(),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "time": fields.get("time", ""), "venue": venue, "category": "Library Programme",
        "adult": "Free" if free else "Check official information", "link": url,
        "tags": ["free"] if free else []}, "nlb_calendar", today)



def parse_free_cards(soup, key, today, cfg, clean_event):
    """Read only clearly free, dated, in-person cards from official SG government listings."""
    candidates = list(soup.select("article, [class*='event'], [class*='card'], [class*='listing'], li"))
    seen_nodes = {id(node) for node in candidates}
    for heading in soup.select("h2, h3, h4, h5"):
        for parent in list(heading.parents)[:4]:
            if id(parent) not in seen_nodes:
                candidates.append(parent)
                seen_nodes.add(id(parent))
    # Prefer small cards so page containers do not combine unrelated event details.
    candidates.sort(key=lambda node: len(node.get_text(" ", strip=True)))
    diagnostic = []
    for node in candidates:
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if "free" in text.casefold() or key == "onepa" and "event" in text.casefold():
            heading = node.find(["h1", "h2", "h3", "h4", "h5"])
            links = [a.get("href", "") for a in node.find_all("a", href=True)[:2]]
            diagnostic.append({"tag": node.name, "class": node.get("class"), "heading": heading.get_text(" ", strip=True) if heading else "", "links": links, "text": text[:420]})
            if len(diagnostic) >= 12:
                break
    print(f"Directory {key} card sample: {json.dumps(diagnostic, ensure_ascii=False)}", flush=True)
    results, seen = [], set()
    for card in candidates:
        if card.name in {"html", "body", "script", "style", "nav", "footer"} or card.find_parent(["nav", "footer"]):
            continue
        title_node = card.find(["h1", "h2", "h3", "h4", "h5"])
        if title_node and len(card.find_all(["h1", "h2", "h3", "h4", "h5"])) > 1:
            continue
        text = card.get_text(" ", strip=True)
        link_node = card if card.name == "a" and card.get("href") else card.find("a", href=True)
        if not title_node and not link_node:
            continue
        title = BeautifulSoup(title_node.get_text(" ", strip=True) if title_node else link_node.get_text(" ", strip=True), "html.parser").get_text(" ", strip=True)
        if not title_node:
            title = re.sub(r"\s+(?:EXHIBITIONS|WORKSHOP|GUIDED TOURS|PERFORMANCES|LECTURES/TALKS)\s*[|•·].*$", "", title, flags=re.I)
        trace = (key == "onepa" and "Free Walk" in text) or (key == "nparks" and "Pressing Plant Matters" in text)
        if trace: print("PARSER TRACE title", repr(title), "text", repr(text), flush=True)
        if not title or len(title) > 180 or len(text) > 1800:
            if trace: print("PARSER TRACE title/length rejected", flush=True)
            continue
        # Require a standalone free price/entry label; reject free gifts and paid tiers.
        labels = {re.sub(r"\s+", " ", item.get_text(" ", strip=True)).strip().casefold()
                  for item in card.find_all(True)}
        labels.add(re.sub(r"\s+", " ", text).strip().casefold())
        explicit_free = any(label in {
            "free", "free event", "free admission", "admission: free",
            "price: free", "fee: free", "ticket: free", "tickets: free",
            "free of charge", "free to attend"
        } for label in labels) or any(re.search(r"(?:\||[·•])\s*free\s*$", label, re.I) for label in labels)
        if not explicit_free or re.search(r"(?:S?\$|\bSGD\s*)[1-9]\d*(?:\.\d{1,2})?\b|\bpaid admission\b", text, re.I):
            continue
        if title_node:
            link_node = title_node if title_node.name == "a" and title_node.get("href") else title_node.find("a", href=True)
            if not link_node:
                link_node = title_node.find_parent("a", href=True)
        if not link_node:
            continue
        link = urljoin(cfg["url"], link_node["href"])
        if trace: print("PARSER TRACE link", link, "allowed", allowed(link, key, cfg), "base", cfg["url"], flush=True)
        if not allowed(link, key, cfg) or link.rstrip("/") == cfg["url"].rstrip("/"):
            if trace: print("PARSER TRACE link rejected", flush=True)
            continue
        clean_text = re.sub(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+", "", text, flags=re.I)
        month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        until = re.search(r"\buntil\s+(\d{1,2}\s+" + month + r"(?:\s+\d{4})?)", clean_text, re.I)
        iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", clean_text)
        human_dates = re.findall(r"\b\d{1,2}\s+" + month + r"(?:\s+\d{4})?\b|\b" + month + r"\s+\d{1,2},?(?:\s+\d{4})?\b", clean_text, re.I)
        raw_dates = [until.group(1)] if until else (iso_dates or human_dates)
        dates = []
        for raw in raw_dates:
            value = stamp(raw)
            if not value:
                for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d %b", "%d %B", "%b %d", "%B %d"):
                    try:
                        value = datetime.strptime(raw, fmt).replace(year=today.year, tzinfo=SGT)
                        break
                    except ValueError:
                        pass
            if value and value.date() not in dates:
                dates.append(value.date())
        if trace: print("PARSER TRACE dates", dates, "until", bool(until), flush=True)
        if not dates:
            if trace: print("PARSER TRACE no date", repr(raw_dates), flush=True)
            continue
        start, end = dates[0], dates[1] if len(dates) > 1 else dates[0]
        if until:
            start = min(today, end)
        if end < today:
            continue
        period = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
        ident = (title.casefold(), link)
        if ident in seen:
            continue
        seen.add(ident)
        results.append(clean_event({
            "title": title, "overview": re.sub(r"\s+", " ", text)[:700],
            "period": period, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "time": "Check official information", "venue": cfg["name"],
            "category": "Community Event" if key == "onepa" else "Parks Event",
            "adult": "Free", "tags": ["free"], "link": link
        }, key, today))
    print(f"Directory {key}: {len(results)} verified free active card records", flush=True)
    return results
