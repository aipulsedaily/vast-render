# Operations

Runbook for whoever owns the money. Agents should read
[agents.md](agents.md) instead.

## Running the broker

```bash
cd ~/vast-render
scripts/brokerd.sh start /home/zany/opus5-car-render/work/f1_complete.blend
scripts/brokerd.sh status
scripts/brokerd.sh stop        # stops supervising, then kill -9 (keeps the GPU)
```

That supervises it and — more importantly — **detaches it**. Read the next
section before starting one any other way.

In the foreground, for a quick look:

```bash
VASTRENDER_SCENE=/home/zany/opus5-car-render/work/f1_complete.blend \
  .venv/bin/python -m broker.app
```

It listens on `127.0.0.1:8760`, rents a GPU when the first job arrives, stops it
after `IDLE_GRACE` and destroys it after `HIBERNATE`. Stopping the broker tears
the instance down — an instance must never outlive its broker — **unless it is
mid-render**, in which case the frame wins and the in-container watchdog becomes
the backstop. See "Never let something else own the broker's lifetime" below.

Useful overrides, all `VASTRENDER_`-prefixed:

| var | default | notes |
|---|---|---|
| `SCENE` | `./scene.blend` | the locally assembled blend |
| `IDLE_GRACE` | `300` | seconds of idle before the instance is *stopped* |
| `HIBERNATE` | `3600` | seconds stopped before it is *destroyed* |
| `MAX_BATCH_USD` | `20.0` | pauses and destroys past this |
| `DISK_GB` | `30` | instance disk |
| `MAX_PER_AGENT` | `25` | queued-job cap per agent |
| `MAX_QUEUE_DEPTH` | `200` | global admission limit |
| `POLL_INTERVAL` | `10.0` | do not lower — vast rate-limits per IP |
| `DEPLOY_ATTEMPTS` | `3` | deploy retries per dispatch pass, same GPU |
| `MAX_TRANSPORT_ROUNDS` | `3` | rounds of transport failure before the GPU is replaced |
| `MAX_STALLED_ROUNDS` | `2` | *hopeless* rounds — see below — before the GPU is replaced, whatever `MAX_TRANSPORT_ROUNDS` still allows |
| `PUSH_STREAMS` | `8` | concurrent SSH connections per bulk push |
| `PUSH_SERIAL_AFTER` | `2` | all-streams-reset failures before the push drops to one stream |
| `RECONCILE_AFTER_HEARTBEATS` | `3` | failed heartbeats before asking vast.ai whether the instance still exists |
| `SCENE_ROOT` | scene's dir | primary permitted scene directory |
| `SCENE_ROOTS` | round 1 + round 2 + `./scenes` | `:`-separated list; every `--scene` must resolve inside one of these. Overrides the defaults in `config.py` wholesale |
| `SCENE_CACHE_GB` | `8.0` | on-instance scene cache ceiling in TOTAL BYTES, evicted least-recently-used. Lowered further at runtime whenever the measured disk cannot afford it |
| `DISK_RESERVE_GB` | `2.0` | free space that must survive every scene upload, measured with `df` on the instance |
| `DISK_SAMPLE_SEC` | `300` | how often the heartbeat thread re-measures the instance disk for `rq status` |
| `CACHE_DIR_GLOBS` | `blendcache_*:cache:…` | `:`-separated directory patterns beside a .blend that are shipped with it (sim caches, textures) |
| `SEQ_DIR` | `out/seq` | where rendered frame sequences land, one directory per `--name` |
| `MAX_FRAMES_PER_JOB` | `5000` | blast radius for a mistyped range, not a technical limit |
| `SCENE_BATCH_MAX` | `25` | jobs served for one scene before re-evaluating globally |
| `SCENE_STARVE_SEC` | `300` | **floor** for yielding to another scene whose oldest job has waited this long |
| `SCENE_PRIO_BOOST_SEC` | `20` | seconds of head start per priority point in the scene choice (lower `prio` = more urgent) |
| `SCENE_PRIO_BOOST_MAX_SEC` | `1800` | **the bound** — no scene is deferred more than this beyond its FIFO turn, whatever `prio` says |
| `SCENE_SWITCH_PAYBACK` | `2.0` | multiple of the loaded scene's reload cost the wait must beat before preempting |
| `SCENE_RELOAD_BASE_SEC` | `60` | assumed switch cost before one has been measured |
| `SCENE_RELOAD_SEC_PER_GB` | `300` | …plus this per GB of scene |
| `ASSET_DIRS` | auto | `:`-separated dirs mirrored to the instance at their absolute paths |
| `PROGRESS_INTERVAL` | `15.0` | seconds between progress polls off the instance |
| `STALL_WARN_SEC` | `600` | warn (never kill) if the sample counter stops advancing |
| `REATTACH_SEC` | `5400` | how long to reattach to a render whose job socket dropped |
| `KEEP_ON_EXIT` | `false` | leave the instance up for the next broker |

### Never let something else own the broker's lifetime

The most expensive "bug" this project has had was not a bug in the broker. Two
multi-hour batches were lost to brokers started like this, as background tasks
of the agent harness:

```bash
# WRONG. `exec` makes the broker *be* the background task's process.
cd ~/vast-render && VASTRENDER_SCENE=... exec .venv/bin/python -m broker.app >> state/broker.log 2>&1
```

Whatever reaps the task reaps the broker: mid-render, no traceback (no Python
ran), no teardown (no Python ran), and a rented GPU with nobody managing it.
Nine brokers were started that way and **not one logged a shutdown line**. Full
write-up in [incidents.md](incidents.md).

Use `scripts/brokerd.sh start`, or at minimum:

```bash
VASTRENDER_SCENE=... setsid nohup .venv/bin/python -m broker.app >> state/broker.log 2>&1 < /dev/null &
```

A correctly detached broker is a **session leader**, and says so at startup:

```
16:24:05 INFO    broker    broker pid=2661901 ppid=1 pgid=2661901 sid=2661901
```

`pid == pgid == sid`. If they differ you get a warning naming the process group
that can kill it:

```
WARNING broker  THIS BROKER IS NOT DETACHED (pid 2648211, but pgid 191454 / sid 6205)
                — its lifetime belongs to another process group, which can kill
                it mid-render with no traceback and no teardown.
```

(A broker started by `brokerd.sh` shares the *supervisor's* group on purpose and
says so instead: `supervised by brokerd pid N`.)

Check any time with `scripts/brokerd.sh status`.

### When the broker dies, the log now says how

A process cannot report its own SIGKILL — only its parent can. Under
`brokerd.sh` that parent exists, so the log carries the wait status:

```
16:30:12 ERROR   brokerd   broker KILLED BY SIGNAL 9 (KILL) — it did not exit on
                           its own, so no shutdown or teardown ran
16:30:12 WARNING brokerd   restarting in 2s — queued jobs survive in SQLite and
                           every 'running' row is reclaimed at startup
```

Inside the process, every way Python can end now writes one line first —
unhandled exception on any thread or on the event loop, a named signal, a
`faulthandler` dump on SIGSEGV, or `process exiting normally (atexit)`. Which
means **silence is now itself a diagnosis**: nothing at all in the tail of the
log can only be SIGKILL.

To inspect a broker that looks stuck without disturbing it:

```bash
kill -USR1 $(pgrep -f "python -m broker.app")   # every thread's stack -> broker.log
```

Two related guarantees:

* **Queued work survives a restart.** Jobs live in SQLite, and every `running`
  row is requeued at startup — a fresh process is by definition executing
  nothing. If the instance is still rendering one of them, the next claim
  reattaches and collects the frame rather than re-rendering it.
* **Shutdown will not discard a live frame.** `stop()` asks the instance what it
  is doing and destroys only on a reachable, parsed, definitely-idle answer.
  A render in flight keeps its GPU, loudly, and the in-container watchdog is the
  backstop — a bounded 30 min of billing (~$0.15) against a frame that is often
  40 minutes of GPU. This path had already destroyed one healthy instance on a
  SIGTERM at 05:58:59.

### A dropped tunnel is not a dead worker

The single most expensive confusion this project has had. Full write-up in
[incidents.md](incidents.md); the operational summary:

The worker renders on its main thread and serves strictly serially, so **it
cannot answer a ping while rendering** — silence on the job socket is the
expected state during exactly the window where liveness matters. If the SSH
tunnel dies, the broker reads EOF, and the honest statement is "my forward
ended", not "the worker died".

