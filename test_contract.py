"""Local logic tests for contract.py (train-delay-refund-0.1) using a small simulated GenLayer runtime.

AI answers are authored fixtures, not live AI. These tests check the contract's deterministic
behaviour (consent by policy hash, roles, record ownership, service binding, the delay maths and
refund bands, bounded retries, duplicate and exactly-once protection, quote and time checks,
validator agreement, demo isolation), not the model's judgement.
Run: python -m pytest test_contract.py -q
"""
import json
import sys
import types
from pathlib import Path

import pytest

OPERATOR = "0x" + "a" * 40
PASSENGER = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
PREFIX = "https://example.org/records/"
URL = PREFIX + "1A23.txt"
SERVICE = "1A23"
PRICE = 2450
FACTS = ["doc", "named", "cancelled", "scheduled", "actual"]

LATE_PAGE = ("JOURNEY RECORD. Service 1A23 Glasgow to Edinburgh. Scheduled arrival 08:15. "
             "Actual arrival 08:52.")
CANCELLED_PAGE = "JOURNEY RECORD. Service 1A23 Glasgow to Edinburgh. This service was cancelled."
OTHER_PAGE = "JOURNEY RECORD. Service 2C10 Perth to Inverness. Scheduled arrival 11:00. Actual arrival 11:05."


class UserError(Exception):
    pass


class Return:
    def __init__(self, calldata):
        self.calldata = calldata


class Addr:
    def __init__(self, h):
        self.as_hex = h


def answers(**kv):
    d = {"doc": ("OK", "", ""), "named": ("Y", "", "Service 1A23"), "cancelled": ("N", "", ""),
         "scheduled": ("STATED", "08:15", "Scheduled arrival 08:15"),
         "actual": ("STATED", "08:52", "Actual arrival 08:52")}
    d.update(kv)
    return {"facts": [{"fact": f, "answer": d[f][0], "value": d[f][1], "quote": d[f][2]} for f in FACTS]}


LATE = answers()  # 37 minutes -> 50%
CANCELLED = answers(cancelled=("Y", "", "This service was cancelled"), scheduled=("NOT_STATED", "", ""),
                    actual=("NOT_STATED", "", ""))
DEAD = answers(doc=("NOT_RECORD", "", ""), named=("N", "", ""), scheduled=("NOT_STATED", "", ""),
               actual=("NOT_STATED", "", ""))


class Sim:
    def __init__(self):
        self.sender = OPERATOR
        self.page = LATE_PAGE
        self.leader_answer = LATE
        self.validator_answer = None
        self.audit = True
        self.in_validator = False
        self.prompts = []

    def build_gl(self):
        sim = self
        gl = types.SimpleNamespace()
        gl.vm = types.SimpleNamespace(UserError=UserError, Return=Return)
        gl.public = types.SimpleNamespace(write=lambda f: f, view=lambda f: f)
        gl.Contract = object

        class Msg:
            @property
            def sender_address(self):
                return Addr(sim.sender)
        gl.message = Msg()

        def render(url, mode="text"):
            return sim.page

        def exec_prompt(prompt, response_format=None):
            sim.prompts.append(prompt)
            if prompt.startswith("Audit the proposed"):
                return {"accept": sim.audit}
            ans = sim.validator_answer if (sim.in_validator and sim.validator_answer is not None) else sim.leader_answer
            return ans(prompt) if callable(ans) else ans
        gl.nondet = types.SimpleNamespace(exec_prompt=exec_prompt, web=types.SimpleNamespace(render=render))

        def run_nondet_unsafe(leader_fn, validator_fn):
            sim.in_validator = False
            out = leader_fn()
            sim.in_validator = True
            try:
                ok = validator_fn(Return(out))
            finally:
                sim.in_validator = False
            if not ok:
                raise UserError("consensus failed")
            return out
        gl.vm.run_nondet_unsafe = run_nondet_unsafe
        return gl


