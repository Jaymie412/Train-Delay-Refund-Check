# Train Delay Refund Check verification

Contract file: `contract.py`, version `train-delay-refund-0.1`.

## Local checks completed

- Python syntax compilation succeeded.
- `test_contract.py`: **31 tests passed** against a simulated GenLayer runtime. The test runner rolls back every change when a write fails, like a real GenLayer transaction.
- The contract uses the same structure and GenLayer SDK calls (`gl.vm.run_nondet_unsafe`, `gl.vm.Return`, `gl.nondet.exec_prompt`, `gl.nondet.web.render`, `gl.vm.UserError`, `gl.message.sender_address`) as my Vegan Recipe Challenge and Dog-Friendly Stay Check, which ran end to end on GenLayer Studio with the same `py-genlayer` runner.
- Every storage write happens after the validators have agreed.

The official `genlayer-test` package couldn't be installed in the environment used, so the tests use a small simulated runtime. All AI answers in the tests are authored fixtures. They check the contract's deterministic behaviour, not the AI's judgement.

## What the tests cover

**Settling and the maths**
1. A train 37 minutes late: SETTLED, 50%, 1225 pence on a 2450 pence ticket, added to both records.
2. A cancelled train: 100% of the ticket price.
3. Band edges: 14 → 0%, 15 → 25%, 29 → 25%, 30 → 50%, 59 → 50%, 60 → 100%.
4. Arrival after midnight (23:50 → 00:35 = 45 minutes) and early arrival (counts as 0).
5. A record without both arrival times settles with no refund (`arrival_times_not_shown`).
6. Refunds round down to whole pence.
7. Time parsing accepts H:MM and HH:MM and refuses 24:00, 12:60, 0905 and other bad values.

**Consent, roles and record ownership**
8. The policy hash covers the bands, the owner, the record prefix and the fare cap.
9. A wrong policy hash is refused and nothing is read.
10. The operator can't file a claim against itself.
11. A record link outside the operator's own prefix is refused before anything is read.
12. Input limits on the prefix, the fare cap, the ticket price (up to the fare cap), the service ID and the operator ID.

**Binding to the service**
13. A record for another service closes as UNRELATED: nothing owed, no retry.
14. If the AI claims another service matches, code overrules it; "1A234" doesn't count as "1A23".

**Duplicates and exactly-once**
15. The same passenger can't claim twice for the same record and service, even if the service is written differently.
16. A different passenger on the same train gets a separate claim.
17. A settled claim can't be retried, and a direct internal replay is refused; the records stay at one claim.
18. An UNRELATED claim frees the slot, so the passenger can file the right claim.

**Bounded retries**
19. An unreadable record: UNREACHABLE, one retry by anyone, then CLOSED_UNAVAILABLE with nothing owed.
20. A retry that can read the record settles normally.
21. While UNREACHABLE, the passenger must retry rather than file again.

**Consensus and AI-output checks**
22. A validator reaching a different band blocks the transaction and nothing is saved.
23. Validators quoting different words but reaching the same band still agree.
24. A failed audit blocks the transaction.
25. A time that isn't inside its quote, a made-up time and a made-up cancellation are rejected.
26. Missing or extra facts, extra fields and unknown answers are rejected; values and quotes on unquoted answers are cleared; the record can't fake the evidence section markers.
27. A malformed AI reply is asked again.

**Demos**
28. The demo labels and refunds follow the code rules; demos create no operators, claims or records; every demo record names the service except demo 6.

## Live Studio evidence

See [DEMO_RESULTS.md](DEMO_RESULTS.md).
