# Train Delay Refund Check

A GenLayer Intelligent Contract that answers: **was this train late enough for a refund, and how much is owed?**

A train operator registers once. It says where its journey records are published, sets its highest fare and agrees to fixed refund bands, all fixed by a `policy_hash`. A passenger files a claim against that exact policy: the service, the ticket price and a link to one of the operator's journey records. In the same transaction, GenLayer validators each read the record and report the scheduled and actual arrival times, and whether the train was cancelled, quoting the record word for word. **Code** checks the quotes, works out the delay and the refund, and adds it to both the operator's and the passenger's records exactly once.

Example: service 1A23 was due at 08:15 and arrived at 08:52. That's 37 minutes late, which earns a 50% refund, so a £24.50 ticket gets £12.25 back.

## Why I built it

Delay refunds are often confusing: people don't know how late "late" is, or how much they're owed, so many never claim. This contract makes the rule plain, works the answer out the same way every time, and shows the exact words in the record it was based on.

## Refund bands (code)

| Delay at the final destination | Refund |
|---|---|
| Under 15 minutes | 0% |
| 15–29 minutes | 25% |
| 30–59 minutes | 50% |
| 60 minutes or more | 100% |
| Cancelled | 100% |

- Delay = actual arrival − scheduled arrival, in minutes. An arrival after midnight is handled; an early arrival counts as 0.
- Refund = ticket price × percent, rounded down to whole pence.
- If the record shows neither a cancellation nor both arrival times, the claim settles with no refund (`arrival_times_not_shown`).

## How it stays fair

| Risk | How the contract handles it |
|---|---|
| An operator being held to rules it didn't agree to | The operator registers its own policy. A claim must quote the exact `policy_hash`, so the passenger agrees to the same terms. |
| A passenger linking a fake record | The record link must sit under the operator's own `record_url_prefix`, where only the operator publishes. Any other link is refused before anything is read. |
| A passenger inflating the ticket price | The price must be between £1 and the operator's own `max_fare_pence`. |
| A record for a different train | The AI must quote where the record names the service, and **code** checks that the quote contains the service ID. Otherwise the claim closes as **UNRELATED**, with nothing owed. |
| Claiming twice for the same journey | One claim per passenger, record and service. A second one is refused while the first is open or settled. |
| A claim left open forever | It settles in the same transaction. An unreadable record can be retried once by anyone, then closes as CLOSED_UNAVAILABLE, with nothing owed. |
| Counting a refund twice | One internal method updates records. It refuses a claim already counted (a flag on the claim, plus `claim_ids` on both records). |
| The AI making up times | Every time must sit inside a quote, and every quote must appear in the record word for word. The AI never works out the delay or the refund; code does. |
| Instructions hidden in a record | The record is treated as data. Validators audit the leader's answers for signs that hidden instructions were followed. |

## How consensus works

Using `gl.vm.run_nondet_unsafe`, the leader fetches the record and answers five questions (readable, names the service, cancelled, scheduled arrival, actual arrival). Each validator fetches the record itself, answers every question independently, checks the leader's answers against the same strict rules, and must reach the **same label** (`refund_0`, `refund_25`, `refund_50`, `refund_100`, `unrelated` or `unreachable`). It then runs an AI audit of the leader's quotes and answers, returning `{"accept": true/false}`. Nothing is stored until all of this passes. A malformed AI reply is asked again, up to 3 times.

## Lifecycle

| Step | Who | Method | Result |
|---|---|---|---|
| Register | Operator | `register_operator(operator_name, record_url_prefix, max_fare_pence)` | operator_id, `policy_hash` |
| File a claim, settled at once | Passenger | `file_claim(operator_id, expected_policy_hash, service_id, ticket_price_pence, record_url)` | SETTLED, UNRELATED or UNREACHABLE |
| Retry an unreadable record | Anyone | `retry_settle(claim_id)` | SETTLED, UNRELATED or CLOSED_UNAVAILABLE |

Read methods: `get_claim_summary`, `get_claim`, `get_operator`, `get_passenger(address)`, `get_counts`, `get_policy`, `get_demo`, `get_demo_result`, `get_demo_summary`. Demo write: `run_demo(case_id)`.

## Quick start in Studio

1. Import `contract.py` into GenLayer Studio and deploy. There are no parameters.
2. Call `run_demo` with `case_id = 3` and wait for it to finish.
3. Call `get_demo_summary` with `result_id = 0`. Expected: label `refund_50`, `delay_minutes: 45`, `refund_pence: 1000`, `matches_expected: true`, `records_created: false`.

See [DEMO_GUIDE.md](DEMO_GUIDE.md) for all six demos and the live walkthrough.

## Demonstrated in Studio

See [DEMO_RESULTS.md](DEMO_RESULTS.md) for the recorded run, deployment address and screenshots.

## Testing

`test_contract.py` contains 31 local tests using a simulated GenLayer runtime and authored AI answers. See [VERIFICATION.md](VERIFICATION.md).

## Boundaries

- The contract records what is owed. It doesn't move money.
- It trusts the operator's own published records. An operator could publish a wrong record; the contract can't check a record against the real world.
- It can't confirm the passenger bought a ticket or paid the price they enter. The price is capped by the operator's highest fare.
- The refund bands are a simple fixed example, not any real operator's scheme.
- The journey records and demos are fictional. Real use would point at an operator's real published records.
- All data is public on-chain.
- Instructions hidden in a record are treated as data. This reduces prompt injection but doesn't guarantee against it.
- Different addresses don't prove different people.

## Built with

- [GenLayer](https://docs.genlayer.com) Intelligent Contracts (Python, GenVM v0.2.16 runner)
- Developed with help from Claude
