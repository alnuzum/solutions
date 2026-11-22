# 2026-11-22 · Live E2E on main, the SMS-login pumping stand: stage report

## Design (written before the code)

**Live E2E.** Red every night since 11.11: `test_purchases_and_a_task_on_the_client[amocrm|kommo|zoho]`.
The pool's won deals of amoCRM, Kommo and Zoho are made once ("now − 60 days", "now − 20 days") and reused;
the test still checks `closed_at > now − (days + 2)`, true for two days after the pool was made. Fix in the
test: a deal made in this run keeps the exact window; a pool deal from an earlier run must be at least
`days − 1` days old. Nothing external is broken.

**Solution 6, `apps/otp-guard` (`alnuzum_otp`, pages `/{ru,en}/otp`)**: an SMS login whose provider is a
counter (nothing is sent, the code is shown on the page), the price of each SMS from public price lists, five
layers each behind a flag (`OTP_LAYERS`, by default all five):

1. `geo`: only the calling codes of the countries served (`OTP_COUNTRIES`, default `998`).
2. `limits`: counters with a growing pause (base × 2ⁿ after the free sends of an hour, capped) on the number,
   its range of 100 numbers, the device, the IP (/32, IPv6 /64) and the network (/24, IPv6 /48).
3. `human`: proof of work in the page (SHA-256, 18 bits), a challenge signed and bound to the session,
   single use, 10 minutes; a request without it is refused.
4. `conversion`: sends and confirmed codes per prefix (calling code + 2 digits) per hour; a prefix whose
   confirmed share over the last 30 minutes (sends older than 3 minutes, at least 20) falls below half of its
   normal share (the trailing day, 80 % until there is enough history) closes for an hour; an alert is
   logged (WARNING) and shown on the page.
5. `budget`: the price of the SMS of the trailing hour under a ceiling (`OTP_USD_PER_HOUR`, $20).

Order of checks: human → geo → closed prefix → limits → budget → send; the refusal names its layer.
State lives in memory of the one process (the platform runs one), pruned after a day and capped; a
production deployment keeps the same counters in Redis. Codes: 6 digits, a hash kept, 5 minutes, 3 tries,
bound to the session that asked; phone numbers in the log are masked, codes never logged.