Getting that wrong destroyed three consecutive 8K frames and then stopped the
instance out from under a fourth. The broker now:

* raises `ConnectionDropped` on a dead job socket and **reattaches** — the
  worker writes its PNG to disk independently of the requesting socket, so the
  finished frame is collected over SSH instead of re-rendered;
* asks the instance what it is doing (`progress.json`) before concluding a
  worker is dead, before restarting one, and before hibernating;
* refuses to restart a worker that is mid-render (`WorkerBusy`), and that
  refusal can never reach the replace-the-hardware branch;
* gives up on a dead tunnel in one poll rather than 599 pings over 1800 s.

If you see `refusing to restart the worker … it is actively rendering`, that is
the guard working. Wait for the frame.

### "I could not ask" is not an answer

The guard above was not enough on its own. It refused to kill a live 8K render
and the broker then marked that job `failed` anyway — because the handler threw
away the job id the guard had just proved and re-queried an SSH endpoint that
was flapping. Full write-up in [incidents.md](incidents.md).

Everything that asks the instance what it is doing now gets **three** answers,
not two:

| answer | means | what may act on it |
|---|---|---|
| `rendering` | reachable, fresh `progress.json`, state rendering | wait / reattach |
| `idle` | reachable, and definitely not rendering | kill, redeploy, hibernate |
| `unknown` | **the probe did not answer** | nothing destructive |

Only `idle` licenses a kill, a redeploy or a stop. An unanswered probe blocks
all three — bounded for the idle timer by `IDLE_UNKNOWN_MAX_SEC` so an
unreachable box cannot bill forever, and that bound is longer than the
in-container watchdog's heartbeat deadline so the watchdog wins first.

Dispatch handles the three busy cases separately, and `progress.json` carries a
job id so this is decided, not guessed:

```
job <id> is ALREADY RENDERING on the instance — reattaching to it instead of
         restarting or failing it
job <id> is queued behind job <other>, which the instance is still rendering —
         waiting for that frame rather than killing it
```

And the last gate before the queue records a failure asks the instance first:

```
job <id> NOT failed — requeued, because the instance is rendering job <id> at
         sample 6896/8192 ...
```

That requeue does not spend an attempt: the job did not fail, the broker lost
track of it. A job that the instance is demonstrably rendering can no longer
reach the `failed` state at all.

Two related guarantees worth knowing:

* **A finished frame is collected, never re-rendered.** Every retry first asks
  whether `/workspace/out/<job_id>.png` already exists, because the worker
  writes its PNG independently of the socket that asked for it — a frame
  survives the broker losing interest in it entirely.
* **The kill guards itself on the instance.** The check and the kill used to be
  two SSH round trips that a flapping endpoint could make disagree. The kill
  command now re-reads `progress.json` itself and refuses with
  `BUSY:<job_id>` before signalling anything.

None of this needs a GPU to test: `broker/test_broker.py` drives all three busy
cases and the idle timer against a stub fleet.

### ...but "never" needs a floor

The probe that produces those three answers is itself SSH, so on a host that
refuses SSH it returns `unknown` **forever** — and "unknown blocks everything
destructive" then means the broker can never replace the instance. Instance
46118513 sat exactly there: correct host-level diagnosis, correct refusal to
destroy a possibly-rendering GPU, three deploy rounds, every job failed, and no
way out while the GPU billed. See [incidents.md](incidents.md).

The floor is a fact that does not travel over the broken channel:

> An instance this broker rented and has **never once run a command on** holds
> no worker, no scene and no frame. The worker is installed over SSH and every
> render is dispatched over SSH, so no contact means nothing to lose.

`Fleet.may_hold_render` carries it, and the name is the point: the question is
not "have we talked to this box". It is set **only where `start_worker` is
called**, plus unconditionally on adoption — a previous broker may have left a
frame in flight, and the escape hatch must never open on a guess. `rendering`
still blocks unconditionally, whatever the flag says.

The first version keyed on "has any ssh command succeeded", which sounds
equivalent and is not: running `true` on a box cannot start a render, yet it
permanently blocked replacement. Instance 46124078 proved it — ssh worked long
enough to provision, the 481 MB Blender push then failed at 3.5% on every
retry, and the flag insisted a box that had never had Blender on it at all
might be mid-frame.

Adoption has one carve-out, or restarting the broker would launder a dead host
back into a protected one: an adopted instance that **rejects our key** is
replaceable. vast.ai writes `authorized_keys` when the container starts, so a
container that lacks it either never provisioned it or has restarted since —
and a restart has already killed any Blender process. It cannot be a sibling
broker's either, since ownership is per account and there is one key.

### Telling the two exit-255s apart

`exit 255` is SSH's generic "I could not run your command at all" and it covers
two opposite situations. The broker now separates them, and so should you:

| stderr | `transport_failed` | `auth_rejected` | means | response |
|---|---|---|---|---|
| `Connection timed out`, `Connection refused`, `Connection reset`, or nothing at all | yes | no | never reached sshd | retry, keep the GPU |
| `Permission denied (publickey)` | yes | **yes** | reached sshd, handshake completed, key refused | replace the host, blacklist the machine |

An auth rejection is *positive* evidence the container is up — TCP, key
exchange and userauth all completed — and *final* evidence we can never use it.
Retrying it cannot succeed.

### "Retry, keep the GPU" needs an exit, and the exit is not a count

"A failed transfer is transport, so retry rather than condemn" is right, and it
assumes the retry can eventually succeed. On 2026-08-02 it could not: machine
55313 (offer 43856614, `192.0.2.16`) reset every SSH connection it was
given. The broker spent **3 rounds x 3 attempts x 4 push attempts, 80 minutes
and $0.41 of GPU** on instance 46579745 learning that, destroyed it — and
re-rented the *same offer* one second later, because it was still the cheapest.
The replacement, 46585570, was a different container on the same host and failed
identically.

What separates a hiccup from a host that will never work is **not how many times
a push failed**. It is whether the failures are getting anywhere. Pushes resume,
so a link that is merely dropping keeps whatever bytes it lands and the
instance's high-water mark climbs. Two symptoms mark a round as *hopeless*:

* **it delivered nothing** — the instance holds no more of the bundle than
  before, so the round achieved literally nothing and further rounds are
  further nothing;
* **a single stream was reset the same way eight were** (`chronic`) — one
  connection cannot trip a connection-rate limit or sshd's `MaxStartups`, so
  this is the far end hanging up on whatever it is given.

`MAX_STALLED_ROUNDS` (2) consecutive hopeless rounds condemns the instance,
blacklists **both the offer and the machine**, and rents elsewhere — about 25
minutes rather than 80. One hopeless round always retries: the first round
against a young container legitimately looks like both symptoms. Any round that
delivers new bytes resets the counter, so a slow-but-progressing link keeps its
full `MAX_TRANSPORT_ROUNDS` budget.

The blacklists are **persisted** (`state/bad_hosts.json`, 24 h TTL). They used
to live only in memory, so the one event guaranteed to clear them was a broker
restart — which is exactly what an operator does when the broker is wedged on a
bad host.

### What the far end's sshd is actually configured to do

Measured 2026-08-02 with `sshd -T` on a live vast.ai container
(`192.0.2.17`, instance 46589007) — these are the numbers the parallel
push is sized against, not guesses:

```
maxstartups 10:30:100     maxsessions 10        logingracetime 120
clientaliveinterval 10    clientalivecountmax 2 tcpkeepalive yes
```

* `PUSH_STREAMS` is **8**, and the heartbeat's own connection makes 9 — one
  under the point where `MaxStartups` begins dropping unauthenticated
  connections. The margin is exactly 1, which is why the stream count is a
  named config value now rather than a literal, and why the fallback lowers
  *concurrency* rather than raising it.
* `MaxStartups` only ever affects connections that have **not authenticated
  yet**. It therefore cannot explain a stream that dies 30–280 s into the data
  phase, which is what the 2026-08-02 failures were.
* **`clientaliveinterval 10` with `clientalivecountmax 2` means the SERVER
  hangs up after 20 s of silence from us.** Our client sets
  `ServerAliveInterval=15 ServerAliveCountMax=4`, i.e. it tolerates 60 s — the
  server is three times stricter than the client expects. All eight streams
  share one uplink, so anything that stalls the local upload stalls all eight
  at once, and 20 s later the server closes all eight at once. That is a
  candidate mechanism for "every stream died at the same moment" that does not
  require the host to be broken, and it is worth checking before condemning a
  machine — although it was *not* what happened on machine 55313, which also
  reset lone pre-auth connections.

