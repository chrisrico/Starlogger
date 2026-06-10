"""Manual commodity-trade parsing + archiving.

Run: .venv/bin/python -m pytest tests/test_trades.py  (or plain `python tests/test_trades.py`)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.archive import build_session_trades
from starlogger.state import State

# Real log lines (IDs intact). Note the format differs between buy and sell:
# buy carries price[...] and a bare "boxSize[..] | unitAmount[..]"; sell carries
# amount[...] and a bracket-wrapped "[boxSize[..] | unitAmount[..]]".
BUY = ('<2026-06-01T16:20:49.543Z> [Notice] <CEntityComponentCommodityUIProvider::SendCommodityBuyRequest> '
       'Sending SShopCommodityBuyRequest - playerId[204772152312] shopId[342890646018] '
       'shopName[SCShop_ht_delta_shubin_m_store] kioskId[342890646017] price[1067040.000000] '
       'shopPricePerCentiSCU[37.049999] resourceGUID[35121003-f1af-481a-b16f-7f48d8af0efb] '
       'autoLoading[0] quantity[28800.000000 cSCU] Cargo Box Data: boxSize[16.000000] | unitAmount[18] '
       '[Team_CoreGameplayFeatures][Shops][UI]\n')
SELL = ('<2026-06-01T03:46:57.282Z> [Notice] <CEntityComponentCommodityUIProvider::SendCommoditySellRequest> '
        'Sending SShopCommoditySellRequest - playerId[204772152312] shopId[286323287386] '
        'shopName[SCShop_Admin_lt_base_g] kioskId[286323287385] amount[793520.000000] '
        'resourceGUID[9e65a7bd-adcf-4129-9ef5-26f4fe13f85b] autoLoading[1] quantity[224] '
        'transactionMode[ResourceContainer] Cargo Box Data:  [boxSize[16] | unitAmount[14]] '
        '[Team_CoreGameplayFeatures][Shops][UI]\n')


def test_buy_sell_parse_scu_and_auec():
    st = State()
    st.feed(BUY)
    st.feed(SELL)
    assert len(st.trades) == 2
    by_action = {t.action: t for t in st.trades.values()}
    buy, sell = by_action["buy"], by_action["sell"]
    # SCU from box data (boxSize x unitAmount), NOT the inconsistent quantity field
    assert buy.scu == 288   # 16 * 18
    assert sell.scu == 224  # 16 * 14
    assert buy.auec == 1067040   # price[...]
    assert sell.auec == 793520   # amount[...]
    assert buy.commodity_guid == "35121003-f1af-481a-b16f-7f48d8af0efb"
    assert buy.unit_price == round(1067040 / 288)


def test_idempotent_refeed():
    st = State()
    st.feed(BUY)
    st.feed(BUY)  # log replay (restart / rotation) must not duplicate
    assert len(st.trades) == 1


def test_build_session_trades_totals():
    st = State()
    st.feed(BUY)
    st.feed(SELL)
    trades, totals = build_session_trades(st)
    assert totals["count"] == 2
    assert totals["spent"] == 1067040
    assert totals["earned"] == 793520
    assert totals["net"] == 793520 - 1067040
    assert totals["buy_scu"] == 288 and totals["sell_scu"] == 224
    # serialized rows carry a resolved (or fallback) commodity name + the GUID
    assert all(t["commodity"] and t["commodity_guid"] for t in trades)


def test_non_trade_line_ignored():
    st = State()
    st.feed("<2026-06-01T16:19:16.640Z> AddingCommodityBox - commodityName[ResourceType.Waste]\n")
    assert len(st.trades) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
