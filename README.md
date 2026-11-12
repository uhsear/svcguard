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
PASS 121  the service started AFTER the failed one is running again  <-- pinned defect
...
PASS 130  the command never runs when one of three never confirms STOPPED  <-- pinned defect
...
PASS 132  the stop phase halts at the failure: the third service is never touched  <-- pinned defect
...
PASS 136  the start after the token failure still happened  <-- pinned defect
...
PASS 139  a stop that failed on the first of three still reports 1 of 3  <-- pinned defect
...
PASS 157  --server-url 'gis.example.org/server' is a usage error before anything is sent  <-- pinned defect
...
PASS 165  the longest prefix --appl of --apply is refused too  <-- pinned defect
...
PASS 172  with stdin not a terminal the password is never prompted for, so a console-less run cannot hang  <-- pinned defect
...
PASS 174  on Windows a stdin that passes isatty but is not a console (the NUL device of a scheduled task) is not prompted for  <-- pinned defect
...
PASS 179  --apply with no credentials and no terminal exits 64, not a traceback  <-- pinned defect
PASS 180  an unwritable log directory exits 64 before any service is touched  <-- pinned defect
...
PASS 186  a reply cut short is retried, not read as a failed start  <-- pinned defect
...
PASS 215  _scrub masks a password in its %20 and slash-kept percent spellings too  <-- pinned defect
...
PASS 219  an admin token echoed in a reply body reaches neither the exception nor the debug log  <-- pinned defect
...
PASS 227  a mistyped --node refuses the run instead of using every node  <-- pinned defect
...
PASS 233  a redirected machine list refuses the run and the token goes nowhere else  <-- pinned defect
...
PASS 242  a shorter list is refused and the note names the dropped service  <-- pinned defect
...
PASS 268  a refused shrink leaves the saved list as it was  <-- pinned defect
...
PASS 272  the saved list is unchanged after --allow-shrink on a run whose login fails  <-- pinned defect
PASS 273  the saved list is unchanged after a growing list on a run whose login fails  <-- pinned defect
PASS 274  a first run that never logs in saves no list  <-- pinned defect
...
PASS 280  a saved list that cannot be written exits 64 before any service is touched  <-- pinned defect
PASS 281  a clean run whose saved list cannot be replaced exits 4, not 0  <-- pinned defect
...
PASS 291  a stop the server refused is still followed by a start, and exits 1 not 3  <-- pinned defect
...
PASS 294  a refused stop and a refused start on a service that is down exit 3, not 1  <-- pinned defect
...
PASS 297  a redirected POST is a refusal and no other host is contacted  <-- pinned defect
PASS 298  with no stdin the user-name prompt reads as blank  <-- pinned defect
PASS 299  a real run with no stdin and no user exits 64, not a traceback  <-- pinned defect
PASS 300  importing the file runs no main()
------------------------------------------------------------
300 assertions, 0 failed
```

The full run prints all 300 assertions. Each `...` line above marks a cut. It needs no
credentials, and its only network traffic goes to a stub server on `127.0.0.1`. The self-test
sets `no_proxy` for that address, so a configured HTTP proxy cannot catch the stub traffic. It
took 13 to 20 seconds on Windows with Python 3.9, 3.12 or 3.13 and about 5 seconds on Linux with
Python 3.12. Most of the Windows time is loopback HTTP: one refused connection alone took
2 seconds there. Time it on an idle machine, because load stretches the Windows figure. Windows
3.9.25, 3.12.10 and 3.13, and Linux 3.9.25 and 3.12.3, print the same 300 PASS lines.

## Install

None. One file, standard library only, Python 3.9 or later. The self-test ran on Windows
with 3.9.25, 3.12.10 and 3.13, and on Linux with 3.9.25 and 3.12.3. Despite the subject matter it does not import `arcpy` or the `arcgis` package, so
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

Without `--apply` you get the plan and make zero HTTP calls. The plan prints the stop order and
the start order. It exits 0, except when `--list-file` shows a shrunk list: the dry run then prints
`WOULD REFUSE` and exits 1, the exit the real run would give.

Check the machine names and keep a list of the services a window guards:

```
python svcguard.py --server-url https://gis.example.org/server --service Parcels.MapServer --service Parcels_Locator.GeocodeServer --node gis-a.example.org --list-file /var/lib/svcguard/window.json --command "python rebuild_locator.py" --apply
```

| Flag | Default | Meaning |
|---|---|---|
| `--server-url URL` | required | Admin REST base, e.g. `https://gis.example.org/server`. A URL with no `http://` or `https://`, no host, or a user name in it is a usage error. An `http://` URL is accepted with a warning in the log |
| `--service NAME.TYPE` | required, repeatable | e.g. `Parcels.MapServer`. A missing dot, a space or other odd character, or the same service twice, is a usage error |
| `--command CMD` | required | Maintenance command, run only while every service is confirmed stopped. A blank command is a usage error |
| `--node NAME` | none, repeatable | A machine name the site must list, matched in full and without regard to case (a short host name does not match a fully qualified one). A name the site does not list refuses the run before any stop, exit 1. This is a check, not a filter: svcguard stops through the site endpoint, so every machine still stops and starts, and a machine the site has that no `--node` names is only warned about |
| `--list-file PATH` | none | JSON file that keeps the service names a run guards. A later run whose list dropped a service is refused, exit 1. Written only with `--apply`. The saved file is replaced only after the run ends in exit 0 or 2 |
| `--allow-shrink` | off | Accept a shorter list than the saved one. Needs `--list-file` |
| `--apply` | off | Actually talk to the server. Without it, zero HTTP calls happen |
| `--user USER` | env, else prompt on a terminal | Admin user. With no terminal and no `ARCGIS_SERVER_USER`, the run exits 64 |
| `--timeout N` | 120 | Seconds for each service to reach `STOPPED` or `STARTED`. 1 or more |
| `--retries N` | 3 | Attempts per HTTP call, exponential backoff. 1 or more |
| `--insecure` | off | Skip TLS verification |
| `--self-test` | off | Run the offline assertion suite and exit |

