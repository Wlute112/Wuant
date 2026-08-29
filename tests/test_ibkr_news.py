from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from quant.news.ibkr import IbkrNewsClient, _broad_tape_contract


class _FakeClient:
    def __init__(self) -> None:
        self.requests = []

    def isConnected(self) -> bool:  # noqa: N802 - mirrors IB API
        return True

    def reqMktData(self, *args) -> None:  # noqa: N802 - mirrors IB API
        self.requests.append(args)


def test_available_providers_drive_broad_and_contract_subscriptions():
    owner = IbkrNewsClient(
        lambda article: None,
        tickers=("SPY", "QQQ"),
        asset_class="equity",
    )
    client = _FakeClient()
    owner._client = client
    owner._receive_providers(
        [
            SimpleNamespace(providerCode="DJNL", providerName="Dow Jones"),
            SimpleNamespace(providerCode="BRFG", providerName="Briefing.com"),
        ]
    )

    assert len(client.requests) == 4
    broad = [request for request in client.requests if request[1].secType == "NEWS"]
    contracts = [request for request in client.requests if request[1].secType == "STK"]
    assert {request[1].symbol for request in broad} == {
        "BRFG:BRFG_ALL",
        "DJNL:DJNL_ALL",
    }
    assert {request[1].symbol for request in contracts} == {"SPY", "QQQ"}
    assert all(request[2] == "mdoff,292:BRFG+DJNL" for request in contracts)


def test_provider_allowlist_cannot_create_unavailable_entitlement():
    owner = IbkrNewsClient(
        lambda article: None,
        tickers=("SPY",),
        asset_class="equity",
        provider_allowlist=("NOT_RETURNED",),
    )
    client = _FakeClient()
    owner._client = client
    owner._receive_providers(
        [SimpleNamespace(providerCode="BRFG", providerName="Briefing.com")]
    )
    assert client.requests == []
    assert owner.status()["providers"] == {}


def test_millisecond_headline_timestamp_and_contract_scope_are_normalized():
    received = []
    owner = IbkrNewsClient(received.append, tickers=("SPY",), asset_class="equity")
    owner._subscription_scopes[123] = "SPY"
    owner._providers["BRFG"] = "Briefing.com"
    epoch_seconds = int(datetime.now(timezone.utc).timestamp()) - 10

    owner._receive_headline(
        123,
        epoch_seconds * 1000,
        "BRFG",
        "article-1",
        "SPY beats estimates",
        "",
    )

    assert len(received) == 1
    assert received[0].symbols == ("SPY",)
    assert received[0].published_at.timestamp() == epoch_seconds
    assert _broad_tape_contract("BRFG").exchange == "BRFG"
