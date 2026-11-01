"""URA empty calendars must not be confused with unreadable pages."""
import sys
from datetime import date
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_whats_on as rw

CFG = {"ura": {"name": "URA", "url": "https://www.ura.gov.sg/get-involved/events-and-exhibitions/", "host": "ura.gov.sg"}}
LISTING = '<h1>Events &amp; Exhibitions</h1><a href="/get-involved/events-and-exhibitions/demo/">Demo Exhibitions</a>'
def read(detail):
    def fetch(url):
        return LISTING if url == CFG["ura"]["url"] else detail
    with rw.patched(CFG):
        return rw.ue.read_source_events(CFG["ura"], "ura", date(2026,11,1), fetch)

def test_expired_calendar_is_successful_empty():
    assert read('<main><h1>Demo</h1><p>Date: 12 August to 26 October 2026</p></main>') == []

def test_unknown_visitor_dates_are_not_successful_empty():
    with pytest.raises(ValueError, match="visitor dates"):
        read('<main><h1>Demo</h1><p>Published 12 August 2026</p></main>')

def test_unreadable_detail_keeps_failure():
    with pytest.raises(ValueError, match="structure"):
        read('<h2>Temporarily unavailable</h2>')

def test_current_exhibition_is_still_imported():
    result = read('<main><h1>New exhibition</h1><p>Date: 1 November to 20 December 2026</p><p>' + 'A public exhibition about Singapore architecture and the history of our urban development. ' * 2 + '</p></main>')
    assert len(result) == 1
    assert result[0]["title"] == "New exhibition"

def test_empty_success_updates_last_check_and_removes_expired_records():
    previous = {"events": [], "sources": [{"id":"ura", "last_success":"2026-10-26T06:32:00+08:00"}]}
    def fetch(url):
        return LISTING if url == CFG["ura"]["url"] else '<main><h1>Demo</h1><p>Date: Until 26 October 2026</p></main>'
    result = rw.run(previous, CFG, today=date(2026,11,1), fetcher=fetch)
    assert result["sources"][0]["status"] == "ok"
    assert result["sources"][0]["last_success"] != previous["sources"][0]["last_success"]
    assert not result["all_sources_failed"]
