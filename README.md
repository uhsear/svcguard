# svcguard

Guarantee ArcGIS Server services restart around risky maintenance, even on failure.

A nightly job stops three services before it rebuilds a locator: a map service, the locator
itself, and a feature service that reads the same data. The rebuild works. The restart loop
starts the feature service, then asks the server to start the locator. The server refuses. The
script raises on the refusal, and the loop ends there. The map service never gets its start
request.

In the morning two services are down. One of them, the locator, is really broken. The other,
the map service, has nothing wrong with it at all. It is down only because it came after the
locator in a loop that stopped at the first error. The log holds one error, about the locator,
so the person on call fixes the locator and does not look for a second outage.

svcguard stops each service, runs your maintenance command, and starts each service again. The
starts run in a `finally` block, and every start is independent: each one gets its own fresh
token, and a failed start never skips the next one. If any service does not confirm `STOPPED`,
the command is refused instead of run against live, in-use data.

```
$ python svcguard.py --self-test
PASS   1  _safe masks a token value
...
PASS  12  HTTP 200 + status=error is a failure  <-- pinned defect
...
PASS  66  nothing is started when nothing was stopped  <-- pinned defect
...
PASS  69  a refused login exits 1, not 3: no service was touched  <-- pinned defect
...
PASS  71  command NEVER runs when wait-for-STOPPED times out  <-- pinned defect
...
PASS  86  a unique prefix of --apply is refused by the parser  <-- pinned defect
PASS  87  an unknown flag exits 64, not argparse's 2  <-- pinned defect
...
PASS 116  the service started AFTER the failed one is running again  <-- pinned defect
...
PASS 125  the command never runs when one of three never confirms STOPPED  <-- pinned defect
...
PASS 127  the stop phase halts at the failure: the third service is never touched  <-- pinned defect
...
PASS 131  the start after the token failure still happened  <-- pinned defect
...
PASS 134  a stop that failed on the first of three still reports 1 of 3  <-- pinned defect
...
PASS 152  --server-url 'gis.example.org/server' is a usage error before anything is sent  <-- pinned defect
...
PASS 160  the longest prefix --appl of --apply is refused too  <-- pinned defect
...
PASS 168  --apply with no credentials and no terminal exits 64, not a traceback  <-- pinned defect
PASS 169  an unwritable log directory exits 64 before any service is touched  <-- pinned defect
...
PASS 175  a reply cut short is retried, not read as a failed start  <-- pinned defect
...
PASS 187  raises() records a message without the text
------------------------------------------------------------
187 assertions, 0 failed
```

The full run prints all 187 assertions. Each `...` line above marks a cut. It needs no
credentials, and its only network traffic goes to a stub server on `127.0.0.1`. The self-test
sets `no_proxy` for that address, so a configured HTTP proxy cannot catch the stub traffic. It
took 10 to 14 seconds on Windows with Python 3.12 or 3.13 and 2 to 4 seconds on Linux with
Python 3.12. Most of the Windows time is loopback HTTP: one refused connection alone took
2 seconds there. Time it on an idle machine, because load stretches the Windows figure. Windows
3.9, 3.12 and 3.13 and Linux 3.12 print the same 187 lines.

## Install

None. One file, standard library only, Python 3.9 or later. The self-test ran on 3.9, 3.12
and 3.13. Despite the subject matter it does not import `arcpy` or the `arcgis` package, so
any Python on the box runs it, including the one ArcGIS Pro ships. There is nothing to
`pip install` and nothing to edit.

```
git clone https://github.com/uhsear/svcguard.git
```

## Quick start

```
python svcguard.py --self-test
```

Exits 0 when every assertion passes, 1 when any fails. It asks for no credential.

## Usage

One service:

```
python svcguard.py --server-url https://gis.example.org/server --service Parcels.MapServer --command "python rebuild_locator.py" --apply
```

Several services: repeat `--service`. They stop in the order given and start in reverse.

```
python svcguard.py --server-url https://gis.example.org/server --service Parcels.MapServer --service Parcels_Locator.GeocodeServer --service Parcels_Edit.FeatureServer --command "python rebuild_locator.py" --apply
```

Without `--apply` you get the plan and exit 0, having made zero HTTP calls. The plan prints the
stop order and the start order.

| Flag | Default | Meaning |
|---|---|---|
| `--server-url URL` | required | Admin REST base, e.g. `https://gis.example.org/server`. A URL with no `http://` or `https://`, no host, or a user name in it is a usage error. An `http://` URL is accepted with a warning in the log |
| `--service NAME.TYPE` | required, repeatable | e.g. `Parcels.MapServer`. A missing dot, a space or other odd character, or the same service twice, is a usage error |
| `--command CMD` | required | Maintenance command, run only while every service is confirmed stopped. A blank command is a usage error |
| `--apply` | off | Actually talk to the server. Without it, zero HTTP calls happen |
| `--user USER` | env, else prompt | Admin user |
| `--timeout N` | 120 | Seconds for each service to reach `STOPPED` or `STARTED`. 1 or more |
| `--retries N` | 3 | Attempts per HTTP call, exponential backoff. 1 or more |
| `--insecure` | off | Skip TLS verification |
| `--self-test` | off | Run the offline assertion suite and exit |