There is no `--password` flag, by design: a password on the command line lands in shell
history and in the process table. Flags must be typed in full. The parser refuses a prefix,
so `--ap` and `--appl` are errors, not `--apply`. A refused flag, an unknown flag and a
non-integer `--timeout` all exit 64, never argparse's default 2, because 2 here means that the
command ran and failed. A mistyped `--password`, or any other guessed secret flag, is hidden
from the parser's error text, which names only the flags that exist (`hide_unrecognized`;
assertions 95 to 97 cover a separate value, a `--name=value` pair and a short flag).

| Exit | Meaning |
|---|---|
| 0 | Every service stopped, the command succeeded, every service started |
| 1 | The command was refused: a service never confirmed `STOPPED`, the server refused a stop (for example a mistyped service name), the login or the server failed before the first stop, a `--node` is not in the site, or the saved list shrank. Every start that was needed succeeded. The one exception is a service whose stop and start the server both refused: it is not counted as down when a status call shows it `STARTED` or answers that the service does not exist |
| 2 | The command failed or could not run. Every start succeeded |
| 3 | At least one service could not be started again. Look now |
| 4 | The run finished clean (it would have been 0) but the saved list could not be updated, so the next run compares against a stale list. Every service is running again |
| 64 | Usage or setup error: a bad flag or value, no credentials and no terminal to ask, a log directory that cannot be written, or a saved list that cannot be read or written. Nothing was contacted |

A failed start outranks everything else, so a run that refused the command and also left a
service down exits 3, not 1. That includes a service whose stop the server refused and whose
start it refused too, unless a status call shows it `STARTED` or does not know the name. If
that status call cannot be read either, the run exits 3, because svcguard cannot tell that the
service is up.

