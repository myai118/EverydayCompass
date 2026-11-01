import json
import sys
from pathlib import Path
from datetime import date
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_events as ue

TODAY = date(2026,11,1)
KEYS = ["sccc","gardens","esplanade","national_museum","national_gallery","ura","nlb","visit_singapore","catch_sg","allevents","nlb_calendar","nparks"]

@pytest.mark.parametrize("key", KEYS)
def test_expired_structured_calendar_is_successful_empty_for_every_source(key):
    node = {"@type":"ExhibitionEvent","name":"Finished", "startDate":"2026-10-01", "endDate":"2026-10-26"}
    html = '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    assert ue.verified_empty_source(html,key,TODAY)
    node["endDate"] = "2026-12-01"
    assert not ue.verified_empty_source('<script type="application/ld+json">'+json.dumps(node)+'</script>',key,TODAY)

def test_explicit_empty_page_requires_loaded_heading():
    assert ue.verified_empty_source("<main><h1>What's On</h1><p>No upcoming events</p></main>","demo",TODAY)
    assert not ue.verified_empty_source("<p>No upcoming events</p>","demo",TODAY)

@pytest.mark.parametrize("text", ["Access denied", "Verify you are human", "Loading...", "Page not found"])
def test_errors_are_never_successful_empty(text):
    assert not ue.verified_empty_source("<h1>What's On</h1><p>No upcoming events</p>"+text,"demo",TODAY)

def test_unknown_dates_or_empty_shell_do_not_hide_parser_failures():
    assert not ue.verified_empty_source("<h1>What's On</h1><p>Explore our events</p>","demo",TODAY)
    assert not ue.verified_empty_source('<script type="application/ld+json">{"@type":"Event","name":"Dates missing"}</script>',"demo",TODAY)
