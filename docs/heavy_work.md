# A machine-wide lock for heavy work

Builds, kind clusters and long test gates are heavy. When several agents or
processes share one machine they should run one at a time, and each run should
leave a record of what ran, for how long and how much memory it needed.
`piceli heavy` does both.

```{admonition} Maturity: preview
:class: note

New in 0.13.0. The JSON of `heavy run` and `heavy status` only gains fields
within a version series.
```

## Run a command under the lock

```sh
piceli heavy run --name gate -- cargo test --workspace
piceli heavy run --wait 600 -- make build
```

`piceli heavy run [--name N] [--wait SECONDS] -- COMMAND...`:

- takes an OS-level `flock` on a lock file in a per-user state directory. A
  holder that crashes, even with `SIGKILL`, releases the lock by itself; there
  is no marker directory to clean up;
- while it waits it prints who holds the lock (command, pid, start time) on
  stderr. Never environment variables;
- runs `COMMAND` without a shell, in its own process group. `SIGINT`,
  `SIGTERM` and `SIGHUP` sent to `piceli` are forwarded to that group;
- exits with the command's exit code (`128 + N` when signal `N` killed it);
- prints one JSON object, the receipt, on stdout. The command's own stdout is
  sent to stderr so the contract (machine output on stdout) holds;
- gives up after `--wait` seconds (default 3600; `0` does not wait) with the
  rejection `heavy-lock-timeout` (exit `2`; the JSON on stdout tells it from
  a command that exited `2`).

## Receipts

Every finished run writes `receipts/<timestamp>-<pid>.json`
(`piceli.heavy-receipt.v1`) in the state directory; the newest 100 are kept.

| Field | Meaning |
| --- | --- |
| `command`, `name`, `cwd` | What ran. Values of options named `token`, `secret`, `password`, `key` or `credential` are replaced by `***`. |
| `sources` | `{commit, dirty}` when `cwd` is in a git repository, else `null`. |
| `started_at`, `finished_at`, `waited_seconds`, `duration_seconds` | Timing; `started_at` is when the lock was taken. |
| `exit_code` | As above. |
| `peak_memory_bytes` | Largest resident set of any process of the command's tree that finished (`getrusage(RUSAGE_CHILDREN)`); not the sum of a parallel build's processes. `null` for Piceli's own builds (in-process). |
| `kind` | `run` or `section` (Piceli's own build). |

## Status

```sh
piceli heavy status --json
```

`state` is `held` or `free`, `holder` names the current holder and `receipts`
lists the most recent ones. Read-only.

## Piceli's own heavy operations

Host builds and Docker `Build.spec` runs take the same lock when
`PICELI_HEAVY_LOCK=1` is set, so an agent running `piceli deploy` and another
running `piceli heavy run -- cargo test` serialize. It is off by default. A
build started by `piceli heavy run` knows it already holds the lock and does
not wait for its own parent.

## State directory

`$PICELI_HEAVY_DIR`, else `$XDG_STATE_HOME/piceli/heavy`, else
`~/.local/state/piceli/heavy` (owner-only). Files: `heavy.lock`,
`holder.json` (present while held), `receipts/`.

## Limits

The lock is advisory and machine-local (a shared network file system is not
supported). If `piceli heavy run` itself is killed with `SIGKILL`, the lock is
released but its child keeps running, outside the lock.

Errors: `heavy-lock-timeout`, `heavy-command-empty`, `heavy-command-missing`
(see {doc}`reference/errors`).