### Nothing else asks whether the instance still exists

Every liveness signal runs over SSH, so "the instance answers" and "the instance
exists" were the same question asked the same way. An instance destroyed out of
band — by hand, by `vastctl reap`, or by a **bid being preempted** — left the
broker reporting `waiting-for-ssh instance=46585570` for a box that was verified
gone, on course to spend a 900 s ssh timeout and then a full deploy budget on it.

`Fleet.reconcile()` asks vast.ai directly. It runs after a round of transport
failures, after `sshd never accepted a command`, and from the heartbeat thread
after `RECONCILE_AFTER_HEARTBEATS` consecutive misses. On `gone` the instance is
*forgotten*, not destroyed — there is nothing there to tear down, and a destroy
of an unknown id would be read as "teardown failed" and retried forever. An API
error returns `unknown` and changes nothing: vast being unreachable is not
evidence about our instance, and dropping it would strand a rented GPU that only
that local record will ever destroy.

The same discipline applies one layer up, at the job tunnel. `WorkerUnreachable`
carries `tunnel_died` and `local` so the fleet branches on the **type**, never
on the message:

| condition | whose fault | response |
|---|---|---|
| `bind [127.0.0.1]:PORT: Address already in use` | **ours** | reap the orphaned `ssh -L`, keep the GPU, retry |
| tunnel dropped by the far end | the link | transport: keep the GPU, retry |
| healthy tunnel, healthy bind, worker never answers | **the scene** | keep the GPU, fail the job — new hardware will not load it either |

The first row is guaranteed by the restart procedure: `kill -9` is the only
sanctioned way to stop a broker, so it cannot clean up its own forward, and the
orphan holds the port for the next one. The broker reaps stale forwards at
startup for exactly this reason. Reading that as broken remote hardware
destroyed two healthy GPUs — see [incidents.md](incidents.md).

When a deploy fails at SSH, run the command by hand before reading any code.
This settles it in seconds and outranks every inference from the log:

```bash
ssh -v -p <port> -i ~/.ssh/id_vast_render root@<host> true
```

A banner followed by `Permission denied (publickey)` is the host failing to
inject the key. Confirm it is not our end — all three of these should agree:

```bash
curl -s -H "Authorization: Bearer $(cat ~/.config/vastai/vast_api_key)" \
     https://console.vast.ai/api/v0/ssh/                    # key on the account
curl -s -H "Authorization: Bearer $(cat ~/.config/vastai/vast_api_key)" \
     https://console.vast.ai/api/v0/instances/<id>/ssh/     # key on the instance
ssh -v -p <ssh_port> -i ~/.ssh/id_vast_render root@sshN.vast.ai true   # proxy too?
```

If the proxy relay refuses the same key, the port mapping is not the problem.
If `detach_ssh` + `attach_ssh` does not repair it within a few minutes, nothing
will — the injection happens at container start and cannot be replayed.

### Many scenes, one warm worker

Jobs carry their own `--scene`, so agents iterating on different variants share
one broker and one GPU. `VASTRENDER_SCENE` is still the default for any job that
does not name one, so nothing that predates this changes behaviour.

The worker holds exactly **one** loaded scene, and switching costs a worker
restart plus a per-camera OptiX prewarm — measured live at **57.6 s** including
a 296 MB upload. So the dispatcher batches:

> **Drain the loaded scene, bounded twice, then switch to the scene with the
> oldest waiting job.**

The two bounds are what stop draining from becoming starvation:
`SCENE_BATCH_MAX` forces a global re-evaluation after N consecutive jobs, and
`SCENE_STARVE_SEC` yields if another scene has had a job waiting too long. The
switch target is always the *oldest waiting job's* scene, so however long a
batch runs, nothing is deferred forever. Fair-share between agents is
unchanged — it still applies inside every claim, including scene-restricted
ones.

#### Preemption has to be worth what it costs

`SCENE_STARVE_SEC` is a **floor**, not the whole threshold. The 57.6 s figure
above is a 296 MB scene; the round-2 film scenes are 4.5 GB, and a switch to one
was measured at **1425 s**. Against that, yielding at 300 s is not generous, it
is self-defeating.

Measured 2026-08-03, with five agents holding work against five scenes: *some*
scene had always been waiting longer than 300 s, so the starvation test fired on
every single dispatch and the dispatcher — whose entire purpose is to avoid a
switch per job — did exactly that. Nine consecutive switches, each logged
"after 1 job(s)". Caught in the act at 07:16, it spent **22.6 minutes** loading
`film7.blend`, rendered **one 65 s frame**, dropped it for two 14 s renders on
two 6 MB scenes, and started paying the 22.6 minutes again — with sixteen jobs
still queued against the scene it had just given up. Roughly 90 % of a paid GPU
went into scene switching.

So the threshold scales with what the switch actually costs:

    starve_threshold = max(SCENE_STARVE_SEC, SCENE_SWITCH_PAYBACK x reload_cost)

`reload_cost` is measured per scene hash and estimated from size
(`SCENE_RELOAD_BASE_SEC + SCENE_RELOAD_SEC_PER_GB x GB`) until it has been, so
the *first* switch away from a big scene is already protected — otherwise the
measurement only ever arrives after the mistake. The payback factor is 2
because a switch is paid twice: once to leave and once to come back.

Small scenes are unaffected — 2 x 120 s for a 0.2 GB scene is under the floor,
so they interleave exactly as before. Only a scene that is genuinely expensive
to reload earns patience, and `SCENE_BATCH_MAX` remains what bounds unfairness.

#### …and a scene you could finish is not yielded

The threshold above compares a WAIT against a COST, which stops discriminating
once every scene in a contended queue has waited longer than any switch costs.
Then every scene reads as starving and the dispatcher round-robins a job at a
time — the same pathology from the other side. Measured 07:51–07:52 with the
threshold fix already live: two 292 MB scenes, 7 and 6 jobs queued, both
waiting ~2400 s, alternating every job; 54 s to switch and render one 6.1 s
frame.

So the dispatcher also asks what the wait cannot: **how much work is on each
side.** If the loaded scene can be finished in less time than leaving and
coming back would cost, it is finished. The waiting scene is served seconds
later and is spared paying for the return trip at all.

#### The bound, and why it needed a test rather than a comment

Every rule above makes the dispatcher keener to hold a loaded scene. Together
they are how batching becomes starvation with a nicer name, so the bound is
`SCENE_BATCH_MAX` and it is now enforced rather than asserted.

It used to bound nothing. A capped batch re-ran `oldest_waiting_scene()`
*without excluding itself*, got itself back — it still held the oldest job —
and reset the counter, so the cap was reachable forever without ever yielding.
A scene submitted after a 60-job batch waited for all 60 whatever the cap said.
The capped re-evaluation now excludes the loaded scene unless nothing else
wants the GPU.

`test_batching_never_becomes_starvation` is the positive control: with the
exclusion removed it reports the small scene served **NEVER**; with it, served
at exactly the cap, after the big scene got a full batch. A bound with no test
that fails when it is exceeded is a comment.

#### Priority decides which scene loads next, not just job order within one

`prio` (**lower is more urgent**, 100 is the default) has always ordered jobs
inside `db.claim`. It did nothing for *which scene gets loaded*, which was
`ORDER BY created ASC` — pure FIFO on submission time. So a `prio 10` job on a
freshly submitted scene lost to a `prio 100` job on an older one for as long as
that scene had work. Measured 2026-08-03: a 13.6 s render sat queued **41
minutes** behind older scenes, with priority set the whole time.

A half-working knob is worse than no knob, because agents reasonably believe it
works and stop looking for the real reason their job is late.

The mechanism is **aging, not ordering**:

    effective_age = (now - created) + clamp((100 - prio) x BOOST, +/- CAP)

and the scene holding the largest effective age is served next. Ordering by
priority outright is unbounded — a steady trickle of urgent work would defer
everything else forever, the same trap `SCENE_BATCH_MAX` fell into. Under aging
the head start is fixed while a deferred scene's age grows without limit, so it
always wins eventually.

At `BOOST` = 20 s/point the default-to-urgent gap (100 → 10) is 1800 s, which
saturates the clamp: **`prio 10` is maximum urgency and anything lower is
identical to it.** The useful gradient is between 100 and 10 (prio 50 → 1000 s,
prio 90 → 200 s); values above 100 deprioritise (prio 150 → −1000 s).