There is no `--password` flag, by design: a password on the command line lands in shell
history and in the process table. Flags must be typed in full. The parser refuses a prefix,
so `--ap` and `--appl` are errors, not `--apply`. A refused flag, an unknown flag and a
non-integer `--timeout` all exit 64, never argparse's default 2, because 2 here means that the
command ran and failed.

| Exit | Meaning |
|---|---|
| 0 | Every service stopped, the command succeeded, every service started |
| 1 | The command was refused: a service never confirmed `STOPPED`, or the login or the server failed before the first stop. Every start that was needed succeeded |
| 2 | The command failed or could not run. Every start succeeded |
| 3 | At least one service could not be started again. Look now |
| 64 | Usage or setup error: a bad flag or value, no credentials and no terminal to ask, or a log directory that cannot be written. Nothing was contacted |

A failed start outranks everything else, so a run that refused the command and also left a
service down exits 3, not 1.

## How several services run

1. **Stop, in the order given.** One token serves the whole stop phase. Each service must reach
   `realTimeState` `STOPPED` before the next stop is sent.
2. **Halt at the first failure.** If a service does not confirm `STOPPED`, no further stop is
   sent. The services after it are never touched and keep running.
3. **Run the command only if every service confirmed `STOPPED`.** Otherwise the command is
   refused.
4. **Start every service a stop was sent for, newest first.** That includes the service the
   stop phase failed on, because a stop that timed out can leave a service half down. If the
   stop phase failed before it sent any stop, such as a refused login or an unreachable server,
   nothing is started, the command is refused, and the run exits 1.
5. **Start each one on its own.** Each start requests a fresh token, sends its start request,
   and polls for `STARTED`. A failure is logged and the next start goes ahead. A token failure
   costs one start only.
6. **Report what is still down.** The log ends with a line such as
   `1 of 3 service(s) are NOT running: Parcels_Locator.GeocodeServer`. The count is the number
   of configured services. The run exits 3.

The self-test drives each of these rules against the stub server: a refused start of the
middle service (assertions 115 to 124), a stop that never lands on the second of three (125
to 129), and a token failure on one start (130 to 132). A single `--service` runs stop, poll,
start, poll and nothing else, as it did before several services existed (133). A stop phase
that fails on the first of three still reports "1 of 3" (134). A refused login and a failed
stop token send no start (66 to 69), and neither does an unreachable server (173).

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
token is absent, the redaction marker is present, and the response was logged at all. A fifth
reads the log of a full `--apply` run over HTTP and finds neither the token nor the password.

A bad `--server-url` is caught before any request leaves: no scheme, no host, or credentials
typed into the URL (they would reach the log and the error messages). With no terminal and no
credentials in the environment, a scheduled run exits 64 with one line, not a traceback.

## What already exists

For a plain restart with no maintenance command in between, Esri's own tools are the better
choice.

