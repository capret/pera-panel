from io import BytesIO
from http.client import IncompleteRead
import json
from urllib.error import URLError
from urllib.parse import parse_qs

import pytest

from pera_panel import workshop
from pera_panel.storage import PanelError


def item(identifier="378160973", **changes):
    return {"publishedfileid": identifier, "result": 1, "consumer_app_id": 322330,
            "title": "Global Positions", "description": "[h1]Map sharing[/h1]\n[b]Find friends[/b] &amp; explore.",
            "time_updated": 1700000000, **changes}


def steam(monkeypatch, items):
    calls = []

    def respond(request, timeout):
        calls.append((request, timeout))
        return BytesIO(json.dumps({"response": {"publishedfiledetails": items}}).encode())

    monkeypatch.setattr(workshop, "urlopen", respond)
    return calls


def test_batch_lookup_matches_ids_and_caches_successes(monkeypatch):
    calls = steam(monkeypatch, [item("99999", title="中文模组"), item()])
    catalog = workshop.Workshop()
    details = catalog.details(["378160973", "99999"])
    assert [row["id"] for row in details] == ["378160973", "99999"]
    assert details[0] == {"id": "378160973", "status": "available", "title": "Global Positions",
                          "description": "Map sharing Find friends & explore.",
                          "updated_at": "2023-11-14T22:13:20+00:00"}
    assert details[1]["title"] == "中文模组"
    request, timeout = calls[0]
    assert request.full_url == workshop.DETAILS_URL
    assert request.get_method() == "POST" and timeout == 8
    assert parse_qs(request.data.decode()) == {"itemcount": ["2"], "publishedfileids[0]": ["378160973"],
                                              "publishedfileids[1]": ["99999"]}
    assert catalog.details(["99999", "378160973"]) == list(reversed(details))
    assert len(calls) == 1


def test_missing_private_wrong_game_and_malformed_items_are_independent(monkeypatch):
    steam(monkeypatch, [item(), item("11111", result=9), item("22222", consumer_app_id=440),
                        item("33333", title=None), item("44444", description={}, time_updated="bad"), None])
    details = workshop.Workshop().details(["378160973", "11111", "22222", "33333", "44444", "55555"])
    assert [row["status"] for row in details] == ["available", "unavailable", "unavailable", "unavailable",
                                                 "available", "unavailable"]
    assert details[4]["description"] == "" and details[4]["updated_at"] is None


@pytest.mark.parametrize("ids", [None, {}, "378160973", ["abcde"], ["1234"], ["012345"],
                                ["12345\n"], ["12345", "12345"], ["9" * 21], [None], [True],
                                [str(10000 + i) for i in range(101)]])
def test_invalid_ids_rejected_before_network(monkeypatch, ids):
    calls = steam(monkeypatch, [])
    with pytest.raises(PanelError):
        workshop.Workshop().details(ids)
    assert not calls


def test_empty_list_needs_no_network(monkeypatch):
    calls = steam(monkeypatch, [])
    assert workshop.Workshop().details([]) == []
    assert not calls


@pytest.mark.parametrize("payload", [b"not JSON", b"null", b"[]", b"{}", b'{"response": null}',
                                    b'{"response": {"publishedfiledetails": {}}}', b"x" * 1025])
def test_invalid_or_oversized_response_is_recoverable(monkeypatch, payload):
    monkeypatch.setattr(workshop, "MAX_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(workshop, "urlopen", lambda *args, **kwargs: BytesIO(payload))
    assert workshop.Workshop().details(["378160973"]) == [{"id": "378160973", "status": "error"}]


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timed out"), OSError("connection lost"),
                                 IncompleteRead(b"truncated")])
def test_network_failure_can_be_retried(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(workshop, "urlopen", fail)
    catalog = workshop.Workshop()
    assert catalog.details(["378160973"]) == [{"id": "378160973", "status": "error"}]
    calls = steam(monkeypatch, [item()])
    assert catalog.details(["378160973"])[0]["title"] == "Global Positions"
    assert len(calls) == 1


def test_expired_metadata_survives_outage_and_refreshes(monkeypatch):
    now = 100.0
    monkeypatch.setattr(workshop.time, "monotonic", lambda: now)
    steam(monkeypatch, [item()])
    catalog = workshop.Workshop()
    original = catalog.details(["378160973"])[0]
    now += workshop.CACHE_SECONDS + 1
    monkeypatch.setattr(workshop, "urlopen", lambda *args, **kwargs: BytesIO(b"bad response"))
    assert catalog.details(["378160973", "99999"]) == [
        {**original, "stale": True}, {"id": "99999", "status": "error"}]
    steam(monkeypatch, [item(title="Renamed mod")])
    assert catalog.details(["378160973"])[0]["title"] == "Renamed mod"
    assert "stale" not in catalog.details(["378160973"])[0]


def test_cache_is_bounded_and_deleted_items_do_not_keep_old_details(monkeypatch):
    monkeypatch.setattr(workshop, "MAX_CACHE_ITEMS", 2)
    calls = steam(monkeypatch, [item("11111"), item("22222"), item("33333")])
    catalog = workshop.Workshop()
    assert len(catalog.details(["11111", "22222", "33333"])) == 3
    assert list(catalog.cache) == ["22222", "33333"]
    catalog.details(["11111"])
    assert len(calls) == 2
    catalog.cache["11111"] = (0, catalog.cache["11111"][1])
    steam(monkeypatch, [item("11111", result=9)])
    assert catalog.details(["11111"]) == [{"id": "11111", "status": "unavailable"}]
    assert "11111" not in catalog.cache


def test_description_is_bounded_plain_text_and_bad_dates_are_ignored():
    info = workshop.parse_details(item(title="t" * 400, description="<b>Intro</b> [url=https://example.org]text[/url] " + "x" * 600,
                                        time_updated=10**100))
    assert len(info["title"]) == 300 and len(info["description"]) == 500
    assert info["description"].startswith("Intro text ") and info["description"].endswith("…")
    assert info["updated_at"] is None
    assert workshop.parse_details(item(title="[DST] 中文模组"))["title"] == "[DST] 中文模组"


def test_metadata_endpoint_requires_auth_and_csrf(client, monkeypatch):
    calls = steam(monkeypatch, [item()])
    assert client.post("/api/workshop/details", json={"ids": ["378160973"]}).status_code == 401
    assert not calls


def test_lookup_works_for_unsaved_mods_without_world_mutation(client, auth, service, monkeypatch):
    calls = steam(monkeypatch, [item()])
    assert client.post("/api/workshop/details", json={"ids": ["378160973"]}).status_code == 403
    response = client.post("/api/workshop/details", json={"ids": ["378160973"]}, headers=auth)
    assert response.status_code == 200 and response.json["mods"][0]["title"] == "Global Positions"
    assert len(calls) == 1 and not service.busy
    assert service.store.all() == [] and service.job_list() == []
    assert client.post("/api/workshop/details", json={"ids": ["bad"]}, headers=auth).status_code == 400
    assert client.post("/api/workshop/details", json=[], headers=auth).status_code == 400
