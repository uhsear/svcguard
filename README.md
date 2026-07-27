# svcguard

Guarantee an ArcGIS Server service restarts around risky maintenance, even on failure.

Stop a service, run your maintenance command, start it again. The restart lives in a
`finally` block and fetches its own fresh token, so it fires even when the command exits
non-zero, raises, cannot find its binary, or outlives the token that stopped the service.
If the service never confirms `STOPPED`, the command is refused instead of run against
live, in-use data.

```
$ python svcguard.py --self-test
PASS   1  _safe masks a token value
PASS   2  _safe preserves non-secret values
...
PASS  12  HTTP 200 + status=error is a failure  <-- pinned defect
...
PASS  64  command NEVER runs when wait-for-STOPPED times out  <-- pinned defect
...
PASS  91  log pruning keeps exactly LOG_RETENTION files
------------------------------------------------------------
91 assertions, 0 failed
```

91 assertions, no sockets, no credentials, 0.6 seconds on Python 3.13.

## Install

None. One file, standard library only, Python 3.8+. Despite the subject matter it does not
import `arcpy` or the `arcgis` package, so any Python on the box runs it, including the one
ArcGIS Pro ships. There is nothing to `pip install` and nothing to edit.

```
git clone https://github.com/uhsear/svcguard.git
```

## Quick start

```
python svcguard.py --self-test
```

Exits 0 when every assertion passes, 1 when any fails. It opens no socket and asks for no
credential.

## Usage

```
python svcguard.py --server-url https://gis.example.org/server --service Parcels.MapServer --command "python rebuild_locator.py" --apply
```

Without `--apply` you get the plan and exit 0, having made zero HTTP calls.

| Flag | Default | Meaning |
|---|---|---|
| `--server-url URL` | required | Admin REST base, e.g. `https://gis.example.org/server` |
| `--service NAME.TYPE` | required | e.g. `Parcels.MapServer`. A missing dot is a usage error |
| `--command CMD` | required | Maintenance command, run only while the service is confirmed stopped |
| `--apply` | off | Actually talk to the server. Without it, zero HTTP calls happen |
| `--user USER` | env, else prompt | Admin user |
| `--timeout N` | 120 | Seconds to reach `STOPPED` or `STARTED` |
| `--retries N` | 3 | Attempts per HTTP call, exponential backoff |
| `--insecure` | off | Skip TLS verification |
| `--self-test` | off | Run the offline assertion suite and exit |

There is no `--password` flag, by design: a password on the command line lands in shell
history and in the process table.

| Exit | Meaning |
|---|---|
| 0 | Stopped, command succeeded, started |
| 1 | Never confirmed `STOPPED`, command refused, restart succeeded |
| 2 | Command failed, restart succeeded |
| 3 | The service could not be started again. Look now |
| 64 | Usage error |

## Configuration

Precedence is **flag, then environment variable, then the `CONFIGURATION` block in the
source**. The same rule is written at the top of `svcguard.py` under `CONFIG PRECEDENCE`.

```
user       --user     >  env ARCGIS_SERVER_USER      >  interactive prompt
password   (no flag)  >  env ARCGIS_SERVER_PASSWORD  >  interactive getpass prompt
tunables   CLI flag   >  CONFIGURATION constant
```

Settings with no flag live in the `CONFIGURATION` block near the top of the file:
`POLL_INTERVAL` 5s, `HTTP_TIMEOUT` 30s, `RETRY_BASE_DELAY` 2s, `RETRY_MAX_DELAY` 60s,
`TOKEN_EXPIRATION` 60 minutes, `LOG_RETENTION` 30 files, `LOG_DIR` a `logs/` folder beside
the script. Change them there, not at the call site.

The password is never a flag, never logged, never written to disk. Every response body is
passed through a redactor before it reaches the DEBUG log file, because the `generateToken`
response body is itself the secret. Four assertions read the log file back to prove the
token is absent, the redaction marker is present, and the response was logged at all.

## Why the obvious version is wrong

**The stop is asynchronous.** `POST /admin/services/NAME.TYPE/stop` returns
`{"status": "success"}` the moment the request is accepted, not when the service is down.
A script that runs its maintenance command on the next line is working against a service
that is still answering requests and still holding locks. svcguard polls `realTimeState`
until it reads `STOPPED`, and if that never arrives it refuses to run the command and exits
1. Assertion 64 fails if this is ever simplified back to trusting the stop response.

**Tokens expire mid-maintenance.** The requested lifetime is 60 minutes, and a locator
rebuild or a compress can outlast it. Reusing the stop-phase token for the restart means
the restart fails with a 498 at exactly the moment it matters most. The restart phase
always requests a fresh token, which also covers the case where the stop phase never
obtained one.

**HTTP 200 is not success.** The admin API answers 200 with `{"status": "error"}` or
`{"error": {"code": 498}}` in the body, so checking the status line alone reads a refusal
as a completed stop. svcguard whitelists `success`: any other status, and any body carrying
an `error` key, is a failure. Retries cover transport faults, 429 and 5xx only. A 401, 403
or 404 is a permanent refusal and is not retried, because retrying it only delays the page
by three backoffs.

## Limitations

- **One service, not a cluster.** It drives the site-level admin endpoint for a single
  service and does not verify per-machine state. Several services means several runs, and
  their ordering is your problem.
- **`STOPPED` is what the API says, not what the machine feels.** There is no check for
  in-flight requests draining, connected editors, or a stuck worker. If the API reports
  `STOPPED` while a file lock lingers, the command still runs.
- **No rollback.** A maintenance command that fails halfway leaves the damage in place and
  svcguard restarts the service on top of it. This guards availability, not integrity.
- **No dependency awareness.** Stopping a service that web apps or other services depend on
  breaks them for the duration. svcguard neither detects nor warns about that.
- **No lock.** Two concurrent runs against the same service will fight each other. Serialize
  them yourself.
- **Exit 3 pages nobody.** A failed restart is written to stderr and the log file, and that
  is where it stops. There is no email, webhook, or escalation. Wire the exit code into
  whatever already wakes you up.
- **Command output is not captured.** The command inherits stdout and stderr, so its output
  goes to your console, not into the svcguard log. Only its exit status and duration are
  recorded.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.
