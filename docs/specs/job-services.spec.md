# Feature: Job services — a task that asks for a home service gets its files, and a tunnel on a rented box

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (fleet coordinator)
- **Module path:** `fleet/dispatcher.py`, `fleet/job_manifest.py`, `fleet/registry_db.py`,
  `fleet/runq.py`, `fleet/coordinator/api_server.py`
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/job-services.spec.md`
- **Related:** `docs/specs/task-dispatcher.spec.md` (invariants 7 ship, 9a endpoints, 13 no secrets
  on the box, 20 owned boxes), `docs/specs/job-artifact-contract.spec.md` (the `job` section),
  `docs/specs/task-bundle.spec.md` (`task.json`)

## Purpose
Some jobs need a service that lives at home: a proof checker, a database, a model server. An owned
box on the LAN can reach it; a rented box cannot, because it is on the internet and home opens no
port to it. The coordinator already holds an ssh connection to every box, so it can carry the
service to the box instead: an ssh reverse tunnel from a port on the rented box's loopback to the
service's address at home.

A service usually also needs a credential (an API key, a CA certificate). Invariant 13 of the
dispatcher keeps every secret at home, and that stays the rule for every task that does not ask for
a service. A task that asks gets the service's files on its box for as long as it runs there.

**Off unless the job asks.** A task whose job does not name a service gets nothing of this feature
on any box: no tunnel, no files, no environment variable.

## The job's side
A job asks in its config, at the top level of the `job` section:

```json
"job": {"manifest_version": 1, "run": [...], "completion_artifact": "results.json",
        "services": ["checker"]}
```

`services` is absent or `[]` by default. `runq` (`add --config`, `sweep`, `submit`) copies it to
the task's `resource_hint_json` as `"services"`, which is where the dispatcher reads it. A named
`runq add --entrypoint` task cannot ask for one.

A task that asks for `checker` is told where the files are by one environment variable,
`FLEET_SERVICE_CHECKER` (the name in capitals, `-` as `_`). Its value is `~/spool/services/checker`:
a path under the worker's home, which the task expands itself (`os.path.expanduser`). The
directory holds:

- `service.json`: the service's `values` (below) for the kind of box the task runs on;
- each file of the service's `files` directory for that kind of box, by the same name.

## Settings
One row of the `settings` table, read from the registry each time it is used (so a change needs no
restart):

| setting | default | meaning |
|---|---|---|
| `services` | `{}` | each service a job may ask for, by name |

```json
{"checker": {
   "rented": {"files": "/run/secrets/services/checker-rented",
              "tunnel": {"home": "172.17.0.1:18120", "box_port": 18100},
              "values": {"url": "https://127.0.0.1:18100", "tls_name": "checker.example"}},
   "owned":  {"files": "/run/secrets/services/checker-owned",
              "values": {"url": "https://192.168.0.10:18100", "tls_name": "checker.example"}}}}
