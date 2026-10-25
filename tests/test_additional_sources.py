import importlib.util
import json
from datetime import date
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "scripts" / "additional_sources.py"
spec = importlib.util.spec_from_file_location("additional_test_reader", P)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
TODAY = date(2026, 10, 25)

def clean(event, key, today):
    return dict(event, source_key=key)

def node(**changes):
    event = {"@type": "Event", "name": "Community Workshop",
        "startDate": "2026-11-03T14:00:00+08:00", "endDate": "2026-11-03T15:00:00+08:00",
        "location": {"@type": "Place", "name": "Community Hall", "address": {"addressCountry": "SG"}},
        "url": "https://www.eventbrite.sg/e/community-workshop-tickets-123",
        "offers": {"price": "0", "priceCurrency": "SGD"}}
    event.update(changes)
    return event

def parse(event, key="eventbrite"):
    cfg = {"url": "https://www.eventbrite.sg/d/singapore/free--events/", "host": "eventbrite.sg"}
    if key == "nlb_calendar":
        cfg = {"url": "https://nlb.libcal.com/calendar/public", "host": "nlb.libcal.com"}
    html = '<script type="application/ld+json">' + json.dumps(event) + '</script>'
    return reader.parse(html, key, TODAY, cfg, clean)

def test_nested_free_event_and_singapore_session_time():
    got = parse({"@type": "ItemList", "itemListElement": [{"item": node()}]})
    assert len(got) == 1
    assert got[0]["adult"] == "Free"
    assert got[0]["time"] == "2:00pm – 3:00pm"
    assert got[0]["start_date"] == "2026-11-03"

def test_expired_and_cancelled_events_excluded():
    assert not parse(node(startDate="2026-10-01", endDate="2026-10-02"))
    assert not parse(node(eventStatus="https://schema.org/EventCancelled"))

def test_paid_unknown_and_free_gift_not_accepted_as_free_event():
    assert not parse(node(offers={"price": "20"}))
    assert not parse(node(offers={}, description="Buy a ticket and get a free gift"))
    assert not parse(node(offers=[{"price": "0"}, {"price": "20"}]))

def test_zero_lower_bound_is_not_proof_of_free_admission():
    assert not parse(node(offers={"lowPrice": "0", "highPrice": "50"}))
    assert not parse(node(offers={"lowPrice": "0"}))

def test_online_and_foreign_location_excluded():
    assert not parse(node(eventAttendanceMode="https://schema.org/OnlineEventAttendanceMode"))
    assert not parse(node(location={"@type": "Place", "name": "Hall", "address": {"addressCountry": "MY"}}))
    assert not parse(node(location={"@type": "VirtualLocation", "name": "Zoom"}))

def test_eventbrite_com_details_accepted():
    assert parse(node(url="https://www.eventbrite.com/e/community-workshop-tickets-123"))

def test_nlb_unconfirmed_admission_remains_unknown():
    got = parse(node(url="https://nlb.libcal.com/event/1234567", offers={},
        location={"@type": "Place", "name": "Central Library"}), "nlb_calendar")
    assert got[0]["adult"] == "Check official information"

def test_nlb_detail_date_and_registration_fields():
    html = """<main><h1>Reading Workshop In-Person</h1>
      <p>This session explores books and reading together in the community. Free admission for participants.</p>
      <dl><dt>Date:</dt><dd>Tuesday, November 3, 2026</dd>
      <dt>Time:</dt><dd>2:00 PM - 3:00 PM</dd>
      <dt>Location:</dt><dd>Central Library</dd></dl></main>"""
    event = reader.parse_libcal_detail(reader.BeautifulSoup(html, "html.parser"),
        "https://nlb.libcal.com/event/1234567", TODAY, clean)
    assert event["adult"] == "Free"
    assert event["start_date"] == "2026-11-03"
    assert event["venue"] == "Central Library"


def test_iso_dates_and_multilingual_event_identity():
    core_path = Path(__file__).resolve().parents[1] / "scripts" / "update_events.py"
    core_spec = importlib.util.spec_from_file_location("additional_core_test", core_path)
    core = importlib.util.module_from_spec(core_spec)
    core_spec.loader.exec_module(core)
    assert core.status_for("2026-11-03 to 2026-11-03", TODAY)[0] == "Upcoming"
    got = core.dedupe([{"title": "中文故事会", "venue": "图书馆"}, {"title": "华文读书会", "venue": "图书馆"}])
    assert len(got) == 2


def test_onepa_and_nparks_accept_only_explicit_free_active_cards():
    for key, host in (("onepa", "www.onepa.gov.sg"), ("nparks", "www.nparks.gov.sg")):
        cfg = {"url": f"https://{host}/events", "host": host, "name": key}
        html = f"""<main>
          <article><h3><a href="/events/free-walk">Free Walk</a></h3>
            <p>Saturday 31 October 2026</p><span>Free</span></article>
          <article><h3><a href="/events/paid-walk">Paid Walk</a></h3>
            <p>Saturday 31 October 2026</p><span>$12</span></article>
          <article><h3><a href="/events/old-walk">Old Walk</a></h3>
            <p>Saturday 1 August 2026</p><span>Free</span></article>
        </main>"""
        found = reader.parse(html, key, TODAY, cfg, clean)
        assert [event["title"] for event in found] == ["Free Walk"]
