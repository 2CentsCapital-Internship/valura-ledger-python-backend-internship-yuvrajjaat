from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

D = Decimal
ZERO = D("0.00")
CENT = D("0.01")

REG_BPS = D("0.0008")

TARIFF = {
    "BRK-A": {"classes": {"equity", "etf"},
              "brokerage": D("0.0020"), "custody": D("0.0004"),
              "broker_cost": D("0.0009"), "custody_cost": D("0.0002"),
              "min_fee": D("1.00"), "ticket": D("0.35")},
    "BRK-B": {"classes": {"equity", "bond"},
              "brokerage": D("0.0015"), "custody": D("0.0005"),
              "broker_cost": D("0.0008"), "custody_cost": D("0.0003"),
              "min_fee": D("2.50"), "ticket": D("3.00")},
    "BRK-C": {"classes": {"etf", "bond"},
              "brokerage": D("0.0025"), "custody": D("0.0003"),
              "broker_cost": D("0.0012"), "custody_cost": D("0.0001"),
              "min_fee": D("0.50"), "ticket": D("0.20")},
}
BROKER_PAYABLE = {"BRK-A": "2411", "BRK-B": "2412", "BRK-C": "2413"}


class Rejected(Exception):
    pass


def money(x) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def qstr(x: Decimal) -> str:
    return format(D(x).normalize(), "f")


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(debit)), "credit": str(money(credit))}


def compute_fees(broker: str, principal: Decimal, partner_rate: Decimal) -> dict:
    t = TARIFF[broker]
    b = max(money(principal * t["brokerage"]), t["min_fee"])
    c = money(principal * t["custody"])
    r = money(principal * REG_BPS)
    bc = money(principal * t["broker_cost"]) + t["ticket"]
    cc = money(principal * t["custody_cost"])
    revenue = b + c
    cost = bc + cc
    ps = money(partner_rate * (revenue - cost)) if revenue > cost else ZERO
    return {"b": b, "c": c, "r": r, "bc": bc, "cc": cc, "ps": ps,
            "payable": BROKER_PAYABLE[broker]}


def route_for(asset_class: str, notional: Decimal) -> str:
    best = None
    for broker in sorted(TARIFF):
        t = TARIFF[broker]
        if asset_class not in t["classes"]:
            continue
        charge = (max(money(notional * t["brokerage"]), t["min_fee"])
                  + money(notional * t["custody"]))
        if best is None or charge < best[0]:
            best = (charge, broker)
    if best is None:
        raise Rejected(f"no broker trades {asset_class}")
    return best[1]


