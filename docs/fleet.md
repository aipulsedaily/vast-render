# The fleet — N brokers, N single-GPU cards, one operation

Built and proven 2026-08-07. `fleetctl` at the repo root, `farm/` beside it.
Read `multi-gpu.md` for *why* the alternatives lose; this is how to run the one
that wins, and what it actually measured.

```
.venv/bin/python fleetctl plan   -n 4 --frames 2715-2762 --sec-per-frame 235 --push-sec 300
.venv/bin/python fleetctl up     -n 4 --scene /home/zany/f1-round2/render/film17_R2943.blend --cap 8
.venv/bin/python fleetctl submit -n 4 --scene ... --frames 2715-2762 --name proof \
                                 --res 3840 2160 --samples 512 --cam ONER
.venv/bin/python fleetctl status -n 4
.venv/bin/python fleetctl record --manifest state/fleet/proof.json --spec-hash <h>   # BEFORE down
.venv/bin/python fleetctl verify --manifest state/fleet/proof.json
.venv/bin/python fleetctl down                                                       # and it PROVES it
```

`plan` spends nothing. `up` rents nothing — an instance appears only when work
is submitted. `record` must run **before** `down`, because after teardown the
machine id is gone and a rate can never be attributed to the silicon that
earned it.

---

## Brokers 1 and 2 are untouchable

`fleet(n)` is brokers **3..n+2**, always. Broker 1 (8760) is `rq`'s default and
the stills queue; broker 2 (8761) is `rq`'s `BULK_URL` and carries client work.
Both are `protected` in `farm/brokers.py`, `scripts/brokerN.sh` refuses them by
index, and no `fleetctl` verb can reach them.

## Addressing: there is no call that gets it wrong

`rq` addresses a broker **by URL and nothing else** — `VASTRENDER_BROKER` and
`BROKER_STATE` do not exist (grep the tree: zero hits). A bare `rq status` reads
`VASTRENDER_URL` or silently answers for 8760. R2-979 reported one broker's
status as two and the output was not wrong, it was **unattributed**.

Worse: **`rq` silently re-routes.** `anim` and `seq` go to `VASTRENDER_BULK_URL`
(8761) unless `VASTRENDER_URL` is set. Eight `rq anim` calls in a loop send all
eight blocks to broker 2 — the live client card — and mention it only on
stderr, on the seventh iteration.

So `farm/brokers.py` does not offer a URL to check. It offers a **broker**, and
the URL, state dir, label, both tunnel ports and the output dir come off one
frozen object. `broker(n)` is the only constructor; `rq_env()` pins
`VASTRENDER_URL` and drops `BULK_URL`. Every command prints the identity it
used: `#3 http://127.0.0.1:8762 state3 fleet03`.

`Broker.verify()` then asks the **kernel**: it finds the pid holding the
listening socket through `/proc/net/tcp` + `/proc/*/fd` and asserts that pid's
`/proc/<pid>/environ` carries the declared `VASTRENDER_DB`, `_PORT` and
`_LABEL`. That is how the process was started, not something it says about
itself. `up` and `submit` refuse a broker that fails it.

## The two invariants that destroy cards if broken

**Labels must be pairwise prefix-disjoint.** `vastctl.our_instances` selects
with `startswith` and `Fleet.adopt_or_reap` **destroys every instance it
returns bar the one it adopts**. The obvious naming fails: **`fleet1` is a
prefix of `fleet10`**. Fleet labels are fixed width (`fleet03`, `fleet10`) so
disjointness follows from distinctness, and `_assert_disjoint()` checks all
12×12 pairs at import, including against the two live literals.

**Tunnel ports must not collide.** `app` calls
`remote.reap_stale_tunnels(local_port)` at startup, which pgreps for
`-L <port>:127.0.0.1:8799` and SIGKILLs every match that is not its own child. A
duplicate does not fail to bind — it kills a sibling's forward mid-frame, and
the sibling reads that as bad hardware on a good box.

Broker n: port `8760+(n-1)`, tunnel `8800-2n`, exec `8799-2n`, `state{n}`,
`out{n}`. Broker 1 and broker 2 fall out of the formula exactly as they are
already running, which is asserted in `--selftest`.

## Exactly-once

Blocks are **contiguous and disjoint by construction** — `split()` verifies the
partition before anything is submitted, and `--frames` refuses a stride, because
PNGs from different hosts are not bit-identical (different driver, different
OIDN build) and striping would put a machine boundary between every adjacent
frame pair.