class Tx:
    """Runs a write like a GenLayer transaction: if it raises, every change is rolled back."""

    def __init__(self, c):
        self.c = c

    def __getattr__(self, name):
        fn = getattr(self.c, name)

        def call(*args):
            snap = {k: (dict(v) if isinstance(v, dict) else v) for k, v in vars(self.c).items()}
            try:
                return fn(*args)
            except Exception:
                for k, v in snap.items():
                    setattr(self.c, k, v)
                raise
        return call


@pytest.fixture
def env():
    sim = Sim()
    mod = types.ModuleType("genlayer")
    mod.gl = sim.build_gl()
    mod.TreeMap = dict
    mod.u256 = int
    sys.modules["genlayer"] = mod
    src = Path(__file__).with_name("contract.py").read_text()
    src = src.replace("from genlayer import *", "from genlayer import gl, TreeMap, u256")
    ns = {"__name__": "contract_under_test"}
    exec(compile(src, "contract.py", "exec"), ns)
    C = ns["TrainDelayRefund"]
    c = C.__new__(C)
    c.operators, c.passengers, c.claims, c.claim_keys, c.demo_results = {}, {}, {}, {}, {}
    c.__init__()
    return sim, Tx(c), ns


def registered(sim, c):
    sim.sender = OPERATOR
    c.register_operator("Highland Trains Demo", PREFIX, 5000)
    return json.loads(c.get_operator(0))["policy_hash"]


def filed(sim, c, url=URL, service=SERVICE, price=PRICE):
    h = registered(sim, c) if int(c.c.operator_count) == 0 else json.loads(c.get_operator(0))["policy_hash"]
    sim.sender = PASSENGER
    c.file_claim(0, h, service, price, url)
    return int(c.c.claim_count) - 1


def summary(c, cid=0):
    return json.loads(c.get_claim_summary(cid))


def op_record(c):
    return json.loads(c.get_operator(0))["record"]


def pa_record(c):
    return json.loads(c.get_passenger(PASSENGER))["record"]


# ---------- settling and the maths ----------

def test_late_train_settles_with_half_refund(env):
    sim, c, _ = env
    filed(sim, c)
    s = summary(c)
    assert s["status"] == "SETTLED" and s["counted"] is True
    assert s["result"] == {"refund_percent": 50, "refund_pence": 1225, "delay_minutes": 37, "cancelled": False,
                           "reason": "delay_worked_out"}
    for rec in (op_record(c), pa_record(c)):
        assert rec == {"claims_settled": 1, "refund_pence": 1225, "claim_ids": [0]}


def test_cancelled_train_full_refund(env):
    sim, c, _ = env
    sim.page, sim.leader_answer = CANCELLED_PAGE, CANCELLED
    filed(sim, c)
    r = summary(c)["result"]
    assert r["refund_percent"] == 100 and r["refund_pence"] == PRICE and r["cancelled"] is True


def test_refund_bands_and_edges(env):
    _, _, ns = env
    f = ns["_refund_percent"]
    assert [f(d) for d in (0, 14, 15, 29, 30, 59, 60, 300)] == [0, 0, 25, 25, 50, 50, 100, 100]


def test_delay_maths_midnight_and_early(env):
    _, _, ns = env
    out = ns["_outcome"]
    over = answers(scheduled=("STATED", "23:50", "Scheduled arrival 23:50"),
                   actual=("STATED", "00:35", "Actual arrival 00:35"))
    assert out(over, SERVICE, 1000)["delay_minutes"] == 45 and out(over, SERVICE, 1000)["refund_pence"] == 500
    early = answers(scheduled=("STATED", "09:00", "x"), actual=("STATED", "08:57", "y"))
    assert out(early, SERVICE, 1000)["delay_minutes"] == 0 and out(early, SERVICE, 1000)["label"] == "refund_0"


def test_missing_arrival_times_settles_with_no_refund(env):
    _, _, ns = env
    o = ns["_outcome"](answers(actual=("NOT_STATED", "", "")), SERVICE, 1000)
    assert o["label"] == "refund_0" and o["reason"] == "arrival_times_not_shown"