```

- **A name** matches `[a-z][a-z0-9-]{0,31}`.
- **Both kinds are required.** Placement knows nothing of services, so a task that asks may land on
  a rented box or an owned one, and each needs its own way in.
- **`files`** (optional): an absolute path on the coordinator. Every entry directly in it whose
  name matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and that is a regular file (not a link) is sent;
  anything else is skipped. At most 16 files of at most 64 KiB each. A file named `service.json`
  is refused.
- **`values`** (optional): an object whose keys match `[a-z][a-z0-9_]{0,31}` and whose values are
  strings, numbers or booleans. Written as `service.json`. The coordinator does not read them.
  They are **never a secret**: they sit in the registry's `settings` row, which is not kept as a
  secret is. A credential goes in `files`.
- **`tunnel`**: required for `rented`, refused for `owned` (an owned box reaches home on the LAN).
  `home` is `HOST:PORT` (HOST an IPv4 address or a name of letters, digits, `.` and `-`) as the
  coordinator's own network sees it; `box_port` is 1024 to 65535, and **no two services may share
  one**: a box may hold tasks asking for both, and one port carries one tunnel.

`service_definition_error(name, definition)` in `registry_db` checks one entry and
`service_port_clash(services, name)` the box ports across entries; each returns the first fault, or
None. `services_error(conn, hint)` applies both to the services a task's hint names.

## Output contract
- Events (`instance_id` set, `task_id` where one applies; the detail is JSON):
  - `service_tunnel_open` `{"service", "box_port", "home", "endpoint"}`;
  - `service_tunnel_down` `{"service", "rc", "why"}`: the tunnel's process had exited when the
    dispatcher looked (`why` is the last 300 characters of its stderr);
  - `service_tunnel_closed` `{"service", "why"}`: no open task on the box asks for it any more, its
    endpoint or its setting changed, or its port was not known to listen on the box's loopback alone;
  - `service_tunnel_checked` `{"service", "listen": [addresses]}`: its port on the box was seen
    listening on the loopback alone (invariant 6a), once per tunnel;
  - `service_tunnel_exposed` `{"service", "box_port", "listen": [addresses]}`: its port listened on
    an address beyond the loopback, or could not be seen listening at all (`listen: []`), and it was
    closed (invariant 6a);
  - `service_tunnel_unrecorded` `{"service", "why"}`: its pid file could not be written (invariant 8);
  - `service_files_written` `{"service", "files": [names]}` (one per ship that wrote them);
  - `service_files_removed` `{"service"}`;
  - `service_tunnels_failed` (plain text, with `instance_id` when one box's step met it): the phase
    met a fault of its own (invariant 6).
- A per-box settings row, `service_files_i<ID>`: the JSON list of the services whose files are on
  that box, so their removal survives a restart.
- The tunnel's stderr: `<data root>/.dispatcher/tunnels/i<ID>.<service>.err`, rewritten at each open.
  Beside it `i<ID>.<service>.pid`: JSON `{"pid", "start", "forward"}`, where `start` is the process's
  start time (field 22 of `/proc/<pid>/stat`) and `forward` its `-R` value.

## Invariants
1. **Asked for, or nothing.** The dispatcher reads a task's services from its stored
   `resource_hint_json` (`"services"`, a list of names). A task with no such key, or `[]`, is
   shipped exactly as before: no file, no tunnel, no environment variable. With `services` at its
   default `{}` and no task asking, no new ssh call and no process is started.
2. **Refused at queue time, with nothing queued.** `runq` refuses a job whose `services` is not a
   list of distinct names (at most 4) that match the name pattern (exit 2), and one naming a service
   that the registry's `services` row does not define or defines wrongly. On the API transport the
   client checks the structure and the coordinator's API checks the definitions against its own
   registry (422), as for `force_box` (task-dispatcher inv. 4i-1).
3. **At ship time, a service the task cannot be given fails the task.** In the serial half of a
   ship (`_ship_prepare`), each service the task asks for is looked up live, validated, and its
   files read for the box's kind (`owned` when the box's `source` is `owned`, `rented` otherwise).
   A service that is no longer defined, is defined wrongly (a box port another service uses
   included), or whose files cannot be read fails the task `claimed → task_failed` with the reason
   and an `[ALERT]`, like an unreadable resume
   checkpoint (task-dispatcher inv. 7): it is a fault of the site's settings, and retrying it on
   another box would fail the same way.
4. **The files go over ssh's standard input, never its arguments.** The pure-I/O half of the ship
   (`_ship_io`) sends one tar stream (each file mode 0600, the directory 0700) to
   `~/spool/services/<name>/` on the box, after the apt step and before the bundle. It is unpacked
   beside the directory and moved into it file by file: each move is one rename, which replaces that
   one file whole, so the directory a running task reads never disappears and no file is ever half
   written. A file the new set lacks is removed first, a dotfile included; a link or a directory
   standing where a file goes is removed, never followed. The first write, or one that finds a link or
   a non-directory at the directory's own name, moves the whole new directory into place; a link at
   `~/spool/services` itself is removed first. Nothing of a service's files is ever
   in a command line, `task.json`, the bundle, the registry, an event, a log, or a file on the
   coordinator outside the service's own `files` directory. A write that fails fails the ship like
   a failed bundle push (the task stays `claimed`). It counts against the box's link (the
   proxy-to-direct switch, the quarantine) only when ssh itself failed (exit 255) or timed out: a
   fault on the box, a full disk say, does not.
5. **Written at every ship, recorded before it is sent.** Each ship of a task that asks writes the
   files again, so a new key reaches a box at its next ship. The serial prepare half adds the
   service to `service_files_i<ID>` before any byte is sent, so files a ship wrote are removed
   (invariant 10) even when its outcome is never applied: a restart mid-ship, or the
   already-delivered path, which writes nothing. Removing files that were never written is
   harmless. A successful write is logged `service_files_written` in the serial apply half.
6. **One tunnel per rented box and service, while an open task there asks.** Each poll cycle,
   after consolidation and just before shipping, whether or not the free-space guard holds,
   `_reconcile_service_tunnels` derives the wanted set from the registry: each `(box, service)` where the box is not `owned`, is
   `live`, `paused` or `draining`, and holds a task in `claimed`, `shipped`, `running`,
   `preempting` or `cancelling` that asks for a service whose `rented.tunnel` is defined without a
   fault (a box port another service uses is one).
   - **A wanted tunnel with no live process is opened**:
     `ssh -N -T -n -R 127.0.0.1:<box_port>:<home>`, over the box's current endpoint and the identity
     of every other ssh call (`ssh_cmd`), with `ExitOnForwardFailure=yes`,
     `ServerAliveInterval=15`, `ServerAliveCountMax=4`, `ForwardAgent=no`, `ForwardX11=no`,
     `PermitLocalCommand=no`, `ControlMaster=no` and `ControlPath=none`. Its own process group,
     stdin from `/dev/null`, stdout discarded, stderr to its `.err` file. Logged
     `service_tunnel_open`.
   - **A wanted tunnel whose process has exited** is logged `service_tunnel_down` and opened again
     in the same pass. When its stderr says the remote forward was refused, the dispatcher first
     ends the `sshd` process of the connection listening on the box's `127.0.0.1:<box_port>` (an
     earlier tunnel's, which the box has not yet noticed is dead). A tunnel opens no session (`-N`),
     so that process keeps the title sshd gives a connection before any session: `sshd: USER`
     (`sshd-session: USER` from OpenSSH 9.8; a connection with a session is `sshd: USER@...`). Only
     those titles, for the user the coordinator logs in as, are signalled: never a title holding `[`
     (the server's own listener, a privileged or post-auth monitor), another user's, or anything on
     another address. A listener of any other kind is left,
     and the new tunnel will fail the same way, which is logged.
   - **A wanted tunnel whose box endpoint (proxy to direct, inv. 9a) or whose `tunnel` setting
     changed** is closed and opened anew.
   - **A tunnel no longer wanted is closed** (`SIGTERM` to its group, `SIGKILL` after 5 seconds) and
     logged `service_tunnel_closed`.
   It leads to `home` and nowhere else: the coordinator's ssh connects each forwarded connection to
   that one address. The phase never raises: the poll loop has no handler. A fault in one box and
   service's step is logged (`service_tunnels_failed`, with the box) and the phase goes on to the
   next; a fault of the pass as a whole is logged the same way and the cycle goes on to ship.

6a. **The far end is checked to listen on the box's loopback alone.** It asks for
    `127.0.0.1`, but the box's sshd decides: one set to `GatewayPorts yes` listens on every address
    whatever was asked. So on each pass after a tunnel opens, until it has been checked, the
    dispatcher reads the box's listening sockets (`/proc/net/tcp` and `/proc/net/tcp6`, over the
    same ssh; those files print each 32-bit word in the host's byte order, and every box this fleet
    rents is little-endian). The read prints a mark only once `/proc/net/tcp` itself was read, so an
    unreadable table cannot pass for an empty one.
    - Its port listening on any address beyond the loopback closes the tunnel at once, logs
      `service_tunnel_exposed` and raises an `[ALERT]` (which names the box's sshd setting
      `GatewayPorts`, or another program holding the port there, as the likely causes).
    - **It fails closed.** With `ExitOnForwardFailure=yes`, a tunnel still alive a pass after it
      opened had its forward taken, so a listener nobody can see means the read is blind, not that
      the port is safe: the second read of a live tunnel that sees it listening nowhere (the table
      unreadable, or the listener not in it) closes it the same way, with `listen: []`.
    - Either way the box and service get no tunnel from this dispatcher again: held closed in memory
      before the close itself, forgotten only once no open task on that box asks for the service
      (whatever the setting says meanwhile); a restarted dispatcher opens it once more and checks
      again.
    - The loopback alone is logged `service_tunnel_checked` and not read again. A read ssh itself
      could not make is tried at the next pass and not counted.

    Until the check, the port may be open beyond the loopback for up to two poll cycles; what it
    leads to still asks for the service's own credential.

7. **Opened before the ship, but not waited on.** The tunnel phase runs before `_ship_all`, so a
   task's tunnel is opening by the time its bundle lands. A tunnel is not confirmed before the ship,
   and a task's first connections may be refused for some seconds, as they may be after a tunnel
   dies and for up to one poll cycle until the next pass opens it again. A task that uses a service
   retries a refused connection for at least that long.
8. **Nothing about a tunnel is kept in memory alone.** The wanted set is derived from the registry
   on every pass. Each tunnel is held in memory first and its pid file written next, before any
   event is logged: whole or not at all (a new file beside it, mode 0600, then one rename), in a
   directory kept 0700, following no link. A file that cannot be written is logged
   `service_tunnel_unrecorded` and written again at each pass while the tunnel is held; this
   dispatcher can still close the tunnel meanwhile. On its first pass a dispatcher reads every such file and ends the process it names only
   when that process started at the recorded time and its command line is a tunnel's with the
   recorded `-R` value, `ExitOnForwardFailure=yes` and `ControlPath=none`: `SIGTERM` to the group it
   leads, `SIGKILL` after 5 seconds if it still runs. A file that does not prove its tunnel signals
   nothing. Every file is removed (a write cut short included), so a restarted dispatcher never
   holds two tunnels for one box and port.
9. **No tunnel to an owned box.** An owned box reaches home on the LAN; it gets the files of
   `owned` (which name the service's LAN address) and nothing is opened.
10. **The files leave a box with the last task that asks.** In the same phase, for each box whose
    `service_files_i<ID>` names a service that no open task on that box asks for, the dispatcher
    removes `~/spool/services/<name>` there, logs `service_files_removed` and drops the name from
    the row. A removal that fails keeps the name for the next pass, and is not logged. The row of a
    box that is `destroyed`, no longer in the registry, or `lost` for more than a day is deleted
    without a call: a rented box's files went with it, and an owned box taken out of the fleet
    keeps them until someone removes its `~/spool/services`. A box in any other state keeps its row
    untouched until it is `live`, `paused` or `draining` again, or gone: `unreachable`, and `lost`
    within its first day, because a `lost` box is most often a passing miss in the provider's
    listing and is adopted back `live` minutes later, its files still on it.
11. **The task is told the directory.** `_build_task_json` adds `FLEET_SERVICE_<NAME>` to
    `task.json`'s `env` for each service the task asks for (task-bundle spec: `env` is
    `{str: str}`). It carries a path, never a secret.
12. **Not in `--dry-run`.** Like every phase that makes ssh calls, the tunnel phase and the files'
    removal do nothing there.

## What a box can do with a tunnel
Connect to the one home address its tunnel names, from its own loopback, while it holds a task
that asks. A rented box's root can read everything the task can, the service's files included; a
service offered to rented boxes must therefore be one whose credential bounds what its holder can
do (its own key, its own limits), and must not be a credential used anywhere else.

The files and the tunnel belong to the box, not to the task: every task runs as the same user, so
while one task there asks, any other task on that box can read the files and use the tunnel, one
that did not ask included. On a box shared by several tasks, a service is shared with all of them.

## Open questions
None.
