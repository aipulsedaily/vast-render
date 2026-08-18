# Render economics — what a 124-second 4K film cost on rented GPUs

Written 2026-08-18, from the campaign that produced the companion film
(`f1-round2`): **2,978 frames at 3840×2160, 512 samples, Cycles, one unbroken
camera take**. Every figure below carries **what it counts**, **how it was
established** (MEASURED / DERIVED / QUOTED) and **where it came from**. A figure
without those three is not published here.

> **This document exists because the first version of these numbers was wrong.**
> The GPU total published for this project was **161.9 GPU-hours at
> $1.42/GPU-hour**. The true figures are **393.5 GPU-hours at $0.58/GPU-hour**.
> The error was not in the arithmetic and not in the database — it was in
> *which table was summed*. [Section 2](#2-what-the-broker-databases-do-and-do-not-record)
> is that mistake in full, and it is the single most useful thing in this file
> for anyone planning a render on this platform.

---

## 1. The headline

| | | class | source |
|---|---|---|---|
| **Total account spend** | **$229.76** | QUOTED | vast.ai account Credit Usage, 2026-07-19 → 2026-08-18. Instances $229.76, serverless $0.00, volumes $0.00 |
| **Total GPU time** | **393.5 GPU-hours** | DERIVED | 161.9 h of test/probe/retry + 231.6 h of delivered master, established as disjoint sets — §2 |
| **Effective rate** | **$0.58 / GPU-hour** | DERIVED | $229.76 ÷ 393.5 h. An **upper bound** — see §2.3 |
| **The delivered master alone** | **231.7 GPU-hours, ~$132.57** | MEASURED / QUOTED | §3 |
| jobs submitted | 2,764 | MEASURED | `SELECT COUNT(*) FROM jobs` over 12 broker databases |
| frame records written | 10,954 | MEASURED | `SELECT COUNT(*) FROM frames` over the same 12 |
| frames delivered | 2,978 | MEASURED | one row per delivered frame, 2,978 distinct sha256 |
| **renders per delivered frame** | **3.68** | DERIVED | 10,954 ÷ 2,978 — test, preview, probe, proving and retry passes |

The spend figure is corroborated rather than merely quoted: credits added over
the period total $275.00, spend $229.76, leaving $45.24 against a balance of
$45.23 — and one broker's own `meta.credit` row independently reads
`{"usd": 45.2304}`. Two ledgers, one cent apart.

**Context worth stating once.** The model-inference bill for the same project was
**$20,740.23** — **90×** the GPU bill. Rendering a 4K film for four months of
compute was the cheap part. Do not plan a project around GPU rental cost until
you have priced the thinking.

---

## 2. What the broker databases do and do not record

Each broker keeps one SQLite file, `state<N>/broker.db`, with two tables that
both hold render time:

| table | one row per | what its `render_sec` means |
|---|---|---|
| `jobs` | **submitted job** — which may be one frame or a thousand | the render seconds the broker attributed **to that job record**. For a long resumable `anim` job it is *not* the sum of its frames |
| `frames` | **delivered frame** — including retries, previews and every pass | the seconds that **one** frame took on the GPU. This is the primary record |

### 2.1 The mistake: summing the wrong table

The published 161.9 h came from:

```sql
-- WRONG for anything containing an anim job. This is the query that produced 161.9 h.
SELECT SUM(COALESCE(render_sec, exec_sec, 0)) FROM jobs;   -- over all 12 state*/broker.db
```

Verified again for this document: **582,907.1 s = 161.92 GPU-hours over 2,764
job rows.** The query is right, the sum is right, and the answer is wrong,
because the master render's 2,978 frames were dispatched as a handful of long
`anim` jobs whose job rows carry only a fraction of the work they performed.
Broker 4's master job row reads `render_sec = 31,940.9`; its `frames` rows for
the same broker sum to **318,282.9** — a factor of **10**.

The same sum over the table that actually records frames:

```sql
-- What the fleet really rendered.
SELECT SUM(render_sec) FROM frames;                        -- over all 12 state*/broker.db
```

**1,210,393.4 s = 336.2 GPU-hours** across 10,954 frame records.

### 2.2 The correction to the correction

The published correction attributes the miss to the master's brokers being
remote, their databases living on the rented machines and therefore never
summed. **That is not what happened, and it matters for anyone auditing their
own run.** The fleet's brokers run *locally* — one local broker process per
rented card — so their databases are right here. All 2,978 delivered frames are
in `state3/`, `state4/` and `state5/`, under a single `spec_hash`
`3cf8d9c4de51280f`, in three contiguous blocks:

| database | frames | rows | render seconds | mean |
|---|---|---:|---:|---:|
| `state3/broker.db` | 1–993 | 993 | 242,034.1 | 243.7 s |
| `state4/broker.db` | 994–1986 | 993 | 308,984.6 | 311.2 s |
| `state5/broker.db` | 1987–2978 | 992 | 282,940.2 | 285.2 s |
| **total** | **1–2978** | **2,978** | **833,958.9 s = 231.66 h** | **280.04 s** |

2,978 rows, 2,978 distinct frame numbers, 2,978 distinct sha256. The right
verdict was reached for the wrong reason: **the master was never off the box, it
was in the other table.**

This upgrades the master's 231.6 GPU-hours from DERIVED (280.0 s mean × 2,978)
to **MEASURED** (the sum itself), and the two agree to 0.01 %.

### 2.3 What the databases never record at all

Even a correct sum over `frames` counts **GPU time spent rendering**. vast.ai
bills **wall-clock rental of the whole instance**, from create to destroy. The
gap is everything in between, and on this campaign it was about a quarter of the
money:

| billed, never in `render_sec` | measured here |
|---|---|
| boot, image pull, Blender install | cold start rent → worker ready: **502 s** on a healthy host |
| scene upload | 258 / 356 / 613 / 625 s for a 7.98 GB scene pushed to four cards, **mean 463 s per card ≈ $0.06** |
| idle between jobs, and waiting on the slowest card in a fleet | the master's fleet ran at **~79 % GPU utilisation** |
| the 12-hour watchdog retirement and its restarts | 37 cold starts ≈ **6.2 h** projected for a 3,000-frame master on one card |
| teardown, and storage while an instance merely *exists* | see §6 — this is the one that bites |
| egress | $1.30/TB measured; **$0.03** for 23 GB of delivered frames. Not a consideration |

So:

**rental hours ≈ GPU-render hours ÷ 0.79** — a **1.27×** multiplier, measured on
this campaign at fleet width 3. Budget with that, not with render time.

And therefore **$0.58/GPU-hour is an upper bound on the rate and a lower bound on
nothing.** It divides the *whole* account spend by *render* hours only. The true
per-rented-hour price paid was **$0.428–$0.455/hr** off the API. The previously
published $1.42/GPU-hour was inflated 2.4× purely because its denominator had
lost the master.

### 2.4 How confident is 393.5?

Honestly: to within a few percent, and it is the high end of a narrow band.
Three independent methods over the same twelve databases:

| method | GPU-hours | what it misses |
|---|---:|---|
| `jobs` only (the published 161.9) | 161.9 | the entire master. **Discard** |
| `frames` only | 336.2 | jobs that write no frame rows — EXEC/CPU jobs, canceled and failed work |
| per-database `max(jobs, frames)` | 383.8 | nothing structurally, but it under-counts where both tables hold disjoint work |
| **published: 161.9 + 231.7** | **393.5** | nothing — but it can **double-count** master seconds that also appear in job rows |

That double-count is bounded and small: every 4K/512 job row across the three
master brokers sums to **53,561 s = 14.9 h**, and not all of it is master work.
So the defensible range is **~379–394 GPU-hours**, and **393.5 is the ceiling of
it**. Published as the headline because it is the authoritative project figure
and because a cost-per-hour computed from the largest credible denominator is
the conservative one for a reader budgeting their own run.

**Reproduce any of this yourself:**

```bash
for db in state*/broker.db; do
  echo -n "$db "
  sqlite3 "$db" "SELECT (SELECT COUNT(*) FROM jobs),
                        (SELECT ROUND(SUM(COALESCE(render_sec,exec_sec,0))) FROM jobs),
                        (SELECT COUNT(*) FROM frames),
                        (SELECT ROUND(SUM(render_sec)) FROM frames);"
done
```

---

## 3. The delivered master

| | | class |
|---|---|---|
| scene | one blend, 10.96 GB, 46,267 objects | MEASURED |
| output | 3840×2160, 24 fps, 2,978 frames, **zero cuts**, one camera | MEASURED |
| engine | Cycles, **512 samples**, adaptive threshold 0.01, OpenImageDenoise | MEASURED |
| colour | AgX, look None, exposure −3.628 stops, SDR | MEASURED |
| hardware | **3 exclusive whole-machine RTX 5090s**, $0.428 / $0.454 / $0.455 per hour | QUOTED off the API |
| split | 993 / 993 / 992 frames, contiguous blocks, exact partition | MEASURED |
| **GPU render time** | **833,958.9 s = 231.66 GPU-hours** | **MEASURED** (§2.2) |
| wall clock | **~97.3–97.7 h**, 2026-08-09 → 2026-08-13 | DERIVED from launch and completion timestamps |
| machine-hours rented | ~292 (3 cards × ~97.5 h) | DERIVED |
| **GPU utilisation** | **~79 %** | DERIVED — 231.7 render-hours over ~292 machine-hours |
| **cost** | **~$132.57** | QUOTED, and an **upper bound** — see below |
| peak VRAM | **5.5 GB** | MEASURED — a 17.7-billion-triangle frame fits in 5.5 GB because 4.97 M instances resolve to ~1,569 distinct source meshes |
| peak host RAM per worker | 52.4 GiB high-water, 64.5 GiB cgroup peak | MEASURED — **this, not VRAM, is what host selection must clear** |
| frames returned | 21.74 GiB, mean **7.48 MiB/frame** | MEASURED, `SUM(bytes)` over the master's frame rows |

**Why $132.57 is an upper bound and is labelled QUOTED.** The three brokers'
cumulative `meta.spend_usd` reads $38.03 + $53.14 + $49.89 = **$141.06**, and
that is each broker's *whole life*, including work before and after the master.
$132.57 is $141.06 less $8.49 of pre-existing banked spend. The film project's
own documentation audit lists the master's isolated cost as unresolved and
explicitly forbids publishing $141.06. The independent size check works:
$132.57 ÷ ~292 machine-hours = **$0.454/machine-hour**, against quoted rates of
$0.428/$0.454/$0.455 — agreement to ~2 %.

Two derived rates worth carrying away:

* **$0.57 per GPU-render-hour** for the master ($132.57 ÷ 231.66 h).
* **$0.045 per delivered 4K frame** ($132.57 ÷ 2,978).

**A note on the film's published output size.** The delivered set is quoted as
23.5 GiB; the broker's own `bytes` column sums to **21.74 GiB (23.34 GB)** for
the same 2,978 frames. The two are the same number under different units. If you
are reconciling storage, check the unit before you look for a missing file.

---

## 4. Per-frame economics — enough to price your own job

Measured across all 2,978 delivered frames, 3840×2160 at 512 samples, adaptive
0.01, OIDN, on RTX 5090:

| | seconds |
|---|---:|
| minimum | **194.7** |
| **median** | **283.8** |
| mean | 280.0 |
| maximum | **445.1** |

Spread is **2.3× min-to-max on the same scene and the same settings**. Three
things drive it, and only one of them is the picture:

1. **Content.** Different beats of the same take differ by well over a minute.
2. **The host.** Four exclusive 5090s rented within two minutes of each other
   returned sustained means of 233.4 / 248.3 / 249.7 / 282.9 s on identical
   work — a **1.21× speed spread**. Combined with a 1.15× price spread the
   **cost per frame spread 1.32×**, and *the fastest card was not the cheapest
   per frame*.
3. **The first frame of any broker.** BVH build. Individual frames ranged
   227–432 s in the proving run and every 432 s frame was a broker's first.
   Never estimate from a first frame.

**To price your own run** — the arithmetic that would have got this campaign
right the first time:

```
GPU-hours      = frames × median_seconds_per_frame ÷ 3600
rental-hours   = GPU-hours ÷ 0.79          # boot, push, idle, retirement, teardown
cost           = rental-hours × $/hr       # use dph_total off the API, not the broker's dph
wall-clock     = rental-hours ÷ cards      # ×1.1 for the slowest-card tax at width 4+
```

For this film: 2,978 × 283.8 ÷ 3600 = 234.7 GPU-h → 297 rental-hours → at
$0.4556/hr, **$135**. Against $132.57 actually paid. The model is good to a few
percent **once it uses a measured median from the real scene**.

**The estimate that was made before the run, for comparison.** `operations.md`
projected the master at **510.5 s/frame and $185 as a floor**, from 4K/512
stills of an earlier assembly on one card. The delivered master came in at
**280.0 s/frame and ~$132.57** — the projection was 1.8× high on time and 1.4×
high on money, and it said "floor". Scene optimisation between projection and
delivery accounts for it. The lesson is not that the projection was bad; it is
that **a per-frame projection is only as current as the scene it was measured
on**, and it must be re-measured after any change to the geometry.

---

## 5. The other 161.9 GPU-hours

10,954 frame records against 2,978 delivered frames is **3.68 renders per
delivered frame**. That is not waste; it is what the delivered frames cost to
become deliverable. From the databases, the non-master work breaks down as:

* **preview and proxy passes** — 960×540 @ 32 samples and 1280×720 @ 64 samples
  dominate the record count. One broker holds 2,979 frames at a 42.5 s mean:
  a complete low-resolution pass of the whole film, for a fraction of a master
  frame's price each.
* **calibration and probing** — sample counts, denoiser variants, per-host rate
  records. The proving run alone was 48 frames at full quality across four
  cards for **$2.00**.
* **retries** — condemned hosts, OOM kills, lost transfers, blank frames.

**Budget a factor of 2–4 on frame count, not 1.0.** Anyone costing a film as
`frames × seconds × rate` will be out by that factor, and the entire difference
is passes that never ship.

---

## 6. Practical guidance — the parts that cost real money

Every item here is an incident this repository has already paid for. The detail
is in `docs/operations.md` and `docs/incidents.md`; this is the money view.

### How many instances to run

**N single-GPU brokers, N exclusive cards. Not one wide box.** Measured, on
this market, for this master:

| architecture | wall clock | total $ |
|---|---:|---:|
| 1 broker, 1 card | 163.1 h | $74.11 |
| **8 brokers, 8 cards** | **20.4 h** | **$74.21** |
| 1 broker, 8 workers on an 8× box | 30.4 h | $83.60 |
| 1 broker, 1 worker on an 8× box | 186.6 h | $512.05 |

**8× the speed for +0.1 % of the money and no new code.** Width is nearly free
because the meter is per-card-hour either way; what width buys is wall clock,
and what it costs is the per-card fixed overhead (one scene push, one cold
start each — measured at **$0.06 and ~10 minutes per card**, concurrent).

Three real limits on how wide to go:

* **Supply, not budget.** At one measured timestamp only **7–11 exclusive
  single-GPU 5090s** cleared the production filter at once. Eight is reachable
  with no comfortable margin, and N brokers shopping one market converge on the
  same cheapest listing — one broker took an offer another had just rented and
  got a 400.
* **Your own workstation.** Eight concurrent `zstd -10` compressions of an
  8 GB scene serialise on a 6-core box. The uplink sustains ~27 MB/s; four
  concurrent pushes measured ~11 MB/s on the wire, so eight *should* fit — but
  that is arithmetic, not a measurement.
* **The slowest card sets the pace.** Four cards delivered **3.31× wall-clock
  speedup, 83 % of theoretical** (90 % in steady state). The missing 10 % is
  predicted almost exactly by `mean ÷ slowest`. Weight the blocks by measured
  per-host rate, and record those rates **before** teardown — after `down` the
  machine id is gone and the rate can never be attributed again.

**Do not buy a wide box on reasoning.** The test is one frame and about $1:
rent it, render a known frame, compare against a known single-card time. This
repository got that question wrong twice on paper before measuring it.

### What bad hosts cost

* **Base rate ~25 %** of rented hosts are unusable in some way.
* **The cost of discovering one is small: ~5 minutes and ~$0.05** of billed
  rental, plus a temporary price rung when the broker falls through to the
  next-cheapest offer. That rung is transient, not cumulative.
* **The cost of *not remembering* one is large.** One host reset every SSH
  connection it was given; the broker burned **80 minutes and $0.41** learning
  that, destroyed the instance, and **re-rented the same offer one second
  later** because it was still the cheapest. Bounded failure detection cut that
  to ~25 minutes; a fleet-wide 7-day blacklist stopped the re-rent.
* **No condemned host has ever recovered.** Two were re-drawn from the market
  later and condemned again on sight, at 24 h and 61 h 19 m. That is why the TTL
  is measured in days.
* **The expensive failure is not the host that fails, it is the host that is
  merely slow.** One box passed every health probe while delivering 14 KB/s
  down against 731 KB/s up: a 7.5 MB frame took **six minutes to fetch against a
  16-second render**, and it billed **68 % of a rental** before anyone noticed.
  Nothing that counts *failures* can see that. Throughput is a first-class
  health signal for this reason.
* **Co-tenancy is invisible from inside the container, and it corrupts output
  rather than failing.** A neighbour holding 17.7 GB of a 32 GB card made Cycles
  return a zero-filled buffer that became a structurally perfect PNG — right
  size, right sha256, no picture. Ask for `gpu_frac >= 0.99`. Exclusivity costs
  about **8 %** and is worth every cent of it.
* **Sort offers by projected total cost, not sticker price.** GPU rates cluster
  within ~2 %; disk rates spread ~6×. Two offers at $0.308 and $0.313/hr came to
  $19.17 and $23.18 over 60 GPU-hours — **$4 apart, almost entirely disk**.

### How retirement works, and why

Instances are **retired on a hard 12-hour wall-clock cap**, enforced in the
broker *and* in an on-instance watchdog. A multi-day master is therefore
interrupted repeatedly and *by design*. Each interruption costs the frame in
flight plus one cold start; nothing already delivered is lost, and the work
resumes on fresh hardware. For a 3,000-frame master that is roughly **37 cold
starts ≈ 6.2 h**, which is inside the 21 % that §2.3's utilisation figure
already accounts for.

Retirement is not only a safety rail — it is also what makes the 7-day host
blacklist coherent: the fleet re-asks "which host?" every 12 hours anyway.

### How to avoid paying for GPUs you are not using

This is the failure mode the repository was built around, and it has still
happened during development. In order of how much they cost:

1. **Destroy, never stop.** Storage bills while an instance *exists*. `stop`
   only ends the GPU meter. A stopped instance is a quiet, indefinite bill.
2. **`--cancel-unavail` on every create.** Without it a failed schedule leaves a
   *stopped* instance behind — an orphan generator.
3. **Assume the broker will die.** Four independent destroy paths cover normal
   exits (`try/finally`, `atexit`, SIGTERM/SIGINT), startup reaping by label,
   and `scripts/panic.sh`. Only one survives a power cut: the **on-instance
   watchdog**, which self-destructs the box if the broker heartbeat goes stale
   (30 min) or the 12 h cap is hit. It authenticates with the per-instance key
   vast injects, so it can destroy itself and nothing else.
4. **Verify teardown against the API, not against the broker.** A broker can
   only see its own label. `fleetctl down` re-queries vast, retries with a
   settle window, exits non-zero naming anything still alive with its $/hr and
   $/day, and counts a *stopped* instance as alive.
5. **Prepaid credit with autobilling off is the only hard ceiling the platform
   offers.** Everything above is software you wrote. Treat the credit balance as
   the real cap.

An idle queue is not an idle GPU: this broker has stopped an instance that was
at 99 % and 420 W mid-frame. Both directions of that mistake cost money.

---

## 7. Sources

Everything here traces to one of four places, and nothing was retyped from
memory:

| figure class | source |
|---|---|
| GPU-hours, job and frame counts, per-frame seconds, byte totals | the twelve `state*/broker.db` files in this repository, queried 2026-08-18. Every SQL statement needed is quoted above |
| account spend, effective rate, the seven corrections | the film project's authoritative cost record and its reconciliation working, 2026-08-18 |
| render settings, VRAM, RAM, output size, wall clock | the film project's master runbook and staging logs |
| host rates, throughput, incidents, architecture comparison | `docs/operations.md`, `docs/fleet.md`, `docs/multi-gpu.md`, `docs/incidents.md` in this repository, each carrying its own measurement date |

**What this document does not establish.** The master's *isolated* dollar cost.
$132.57 is the best available figure and it is an upper bound derived from
broker-lifetime spend, not an invoice for the render. vast.ai's billing is
per-instance-hour and does not decompose by job, so isolating it would require
having rented dedicated instances for the master and nothing else. That was not
done, and no amount of re-reading the databases will produce it.