class Book:
    def __init__(self) -> None:
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.touched: set[str] = set()
        self.seen: set[str] = set()
        self.log: list[dict] = []
        self.events: dict[str, dict] = {}
        self.event_legs: dict[str, list] = {}

        self.withdrawals: dict[str, dict] = {}
        self.fee_refunded: set[str] = set()
        self.orders: dict[str, dict] = {}

        self.todo: dict[str, int] = defaultdict(int)
        self.errors: list[tuple] = []

    def apply(self, ev: dict) -> list[dict]:
        eid = ev["event_id"]
        if eid in self.seen:
            return []
        self.seen.add(eid)
        self.log.append(ev)
        self.events[eid] = ev

        handler = getattr(self, "on_" + ev["type"], None)
        if handler is None:
            self.todo[ev["type"]] += 1
            return []
        try:
            legs = clean(handler(ev["payload"], ev) or [])
        except NotImplementedError:
            self.todo[ev["type"]] += 1
            return []
        except Rejected:
            return []
        except (KeyError, ValueError, InvalidOperation, ArithmeticError,
                TypeError) as exc:
            self.errors.append((eid, ev.get("type"), repr(exc)))
            return []
        except Exception as exc:
            self.errors.append((eid, ev.get("type"), repr(exc)))
            return []
        self._post(legs)
        self.event_legs[eid] = legs
        return legs

    def _post(self, legs: list[dict]) -> None:
        dr = sum(D(l["debit"]) for l in legs)
        cr = sum(D(l["credit"]) for l in legs)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"]))
            self.touched.add(l["account"])

    def on_deposit(self, p, ev):
        a, cid = money(p["amount"]), p["customer_id"]
        return [leg("1100", cid, debit=a), leg("2010", cid, credit=a)]

    def on_fee_charged(self, p, ev):
        a, cid = money(p["amount"]), p["customer_id"]
        return [leg("2010", cid, debit=a), leg("1100", cid, credit=a)]

    def on_fee_refund(self, p, ev):
        src = p["refunds_source_id"]
        orig = self.events.get(src)
        if orig is None or orig["type"] != "fee_charged":
            raise Rejected("fee_refund: unknown or non-fee source")
        if src in self.fee_refunded:
            raise Rejected("fee_refund: already refunded")
        self.fee_refunded.add(src)
        a, cid = money(orig["payload"]["amount"]), p["customer_id"]
        return [leg("1100", cid, debit=a), leg("2010", cid, credit=a)]

    def on_withdrawal_requested(self, p, ev):
        a, cid, wid = money(p["amount"]), p["customer_id"], p["withdrawal_id"]
        self.withdrawals[wid] = {"amount": a, "customer_id": cid}
        return [leg("2010", cid, debit=a), leg("2300", cid, credit=a)]

    def on_withdrawal_settled(self, p, ev):
        w = self.withdrawals.get(p["withdrawal_id"])
        if w is None:
            raise Rejected("withdrawal_settled: unknown withdrawal")
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("1100", w["customer_id"], credit=w["amount"])]

    def on_withdrawal_rejected(self, p, ev):
        w = self.withdrawals.get(p["withdrawal_id"])
        if w is None:
            raise Rejected("withdrawal_rejected: unknown withdrawal")
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("2010", w["customer_id"], credit=w["amount"])]

    def on_interest_credited(self, p, ev):
        cid = p["customer_id"]
        gross = money(p["gross_amount"])
        share = money(p["customer_share"])
        firm = gross - share
        if firm < ZERO:
            raise Rejected("interest_credited: share exceeds gross")
        legs = [leg("1100", cid, debit=gross), leg("2010", cid, credit=share)]
        if firm > ZERO:
            legs.append(leg("4200", cid, credit=firm))
        return legs

    def on_transfer_between_customers(self, p, ev):
        a = money(p["amount"])
        return [leg("2010", p["from_customer_id"], debit=a),
                leg("2010", p["to_customer_id"], credit=a)]

    def on_fx_deposit(self, p, ev):
        cid = p["customer_id"]
        market = money(p["usd_at_market_rate"])
        cust = money(p["usd_at_customer_rate"])
        spread = market - cust
        if spread < ZERO:
            raise Rejected("fx_deposit: negative spread")
        legs = [leg("1100", cid, debit=market), leg("2010", cid, credit=cust)]
        if spread > ZERO:
            legs.append(leg("4100", cid, credit=spread))
        return legs

    def on_order_placed(self, p, ev):
        oid = p["order_id"]
        side = p["side"]
        qty = D(p["quantity"])
        notional = qty * D(p["limit_price"])
        route = route_for(p["asset_class"], notional)
        if side == "buy":
            hold = money(notional + D(p["est_charges"]))
        else:
            hold = ZERO
        self.orders[oid] = {"customer_id": p["customer_id"], "side": side,
                            "symbol": p["symbol"], "route": route,
                            "cash_hold": hold, "open": True}
        return []

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        raise NotImplementedError

    def on_trade_settled(self, p, ev):
        raise NotImplementedError

    def on_order_cancelled(self, p, ev):
        o = self.orders.get(p["order_id"])
        if o is not None:
            o["open"] = False
            o["cash_hold"] = ZERO
        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_broker_fees_settled(self, p, ev):
        raise NotImplementedError

    def on_custodian_fees_settled(self, p, ev):
        raise NotImplementedError

    def on_reg_fees_remitted(self, p, ev):
        raise NotImplementedError

    def on_partner_payout(self, p, ev):
        raise NotImplementedError

    def on_dividend_cash(self, p, ev):
        raise NotImplementedError

    def on_dividend_reinvested(self, p, ev):
        raise NotImplementedError

    def on_stock_split(self, p, ev):
        raise NotImplementedError

    def on_symbol_change(self, p, ev):
        raise NotImplementedError

    def on_reversal(self, p, ev):
        raise NotImplementedError

    def snapshot(self, as_of_event_id: str | None = None) -> dict:
        if as_of_event_id is not None:
            replay = Book()
            for ev in self.log:
                replay.apply(ev)
                if ev["event_id"] == as_of_event_id:
                    break
            return replay._snapshot_current()
        return self._snapshot_current()

    def _snapshot_current(self) -> dict:
        tb: dict[str, Decimal] = {a: ZERO for a in self.touched}
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}

        def cust(cid):
            return customers.setdefault(cid, {"wallet_cash": ZERO,
                                              "cash_hold": ZERO, "positions": {}})

        for (cid, acct), bal in self.balances.items():
            if acct == "2010":
                cust(cid)["wallet_cash"] += -bal

        for o in self.orders.values():
            if o["open"] and o["cash_hold"] != ZERO:
                cust(o["customer_id"])["cash_hold"] += o["cash_hold"]

        routes = {oid: o["route"] for oid, o in self.orders.items()
                  if o["open"]}

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
            "open_order_routes": routes,
        }


def clean(legs: list[dict]) -> list[dict]:
    return [l for l in legs
            if D(l["debit"]) != ZERO or D(l["credit"]) != ZERO]