## How several services run

1. **Stop, in the order given.** One token serves the whole stop phase. Each service must reach
   `realTimeState` `STOPPED` before the next stop is sent.
2. **Halt at the first failure.** If a service does not confirm `STOPPED`, no further stop is
   sent. The services after it are never touched and keep running.
3. **Run the command only if every service confirmed `STOPPED`.** Otherwise the command is
   refused.
4. **Start every service a stop was sent for, newest first.** That includes the service the
   stop phase failed on, because a stop that timed out, met a 5xx reply or lost its connection
   can leave a service half down. A stop the server refused (a 4xx reply, a redirect, or an
   error in the reply body, such as the 404 for a mistyped service name) is followed by a start
   too, because a first attempt that timed out can have stopped the service before the retry was
   refused. Starting a started service is harmless. If that start is refused as well, a status
   call decides: `STARTED` or an unknown name keeps the exit at 1, and anything else, or a
   status that cannot be read, exits 3. If the stop phase failed before it sent any stop, such as a
   refused login or an unreachable server, nothing is started, the command is refused, and the
   run exits 1.
5. **Start each one on its own.** Each start requests a fresh token, sends its start request,
   and polls for `STARTED`. A failure is logged and the next start goes ahead. A token failure
   costs one start only.
6. **Report what is still down.** The log ends with a line such as
   `1 of 3 service(s) are NOT running: Parcels_Locator.GeocodeServer`. The count is the number
   of configured services. The run exits 3.

The self-test drives each of these rules against the stub server: a refused start of the
middle service (assertions 120 to 128), a stop that never lands on the second of three (130
to 134), and a token failure on one start (135 to 137). A single `--service` runs stop, poll,
start, poll and nothing else, as it did before several services existed (138). A stop phase
that fails on the first of three still reports "1 of 3" (139). A refused login and a failed
stop token send no start (66 to 69), and neither does an unreachable server (184). A stop the
server refuses is still followed by a start (291), and a stop refused after it took effect
leaves the service started (292). When that start is refused too, a mistyped name exits 1
(293), a service that is down exits 3, not 1 (294), a service that reads `STARTED` exits 1
(295), and a status that cannot be read exits 3 (296).

## Node names and the saved list

