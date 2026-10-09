# Specs

Each feature of the coordinator was written from a spec, and the spec was kept current as the
feature met reality. A spec states the purpose, the inputs and outputs, and a numbered list of
invariants; comments in the code cite them ("inv. 20c"). Most invariants carry the date and the
incident that produced them, which is the fastest way to learn why a rule exists.

| spec | covers |
|---|---|
| [task-dispatcher](task-dispatcher.spec.md) | the poll loop: placement, renting, shipping, ingest, retries, teardown, preemption, reapers, owned boxes, consolidation, measured headroom |
| [run-registry](run-registry.spec.md) | the SQLite schema, the task state machine, run identity, the `runq` client |
| [job-artifact-contract](job-artifact-contract.spec.md) | the `job` section of a config: how a job describes itself |
| [code-snapshot](code-snapshot.spec.md) | shipping the working tree instead of a git ref |
| [task-bundle](task-bundle.spec.md) | the bundle: manifest, hashes, signatures, compiled code |
| [ship-artifact-build](ship-artifact-build.spec.md) | building the shipping artifact at queue time, the blob store |
| [runq-sweep](runq-sweep.spec.md) | sweep files and per-seed co-location |
| [sweep-supervisor](sweep-supervisor.spec.md) | the launch rules the worker reuses |
| [box-pause](box-pause.spec.md) | pause, hold, drain and resume of an owned box; time-of-day capacity |
| [worker-rolling-upgrade](worker-rolling-upgrade.spec.md) | delivering new worker code without restarting live work |
| [est-defaults](est-defaults.spec.md) | run-time estimates learned from finished tasks |
| [calibration](calibration.spec.md) | the expected-against-actual report |
| [fleet-utilization-monitor](fleet-utilization-monitor.spec.md) | finding under-used capacity and proposing the fix |
| [experiments-retention](experiments-retention.spec.md) | bounding the data root |
| [free-space-guard](free-space-guard.spec.md) | what the dispatcher does when the data root runs low anyway: hold, alert, fail nothing |

`undeliverable-blob-is-misclassified-as-a-code-bug.md` is a diagnosis note, kept because the code
refers to it.

## Reading them outside the project they came from

These are working documents of a private research project, published with machine names, addresses
and the project's name replaced. They still mention that project's experiments, trainers, other
specs, and a set of working rules that are not in this repository; read those as labels. Paths were
rewritten to this repository's layout (`fleet/`, `docs/specs/`). Rented machines are referred to by
stand-in numbers.

Three specs that the code cites are not here, because they describe one site's network, deployment
or tooling more than the coordinator: the container deployment, the HTTPS API's rollout
(`remote-submit`), and the campaign watcher's integration with that site's tools (`run-watch`).
Read a citation of one of them as a label.
