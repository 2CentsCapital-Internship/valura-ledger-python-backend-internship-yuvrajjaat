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
        self.lots: dict[tuple[str, str], list] = defaultdict(list)
        self.lot_seq: int = 0
        self.trades: dict[str, dict] = {}
        self.symbol_alias: dict[tuple[str, str], str] = {}
        self.event_lotops: dict[str, list] = {}
        self._cur_ops: list = []

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
        self._cur_ops = []
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
        self.event_lotops[eid] = self._cur_ops
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
        hold = money(notional + D(p["est_charges"])) if side == "buy" else ZERO
        self.orders[oid] = {"customer_id": p["customer_id"], "side": side,
                            "symbol": p["symbol"], "route": route,
                            "qty_total": qty, "qty_filled": ZERO,
                            "hold_total": hold, "hold_remaining": hold,
                            "open": True}
        return []

    def on_order_partially_filled(self, p, ev):
        return self._fill(p, final=False)

    def on_order_filled(self, p, ev):
        return self._fill(p, final=True)

    def _fill(self, p, final: bool) -> list[dict]:
        cid = p["customer_id"]
        side = p["side"]
        symbol = self._resolve(cid, p["symbol"])
        qty = D(p["quantity"])
        principal = money(p["principal"])

        o = self.orders.get(p.get("order_id"))
        if o is not None:
            if not o["open"] or qty > o["qty_total"] - o["qty_filled"]:
                raise Rejected("overfill")

        f = compute_fees(p["broker"], principal, D(p["partner_rate"]))
        b, c, r = f["b"], f["c"], f["r"]
        bc, cc, ps, pay = f["bc"], f["cc"], f["ps"], f["payable"]

        if side == "buy":
            self._add_lot(cid, symbol, qty, principal)
            legs = [leg("2010", cid, debit=principal + b + c + r),
                    leg("1200", cid, debit=principal),
                    leg("2350", cid, credit=principal),
                    leg("2100", cid, credit=principal),
                    leg("4000", cid, credit=b),
                    leg("4010", cid, credit=c),
                    leg("2400", cid, credit=r),
                    leg("5000", cid, debit=bc),
                    leg(pay, cid, credit=bc),
                    leg("5010", cid, debit=cc),
                    leg("2420", cid, credit=cc),
                    leg("5100", cid, debit=ps),
                    leg("2430", cid, credit=ps)]
        else:
            cost = self._relieve_fifo(cid, symbol, qty)
            legs = [leg("1150", cid, debit=principal),
                    leg("2010", cid, credit=principal - b - c - r),
                    leg("2100", cid, debit=cost),
                    leg("1200", cid, credit=cost),
                    leg("4000", cid, credit=b),
                    leg("4010", cid, credit=c),
                    leg("2400", cid, credit=r),
                    leg("5000", cid, debit=bc),
                    leg(pay, cid, credit=bc),
                    leg("5010", cid, debit=cc),
                    leg("2420", cid, credit=cc),
                    leg("5100", cid, debit=ps),
                    leg("2430", cid, credit=ps)]

        self.trades[p["trade_id"]] = {"side": side, "principal": principal,
                                      "customer_id": cid}
        if o is not None:
            o["qty_filled"] += qty
            if o["side"] == "buy" and not final and o["qty_total"] > ZERO:
                rel = money(o["hold_total"] * qty / o["qty_total"])
                o["hold_remaining"] = max(ZERO, o["hold_remaining"] - rel)
            if final:
                o["hold_remaining"] = ZERO
                o["open"] = False
        return legs

    def _resolve(self, cid, symbol):
        seen = set()
        while (cid, symbol) in self.symbol_alias and symbol not in seen:
            seen.add(symbol)
            symbol = self.symbol_alias[(cid, symbol)]
        return symbol

    def _add_lot(self, cid, symbol, qty, cost) -> None:
        self.lot_seq += 1
        self.lots[(cid, symbol)].append(
            {"id": self.lot_seq, "qty": D(qty), "cost": money(cost)})
        self._cur_ops.append(("add", (cid, symbol), self.lot_seq))

    def _relieve_fifo(self, cid, symbol, qty) -> Decimal:
        key = (cid, symbol)
        lots = self.lots.get(key, [])
        if sum((l["qty"] for l in lots), ZERO) < qty:
            raise Rejected("oversell")
        remaining, cost, kept, records = qty, ZERO, [], []
        for idx, l in enumerate(lots):
            if remaining <= ZERO:
                kept.append(l)
            elif l["qty"] <= remaining:
                cost += l["cost"]
                remaining -= l["qty"]
                records.append(("full", dict(l), idx))
            else:
                relief = money(l["cost"] * remaining / l["qty"])
                cost += relief
                l["qty"] -= remaining
                l["cost"] -= relief
                records.append(("part", l["id"], remaining, relief))
                remaining = ZERO
                kept.append(l)
        self.lots[key] = kept
        if records:
            self._cur_ops.append(("consume", key, records))
        return cost

    def on_trade_settled(self, p, ev):
        tr = self.trades.get(p["trade_id"])
        if tr is None:
            raise Rejected("trade_settled: unknown trade")
        amt, cid = tr["principal"], tr["customer_id"]
        if tr["side"] == "buy":
            return [leg("2350", cid, debit=amt), leg("1100", cid, credit=amt)]
        return [leg("1100", cid, debit=amt), leg("1150", cid, credit=amt)]

    def _settle(self, cid, account) -> list[dict]:
        accrued = money(-self.balances.get((cid, account), ZERO))
        if accrued <= ZERO:
            raise Rejected("nothing outstanding")
        return [leg(account, cid, debit=accrued),
                leg("1100", cid, credit=accrued)]

    def on_order_cancelled(self, p, ev):
        o = self.orders.get(p["order_id"])
        if o is not None:
            o["open"] = False
            o["hold_remaining"] = ZERO
        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_broker_fees_settled(self, p, ev):
        return self._settle(p["customer_id"], BROKER_PAYABLE[p["broker"]])

    def on_custodian_fees_settled(self, p, ev):
        return self._settle(p["customer_id"], "2420")

    def on_reg_fees_remitted(self, p, ev):
        return self._settle(p["customer_id"], "2400")

    def on_partner_payout(self, p, ev):
        return self._settle(p["customer_id"], "2430")

    def on_dividend_cash(self, p, ev):
        cid, net = p["customer_id"], money(p["net_amount"])
        return [leg("1100", cid, debit=net), leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p, ev):
        cid, net = p["customer_id"], money(p["net_amount"])
        sym = self._resolve(cid, p["symbol"])
        self._add_lot(cid, sym, D(p["reinvest_quantity"]), net)
        return [leg("1200", cid, debit=net), leg("2100", cid, credit=net)]

    def on_stock_split(self, p, ev):
        cid = p["customer_id"]
        key = (cid, self._resolve(cid, p["symbol"]))
        factor = D(p["ratio_to"]) / D(p["ratio_from"])
        for l in self.lots.get(key, []):
            l["qty"] = l["qty"] * factor
        self._cur_ops.append(("split", key, factor))
        return []

    def on_symbol_change(self, p, ev):
        cid, old, new = p["customer_id"], p["old_symbol"], p["new_symbol"]
        old = self._resolve(cid, old)
        self.symbol_alias[(cid, old)] = new
        src = self.lots.get((cid, old))
        moved = [l["id"] for l in src] if src else []
        if src:
            self.lots[(cid, new)].extend(src)
            del self.lots[(cid, old)]
        self._cur_ops.append(("rename", cid, old, new, moved))
        return []

    def on_reversal(self, p, ev):
        src = p["reverses_event_id"]
        if src not in self.events:
            raise Rejected("reversal: unknown event")
        for op in reversed(self.event_lotops.get(src, [])):
            self._undo_lotop(op)
        return [leg(l["account"], l["customer_id"],
                    debit=l["credit"], credit=l["debit"])
                for l in self.event_legs.get(src, [])]

    def _undo_lotop(self, op) -> None:
        kind = op[0]
        if kind == "add":
            _, key, lot_id = op
            self.lots[key] = [l for l in self.lots.get(key, [])
                              if l["id"] != lot_id]
        elif kind == "consume":
            _, key, records = op
            lst = self.lots[key]
            for rec in reversed(records):
                if rec[0] == "part":
                    _, lot_id, qd, cd = rec
                    for l in lst:
                        if l["id"] == lot_id:
                            l["qty"] += qd
                            l["cost"] += cd
                            break
                else:
                    _, snap, idx = rec
                    lst.insert(min(idx, len(lst)), dict(snap))
        elif kind == "split":
            _, key, factor = op
            for l in self.lots.get(key, []):
                l["qty"] = l["qty"] / factor
        elif kind == "rename":
            _, cid, old, new, moved = op
            src = self.lots.get((cid, new), [])
            back = [l for l in src if l["id"] in moved]
            self.lots[(cid, new)] = [l for l in src if l["id"] not in moved]
            if back:
                self.lots[(cid, old)] = back + self.lots.get((cid, old), [])
            self.symbol_alias.pop((cid, old), None)

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
            if o["open"] and o["hold_remaining"] != ZERO:
                cust(o["customer_id"])["cash_hold"] += o["hold_remaining"]

        for (cid, symbol), lots in self.lots.items():
            q = sum((l["qty"] for l in lots), ZERO)
            if q != ZERO:
                cost = sum((l["cost"] for l in lots), ZERO)
                cust(cid)["positions"][symbol] = {
                    "quantity": qstr(q), "cost_basis": str(money(cost))}

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