**THE BOUND: no scene is ever deferred more than `SCENE_PRIO_BOOST_MAX_SEC`
(1800 s) beyond its FIFO turn**, whatever an agent puts in `prio` — 0, −1000, a
typo. `test_priority_cannot_starve_a_scene` is the positive control: with the
clamp removed, a `prio -100000` competitor starves the neglected scene and both
bound assertions fail.

Both scene-selection queries share one expression (`db._EFF_AGE`), so the
signal that triggers a switch and the choice of what to switch *to* can never
disagree about who has waited longest. When they disagreed, a high-priority job
could win the target query while never clearing the threshold that causes a
switch at all — priority looking like it works and not working.

#### Where the money actually went

`rq status` prints a `time` line: load seconds versus render seconds for the
current instance, and flags the case where loading exceeded rendering.

Loading a scene is paid GPU time that renders nothing, and this ratio is the
single most useful number for whether the scheduler is behaving. Nobody knew
it was upside-down until it was measured by hand off the log. Counted from the
worker's own `render_sec` — not wall clock, which folds in the fetch and the
queue wait and flatters the ratio — and a scene load that **failed** still
counts as load, because the GPU was rented for every one of those seconds.

Eviction learned the same lesson. The scene cache is LRU by last *use*, and a
scene's stamp is written when it is **selected** — so a scene with jobs merely
waiting carries the oldest possible timestamp and sorted ahead of idle scenes
that had finished hours ago. Scenes with queued work are now evicted **last**
(`Fleet.demanded_scenes`). That is an ordering, not a pin: "has queued work" can
cover the whole cache, and an unevictable cache turns a policy ceiling into a
refused job. Free space still outranks it.

Scene switching is a worker **relaunch**, not `bpy.ops.wm.open_mainfile`. The
dominant cost (prewarm) is identical, while the relaunch path is the hardened
one — pid-verified kill, real `/proc/net/tcp` port check, `setsid --fork`
detachment — and `open_mainfile` would silently drop the `render_stats` handler
that publishes progress, because Blender clears handlers on file load.

Scenes are cached on the instance by content hash at
`/workspace/scenes/<hash>/<original-name>.blend`, so re-selecting one is free:

```
04:53:48  pushing scene f1_exploded_posed_hq.blend (296 MB) hash=b698107bbed1a9c6
04:54:10  scene uploaded in 20.8s
...
04:55:13  scene iter.blend already cached on the instance (hash 6ef...) — no upload
```

The cache is bounded by `SCENE_CACHE_GB` and evicted least-recently-used before
each write, never after. The currently loaded scene is never evicted, and
neither is any scene with a job in flight. See **The disk preflight** below for
what "bounded" now means and what it measures.

`rq status` reports which scene is loaded, what is waiting per scene, and what
the instance's disk actually looks like:

```
scene    default=/home/zany/opus5-car-render/work/iter.blend
loaded   f1_exploded_posed_hq.blend  (batch 2 job(s) served)
waiting  iter.blend=8  f1_exploded_posed_hq.blend=2
disk     8.7G used of 30.0G (29%)  free 22.9G   cache 7.76G in 18 scene(s) (budget 8.0G)  measured 41s ago
```

### The disk preflight

The cap used to be 12 GB and it had **never fired**. Measured live on instance
46133943, nine hours into a 435-item campaign:

    /workspace/scenes      8.8 G  across 41 cached scenes, none ever evicted
    /workspace/blender     1.2 G  (the install)
    /workspace/blender.tar.zst  460 M  kept after extraction — read by nothing
    df /                   11 G used of 30 G

Nothing was broken in the sense of throwing an error. The cache simply had not
reached its ceiling yet, so the eviction that existed had never run once — and
a bound that has never executed is not a bound, it is an untested branch. Half
an hour later the same cache was **11.5 GB across 48 scenes**, because the
campaign re-assembles each item and pushes it under a new content hash: the
per-item blend grew from 270 MB to **882 MB**, one every two minutes. The
16 GB instance this farm is moving to could not hold the old ceiling at all.

So there are now three bounds, and they are deliberately different kinds of
thing:

| bound | source | unmet means |
|---|---|---|
| `SCENE_CACHE_GB` | policy | evict as far as the pins allow, then **warn** |
| the measured disk | `df` on the instance | lower the budget to fit, silently |
| `DISK_RESERVE_GB` free after the upload | physics | **`DiskFull` — the job fails** |

The free-space rule is the hard one because ENOSPC is not a clean failure
anywhere in this pipeline: Blender does not refuse to render, it writes a short
PNG, which is the same defect class as the truncated `scp` this project already
lost a frame to. The budget is soft because refusing a job over a policy
ceiling while the disk demonstrably has room is a check refusing legitimate
work, and checks that do that get switched off.

Every number in that decision is **measured, twice**. `remote.disk_state` runs
one `df -kP` plus a `du -sb` per cached scene (0.38 s over 42 scenes) and
returns `ok=False` with a reason whenever it could not read them — truncated
output, an unparseable `du`, a dead SSH. A preflight that cannot measure
**refuses**; it never treats an unmeasured disk as an empty one. That is the
`R2-018` lesson from the scene project, where two gates printed a green verdict
over an empty subject. Then, after the removals, the disk is measured *again*,
because `rm -rf` runs with `check=False` and "we sent the command" is not
evidence that the bytes are gone.

"Least recently **used**" means the last time a job selected that scene, not
when it was uploaded: `touch_scene` stamps the directory on every cache hit and
on the fast path of `ensure_ready` (rate-limited, so a 3,000-frame sequence does
not buy 3,000 SSH round trips). A scene rendered all day must outlive one pushed
an hour ago and never opened again.

A `DiskFull` is neither transport nor broken hardware, so it takes neither of
this broker's two reflexes. It is re-raised straight out of `_try_deploy` and
`_switch_scene` — retrying measures the same disk three more times, and
replacing the instance rents an identically sized volume, re-pushes Blender and
re-pushes the scene to reach the identical verdict — and it fails the job
**terminally**, with every number in the message:

```
NOT ENOUGH DISK on 192.0.2.18:28922 (instance 46133943) for 3.90G of scene.
After evicting 12 scene(s) (4.31G) the disk holds 14.8G of 16.0G with 1.15G
free; the upload needs 3.90G plus a 2.00G reserve, i.e. 4.75G more than exists.
Scene cache is 9.12G in 6 scene(s); non-cache use (image, Blender, output) is
1.71G. Unevictable (loaded or in flight): 0ad0c27a, 8dbc1929. Largest cached:
740bea74=3.90G, 612f956c=1.07G, cfdc4a02=1.07G. Rent a larger disk
(VASTRENDER_DISK_GB) or render a smaller assembly — retrying cannot create space.
```

Measured on the live instance, out of process so the running campaign was not
disturbed: 48 scenes / 11.48 GB evicted down to 22 / 7.82 GB, free space
18.99 GB → 22.92 GB, the loaded scene untouched, and `df /` 11 G → 8.7 G used.
The broker's heartbeat beat every 61 s throughout, maximum observed staleness
55 s against the watchdog's 1800 s deadline — eviction runs on the dispatch
thread and the heartbeat has always had its own.

Removals are **batched**, one SSH command per 50 scenes. Measured: 26
directories removed one connection at a time took 67.6 s, almost all of it key
exchange at 69 ms RTT; batched, the same work is a single round trip.

### `blender.tar.zst` is deleted after extraction

460 MB that nothing reads. It is removed once the install is proven good —
`blender --version` answering, not merely `test -x` passing, because a truncated
extract leaves an executable at exactly the path the resume check tests for.

Deleting it cannot force a re-push, and that was checked rather than assumed:
`Fleet._deploy` skips the bundle push on `test -x {root}/blender/blender`, and
`PROVISION` re-extracts only when that same test fails. Both look at the
installed build; neither has ever looked at the archive. If the install really
is gone, the bundle is re-pushed from here, which is the correct answer.

**A `--scene` is a path on two machines, so it is validated like one.** It is
resolved first — following `..` and symlinks — and only then required to sit
under `SCENE_ROOT`, plus be an existing `.blend`. A prefix test applied before
resolution passes `root/../../etc/shadow` happily. This is the same reasoning
that makes job ids broker-minted: a client-supplied id was once a traversal into
the read-only scene project. Rejections are HTTP 400 at submit, not a failure
discovered minutes later.

### Render progress, and why it comes from a file

