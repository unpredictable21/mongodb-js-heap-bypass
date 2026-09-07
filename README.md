# Server-side JS heap limit is enforced per-scope via thread-local accounting — concurrent connections bypass the aggregate cap (K connections = K × 1.1 GB)

## Summary

MongoDB Server's server-side JavaScript engine (SpiderMonkey with a custom allocator, `src/mongo/scripting/mozjs/shell/jscustomallocator.cpp`) enforces the configured JS heap limit (`jsHeapLimitMB`, default derived from available memory; measured 1100 MB on our 15 GB test host) **per JS scope, using thread-local counters** (`malloc_bytes`/`mmap_bytes`/`max_bytes` are all `thread_local`, jscustomallocator.cpp:83-85). There is no global accounting across scopes.

As a result, K concurrent authenticated connections running allocation-heavy `$where` / `$function` / `$accumulator` JavaScript each receive an independent K × limit budget. We measured 4 concurrent connections growing mongod RSS by ~2.0 GB before the per-scope interrupts fired — nearly 2× the configured global limit, with K=50 connections plausibly reaching ~55 GB of aggregate demand against any RAM budget.

This is the multi-connection aggregate dimension of the resource-exhaustion weakness family addressed in CVE-2026-8199. Interrupts themselves work correctly per-scope (no memory-unsafe behavior observed); the gap is the missing aggregate accounting.

## Affected version

- MongoDB Server 8.3.8 (latest stable at time of testing, commit d100bf19)
- Likely all maintained versions with server-side JS enabled (scripting is enabled by default); the thread-local design is longstanding.

## Impact

An authenticated user with JS-executing privileges (any user that can run `$where`/`$function`/`$accumulator` — default roles such as `read`/`dbAdmin` qualify on collections they can query) can multiply effective JS heap consumption by the number of concurrent connections. This enables memory-exhaustion denial of service at a scale the configured `jsHeapLimitMB` explicitly intends to prevent, and can drive the host into OOM conditions (affecting other services on the same host), while each individual connection remains within its own limit and therefore looks legitimate to per-scope monitoring.

Severity (our assessment): **Low-to-Medium DoS**, CWE-770 / CWE-400.

## Reproduction

Environment: mongod 8.3.8 (official ubuntu2404 build), 15 GB RAM VM, JS enabled (default), no auth (lab).

1. Confirm the limit:

```javascript
db.adminCommand({ getParameter: "*" }).jsHeapLimitMB   // → 1100 on our host
```

2. Run the attached PoC (`poc-js-heap.py`) against the target. It opens K=4 concurrent connections; each runs a `$where` that appends ~1 MB strings to a global array in a loop:

```js
(function() {
    globalThis.__a = globalThis.__a || [];
    for (var i = 0; i < 2000; i++) {
        globalThis.__a.push(new Array(1024*1024/8).fill(1.1).join('x'));
    }
    return false;
})()
```

3. Observed output ( mongod RSS sampled every 250 ms):

```
jsHeapLimitMB: 1100
K=4 wall=60.5s baseRSS=170MB peakRSS=2190MB growth=2019MB
  worker0: where-err:Executor error ... :: caused by :: JavaScript execution interrupted
  worker1: (same)
  worker2: (same)
  worker3: (same)
```

Each connection was correctly interrupted at its own scope limit, but aggregate RSS growth (~2.0 GB) exceeded the single configured limit (1.1 GB) by ~1.8× at K=4 — bounded in this run only by the JS loop's allocation rate during the 60 s window. Scaling is linear in K; the per-scope limit provides no aggregate protection.

## Suggested fix

Maintain a process-global (atomic) counter of outstanding JS allocations in addition to the thread-local one (the counters already exist in `jscustomallocator.cpp`; they are only scoped incorrectly), and have `signal_oom()` fire when either the per-scope or the global budget is exceeded. Alternatively, serialize server-side JS execution through a bounded pool of scopes/threads and cap the pool's total budget.