**A mistyped node is refused, never widened.** With `--node`, svcguard logs in, then sends one
GET to the site machines resource, documented on the Esri
[`machines`](https://developers.arcgis.com/rest/enterprise-administration/server/machines/)
page. Its Methods section documents GET for reading machines, and it names each machine under
`machineName`. The page also lists Register and Rename, which svcguard never calls.
If a `--node` is not in that list, or the reply cannot be read, the run exits 1 before any stop.
It does not fall back to "all nodes". Assertions 227 and 228 cover the mistyped name and show
that no stop, start or status request follows it.

**A shrunk list is refused.** A maintenance window grows by hand: someone adds a service,
someone else drops one while editing the command line, and the dropped service is no longer
stopped before the command or started after it. With `--list-file`, each run compares its own
services with the saved ones, without regard to case. A service that dropped out is refused
with exit 1 (assertions 242 and 268) unless `--allow-shrink` says it was deliberate. A swap, one
service added and one dropped, counts as a shrink. A file that exists but is not a svcguard list
exits 64 and is never read as "no list" (248 to 254, 278, 279).

**The list moves only after the command ran.** svcguard stages the new list beside the old one
and replaces the old one only when the run ends in exit 0 or 2. A run refused at the login or the
node check, or one with a failed start, leaves the saved list as it was (272 to 274, 282, 283). Otherwise a
failed `--allow-shrink` run would store the short list and hide the shrink from the next run. A
dry run never writes the file (258).

## Configuration

Precedence is **flag, then environment variable, then the `CONFIGURATION` block in the
source**. The same rule is written at the top of `svcguard.py` under `CONFIG PRECEDENCE`.

```
user       --user     >  env ARCGIS_SERVER_USER      >  interactive prompt
password   (no flag)  >  env ARCGIS_SERVER_PASSWORD  >  interactive getpass prompt (console only)
tunables   CLI flag   >  CONFIGURATION constant
```

Settings with no flag live in the `CONFIGURATION` block near the top of the file:
`POLL_INTERVAL` 5s, `HTTP_TIMEOUT` 30s, `RETRY_BASE_DELAY` 2s, `RETRY_MAX_DELAY` 60s,
`TOKEN_EXPIRATION` 60 minutes, `LOG_RETENTION` 30 files, `LOG_DIR` a `logs/` folder beside
the script. Change them there, not at the call site.

The password is never a flag, never logged, never written to disk. Every response body is
passed through a redactor before it reaches the DEBUG log file, because the `generateToken`
response body is itself the secret. Three assertions read the log file back to prove the
token is absent and the redaction marker is present (102 to 104). A fourth checks that the
polled states were logged at all (105). A fifth (145) reads the log of a full `--apply` run
over HTTP and finds neither the token nor the password. A sixth (219) has a server echo the
request target, token included, inside an error body, and finds the token in neither the
exception nor the log: svcguard also masks the secrets it sent wherever a reply repeats them.

A bad `--server-url` is caught before any request leaves: no scheme, no host, or credentials
typed into the URL (they would reach the log and the error messages). With no terminal and no
credentials in the environment, a scheduled run exits 64 with one line, not a traceback. That
includes a process with no stdin at all (assertions 298 and 299, the second a real child
process). svcguard prompts for the user name and the password only when stdin is a console.
Before this check, the user-name prompt read a piped line as the name, blocked on an open pipe,
and crashed with a `RuntimeError` when stdin was closed. On Windows `os.isatty()` also
answers true for the NUL device that a scheduler gives a task, and `getpass` then waits on a
console nobody can type at. So svcguard asks Windows whether the stdin handle is a console
input buffer, through
[`GetStdHandle`](https://learn.microsoft.com/en-us/windows/console/getstdhandle) and
[`GetConsoleMode`](https://learn.microsoft.com/en-us/windows/console/getconsolemode)
(assertions 172 to 177). Microsoft documents `GetConsoleMode` for a console buffer handle and
a zero return on failure. That it fails for NUL was observed by hand on Windows with Python
3.13: an inherited NUL stdin passed `isatty()` and failed the console probe. A detached run with
stdin on NUL exiting 64 at once, where before it hung, is also a hand observation, not a
self-test result. Assertions 174 to 177 replace the Windows library with a stub that answers by
handle number, so the console-true side never runs on a real console. The Microsoft page also
says `GetConsoleMode` should not be used to decide whether a handle is a console, and names
`NtQueryVolumeInformationFile` as the alternative. svcguard keeps `GetConsoleMode` because it
fails safe: a doubtful handle reads as no console, so the run exits 64 instead of hanging. A
password piped into stdin is not read; set `ARCGIS_SERVER_PASSWORD`.

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
start in svcguard is wrapped on its own, so assertion 121 fails if a refused start ever ends
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
  covered (checked by a monkeypatched run, not by a self-test assertion; see below). A further Ctrl-C during the start phase is not caught.
- **Exit 3 means "could not confirm".** A server that becomes unreachable during the command
  gives exit 3, although the services may be fine. Check the log before you restart anything.
- **A start sent to a running service is assumed to succeed.** This happens after a stop that
  timed out. The Esri start page does not document the reply, so a refusal there would show as
  an exit 3 that is not an outage.
- **The command sees the whole environment.** That includes `ARCGIS_SERVER_PASSWORD`. A
  command that prints or logs its environment writes the admin password to that output.
- **The command text is logged.** `--command` appears in the dry run, on the console and in the
  log file. Keep passwords out of it, for example a `sqlcmd -P` flag.
- **A redirected POST is refused.** A 301, 302 or 303 reply to the login, a stop, a start or a
  status request is an exit 1 refusal and svcguard contacts no other host (assertion 297).
  Before this check urllib sent a body-less GET to the new address and svcguard believed its
  JSON. Give the final URL, for example the one behind a load balancer that redirects http to
  https.
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
- **Coverage is 100 percent.** The self-test imports the file once under another module name
  (assertion 300) to reach the false arc of `if __name__ == "__main__":`.
- **The saved list can fail to update after a good run.** If the command ran but the saved list
  cannot be replaced, for example because another program holds the file open, svcguard logs an
  error and exits 4 instead of 0 (assertion 281), so a scheduler sees that the baseline is
  stale. A run that already exits 2 keeps 2. The next run compares against the old list, and
  a service dropped in between is not reported until a run replaces the list. Run once by hand
  after exit 4.
- **A dry run does not test the saved list path.** With `--list-file` in a directory that does
  not exist, the dry run says the list will be saved and exits 0. The real run then exits 64.
- **Ctrl-C can leave `PATH.tmp` behind.** An interrupt after the list is staged ends with a
  traceback and a stray temporary file next to the saved list. The saved list itself is intact.
- **Secrets are masked by key name, and by value for what svcguard sent.** svcguard masks
  values under the keys `token`, `password`, `passwd`, `secret` and `credentials`. It also masks
  the token or password it sent when a reply repeats it in any string, as plain text or in a
  percent-encoded form (`+` or `%20` for a space, with `/` encoded or kept; assertions 214 to
  219). Another encoding of a repeated secret, such as HTML escaping, is not masked.
  A different secret that a server invents and puts under another key is not masked.
- **An empty service type passes validation.** A `--service` value such as `x.` has a dot and
  allowed characters, so svcguard sends it. The server refuses it (exit 1). svcguard does not
  check that the type is a real service type.
- **A non-numeric port passes validation.** `--server-url https://h:abc/server` has a scheme,
  a host and no credentials. It fails at the first request (exit 1, nothing stopped).
- **`--node` is a check, not a filter.** `--node gis-a` does not stop only that machine. The
  services still stop for the whole site.
- **The 404 for a mistyped service name is the stub's reply.** The Esri
  [`stopService`](https://developers.arcgis.com/rest/enterprise-administration/server/stopservice/)
  page does not document the status for an unknown name. svcguard treats any 4xx as a refused
  stop; the 404 in the self-test is an assumption about a real server. The same assumption
  holds for the status call after a refused stop and a refused start: svcguard reads an error
  body from the status resource as "the server does not know this service".
- **A refused stop is still followed by one start request.** This is deliberate. A first stop
  attempt that timed out can have taken effect before the retry was refused, and starting a
  started service is harmless. For a mistyped name the server refuses that start too.
- **A refused stop for a mistyped name logs a misleading line.** The follow-up start fails and
  the log holds `RESTART FAILED, <service> may be down` at CRITICAL, although the exit is 1 and
  nothing is down. The status call has decided by then; the log line is written before it.
- **A console that cannot encode `--command` can break the dry run.** The dry run prints the
  command with `%r`. A character the console code page cannot encode may raise an error there.
  The dry run writes nothing.
- **A shrunk-list refusal reaches stderr only.** The refusal comes before logging starts, so no
  log file holds it. For this cause the log line before exit 1 does not exist; read the console.
- **A refused `--node` ends with a misleading log line.** The log says "REFUSING to run the
  maintenance command: not every service is confirmed STOPPED". The line before it,
  "--node not in this site", names the real cause.
- **The Ctrl-C start-up behaviour was checked by hand, not by the self-test.** On Linux with
  Python 3.12 against a local stub, Ctrl-C during the command started the services again and
  then left the traceback. A later audit could not reproduce this with a real signal: its Linux
  attempt ignored SIGINT, because background jobs of a non-interactive shell start with SIGINT
  ignored. A second audit drove the guard with a command that raises `KeyboardInterrupt`
  (a monkeypatched run). Both services were started again, the interrupt propagated, and a
  stray `PATH.tmp` stayed. A real Ctrl-C on a console is still unconfirmed.
- **TLS is tested on the SSL context only.** Assertions 15 to 18 check the context object that
  `--insecure` changes. The whole self-test runs over plain `http://` on `127.0.0.1`, so no real
  TLS handshake or certificate check is exercised.
- **Esri documents the machines GET, not a token in its query string.** The
  [`machines`](https://developers.arcgis.com/rest/enterprise-administration/server/machines/)
  page documents GET and the `machineName` reply. Its Tokens text asks for the token in the body
  of a POST. svcguard relies on the admin API accepting the query token on this GET.
- **A dry run does not check `--node`.** It makes no HTTP call, so it exits 0 for a mistyped
  name. Only the real run checks the names against the site.
- **Names are not checked for path segments.** `--service` accepts `.`, `..` and `/` in a name,
  and `--server-url` accepts a query string or a fragment. A name or URL like that changes the
  admin request path, and the dry run and error text print the URL as typed. The values come from
  the operator, never from the server.
- **`PATH.tmp` is overwritten and removed.** svcguard stages the new list in the saved list's
  path plus `.tmp`. It overwrites any file already there and deletes it when the run is refused
  or fails, even if svcguard did not make it.
- **`--node` is a check, not a filter.** A valid partial `--node` still stops and starts the
  services across every machine of the site, and only logs a warning about the machines it did
  not name.
- **The `--node` check puts the token in a URL.** It sends the short-lived admin token as a
  query parameter on one GET request, so it can appear in the access log of the server or any
  proxy on the path. svcguard's own log and output never carry it.
- **A redirected machine list is refused.** The machines GET carries the token in its query
  string, so svcguard follows no redirect there. A 301, 302 or 303 reply is an exit 1 refusal
  (assertion 233), and the token goes to no other host. Give the final URL.
- **A `--node` name must match exactly.** The comparison ignores case only. A short host name
  does not match a fully qualified one, so the run is refused and you must type the listed name.
- **The saved list stores service names only.** A service that moved to another folder or type
  shows as one removed and one added, and the shrink is refused until you pass `--allow-shrink`.
  Two `--service` names that differ only in case are not caught as a repeat.
- **The saved list guards drift only while the file survives.** Deleting or moving it makes the
  next run a first run, and a shrink is not reported.
- **Give `--list-file` an absolute path.** A relative path resolves against the working directory
  of the scheduler. If that changes between runs, the run reads no list, counts as the first,
  and writes a new file, which hides a shrink.
- **The saved list records what the run intended, once the command ran.** It holds the services
  this run named, not proof that each was guarded. A crash while saving can leave a stray `.tmp`
  file beside it.
- **A first run that is refused or leaves a service down saves no list.** The list moves only
  after the command ran, so the next run counts as a first run and has nothing to compare with.
- **A short secret can mask part of a reply.** svcguard masks the password it sent wherever a
  reply repeats it. A password of a few characters that also occurs inside a word of the reply
  turns that word into `***REDACTED***`, so a good reply can read as a failure. The result is a
  refusal, never a clean run.
- **Two runs that start in the same second write to one log file.** The name is
  `svcguard_YYYYMMDD_HHMMSS.log`, and both runs append to it.
- **Delete `logs/` before you zip the directory.** The log folder is ignored by git, but its
  files can hold the account name of the person who ran the tool.
- **Exit 1 has two causes.** It covers both a refusal on a finding (an unknown `--node`, a shrunk
  list, a service that never reached `STOPPED`) and a login or server that failed. Read the log
  line before it.
- **Ctrl-C is not turned into an exit code.** The `finally` block still starts the services,
  but the interrupt then leaves as a traceback, with an exit status outside the documented set.
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