`rq status` shows a running job's sample counter, percent, elapsed and ETA:

```
running  eb1fe5e0252d  finals  3712/8192 (45%)  12m31s elapsed, ~22m08s remaining
```

Two constraints shaped how this works, both measured rather than assumed.

**It cannot be asked over the job socket.** The worker is strictly serial, so
`bpy.ops.render.render()` holds the main thread for the whole frame and the
socket will not answer even a `ping`. The worker publishes to
`/workspace/progress.json` instead, and the broker reads it over SSH on its own
thread — the same reason the heartbeat has its own thread.

**It cannot be scraped from stdout.** Rendering via `bpy.ops` from a script
prints *nothing*: measured 0 occurrences of `Sample` and 253 bytes of stdout
across a 20 s render. Those familiar `Fra:1 … Sample N/M` lines come only from
Blender's own command-line render path. That is why worker.log sat at 1661 bytes
through a 40-minute frame. The source is `bpy.app.handlers.render_stats`, which
fires on the main thread inside the render, so no thread is added to a process
whose first rule is never to thread.

**The counter is not a steady tick.** `render_stats` reports intermediate
samples only when **adaptive sampling is on** (it always is for Cycles here);
with it off, Blender reports sample 0, sample 1, then silence until the end.
Even with it on, updates land at convergence checkpoints — one measured batch
jumped 191 samples in a single 22 s step. So `STALL_WARN_SEC` is deliberately
generous, and the watchdog **only warns**:

```
WARNING broker  STALL WARNING: job <id> has not advanced past sample 3712/8192
                for 11 min (phase 'Sample 3712/8192', 24 min into the render).
                NOT killing it — ...
```

It never kills. Killing on suspicion is the more expensive error, repeatedly:
this project has destroyed a healthy GPU over a retryable upload and killed a
fully pre-warmed worker every ten minutes over a launch that had succeeded. A
warning costs nothing; a wrong kill costs a re-rent and a re-upload.

Reaching `N/N` is not treated as a stall — denoising, compositing and PNG
encoding all run afterwards with the counter parked (18 s of that at 2000x2000,
minutes at 8K).

### External assets travel separately from the .blend

An unpacked `.blend` stores references to HDRIs and textures as **absolute
paths**, and the blend does not carry their contents. The broker shipped only
the blend, so every remote frame rendered with:

```
WARNING Image file /home/zany/opus5-car-render/assets/city.exr does not exist.
ERROR Failed to load 1 image files
```

Blender renders anyway. The frame comes back looking plausible while being lit
differently from the local one, and nothing in the broker log mentioned it —
about the worst shape a bug can take on a batch of finals.

`ASSET_DIRS` now defaults to an `assets` directory found beside the scene or
beside its parent, mirrored to the instance at the identical absolute path. Set
it explicitly for any other layout. Independently, after every deploy the broker
reads the worker's log back and reports anything still unresolved:

```
WARNING fleet  MISSING ASSET on the instance: /path/to/thing.exr — the render
               will not match local. Add its directory to VASTRENDER_ASSET_DIRS.
```

so an un-shipped dependency is loud even when the heuristic misses it.

### Never background the worker launch with `&` after an `&&` chain

`&` binds looser than `&&`. `A && B && blender ... > log 2>&1 &` backgrounds the
*entire* AND-list as one subshell, which then runs blender in the **foreground**
and sits in `wait()` while still holding sshd's stdout/stderr pipes — so ssh
never sees EOF and the call blocks for its full timeout on a worker that started
perfectly. This cost ten minutes per deploy, in a loop, killing a healthy
pre-warmed worker each time.

`< /dev/null` does **not** fix it: bash already redirects an async list's stdin
to `/dev/null` when job control is off. The launch uses `setsid --fork` with no
`&` at all, and `LAUNCH_TIMEOUT` is 60 s because the call must return instantly.

### Only one broker at a time

Starting a second broker while one is running used to **destroy the first one's
GPU**. uvicorn runs lifespan startup before it binds the port, so the second
process got through `adopt_or_reap`, took ownership of the live instance, failed
its bind with "address already in use", and then destroyed on the way out what
it had just adopted. `timeout 25 .venv/bin/python -m broker.app`, run only to
read a startup message, was enough to do it.

The broker now takes an exclusive `flock` on `state/broker.lock` before anything
else and refuses to start if it is held, naming the PID that holds it:

```
ERROR broker  another broker already holds .../state/broker.lock (pid 2513161)
              — refusing to start. ...
```

Exit status 3, nothing adopted, nothing destroyed. The lock is released by the
kernel however the holder dies, including `kill -9`, so there is never a stale
one to clear. If a start is refused and you believe nothing is running:

```bash
pgrep -f "python -m broker.app"    # empty means the lock really is stale-free
```

`KEEP_ON_EXIT` stays **off** by default — an instance must not outlive its
broker, and with the lock in place a shutdown can no longer be triggered by an
accidental second start.

## Safety first

Two commands matter more than the rest. Learn these before renting anything.

```bash
scripts/panic.sh                              # stop broker + destroy everything
.venv/bin/python vastctl/vastctl.py status    # credit + every instance we own
.venv/bin/python vastctl/vastctl.py reap      # destroy all of them, verified
```

`panic.sh` deliberately does not talk to the broker — it has to work when the
broker is the thing that went wrong. All of these are idempotent, safe to run
twice, and verify each teardown by polling until the id disappears, because an
API success response is not proof the instance is gone.

Four independent things will destroy an instance, so no single failure strands
one: idle timeout, broker shutdown, the in-container watchdog, and `panic.sh`.

### Why destroy and never stop

vast.ai bills three separate meters:

| meter | charged when |
|---|---|
| GPU | instance is **running** |
| storage | instance **exists** — running *or stopped* |
| bandwidth | per byte transferred |

So `stop` ends only the GPU charge and silently keeps billing disk. Nothing in
this project ever calls stop.

### What has no server-side safety net

Verified against the vast CLI source, none of these exist:

- a spend cap or budget limit
- an instance TTL, `--ttl`, or `--end-date`
- scheduled destruction (their scheduler supports five commands; destroy is not
  one of them)

The only real ceilings are **prepaid credit with autobilling off** (currently
$25.00, autobill `None` — correct) and the in-container watchdog described
below.

### The watchdog

Every instance is created with an onstart script that installs a self-destruct
loop. It destroys the instance if either:

- the broker heartbeat at `/workspace/.broker_heartbeat` goes stale
  (`HEARTBEAT_STALE_SEC`, 30 min), or
- the instance exceeds a hard wall-clock cap (`MAX_INSTANCE_HOURS`, 12 h)

It authenticates with `CONTAINER_API_KEY`, which vast injects and scopes to that
instance alone — so leaving it on the box grants nothing beyond self-termination.
This is the **only** teardown path that survives the broker crashing or this
machine losing power.

## Picking a host

```bash
.venv/bin/python vastctl/vastctl.py offers --hours 8 --disk 30
```

Sorted by **projected total cost**, not sticker price. This matters: GPU rates
cluster tightly around $0.308–0.313/hr while disk rates spread about 6×, so the
disk line can dominate the difference on a long batch.

Worked example at 60 GPU-hours (900 frames @ 4 min):

| offer | $/hr | disk $/GB/mo | GPU | disk | **total** |
|---|---|---|---|---|---|
| 39996098 | 0.308 | 0.133 | $18.48 | $0.66 | **$19.17** |
| 44128497 | 0.313 | 0.867 | $18.78 | $4.27 | **$23.18** |

Same GPU, $4 apart, almost entirely disk. The second one's advantage is a 4 Gbps
network — which at 63 MB per scene upload buys about two seconds.

`cuda_vers>=12.8` is in the query and is not optional: below it Cycles ships no
sm_120 cubin for Blackwell, and the render either fails or silently falls back
to CPU.

## Running a worker

Locally, against the 1070:

```bash
mkdir -p out/smoke
TMPDIR=$PWD/state /opt/blender-5.2.0-linux-x64/blender -b <scene>.blend \
  -P worker/server.py -- --port 8799 --out-dir $PWD/out/smoke
```

Submit a job:

```bash
python3 worker/client.py --cam CAM_FrontQuarter --res 1920 1080 --samples 128
python3 worker/client.py --ping        # readiness / job count
python3 worker/client.py --shutdown
```

Run the correctness checks against a live worker:

```bash
cd worker && python3 test_worker.py --port 8799
```