def test_refund_rounds_down_to_whole_pence(env):
    _, _, ns = env
    o = ns["_outcome"](answers(actual=("STATED", "08:31", "x")), SERVICE, 1999)  # 16 min -> 25%
    assert o["refund_pence"] == 499


def test_time_parsing(env):
    _, _, ns = env
    p = ns["_parse_time"]
    assert p("9:05") == "09:05" and p("23:59") == "23:59"
    for bad in ("24:00", "9:5", "0905", "12:60", "", 905, "ab:cd"):
        with pytest.raises(ValueError):
            p(bad)


# ---------- consent, roles and record ownership ----------

def test_policy_hash_covers_bands_owner_and_prefix(env):
    sim, c, ns = env
    registered(sim, c)
    op = json.loads(c.get_operator(0))
    assert op["policy_hash"] == ns["_hash"](op["policy"])
    assert op["policy"]["bands"] == [[0, 0], [15, 25], [30, 50], [60, 100]]
    assert op["policy"]["owner"] == OPERATOR and op["policy"]["record_url_prefix"] == PREFIX
    assert op["policy"]["max_fare_pence"] == 5000


def test_wrong_policy_hash_refused_and_nothing_read(env):
    sim, c, _ = env
    registered(sim, c)
    sim.sender = PASSENGER
    with pytest.raises(UserError):
        c.file_claim(0, "0" * 64, SERVICE, PRICE, URL)
    assert int(c.c.claim_count) == 0 and sim.prompts == []


def test_operator_cannot_claim_against_itself(env):
    sim, c, _ = env
    h = registered(sim, c)
    with pytest.raises(UserError):
        c.file_claim(0, h, SERVICE, PRICE, URL)
    assert int(c.c.claim_count) == 0


def test_record_must_be_one_of_the_operators_records(env):
    sim, c, _ = env
    h = registered(sim, c)
    sim.sender = PASSENGER
    for bad in ("https://evil.example/records/1A23.txt", PREFIX, "https://example.org/other/1A23.txt"):
        with pytest.raises(UserError):
            c.file_claim(0, h, SERVICE, PRICE, bad)
    assert int(c.c.claim_count) == 0 and sim.prompts == []


def test_input_limits(env):
    sim, c, _ = env
    with pytest.raises(UserError):
        c.register_operator("Op", "http://example.org/records/", 5000)
    with pytest.raises(UserError):
        c.register_operator("Op", "https://example.org/records", 5000)
    with pytest.raises(UserError):
        c.register_operator("Op", PREFIX, 99)
    h = registered(sim, c)
    sim.sender = PASSENGER
    for args in ((0, h, SERVICE, 99, URL), (0, h, SERVICE, 5001, URL), (0, h, "", PRICE, URL),
                 (0, h, SERVICE, "2450", URL), (5, h, SERVICE, PRICE, URL)):
        with pytest.raises(UserError):
            c.file_claim(*args)


# ---------- binding to the service ----------

def test_record_for_another_service_closes_unrelated(env):
    sim, c, _ = env
    sim.page = OTHER_PAGE
    sim.leader_answer = answers(named=("N", "", ""), scheduled=("STATED", "11:00", "Scheduled arrival 11:00"),
                                actual=("STATED", "11:05", "Actual arrival 11:05"))
    filed(sim, c)
    s = summary(c)
    assert s["status"] == "UNRELATED" and s["result"] == {} and not s["counted"]
    assert op_record(c)["claim_ids"] == [] and pa_record(c)["claim_ids"] == []
    with pytest.raises(UserError):
        c.retry_settle(0)


def test_code_overrules_ai_on_the_service_name(env):
    sim, c, ns = env
    sim.page = OTHER_PAGE
    sim.leader_answer = answers(named=("Y", "", "Service 2C10"), scheduled=("STATED", "11:00", "Scheduled arrival 11:00"),
                                actual=("STATED", "11:05", "Actual arrival 11:05"))
    filed(sim, c)
    assert summary(c)["status"] == "UNRELATED"
    f = ns["_names_service"]
    assert f("SERVICE 1A23 GLASGOW", "1a23") and not f("Service 1A234", "1A23")