`fleetctl verify` then proves it on the bytes: every frame present, hashed, and
cross-checked against the sha256 **the broker independently recorded at fetch
time** in its own `frames` table — plus one resolution across the whole set and
no blank-gate failures. Hashing a file and comparing it to itself proves
nothing.

`db.claim`'s cross-process atomicity is separately proved in
`farm/test_claim_crossproc.py` (8 real processes, one queue), with a control
that fails. See R2-1241.

## Teardown is not finished when the broker says so

`rq teardown` reports success from one broker, which by design cannot see any
other label. `fleetctl down` re-queries the **API** afterwards, retries with a
settle window, and exits non-zero naming any fleet instance still alive with its
`$/hr`, its `$/day` and the exact `vastctl destroy` line. It also names every
**non-fleet** instance still billing, so `fleet down` cannot read as `farm
down`, and counts `cold` as alive — a stopped instance has stopped paying for
the GPU and is still paying for its disk.

---

## MEASURED 2026-08-07, 15:03–15:2x UTC — the scene push

**The question:** eight rentals need eight copies of the 8 GB scene, where one
wide box needs one. `multi-gpu.md` calls that *"the single strongest argument
for one 8-GPU box over eight 1-GPU instances."* Prior evidence was contradictory
— 4–5 MB/s uplink measured at R2-1047, 617 s for one push from the card probe,
and a 4.5× fetch spread between two live hosts.

**Four brokers pushed `film17_R2943.blend` (7,980 MB) concurrently:**

| broker | host | blender 481 MB | scene 7,980 MB | scene MB/s raw | rent→ready |
|---|---|---|---|---|---|
| 5 | 192.0.2.20 | 8.9 s (54.4 MB/s) | **191.5 s** | 41.7 | 258 s |
| 6 | 192.0.2.21 | 15.7 s (30.7 MB/s) | **242.6 s** | 32.9 | 356 s |
| 3 | 192.0.2.22 | 44.3 s (10.9 MB/s) | **389.8 s** | 20.5 | 613 s |
| 4 | 192.0.2.23 | 63.8 s (7.5 MB/s) | **439.9 s** | 18.1 | 625 s |

All four overlapped for 106 s; three for a further 119 s. 31,920 MB of source
moved in 527 s wall = **60.6 MB/s aggregate**, and the blend compresses **5.43×
at zstd -10** (measured on a 400 MB sample: 102 MB/s compression throughput, so
compression is not the constraint either) — so ~**11 MB/s on the wire**, against
the ~27 MB/s this uplink is known to sustain.

**They did not serialise, and the local box was not the limit.** The proof is
the rank order: each host's scene rate tracks the rate that host took the
Blender bundle at **before** the scene contention began, in exactly the same
order. The spread is host ingest — the ±45 % host lottery applying to the push —
not local contention. Note also that the two slow hosts went *faster* on the
8 GB payload than on the 481 MB one: fixed per-transfer overhead amortising,
exactly as `incidents.md` warns.

### What the push actually costs

Paid, non-rendering rental time from `renting offer` to `deploy finished`:
**258 / 356 / 613 / 625 s, mean 463 s per card.** At the mean live rate of
$0.4556/hr that is **$0.059 per card — $0.47 for eight.**

Against a master of ~$82 that is **0.6 % of the money**, and because the pushes
run concurrently the fleet's cold start is bounded by its *slowest* card:
**625 s = 10.4 min against a 20.4 h render, 0.85 % of the wall clock.**

> **The strongest argument for a wide box is worth about half a dollar and ten
> minutes.** It does not change the ranking. `multi-gpu.md` has now been wrong
> about this class of question twice by reasoning about it on paper; this is a
> measurement, at N=4, on the shipping 7.98 GB film scene.

**Honestly labelled extrapolation:** at N=8 the same scene is ~22 MB/s on the
wire against a ~27 MB/s known ceiling — it should still fit, but that is
arithmetic, not a measurement, and the first eight-wide run should watch the
aggregate. If a future scene grows or the uplink degrades, the push is the first
thing to re-measure, and `fleetctl status` now prints live push throughput per
broker for exactly that reason.

## MEASURED 2026-08-07 15:03 UTC — the live rate, with a timestamp