## Frame sequences

A shot is one job covering a contiguous frame range. The dispatcher loops over
the frames; the worker renders one frame per call, exactly as it renders a
still.

That split is the whole design. Every hard-won behaviour around a single render
— reattach instead of re-render when the socket dies, collect a PNG that is
already on the box, never restart a worker mid-frame, never mark a job failed
while the instance is rendering it — is shared code (`Broker.render_one`), so a
sequence cannot regress any of it. What the sequence layer adds is only what a
still does not need: resume, per-frame verification, and cleanup.

**Every frame is a separate render on the instance**, keyed `<job>_f000123`.
That is why `progress.json`, `collect_finished` and `await_render` keep working
unchanged for animation — from the worker's point of view nothing new is
happening. The broker holds two ids at once: `current_job` (the queue row, whose
lease must stay alive) and `current_key` (what the worker calls this frame).
Confusing them silently discards every progress update an animation produces.

### Resume

The record is `frames(seq, frame)` in SQLite plus the file it points at, and the
resume set is recomputed from **both** on every dispatch pass. A row says a
frame was delivered; only the file says it still is.

    rq anim --name shot --frames 1-3000     # renders 1-3000
    <interrupted at 1841>
    rq anim --name shot --frames 1-3000     # renders 1842-3000

Deleting a frame from the sequence directory forces exactly that frame to be
re-rendered. Corrupting one has the same effect, because the check is
structural, not a counter.

A frame is recorded only after it has been fetched **and** verified locally:
byte count against the source, PNG signature and IEND, dimensions against what
the worker reported, and sha256 against the digest the worker computed on the
file it wrote. The digest is the one that matters — a size check catches the
truncated transfer this project has already seen, but not a corrupted one, and
nobody eyeballs three thousand frames.

#### "Rendered this run" vs "already on disk"

Three cases, and they are decided differently:

| what is on disk | what the record says | verdict |
|---|---|---|
| a file, no row | nothing | **render it** — a file alone is never evidence |
| a file, a `done` row it matches | delivered at time *T* | **skip it** |
| a file, a `done` row it does not match | delivered at time *T* | **re-render it** |

"Matches" is: complete PNG, byte count equal to the recorded one, dimensions
equal to the recorded ones, a non-blank verdict recorded at delivery, and
**mtime not later than the row's `finished`**. Under `--deep` it also re-hashes
against the worker's digest.

That last clause is what closes the case a resume could not previously see.
Measured on the live farm 2026-08-02: one flipped byte in a delivered
716,012-byte frame passed size, dimensions and structure, and only `rq seq
verify`'s sha256 caught it — but a resume never runs the deep pass, because
re-hashing a 2,978-frame 4K master is ~100 GB of reads on **every** planning
pass. The file lands (`tmp.replace`) strictly before its row is written, so a
genuine frame's mtime is always the earlier of the two; anything later is a file
that changed after delivery. It costs nothing — the `stat` already happened.

Side effect worth knowing: `touch` on a frame now forces exactly that frame to
re-render, the same way deleting it does.

#### The disk that actually fills is this one

The instance cannot fill: each frame is deleted there the moment its local copy
verifies, so no more than one is ever on the box — confirmed live mid-batch,
`/workspace/out` empty. Every byte accumulates **locally**, and until 2026-08-02
nothing measured that.

The arithmetic for the round-2 master, from measured numbers: 4K frames of
`render3.blend` come back at **34.3 MB**, so 2,978 of them is **102 GB**, and
`/` had **84 GB** free. The batch fills the disk at about frame **2,455** —
eighteen days and ~$155 of GPU in — after which every remaining frame fails on
write. `POST /sequences` now returns `local_disk` and `rq anim` prints it beside
the cost projection. It is a warning, not a refusal: the operator may be freeing
space or moving frames off as they land, and a check that refuses a legitimate
multi-day batch is one that gets switched off.

### Fetching a frame is mostly handshake

Measured against a live instance at **90 ms RTT**, pulling one file, three runs
each:

| method | 120 KB | 8 MB (a 4K frame) |
|---|---|---|
| scp, its own connection (original) | 7.3 s | — |
| scp over the shared ControlMaster | 4.6 s | — |
| **`cat` over the shared master** | **3.1 s** | **10.6 s** (0.8 MB/s) |
| 8 parallel byte-range streams | 2.2 s | 20.0 s (0.4 MB/s) |

At these sizes the payload is noise; almost all of it is TCP setup, key exchange
and scp's own protocol handshake. `fetch_file` therefore streams over the
existing ControlMaster first and keeps scp — on the shared master, then on a
private one — as fallbacks. Correctness does not depend on which path wins: the
file is size-checked against the source and sha256-checked by the caller against
the digest the worker computed on the file it wrote.

**Parallel byte-range fetching was tried and is slower.** 90 ms RTT genuinely
does suggest a windowing problem, and the first micro-benchmark appeared to
confirm it — but that benchmark used `dd bs=1`, which is pathologically slow and
made the single-stream baseline look like 0.19 MB/s when it is really ~0.8. Done
properly, eight streams pulled 8 MB in 20.0 s against 10.6 s for one. Same
answer this project already recorded for parallel *upload*: no gain. The finding
is kept as a comment in `remote.py` so nobody re-derives it from the RTT alone.

### Why frames are fetched between renders, not in the background

Fetching in parallel with the next render would keep the GPU busy during the
~2 s transfer. It would also put an unbounded number of unverified frames on a
30 GB disk — 3,000 4K PNGs is ~24 GB on a disk that already holds Blender and a
288 MB scene — and add a second concurrency story to the part of this system
that must never lose work. At 4K final quality a frame is minutes; the fetch is
noise. Each frame is deleted from the instance the moment its local copy
verifies.

### Scenes travel as directories now

A scene is uploaded to `/workspace/scenes/<hash>/<original-name>.blend`, not to
`/workspace/scenes/<hash>.blend`. Blender resolves `//` references against the
directory holding the .blend and, for `//blendcache_<name>/`, against its
*filename* — so the old flat layout broke both, and a physics cache that could
not be found was silently re-simulated rather than reported. Sibling directories
matching `CACHE_DIR_GLOBS` (`blendcache_*`, `cache`, `sim`, `textures`, …) are
pushed into that directory under their own names.

A `.complete` marker is written last, after the .blend *and* every sibling. The
cache check requires it, because a push that died between them leaves a
perfectly valid .blend beside a half-copied cache tree — which Blender treats as
no cache at all.

### A render scene must carry no live rigid-body world at all

The worker refuses a frame whose physics caches do not cover it, because
Blender does not fail on a missing cache — it *simulates*, and a simulation
reached by jumping to a frame does not continue the frame before it. That
refusal is correct and stays. Observed 2026-08-03 on
`render/breach/wit_static.blend`, which reached the farm carrying a
`rigidbody_world` with nothing baked into it: frame 1 would have been simulated
from rest rather than read.

The general rule the refusal implies is stronger than "bake before you submit":

> **A scene that can still simulate is a scene that can disagree with its own
> bake.** By the time a blend is a render input, its simulation should not
> exist — the geometry should be baked down and the `rigidbody_world` removed,
> not merely cached.

A cache is a *promise* that the sim and the bake agree, and every mechanism
that can break the promise survives into the render: a cache that does not span
the submitted range, a disk cache that was never ticked so the points live only
in memory and do not travel, an object added to the world after the bake, a
frame stepped to out of order. Each of those produces a plausible image that no
single-frame inspection catches — which on a one-shot 4K film is the most
expensive class of defect there is.

Keeping the world and trusting `require_caches` makes the worker the last line
of defence against a scene that should never have shipped. Shipping no
simulation at all removes the question: there is nothing left that *could*
re-simulate. `--no-require-caches` still exists for the case where the
difference is genuinely acceptable, and the reply reports the problem either
way.

### Seams

Two frames of a cut-free shot rendered with different settings are a defect that
no single-frame inspection finds. So every frame records `spec_hash` — the
image-determining parameters plus the **content hash of the .blend** — and a
submission that would mix hashes inside one sequence is refused with 409 at
submit time. Reassembling the scene therefore invalidates a resume, loudly,
which is the correct answer for a shot that must not have a boundary in it.

### Blank frames, and why they are the worst kind of hole

Every frame is decoded and measured after it is fetched and verified, inside
`Broker.collect`, beside the sha256 rather than in a pass of its own. See
`broker/imgstat.py`; the incident that produced it is in `docs/incidents.md`.