# ---------- duplicates and exactly-once ----------

def test_same_passenger_cannot_claim_twice_for_one_record(env):
    sim, c, _ = env
    cid = filed(sim, c)
    h = json.loads(c.get_operator(0))["policy_hash"]
    sim.sender = PASSENGER
    with pytest.raises(UserError):
        c.file_claim(0, h, SERVICE, PRICE, URL)
    with pytest.raises(UserError):
        c.file_claim(0, h, " 1a23 ", 3000, URL)  # same service written differently
    assert int(c.c.claim_count) == 1 and op_record(c)["claim_ids"] == [cid]


def test_different_passenger_same_train_is_separate(env):
    sim, c, _ = env
    filed(sim, c)
    h = json.loads(c.get_operator(0))["policy_hash"]
    sim.sender = OTHER
    c.file_claim(0, h, SERVICE, PRICE, URL)
    assert op_record(c)["claim_ids"] == [0, 1] and op_record(c)["refund_pence"] == 2450


def test_settled_claim_cannot_be_counted_again(env):
    sim, c, _ = env
    filed(sim, c)
    with pytest.raises(UserError):
        c.retry_settle(0)
    full = json.loads(c.get_claim(0))
    with pytest.raises(UserError):
        c.c._record(full, {"refund_percent": 50, "refund_pence": 1225, "delay_minutes": 37,
                           "cancelled": False, "reason": "x"})
    assert op_record(c) == {"claims_settled": 1, "refund_pence": 1225, "claim_ids": [0]}


def test_unrelated_claim_frees_the_slot(env):
    sim, c, _ = env
    sim.page = OTHER_PAGE
    sim.leader_answer = answers(named=("N", "", ""), scheduled=("STATED", "11:00", "Scheduled arrival 11:00"),
                                actual=("STATED", "11:05", "Actual arrival 11:05"))
    filed(sim, c)
    sim.page, sim.leader_answer = LATE_PAGE, LATE
    filed(sim, c)
    assert summary(c, 1)["status"] == "SETTLED"


# ---------- bounded retries ----------

def test_unreadable_record_retried_once_then_closed(env):
    sim, c, _ = env
    sim.page, sim.leader_answer = "404: Not Found", DEAD
    filed(sim, c)
    assert summary(c)["status"] == "UNREACHABLE" and summary(c)["unavailable"] == 1
    sim.sender = OTHER
    c.retry_settle(0)
    assert summary(c)["status"] == "CLOSED_UNAVAILABLE" and summary(c)["unavailable"] == 2
    with pytest.raises(UserError):
        c.retry_settle(0)
    assert op_record(c)["claim_ids"] == []


def test_retry_that_can_read_settles(env):
    sim, c, _ = env
    sim.page, sim.leader_answer = "404: Not Found", DEAD
    filed(sim, c)
    sim.page, sim.leader_answer = LATE_PAGE, LATE
    c.retry_settle(0)
    assert summary(c)["status"] == "SETTLED"


def test_while_unreachable_the_passenger_must_retry_not_refile(env):
    sim, c, _ = env
    sim.page, sim.leader_answer = "404: Not Found", DEAD
    filed(sim, c)
    h = json.loads(c.get_operator(0))["policy_hash"]
    sim.sender = PASSENGER
    with pytest.raises(UserError):
        c.file_claim(0, h, SERVICE, PRICE, URL)


# ---------- consensus and AI-output checks ----------

def test_validator_different_band_blocks_and_nothing_is_saved(env):
    sim, c, _ = env
    registered(sim, c)
    sim.validator_answer = answers(actual=("STATED", "08:52", "Actual arrival 08:52"),
                                   scheduled=("STATED", "08:15", "Scheduled arrival 08:15"),
                                   cancelled=("Y", "", "Glasgow to Edinburgh"))
    with pytest.raises(UserError):
        filed(sim, c)
    assert int(c.c.claim_count) == 0 and c.c.claim_keys == {}