**Lab mode** (the attack's spoofed IP, network and clock): headers `X-Lab-Ip`, `X-Lab-Net`, `X-Lab-Time` and
`POST /lab/otp/reset` exist only when the solution is built with `lab=True` (the attack script) or
`OTP_LAB=1` with `APP_ENV` other than `prod`/`stand`, and only for a loopback peer. In production the route is
404 and the headers are ignored; proved by tests and by requests to apps.alnuzum.com.

**The attack** (`apps/otp-guard/attack/simulate.py`, in-process over HTTP/ASGI, one simulated hour, seeded):
12 000 browsers of residential proxies (each its own IP in thousands of /24s, hundreds of networks, its own
fingerprint), 3 attempts each on numbers taken in a row inside ranges: half Uzbek, half expensive foreign
prefixes (RU, KG, TJ, AZ, ID); in parallel 600 honest users (mobile carriers behind shared NAT and home
broadband, 2 % with a Russian number, some ask twice, 90 % enter the code). Runs per configuration: no layer,
each layer alone, all five; plus layer 3 against a script without a browser. The attacker's browsers solve
the proof of work: in the lab its difficulty is lowered to keep the run short, and the CPU it would cost is
the solve time measured in headless Chromium (`attack/browsers.py`, playwright) × the solved challenges.

**Page**: try the login, the live counters of the stand (spend this hour, closed prefixes, alerts, shares by
prefix and hour), the table of the run from `results.json`, the command, links to the code.

## Rulings

- Ruling: layer 3 is a proof of work, not Cloudflare Turnstile — no Cloudflare account exists (none in the keychain or the
  notes, the DNS of alnuzum.com is at Namecheap), and Turnstile's test keys verify nothing — the cost: Turnstile's
  look at the browser itself is not measured; the PoW cuts off scripts entirely and only makes a real browser pay CPU
  time per attempt, which the run shows openly.
- Ruling: the counters live in the memory of the one process the platform runs — no new service on the server and
  nothing to migrate — the cost: a deploy resets the stand's counters; with several processes the same counters go to
  Redis (the interface is one class).
- Ruling: the "provider's network" is the /24 (IPv6 /48) — the stand has no IP-to-ASN database — the cost: residential
  proxies spread over thousands of /24s pass this key; production takes the ASN from GeoLite2-ASN or ipinfo.
- Ruling: layer 4 closes a prefix of the calling code and three digits (operator + first digit of its range), not the
  operator — closing a whole operator for a pumped range would lock out its honest subscribers — the cost: an attack
  spread over many ranges trips later; honest users inside a closed range wait an hour (counted in the table).
- Ruling: layer 4's "normal share" is the prefix's own share over the day before once it has 200 sends, 80 % until
  then — the stand starts empty — the cost: the first day trips on a fixed bar.
- Ruling: layer 1 serves +998 only — the brief's client works in Uzbekistan — the cost: 2 % of the run's honest users
  carry a Russian number and are kept out by it (counted in the table).
- Ruling: the hourly ceiling is $20 across all destinations, about 4× the honest hour of the run — the cost: alone
  it is a circuit breaker, it stops honest users too until the hour rolls over (counted in the table).
- Ruling: the lab's proof of work is 8 bits, the stand's 20 — 36 000 solutions of 20 bits in Python would take hours —
  the cost: the attack's CPU price is the median time a real headless Chromium needed at 20 bits × the attempts,
  not a solve of every one.
- Ruling: the code is shown on the page and nobody stands behind the login — it is a stand of the protection, not an
  account system — the cost: anyone can "sign in" to any number on the stand, as the page says.
- Ruling: lab mode is three locks — a flag (`lab=True` in the script or `OTP_LAB=1`), an environment other than
  `prod`/`stand`, a loopback peer — and in production its route is not mounted at all — the cost: none for production;
  the attack runs in process or against a local server only.

## Numbers (seed 7, one simulated hour; `results.json`; re-run after the narrow check's fixes: the pumping rows came out the same)

12 000 proxied browsers × 3 attempts (half Uzbek ranges, half RU/KG/TJ/AZ/ID ranges, numbers in a row), 600 honest users.

| configuration | code requests | SMS (attack / honest) | cost of the hour, $ | first refusal | attack stopped | honest kept out | attack refused by |
|---|---:|---:|---:|---:|---:|---:|---|
| no protection | 36,663 | 36,000 / 663 | 9,147.83 | — | — | 0 (0.0 %) | — |
| layer 1: countries | 36,676 | 18,105 / 670 | 148.32 | 00:00 | — | 6 (1.0 %) | geo 17,895 |
| layer 2: limits | 36,663 | 1,336 / 663 | 379.69 | 00:01 | — | 0 (0.0 %) | block 34,664 |
| layer 3: browser check (attack in browsers) | 36,663 | 36,000 / 663 | 9,147.83 | — | — | 0 (0.0 %) | — |
| layer 3 against a script without a browser | 36,663 | 0 / 663 | 10.45 | 00:00 | 00:00 | 0 (0.0 %) | human 36,000 |
| layer 4: confirmed share | 36,681 | 1,967 / 661 | 514.63 | 03:10 | 04:00 | 20 (3.3 %) | closed 34,033 |
| layer 5: spend ceiling | 36,601 | 194 / 1 | 39.99 | 00:07 | — | 599 (99.8 %) | budget 35,806 |
| **all five** | 36,686 | 90 / 661 | 5.93 | 00:00 | 00:00 | 23 (3.8 %) | geo 17,909, block 2,537, closed 15,464 |
| closing a stranger's range, layer 4 before the fix (all layers) | 770 | 21 / 660 | 5.38 | 02:30 | 06:00 | 17 (2.8 %); of the 12 with a number in +998 952: 11 | ip 20, closed 49; ranges closed: +998 952 |
| closing a stranger's range, layer 4 now (all layers) | 770 | 24 / 671 | 5.49 | 02:30 | 10:00 | 6 (1.0 %); of the 12 with a number in +998 952: 0 | ip 66; ranges closed: none |

Real headless Chromium (playwright 1.65.2, headless shell 159), all five layers, proof of work 20 bits: 20/20
browsers solved it, median 1509 ms, max 3963 ms (the machine ran at load average ~22–40 from other
sessions; an idle 3-browser probe gave a 420 ms median); 20 distinct device fingerprints; outcomes {'sent': 3, 'block': 17}
(layer 2's range of 100 stopped them); 20/20 plain-HTTP requests without the work refused by
layer 3. Attack CPU at that median: 36 000 × 1.51 s = 15.1 CPU hours.

Reproduce: `uv run python apps/otp-guard/attack/simulate.py && uv run --with playwright python apps/otp-guard/attack/browsers.py` (the table run took 576 s on 8 processes).

## Gate (local, the commands of CI's jobs, before the merge)

```
ruff check .                         All checks passed!
ruff format --check .                188 files already formatted
mypy --strict (CI's list + apps/otp-guard/src, apps/otp-guard/attack)   Success: no issues found in 146 source files
bandit (CI's list + the two new paths)   exit 0, no issues (two seeded generators of the attack marked nosec B311)
pip-audit --strict (uv export)       No known vulnerabilities found
pytest --cov --cov-fail-under=90     928 passed, 1 skipped, 25 deselected; total coverage 90.81 %
                                     alnuzum_otp: guard.py 98 %, web.py 97 %, numbers.py and pow.py 100 %
solutions discovered                 ['contracts', 'mail', 'orders', 'otp', 'voice', 'winback'] (CI's compose job checks the same list)
gitleaks git origin/main..HEAD       no leaks found
```

## Access probes (tests, before the merge)

| request | expected | test |
|---|---|---|
| `POST /ru/otp/send`, `/ru/otp/verify` without a session or CSRF token | 403 | `test_without_a_session_or_token_nothing_is_sent` |
| the same with a forged token / a foreign `Origin` | 403 | same |
| `GET /ru/otp/challenge` without a session | 401 | same |
| a proof of work issued to another session, or spent | refused, layer 3 | `test_a_spent_or_foreign_challenge_is_refused`, `test_proof_of_work_is_bound_signed_fresh_and_spent_once` |
| a code verified from another session | `expired` | `test_a_code_takes_three_tries_and_belongs_to_its_session` |
| `POST /lab/otp/reset`, `GET /lab/otp/state` in `prod`/`stand` (even with `OTP_LAB=1`) or without the flag | 404 | `test_outside_the_lab_its_headers_are_ignored`, `test_lab_mode_by_environment_only_outside_prod` |
| the same in the lab from a non-loopback peer | 403 | same |
| `X-Lab-Ip` / `X-Lab-Time` outside the lab | ignored: the real IP collects the IP limit, the number's pause is not reset | same |

## Narrow check (fresh agent) and the fixes before the merge

No Critical. Fixed before the merge:

1. Important, layer 4 as a weapon against honest users: 20 unconfirmed codes from a couple of addresses closed a
   range for everybody for an hour, again every hour. Now a range closes only when its unconfirmed codes in the window
   came from 8 or more networks (/24, IPv6 /48) and the window holds 3× the range's normal volume (the day before)
   besides the share falling under half of normal (`Config.networks`, `Config.surge`). The run got a "closing a
   stranger's range" scenario (one visitor, three IPv4 /24s, a code every 10 s for 15 minutes to the range most honest
   numbers fall in, all layers on) run with the old rule and the new one; see the table.
   Ruling: count networks and volume rather than raise the proof of work for the range — a griefer with real
   browsers pays a harder proof of work as easily as an attacker does — the cost: a griefer with residential proxies
   in 8+ /24s at 3× a range's volume can still close it; a sparse range (no history) needs only the 20-code floor.
2. Minor, lab mode behind `X-Forwarded-For`: `OTP_LAB` is gone (only code passes `lab=True`), lab mode exists only in
   `dev`/`lab` environments (an allowlist), and a request with `X-Forwarded-For` or `Forwarded` is never a lab peer.
3. Minor, `?w=²` or 5 000 digits gave 500: the pause is read only as at most five ASCII digits.
4. Minor, Live E2E: when both pool deals come from an earlier run, their closed_at must lie 40 ± 1 days apart (a
   closed_at read from the creation date would put them minutes apart).

Limitation, by design: the device fingerprint is computed by the page and any client can forge it; an antidetect
browser changes it on every request, so the device limit holds only a lazy attack (README says so).