The failure mode is specific to sequences, and it is why this is a check rather
than a flag someone might remember to pass. A black frame verifies — right size,
right dimensions, right digest, IEND present — so it is written `done`, and a
`done` row is exactly what a resume trusts to skip a frame. Every future
re-submission of that range reports it as "already delivered". The hole is
permanent, survives every retry, and is discovered when the video is assembled
and watched. In a single unbroken take there is nothing to cut around it.

So:

* `plan_range` refuses a recorded blank verdict on the cheap pass and re-decodes
  the file on `--deep`, which is what `rq seq verify` runs.
* `Broker.blank_gate` fails the frame *before* `db.frame_done` is called, so a
  blank frame never reaches the table in the first place.
* A blank still is failed with `db.fail_terminal`, not `db.fail` — no retry.
  `MAX_ATTEMPTS` is 3, and a camera aimed at nothing renders black three times
  for three times the money.

**Cost of the check.** Pillow decodes a 3840x2160 frame in ~0.5 s and 7680x4320
in ~2.1 s, against render times of minutes. Pillow is not a declared dependency
here (it arrives transitively), so a pure-stdlib decoder sits behind it —
correct, cross-checked against Pillow in the test suite, and roughly 30x slower.
That decoder is also used deliberately for 16-bit *greyscale* PNGs, where
Pillow's own `convert("L")` clips at 255 instead of scaling and would misreport
a perfectly good frame as `UNIFORM`.

**Tuning**, all `VASTRENDER_`-prefixed, all in `broker/config.py` with the
derivation of each number written beside it:

| variable | default | what it does |
|---|---|---|
| `BLANK_SD_MAX` | 0.005 | at or below this standard deviation the frame is flat |
| `BLACK_MEAN_MAX` | 0.005 | flat *and* this dark is `BLACK` rather than `UNIFORM` |
| `BLANK_ALPHA_MAX` | 0.004 | every pixel at or under this alpha is `TRANSPARENT` |
| `SUSPECT_SD_MAX` | 0.02 | reported loudly, never fatal |
| `SUSPECT_LEVELS_MAX` | 16 | a megapixel image on this few luminance levels is not a render |
| `SEQ_OUTLIER_WINDOW` | 25 | neighbours each frame is compared against |
| `SEQ_OUTLIER_Z` | 8.0 | robust z-score, in MADs |
| `SEQ_OUTLIER_MEAN_FLOOR` | 0.02 | absolute floor, so a locked-off shot does not flag every frame |
| `BLANK_FAILS_JOB` | true | farm-wide kill switch; the per-job override is `--allow-blank` |

Turning `BLANK_FAILS_JOB` off does not turn the measurement off — the numbers
are still taken, recorded and reported. It only stops the refusal.

### The 12-hour ceiling

The in-container watchdog destroys an instance at `MAX_INSTANCE_HOURS` (12)
regardless of what it is doing. A 3,000-frame 4K batch is far longer than that,
so it *will* be interrupted, repeatedly. Each interruption costs the frame in
flight plus a cold start; nothing already delivered is lost, and the job resumes
on fresh hardware. `rq anim` folds those restarts into its cost projection.

### Spend now survives a restart

`MAX_BATCH_USD` was only banked at teardown, which made it useless in the one
case it exists for: the supported way to restart a broker is `kill -9`, and a
SIGKILLed broker banked nothing. The current instance's accrual is now
checkpointed to `meta.live_spend` on the heartbeat thread every 60 s, counted
into the total when it names an instance this process does not own, and folded
back into `fleet.gpu_seconds` when the instance is re-adopted. `rq budget --set`
changes the cap without a restart, because a restart is the riskiest routine act
in this system and should never be the way to change a number.

vast.ai's own credit figure is sampled every 10 minutes alongside it. Everything
else here is derived from a rate and a clock; the credit delta is what was
actually taken.

## Measured behaviour

On the GTX 1070 with `f1_showroom.blend` (6.2 MB):

| | |
|---|---|
| pre-warm, first camera | 2.72 s |
| pre-warm, each camera after | ~0.19 s |
| render 1 (cold sync + BVH) | 20.8 s |
| render 2 | 8.5 s |
| render 3 | 7.4 s |

~13 s of sync/BVH eliminated per job. The gap widens with scene weight — on a
208 MB scene the same effect measured 23–32 s down to 0.6–2.2 s.

The first-camera cost is an OptiX pipeline build for a new kernel feature set,
which is why pre-warm sweeps every camera in the scene plus DOF on/off before
accepting work. Cameras are **discovered** from the blend, never configured, so
this stays correct whatever the agents build.

## Transferring the scene

rsync delta sync is useless on `.blend` files — measured 14.7% match between
revisions, because Blender rewrites the whole file on save and embeds pointer
addresses that shift every run. Compress and send whole:

```bash
zstd -19 -T6 -c scene.blend | ssh vast 'zstd -d -o /workspace/scene.blend'
```

285 MB → 62.9 MB, 4.53×. Below ~14 Mbps upload use `-19`; above it `-10` is the
better time/size tradeoff.

Never enable Blender's *Compress File* preference — a zstd-wrapped blend defeats
external compression.

### SSH multiplexing

Required for the many-small-jobs pattern; without it every command pays a full
handshake.

```
Host vast
    HostName <instance_ip>
    Port <external_port>
    User root
    IdentityFile ~/.ssh/id_vast_render
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
    ServerAliveInterval 30
    Compression no
```

`Compression no` is deliberate — the payload is already zstd, so SSH's zlib
would burn CPU on incompressible bytes.

### Reading the log

`state/broker.log` is a long-lived append file spanning many sessions. Grepping
it whole to watch for an event matches history and returns instantly — record
the byte offset first and read forward from it:

```bash
OFF=$(stat -c %s state/broker.log)
until tail -c +$OFF state/broker.log | grep -q "worker ready"; do sleep 20; done
```

### Measured: frame-sequence throughput

From the live end-to-end runs on 2026-07-28 (`anim_test.blend`, RTX 5090):

| | |
|---|---|
| cold start (rent → worker ready) | 502 s on a healthy host |
| resume from hibernation | 52 s, no re-upload |
| render, 320×180 @ 24 samples | 0.6 s |
| render, 640×360 @ 32 samples | 0.6-0.7 s |
| **wall clock per frame** | **6.5 s** at 640×360 |

The gap between 0.6 s of render and 6.5 s of wall clock is per-frame remote
overhead: a progress probe, a worker round trip, the fetch, and a remote delete.
It was ~14 s before `fetch_file` learned to stream over the shared ControlMaster.

That overhead is fixed, not proportional, so it matters at preview resolutions
and disappears at final quality — at 4K/2048 samples a frame is ~60 s of render
against the same ~6 s of overhead, under 10%. For 3,000 frames it is about 5
hours of wall clock and $1.70 of GPU time, which is the price of verifying every
frame as it lands rather than discovering a gap at delivery.

Re-measured 2026-08-02, same fixture, 1280×720 @ 24 samples, 25 frames across
three sequences: **0.69 s render, 5.7–6.0 s wall clock**. Unchanged.

### What the 2,978-frame 4K master costs

Projected from measurements on **this** hardware and **this** scene, not a
model. The basis is four-figure-second 4K stills of
`render/world/assembly/r2/render3.blend` (4.2 GB) on instance 46589007:

| | measured |
|---|---|
| 3840×2160 @ 512 samples | 519.6 s, 501.5 s → **510.5 s/frame** |
| 3840×2160 @ 640 samples | 610–641 s |
| returned PNG | 30.5–34.7 MB → **34.3 MB/frame** at 512 |
| per-frame non-render overhead on a 34 MB frame | **31 s** (job total 550.6 s vs render 519.6 s) |

| the film, 2,978 frames | |
|---|---|
| render | 422.3 GPU-hours |
| per-frame overhead | 25.6 h |
| cold starts (37 × 10 min, the 12 h watchdog cap) | 6.2 h |
| **total wall clock** | **454 h ≈ 18.9 days** on one 5090 |
| **GPU cost at $0.4083/hr** | **$185** |

Two caveats that must travel with that number. It is a **512-sample** basis, and
sample count is close to linear here — at 640 it is $232, and the delivery spec
is not settled. And it is a basis of **static-camera stills of the assembly**,
not of beat 3, whose rigid-body destruction changes geometry every frame and
therefore rebuilds the BVH every frame; `use_persistent_data` buys nothing
there. Treat $185 as the floor.