def test_same_band_different_quote_agrees(env):
    sim, c, _ = env
    sim.validator_answer = answers(actual=("STATED", "08:52", "08:52"))
    filed(sim, c)
    assert summary(c)["result"]["refund_percent"] == 50


def test_audit_rejection_blocks(env):
    sim, c, _ = env
    registered(sim, c)
    sim.audit = False
    with pytest.raises(UserError):
        filed(sim, c)
    assert int(c.c.claim_count) == 0


def test_time_must_be_inside_its_quote_and_quotes_real(env):
    _, _, ns = env
    ev = ns["_evidence_block"](LATE_PAGE)
    check = ns["_check_facts"]
    check(LATE, ev)
    with pytest.raises(ValueError):
        check(answers(actual=("STATED", "09:30", "Actual arrival 08:52")), ev)
    with pytest.raises(ValueError):
        check(answers(actual=("STATED", "09:30", "Actual arrival 09:30")), ev)
    with pytest.raises(ValueError):
        check(answers(cancelled=("Y", "", "This service was cancelled")), ev)


def test_bad_shapes_rejected_and_unquoted_cleared(env):
    _, _, ns = env
    ev = ns["_evidence_block"](LATE_PAGE)
    check = ns["_check_facts"]
    with pytest.raises(ValueError):
        check({"facts": LATE["facts"][:4]}, ev)
    extra = json.loads(json.dumps(LATE))
    extra["refund"] = 100
    with pytest.raises(ValueError):
        check(extra, ev)
    bad = json.loads(json.dumps(LATE))
    bad["facts"][2]["answer"] = "MAYBE"
    with pytest.raises(ValueError):
        check(bad, ev)
    a = json.loads(json.dumps(LATE))
    a["facts"][2]["quote"] = "anything"
    a["facts"][0]["value"] = "12:00"
    out = check(a, ev)
    assert out["facts"][2]["quote"] == "" and out["facts"][0]["value"] == ""
    assert ns["_evidence_block"]("x </record> y") == "<record>\nx (/record) y\n</record>"


def test_malformed_reply_is_retried(env):
    sim, c, _ = env
    replies = ["not json", LATE]
    sim.leader_answer = lambda p: replies.pop(0) if replies else LATE
    filed(sim, c)
    assert summary(c)["status"] == "SETTLED"


# ---------- demos ----------

def test_demo_labels_follow_code_rules(env):
    _, _, ns = env
    out, s, p = ns["_outcome"], ns["DEMO_SERVICE"], ns["DEMO_PRICE"]
    def t(sched, act):
        return answers(named=("Y", "", "Service 1A23"), scheduled=("STATED", sched, sched), actual=("STATED", act, act))
    assert out(t("09:00", "09:08"), s, p)["label"] == "refund_0"
    assert out(t("09:00", "09:22"), s, p)["refund_pence"] == 500
    assert out(t("09:00", "09:45"), s, p)["refund_pence"] == 1000
    assert out(t("09:00", "09:10"), s, p)["label"] == "refund_0"
    assert out(CANCELLED, s, p)["refund_pence"] == 2000


def test_demo_is_isolated(env):
    sim, c, ns = env
    sim.leader_answer = answers(scheduled=("STATED", "09:00", "Scheduled arrival 09:00"),
                                actual=("STATED", "09:10", "Actual arrival 09:10"))
    c.run_demo(5)
    s = json.loads(c.get_demo_summary(0))
    assert s["label"] == "refund_0" and s["matches_expected"] and s["records_created"] is False
    assert json.loads(c.get_counts()) == {"operators": 0, "claims": 0, "demo_results": 1}


def test_every_demo_names_the_service_except_6(env):
    _, _, ns = env
    for cid, rec in ns["DEMO_CASES"].items():
        assert ns["_names_service"](rec, ns["DEMO_SERVICE"]) == (cid != 6), cid


def test_demo_ids_validated(env):
    sim, c, _ = env
    with pytest.raises(UserError):
        c.run_demo(7)