- The admin API has a batch operation,
  [`startServices`](https://developers.arcgis.com/rest/enterprise-administration/server/start-services/)
  and its twin
  [`stopServices`](https://developers.arcgis.com/rest/enterprise-administration/server/stopservices/).
  One POST names a list of services, and the server starts them
  on every reachable machine in the site. There is no client-side loop to break. Its page does
  not say what happens when one service in the list fails, or whether the call waits for the
  services to come up.
- The ArcGIS API for Python wraps the same endpoints as
  [`Service.start()` and `Service.stop()`](https://developers.arcgis.com/python/latest/api-reference/arcgis.gis.server.html),
  each documented as returning a boolean. A loop over several services can check each result
  and carry on to the next.

Neither one runs a command between the stop and the start, or refuses that command when a stop
does not land. svcguard exists for that gap: the maintenance window, not the restart alone.

## Why the obvious version is wrong

**A loop that raises on a failed start strands the rest.** That is the story at the top. Every
start in svcguard is wrapped on its own, so assertion 116 fails if a refused start ever ends
the loop again.

**The stop is asynchronous, or might be.** The
[stop page](https://developers.arcgis.com/rest/enterprise-administration/server/stopservice/)
documents the reply as `{"status": "success"}` and does not say whether the instances are gone
when it arrives. The
[status page](https://developers.arcgis.com/rest/enterprise-administration/server/service-status/)
separates `configuredState` from `realTimeState` and recommends the real-time value. svcguard
trusts neither the stop reply nor the configured state. It polls `realTimeState` until it reads
`STOPPED`, and if that never arrives it refuses to run the command and exits 1. Assertion 71
fails if this is ever simplified back to trusting the stop response.

**Tokens expire mid-maintenance.** The requested lifetime is 60 minutes, and
[`generateToken`](https://developers.arcgis.com/rest/enterprise-administration/server/generatetoken/)
takes that value in minutes. A locator rebuild or a compress can outlast it. Reusing the
stop-phase token for the restart means the restart can fail with an invalid-token error at
exactly the moment it matters most. Every start requests a fresh token, which also covers the
case where the stop phase never obtained one.

**HTTP 200 is not success.** The admin API reports a refusal in the JSON body. The
[start page](https://developers.arcgis.com/rest/enterprise-administration/server/startservice/)
documents `{"status": "error", "code": 500, ...}` as the reply for a geocode or geoprocessing
service published from ArcMap, which cannot start after an upgrade to ArcGIS Enterprise 11.0. Checking the status line alone
reads that refusal as a completed start. svcguard whitelists `success`: any other status, and
any body carrying an `error` key, is a failure. The stub server answers a refused start with
that documented body. Retries cover transport faults, 429 and 5xx only. A 401, 403 or 404 is a
permanent refusal and is not retried, because retrying it only delays the page by three
backoffs.

## Limitations

- **Site-level state, not per machine.** It drives the site-level admin endpoint and does not
  verify the state of each machine in a cluster.
- **Starts are sequential.** Each start waits up to `--timeout` for `STARTED`. Three services
  that all fail to come up can take three timeouts before the run exits 3.
- **No dependency awareness.** svcguard does not know which service depends on which. It stops
  in the order you give and starts in reverse. Stopping a service that web apps or other
  services depend on breaks them for the duration, and svcguard neither detects nor warns about
  that.
- **`STOPPED` is what the API says, not what the machine feels.** There is no check for
  in-flight requests draining, connected editors, or a stuck worker. If the API reports
  `STOPPED` while a file lock lingers, the command still runs.
- **No rollback.** A maintenance command that fails halfway leaves the damage in place and
  svcguard restarts the services on top of it. This guards availability, not integrity.
- **A service stopped on purpose is started.** svcguard does not record each service's state
  before it stops it. It starts every service it reached, so a service that was already stopped
  on purpose is switched on.
- **A killed svcguard restarts nothing.** SIGTERM, SIGKILL, `TerminateProcess` and power loss
  skip the `finally` block. The guarantee covers a failing command, not a killed tool.
- **A second interrupt abandons the starts that have not run.** Ctrl-C during the command is
  covered. A further Ctrl-C during the start phase is not caught.
- **Exit 3 means "could not confirm".** A server that becomes unreachable during the command
  gives exit 3, although the services may be fine. Check the log before you restart anything.
- **A start sent to a running service is assumed to succeed.** This happens after a stop that
  timed out. The Esri start page does not document the reply, so a refusal there would show as
  an exit 3 that is not an outage.
- **The command sees the whole environment.** That includes `ARCGIS_SERVER_PASSWORD`. A
  command that prints or logs its environment writes the admin password to that output.
- **The command text is logged.** `--command` appears in the dry run, on the console and in the
  log file. Keep passwords out of it, for example a `sqlcmd -P` flag.
- **Redirects are followed.** urllib follows a 301, 302 or 303 reply to the `Location` address,
  but sends only a GET without the password or token there. The redirect turns the login POST
  into a GET, so a login behind a redirect does not complete. Give the final URL.
- **`http://` is warned about, not refused.** The admin password then travels in clear text.
  Use `https://` unless the network is trusted.
- **The script directory must be writable.** The log folder lives beside the script. If it
  cannot be created, the run exits 64 before it touches a service. Edit `LOG_DIR` to move it.
- **No lock.** Two concurrent runs against the same service will fight each other. Serialize
  them yourself.
- **Exit 3 pages nobody.** A failed restart is written to stderr and the log file, and that
  is where it stops. There is no email, webhook, or escalation. Wire the exit code into
  whatever already wakes you up.
- **Command output is not captured.** The command inherits stdout and stderr, so its output
  goes to your console, not into the svcguard log. Only its exit status and duration are
  recorded.
- **The stub is not a server.** The self-test's admin API keeps one state per service and
  changes it at once. It proves the order of requests and what svcguard does with each answer.
  It does not prove how long a real site takes to stop a service.
- **One branch is never taken.** The self-test covers every statement and every branch but
  one: the false arc of `if __name__ == "__main__":`. Run as a script, that test is always true.
- **A restart does not prove the data is current.** svcguard proves the services are up again.
  It does not prove that the rebuilt artifact matches its source. Usually you compare a
  last-edited timestamp, but a source layer without editor tracking has none. Checking derived
  columns against their source field is not enough either. A renamed feature or a moved point
  changes what the artifact should hold, and the derived columns still agree. One fix is to
  hash every input the build reads, attributes and geometry, and write the hash beside the
  deployed artifact. The next preflight hashes the source again and compares. No stored hash
  means you cannot prove the artifact is current. svcguard does none of this, so put that check
  in your maintenance command.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [jobharness](https://github.com/uhsear/jobharness) - the harness the maintenance script itself should run under
- [svcsource](https://github.com/uhsear/svcsource) - what each of those services actually points at
- [prostall](https://github.com/uhsear/prostall) - why the workstation talking to them is slow