### Egress

Measured on the rented offer: `inet_up_cost = inet_down_cost = $0.00130208/GB`,
i.e. **$1.30/TB** against the `MAX_INET_COST_PER_TB` ceiling of $4.00.

| | |
|---|---|
| frames down, 2,978 × 34.3 MB | 102 GB → **$0.13** |
| scene up, ~38 uploads × ~2.1 GB compressed | 80 GB → **$0.10** |
| **total** | **$0.24** |

Bandwidth is not a consideration for this film. It is 0.1% of the GPU bill even
at the $4/TB ceiling, and the ceiling exists to exclude the outlier hosts that
charge per-GB rates comparable to the GPU itself, not to ration transfers.

---

## EXEC jobs — CPU work on the rented box

### What runs where

| process | port | concurrency | started by |
|---|---|---|---|
| `worker/server.py` | 8799 | **1** — Blender's never-thread law, and one GPU | `remote.start_worker` |
| `worker/exec_server.py` | 8800 | `VASTRENDER_EXEC_SLOTS`, default 12 | `execremote.start_exec_server` |

Both are `blender` processes and both are found by `pgrep -f` on their **remote**
command line — `/workspace/server.py` and `/workspace/exec_server.py`
respectively. Those two patterns do not match each other (the substring
`/workspace/server.py` does not occur in `/workspace/exec_server.py`), which is
what stops a worker restart from killing every build in flight. Confirm before
changing either:

```bash
ssh … 'pgrep -af "/workspace/server.py"; echo ---; pgrep -af "/workspace/exec_server.py"'
```

### The exec server does not survive a hibernation

Stopping the instance stops its container, so the exec server is gone after
every stop→resume, every instance replacement, and every broker restart. This is
handled — `ExecService.ensure_ready` probes and restarts it — but it means the
first exec job after an idle period pays a restart, and it means **the input
bundles survive while the server does not** (they are on the disk, which is
kept, and they are content-addressed, so a resume re-uses them for free).

### The idle timer knows about exec now

`maybe_idle_down` refuses to stop the instance while any exec job is in flight
*or waiting*. Without that clause an exec-only workload would leave the render
dispatcher's `last_work` clock untouched, and the broker would stop the box —
SIGKILLing every build on it, since a stopped container runs no processes — one
second before waking it again.

### Restarting the broker to pick up exec changes

Same procedure as always, and the scene is still a POSITIONAL argument:

```bash
scripts/brokerd.sh stop
scripts/brokerd.sh start /home/zany/f1-round2/world/beat1_anim.blend
```

Omitting the scene silently switches the default. Wait for the queue to drain
first — `./rq status` until `depth=0` — because `kill -9` is what a restart
does and an exec child killed mid-build loses its work.

### When something is wrong

```bash
./rq status                      # exec slots in use, and what is in each
ssh … 'tail -50 /workspace/exec.log'          # the exec server's own log
ssh … 'ls -la /workspace/exec /workspace/bundles'
ssh … 'cat /workspace/exec/<job_id>/job.log'  # one child's stdout+stderr
```

The child's log tail also comes back in the failure reply and is stored in the
job's `err`, so the usual first question — "what did Blender actually say?" —
is answerable from `./rq status -v` without an SSH session.

### Disk

Exec children write `.blend` files that dwarf anything the render path handles
— wave 1's 28 items produced 28 GB. Three things bound it:

  * everything outside `out/` is deleted the instant the child exits,
  * `--min-free-gb` (default 4) refuses to *start* a job below that floor,
    because Blender writes a short file rather than failing on ENOSPC,
  * the job directory itself goes on `release`, after the broker has fetched
    and verified the outputs.

Bundle staging goes through the same `disk_state` / `evict_to_fit` preflight as
a scene upload. It is 8 MB, but "small" is a property of the bundle and not of
the disk it lands on.

### Known limitation: an exec job on a cold farm pays a RENDER deploy

`ExecService.ensure_ready` calls `Fleet.ensure_ready(scene)` to guarantee an
instance exists, and on a broker that has not yet loaded a scene that means the
full render deploy — a 291 MB `.blend` upload, a worker restart and a per-camera
OptiX prewarm. Measured end to end on the first exec job after a broker restart:
**1.7 s of build on the box inside 348 s of job**, essentially all of it that
deploy.

It is not a correctness problem and it is paid once per instance, but it is
worth knowing before timing a single exec job on a cold farm. The reason it is
shared rather than special-cased is that `Fleet.ensure_ready` is the only code
allowed to rent, resume, replace and refuse, and every one of those behaviours
was bought with an incident. A cheaper "instance exists and Blender is
installed, but load no scene" path is a worthwhile follow-up; it is not worth a
second copy of the rental logic.

### The A/B that was supposed to justify this, and what it actually said

Fixed in advance by `f1-round2/docs/PLAN-throughput-optimisation.md` §4.2:
**adopt remote exec iff its items/hour is at least 2x the local machine's.**
Run on 26 real wave-1 item modules, each unit being exactly what an item agent
does — import the module, run `test_scene()`, save the `.blend` — with a pull
queue keeping both sides saturated rather than wave-synchronised.

| run | where | slots | units | wall | items/h | mean/item | idle slots |
|---|---|---|---|---|---|---|---|
| A4 | local | 4 | 26 | 989.6 s | 94.6 | 137 s | 10 % |
| A52 | local | 4 | 52 | 1964.3 s | **95.3** | 146 s | 3 % |
| B1 | remote | 1 | 1 | 81.7 s | 44.1 | 80 s | — |
| B12 | remote | 12 | 26 | 805.5 s | 116.2 | 192 s | 48 % |
| B52 | remote | 12 | 52 | 1228.6 s | 152.4 | 214 s | 25 % |
| B20 | remote | 20 | 52 | 1170.1 s | 160.0 | 289 s | 36 % |
| B52clean | remote | 12 | 52 | 1184.0 s | 158.1 | 206 s | 25 % |

    B12 / A52  = 1.22x      B52 / A52 = 1.60x
    B20 / A52  = 1.68x      B52clean / A52 = 1.66x        bar = 2.00x

**The bar is not met, on any of four configurations.** Reported as a reject.

Three things the numbers say that the plan did not expect:

**The skill's per-core claim is right, and understated.** *"The rented EPYC is
~1.5x slower per core"* — measured 1.97x on one build alone (80.0 s remote vs
38.9 s local for `kerb_precast_unit`) and a median 1.34x across 26 items with
each side at its own concurrency.

**The remote box does not scale with slots.** Mean per-item wall clock goes
80 s → 206 s → 289 s as concurrency goes 1 → 12 → 20, so throughput plateaus
near 160 items/hour whatever the slot count. Twelve is not leaving anything on
the table; twenty buys 5 %. A steady-state projection from the 12-way per-item
times said 2.05x, and running it at 20 slots refuted that projection — the
arithmetic had assumed per-item time is independent of concurrency, and it is
not.

**It is not render contention.** `B52clean` ran with the GPU completely idle and
gained 3.7 % over `B52`, which shared the box with a 4K render every 150 s.

**The rented hardware is not what the plan sized against.** Measured from the
cgroup, which is the only honest source inside a container:

    /sys/fs/cgroup/cpu.max      2304000 100000   ->  23.04 CPUs   (plan assumed 32)
    /sys/fs/cgroup/memory.max   97169440768      ->  90.5 GiB     (plan assumed 515 GB)
    nproc / MemTotal / loadavg  96 / 188 GB / 99.5   — all the HOST's, all wrong here

#### What would change the answer

  * **A second instance.** Nothing here is per-instance-limited; the ceiling is
    one box's memory bandwidth. Two boxes at 158 items/h each is 3.3x, and the
    exec server is already content-addressed and stateless enough for it. That
    is the lever, not the slot count.
  * **Raising the LOCAL concurrency cap.** The 4-way local figure is set by the
    workflow runtime, not by the hardware; A52 measured only 3 % idle slots, so
    the local box is genuinely saturated at 4 — but 6-way would move the
    denominator, which is `PLAN-throughput-optimisation.md` §6's own
    highest-leverage recommendation.
  * **Counting the transfer that exec deletes.** This A/B measured BUILD ONLY.
    It does not count the 81 % of in-job wall clock that 553 measured render
    jobs spent not rendering, most of it pushing assembled blends. A build+gate
    unit, where the blend never leaves the instance, is a different measurement
    and is the one worth taking next.