Every figure with a `$/hr` in it needs a timestamp, because offers churn
continuously on this platform. Four cards rented within two minutes of each
other, `dph_total` **off the API** (which includes storage; the broker's
headline `dph` does not, and is 1.5–8.9 % low):

| instance | machine | API $/hr | gpu_frac |
|---|---|---|---|
| 47088518 | 144732 | 0.4237 | 1.000 |
| 47088546 | 127280 | 0.4356 | 1.000 |
| 47088573 | 137580 | 0.4741 | 1.000 |
| 47088605 | 43130 | 0.4889 | 1.000 |

**Mean $0.4556/hr, 15 % spread, all four exclusive.** Against the $0.4488 /
$0.3999 the original comparison was built on, the market has moved up ~7–14 %,
so every dollar figure in `multi-gpu.md` is low by roughly that much.

### And what each machine actually achieved — `farm/hostrates.json`

Same scene hash, same `spec_hash`, 12 frames each, all four rented within two
minutes of one another:

| machine | API $/hr | s/frame | **$/frame** |
|---|---|---|---|
| 144732 | 0.4237 | 248.3 | **0.02922** ← cheapest per frame |
| 127280 | 0.4356 | 249.7 | 0.03021 |
| 137580 | **0.4741** | **233.4** ← fastest | 0.03074 |
| 43130 | 0.4889 | 282.9 | 0.03843 |

**The fastest card is not the cheapest per frame** — 137580 renders 6 % faster
for 12 % more money, so it is 5 % dearer per frame. Price spread 1.15×, speed
spread 1.21×, and they compounded: **$/frame spread 1.32×**.

**The ±45 % host lottery is too big a number for exclusive 1× cards.** Four
cards, same work, same hour: **1.21×**. The ±45 % came from including an 8-GPU
box's per-GPU rate alongside two 1× hosts. Exclusive single-GPU 5090s cluster
within ~1.2× on sustained rate. (Individual frames ranged 227–432 s = 1.9×, but
every 432 s frame is a broker's *first* — the BVH build. Sustained means are
what a master sees.)

**Supply, same timestamp:** 22 offers passed the full production filter; **7
were exclusive single-GPU**, 15 were shared, 0 were multi-GPU. Plus the 4
already held, that is **11 exclusive machines available at once**. Eight is
reachable today with three to spare, and that is not comfortable margin — see
the fleet-width note in `STAGING-R2-1241-to-R2-1270.md`.

One real fleet-scale effect: **broker 6 tried to rent the offer broker 4 had
just taken** (37398591) and got a 400. `_rent` fell through to the next offer in
1 second and nothing was lost — but N brokers shopping one market converge on
the same cheapest listing, and at N=8 that happens more often.

## MEASURED 2026-08-07 15:03–16:09 UTC — the proving run

48 frames of `film17_R2943.blend` (hash `ec95e539bb6a04d4`), frames 2715–2762,
3840×2160 / 512 spp, camera ONER, `spec_hash c49ed585b3812fe5`, across brokers
3–6 on four separate exclusive 5090s.

```
present 48   missing 0   duplicated 0   outside range 0
48/48 sha256 match the hash the BROKER recorded at fetch time
one resolution (3840x2160)   blank gate 48 OK   48 distinct hashes

fleet wall            3,954 s = 1.098 h
single card (measured
counterfactual)      13,069 s = 3.630 h
                     = 1 median deploy + every frame's render_sec + serial collect

WALL-CLOCK SPEEDUP   3.31x on 4 cards   (83 % of theoretical, deploys included)
STEADY STATE         3.60x              (90 %, render+collect only)

COST off the API      $2.0019   planned $1.12-$2.50
TEARDOWN              0 fleet instances alive, verified against the API
```

**Serial broker work: median 10.30 s/frame** (5.39–21.22, n=44) — 4.1 % of a
253.6 s frame, on *each broker's own dispatch thread in its own process*. It
does not sum across brokers, so the dispatch thread cannot saturate at any N.
The "42 % busy at eight workers" figure belongs to the one-broker-N-workers
design, not to this one.

**Where the missing 10 % goes:** the fleet waits for its slowest card. Mean
253.6 s/frame against the slowest host's 282.9 gives `mean / slowest = 89.6 %`,
which predicts the measured 90 % steady state almost exactly. That is what
`--weights` is for, and why `record` is worth $0.02 a host.
