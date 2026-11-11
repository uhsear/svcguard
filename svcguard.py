#!/usr/bin/env python3
"""svcguard: run a maintenance command against stopped ArcGIS Server services and
guarantee every service is started again afterwards, even when the command fails.

SEVERAL SERVICES
    Repeat --service. The services stop in the order given and start in reverse.
    The stop phase halts at the first service that does not confirm STOPPED, and
    the command runs only when every service confirmed STOPPED. Every service a stop
    was sent for is then started, each with its own fresh token, and one failed
    start never skips the starts after it. When no stop was sent, nothing is started.

NODE NAMES
    --node NAME (repeatable) states which machines of the site this run may touch.
    svcguard stops a service through the site endpoint and cannot aim at one machine,
    so --node is a check, not a filter. A name the site does not list refuses the
    run before any stop (exit 1); it never falls through to "all nodes". Machines
    the site has and --node does not name are warned about. Without --node, no
    machine call is made.

SAVED LIST
    --list-file PATH keeps the services a run guards. The next run compares its own
    list with the saved one. A service that dropped out is refused (exit 1) unless
    --allow-shrink says the shrink is deliberate. The file is written only with
    --apply, and only when it is new, grew, or --allow-shrink accepted a shrink.

Stdlib only. Python 3.9+.

CONFIG PRECEDENCE (identical to the README):
    user      --user           >  env ARCGIS_SERVER_USER      >  interactive getpass prompt
    password  (no flag exists) >  env ARCGIS_SERVER_PASSWORD  >  interactive getpass prompt
    all other tunables:  CLI flag  >  CONFIGURATION constant below
The password is never accepted as a flag, never logged, and never written to disk.

EXIT CODES
    0   every service stopped, command succeeded, every service started
    1   the command was REFUSED: a service never reached STOPPED, the login or the
        server failed before the first stop, a --node is not in the site, or the saved
        list shrank; every start that was needed succeeded
    2   command failed (non-zero or raised); restarts succeeded
    3   at least one service could not be started again. A human needs to look now
   64   usage or setup error: a bad flag, a bad value, no credentials, an unwritable
        log directory, an unreadable or unwritable saved list. Nothing was contacted
"""

import argparse
import datetime
import getpass
import glob
import http.client
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.2.0"

# ==========================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# ==========================================================================
POLL_INTERVAL = 5          # seconds between realTimeState polls
HTTP_TIMEOUT = 30          # seconds for a single HTTP request
LOG_RETENTION = 30         # number of svcguard_*.log files to keep
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
RETRY_BASE_DELAY = 2       # seconds; first backoff sleep
RETRY_MAX_DELAY = 60       # seconds; backoff ceiling
TOKEN_EXPIRATION = 60      # minutes requested from generateToken
# ==========================================================================

log = logging.getLogger("svcguard")

# Indirection points. The self-test replaces these; production never does.
_now = time.monotonic
_sleep = time.sleep


class SvcGuardError(Exception):
    """Non-retryable failure: the server answered, and the answer was bad."""


class TransientError(Exception):
    """Retryable failure: transport, timeout, 5xx, unparseable body."""


class StateTimeout(SvcGuardError):
    """The service never reached the requested realTimeState in time."""


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_SECRET_KEYS = frozenset(("token", "password", "passwd", "secret", "credentials"))


def _safe(obj):
    """Deep-copy a response with secret-looking values masked.

    Every path that stringifies a server response goes through this, because the
    generateToken response body *is* the secret and the file handler is at DEBUG.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                out[key] = "***REDACTED***"
            else:
                out[key] = _safe(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_safe(item) for item in obj]
    return obj


def _scrub(text, data):
    """Mask the secret values of a request form inside an error message.

    A GET carries its token in the query string, and http.client puts the whole request
    target in some of its messages.
    """
    for key, value in data.items():
        if key.lower() in _SECRET_KEYS and value:
            for form in (str(value), urllib.parse.quote_plus(str(value))):
                text = text.replace(form, "***REDACTED***")
    return text


def _prune_logs(log_dir, keep=LOG_RETENTION):
    files = sorted(glob.glob(os.path.join(log_dir, "svcguard_*.log")))
    removed = []
    for path in files[:-keep] if keep > 0 else files:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def _setup_logging(log_dir, retention=LOG_RETENTION):
    """File handler at DEBUG, console at INFO. Returns the log file path."""
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, "svcguard_%s.log" % stamp)

    log.setLevel(logging.DEBUG)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(console)

    _prune_logs(log_dir, retention)
    return path


def _close_logging():
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()


# --------------------------------------------------------------------------
# HTTP. Every network byte in this tool goes through _http_json
# --------------------------------------------------------------------------

def _ssl_context(insecure):
    ctx = ssl.create_default_context()
    if insecure:
        # Deliberate reversal of the source script, which hardcoded verify=False.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_json(url, data, insecure=False, http_timeout=HTTP_TIMEOUT, method="POST"):
    """POST a form body (or GET with it as a query), return the parsed JSON object.

    The one HTTP chokepoint. Only the admin `machines` resource is documented as GET
    only; everything else here is a POST with the token in the body. A GET puts the token
    in the query string, so no message below may carry the full request URL.
    """
    form = urllib.parse.urlencode(data)
    headers = {"User-Agent": "svcguard/%s" % __version__}
    try:
        if method == "GET":
            request = urllib.request.Request(url + "?" + form, headers=headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = urllib.request.Request(url, data=form.encode("utf-8"), headers=headers)
        opened = urllib.request.urlopen(
            request, timeout=http_timeout, context=_ssl_context(insecure)
        )
        try:
            # utf-8-sig: a byte order mark would make json.loads refuse a good body.
            raw = opened.read().decode("utf-8-sig", "replace")
        finally:
            opened.close()
    except urllib.error.HTTPError as exc:
        # 5xx and 429 can clear on their own; 4xx (401 bad creds, 403, 404 wrong
        # service) is a permanent refusal. Retrying it only delays the page.
        if exc.code >= 500 or exc.code == 429:
            raise TransientError("HTTP %s from %s" % (exc.code, url))
        raise SvcGuardError("HTTP %s from %s (not retryable)" % (exc.code, url))
    except (ValueError, http.client.InvalidURL) as exc:
        # A URL urllib or http.client cannot use. No retry will fix it, and the message
        # can carry the request target, which holds the token on a GET.
        raise SvcGuardError("bad request URL %s: %s" % (url, _scrub(str(exc), data)))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # HTTPException covers a body cut short (IncompleteRead) and a garbage status
        # line (BadStatusLine); both are transport faults and clear on a retry.
        raise TransientError("%s: %s" % (type(exc).__name__, exc))

    try:
        parsed = json.loads(raw)
    except ValueError:
        raise TransientError("non-JSON response from %s (%d bytes)" % (url, len(raw)))
    log.debug("response from %s: %s", url, _safe(parsed))
    return parsed


def _check_ags(resp, what):
    """ArcGIS Server answers HTTP 200 with failure in the body. A 200 is not success."""
    if not isinstance(resp, dict):
        raise SvcGuardError("%s: expected a JSON object, got %s" % (what, type(resp).__name__))
    if "error" in resp:
        err = resp["error"]
        code = err.get("code") if isinstance(err, dict) else None
        raise SvcGuardError("%s: server error code=%s body=%s" % (what, code, _safe(resp)))
    # Whitelist, not blacklist: AGS says "error" here, not "failed", and a future
    # endpoint could invent a third word. Anything that is not "success" is not.
    if "status" in resp and resp["status"] != "success":
        raise SvcGuardError("%s: status=%s body=%s" % (what, resp["status"], _safe(resp)))
    return resp


def _backoff(attempt):
    return min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1)))


def _request(url, data, insecure, retries, what, method="POST"):
    """Retry transport failures with backoff. Never retry a server-side refusal."""
    extra = {} if method == "POST" else {"method": method}
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = _http_json(url, data, insecure, HTTP_TIMEOUT, **extra)
        except TransientError as exc:
            if attempt >= retries:
                raise SvcGuardError("%s failed after %d attempt(s): %s" % (what, attempt, exc))
            delay = _backoff(attempt)
            log.warning("%s attempt %d failed (%s); retrying in %ss", what, attempt, exc, delay)
            _sleep(delay)
            continue
        return _check_ags(resp, what)


# --------------------------------------------------------------------------
# ArcGIS Server admin operations
# --------------------------------------------------------------------------

def get_token(server_url, username, password, insecure, retries):
    resp = _request(
        server_url.rstrip("/") + "/admin/generateToken",
        {
            "username": username,
            "password": password,
            "client": "requestip",
            "expiration": TOKEN_EXPIRATION,
            "f": "json",
        },
        insecure,
        retries,
        "token generation",
    )
    token = resp.get("token")
    if not token:
        raise SvcGuardError("token generation returned no token: %s" % (_safe(resp),))
    # _safe() is the only reason this line is allowed to exist.
    log.debug("token generation succeeded: %s", _safe(resp))
    return token


def manage_service(server_url, service, token, action, insecure, retries):
    what = "service %s" % action
    resp = _request(
        "%s/admin/services/%s/%s" % (server_url.rstrip("/"), service, action),
        {"token": token, "f": "json"},
        insecure,
        retries,
        what,
    )
    if resp.get("status") != "success":
        raise SvcGuardError("%s: unexpected body %s" % (what, _safe(resp)))
    # Accepted, not done. The caller must poll realTimeState.
    log.info("%s request accepted (async; polling realTimeState)", what)
    return resp


def get_service_state(server_url, service, token, insecure, retries):
    resp = _request(
        "%s/admin/services/%s/status" % (server_url.rstrip("/"), service),
        {"token": token, "f": "json"},
        insecure,
        retries,
        "service status",
    )
    return resp.get("realTimeState", "UNKNOWN")


def get_machines(server_url, token, insecure, retries):
    """The machine names the site lists. The admin machines resource is GET only."""
    resp = _request(
        "%s/admin/machines" % server_url.rstrip("/"),
        {"token": token, "f": "json"},
        insecure,
        retries,
        "machine list",
        method="GET",
    )
    machines = resp.get("machines")
    if not isinstance(machines, list) or not all(
            isinstance(item, dict) and isinstance(item.get("machineName"), str)
            and item["machineName"] for item in machines):
        raise SvcGuardError("machine list: unexpected body %s" % (_safe(resp),))
    return [item["machineName"] for item in machines]


def node_drift(wanted, machines):
    """Compare --node names with the site's machines. Pure.

    Returns (unknown, unnamed): the wanted names the site does not list, and the site
    machines no wanted name covers. Machine names compare case-insensitively.
    """
    have = set(name.lower() for name in machines)
    want = set(name.lower() for name in wanted)
    unknown = [name for name in wanted if name.lower() not in have]
    unnamed = [name for name in machines if name.lower() not in want]
    return unknown, unnamed


def verify_nodes(server_url, token, nodes, insecure, retries):
    """Refuse a --node the site does not list. Warn about machines --node leaves out."""
    machines = get_machines(server_url, token, insecure, retries)
    unknown, unnamed = node_drift(nodes, machines)
    if unknown:
        raise SvcGuardError("--node not in this site: %s. The site lists: %s"
                            % (", ".join(unknown), ", ".join(machines) or "no machines"))
    if unnamed:
        log.warning("svcguard stops through the site endpoint and cannot aim at one "
                    "machine. --node does not name: %s", ", ".join(unnamed))


def wait_for_state(server_url, service, token, target, insecure, retries, timeout):
    """Poll until realTimeState == target. Raise StateTimeout otherwise.

    The stop/start response only means 'accepted'. Trusting it is how a maintenance
    command ends up running against a still-live service.
    """
    deadline = _now() + timeout
    while True:
        try:
            state = get_service_state(server_url, service, token, insecure, retries)
            log.info("  realTimeState=%s (want %s)", state, target)
            if state == target:
                return True
        except SvcGuardError as exc:
            log.warning("  status check failed, will keep polling: %s", exc)
        if _now() >= deadline:
            raise StateTimeout(
                "service did not reach %s within %ss" % (target, timeout)
            )
        _sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# saved service list
# --------------------------------------------------------------------------

def list_drift(saved, current):
    """Compare the saved service list with this run's. Pure.

    Returns (removed, added). Names compare case-insensitively, so a retyped case is not
    a shrink. Order is not compared: only the set decides.
    """
    now = set(name.lower() for name in current)
    before = set(name.lower() for name in saved)
    removed = [name for name in saved if name.lower() not in now]
    added = [name for name in current if name.lower() not in before]
    return removed, added


def list_verdict(saved, current, allow_shrink):
    """Decide what this run does with the saved list. Pure.

    Returns (verdict, note). The verdict is first, same, grew, shrunk (refuse) or
    allowed (a shrink --allow-shrink accepted). saved is None when no list exists yet.
    """
    if saved is None:
        return "first", "no saved list yet; this run's list will be saved"
    removed, added = list_drift(saved, current)
    if removed and not allow_shrink:
        return "shrunk", ("the saved list shrank: %s no longer named. A missing service "
                          "is not stopped before the command and is not started after "
                          "it. Add it back, or pass --allow-shrink if the shrink is "
                          "deliberate" % ", ".join(removed))
    if removed:
        return "allowed", ("the saved list shrank and --allow-shrink accepted it: %s "
                           "dropped" % ", ".join(removed))
    if added:
        return "grew", "the saved list grew: %s added" % ", ".join(added)
    return "same", "the saved list matches this run"


def read_list(path):
    """Return the saved service names, or None when the file does not exist.

    A file that exists but cannot be read as a svcguard list raises SvcGuardError. It
    is never read as "no list", because that would let a damaged file hide a shrink.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SvcGuardError("saved list %s cannot be read (%s). Fix it or delete it"
                            % (path, exc))
    names = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and "." in name and set(name) <= _SERVICE_CHARS
            for name in names):
        raise SvcGuardError("saved list %s is not a svcguard list: expected "
                            '{"services": ["Name.Type", ...]}. Fix it or delete it' % path)
    return names


def write_list(path, services):
    """Save the services this run guards. Write a temp file, then replace the list."""
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"svcguard_list": 1, "services": services}, handle, indent=2)
        handle.write("\n")
    os.replace(temp, path)


# --------------------------------------------------------------------------
# maintenance command
# --------------------------------------------------------------------------

def _run_command(command):
    """Return the exit status. Raises OSError if the shell itself cannot start."""
    log.info("Running maintenance command: %s", command)
    started = datetime.datetime.now()
    # shell=True: --command is operator-supplied on their own machine, so the shell
    # is a convenience, not a trust boundary.
    completed = subprocess.run(command, shell=True)
    log.info("Command exited %s after %s", completed.returncode,
             datetime.datetime.now() - started)
    return completed.returncode


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def restart_order(services, reached):
    """The services to start again, in start order. Pure.

    reached is the index of the last service a stop request was sent for, or -1
    when none was sent. The stop phase halts at its first failure, so the services
    after it were never touched and are left exactly as they were. The one it failed
    on is started too: a stop that timed out can leave a service half down. When no
    stop was sent, nothing was stopped and nothing is started.
    """
    return list(reversed(services[:reached + 1]))


def exit_code(all_stopped, command_rc, failed_starts):
    """Combine the three phase results into one exit code. Pure."""
    if failed_starts:
        return 3
    if not all_stopped:
        return 1
    if command_rc != 0:
        return 2
    return 0


def _start_one(server_url, service, username, password, insecure, retries, timeout):
    """Start one service. Never raises, so one failed start cannot skip the next.

    Each start gets a FRESH token: the stop-phase token may have expired during a
    long command, it may never have existed if the stop phase's own token call
    threw, and a token failure here must cost this service only.
    """
    try:
        log.info("Requesting FRESH token to start %s ...", service)
        token = get_token(server_url, username, password, insecure, retries)
        log.info("Starting %s ...", service)
        manage_service(server_url, service, token, "start", insecure, retries)
        wait_for_state(server_url, service, token, "STARTED", insecure, retries, timeout)
        log.info("%s is STARTED.", service)
        return True
    except Exception as exc:
        log.critical("RESTART FAILED, %s may be down: %s", service, exc)
        return False


def guard(server_url, services, command, username, password, insecure, retries, timeout,
          nodes=None):
    """stop each -> command -> ALWAYS start each, independently. Returns an exit code.

    nodes, when given, is checked against the site's machines before the first stop.
    """
    reached = -1      # index of the last service a stop was sent for; -1 = none yet
    all_stopped = False
    command_rc = None
    failed = []

    try:
        try:
            log.info("Requesting token for stop phase...")
            token = get_token(server_url, username, password, insecure, retries)
            if nodes:
                verify_nodes(server_url, token, nodes, insecure, retries)
            for index, service in enumerate(services):
                reached = index
                log.info("Stopping %s ...", service)
                manage_service(server_url, service, token, "stop", insecure, retries)
                wait_for_state(server_url, service, token, "STOPPED", insecure, retries,
                               timeout)
                log.info("%s is STOPPED.", service)
            all_stopped = True
        except Exception as exc:
            if reached < 0:
                log.error("Stop phase failed before any stop was sent: %s", exc)
            else:
                log.error("Stop phase failed at %s: %s", services[reached], exc)

        if all_stopped:
            try:
                command_rc = _run_command(command)
            except Exception as exc:
                log.error("Maintenance command could not run: %s", exc)
                command_rc = -1
        else:
            log.error(
                "REFUSING to run the maintenance command: not every service is confirmed "
                "STOPPED. Running it now would operate on live, in-use data."
            )
    finally:
        for service in restart_order(services, reached):
            if not _start_one(server_url, service, username, password, insecure, retries,
                              timeout):
                failed.append(service)
        if failed:
            log.critical("%d of %d service(s) are NOT running: %s",
                         len(failed), len(services), ", ".join(failed))

    return exit_code(all_stopped, command_rc, failed)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, but 2 means 'the command ran and failed' here."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


_SERVICE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./")


def validate_target(server_url, services):
    """Return a usage-error message, or None when the target looks usable. Pure.

    Catches a typo before any request leaves: a URL urllib cannot open, credentials
    typed into the URL (they would be echoed in logs), a service name that would
    break the request path.
    """
    try:
        parts = urllib.parse.urlsplit(server_url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        return "--server-url must start with https:// (or http://) and name a host"
    if "@" in parts.netloc:
        return ("--server-url must not carry credentials; use ARCGIS_SERVER_USER and "
                "ARCGIS_SERVER_PASSWORD")
    for name in services:
        if "." not in name or not set(name) <= _SERVICE_CHARS:
            return "--service must be NAME.TYPE, e.g. Parcels_Locator.GeocodeServer"
    return None


_NODE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def validate_nodes(nodes):
    """Return a usage-error message for --node values, or None. Pure."""
    for name in nodes:
        if not name or not set(name) <= _NODE_CHARS:
            return "--node must be a machine name of letters, digits, dot, dash or underscore"
    folded = [name.lower() for name in nodes]
    repeated = sorted(set(name for name in folded if folded.count(name) > 1))
    if repeated:
        return "--node given more than once: %s" % ", ".join(repeated)
    return None


def build_parser():
    parser = _Parser(
        prog="svcguard",
        description="Guarantee ArcGIS Server services restart around risky maintenance, "
                    "even on failure.",
        epilog="Password: env ARCGIS_SERVER_PASSWORD, else prompt. There is no --password flag.",
        allow_abbrev=False,
    )
    parser.add_argument("--server-url",
                        help="AGS admin REST base, e.g. https://gis.example.org/server")
    parser.add_argument("--service", action="append",
                        help="NAME.TYPE, e.g. Parcels_Locator.GeocodeServer. Repeat for "
                             "several: they stop in the order given and start in reverse")
    parser.add_argument("--command", help="maintenance command run while the service is stopped")
    parser.add_argument("--apply", action="store_true",
                        help="actually talk to the server; without it, zero HTTP calls happen")
    parser.add_argument("--user", default=None,
                        help="admin user (default: env ARCGIS_SERVER_USER, else prompt)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="seconds to reach STOPPED/STARTED (default 120)")
    parser.add_argument("--retries", type=int, default=3,
                        help="attempts per HTTP call (default 3)")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (off by default; the source script "
                             "hardcoded verify=False, this reverses that)")
    parser.add_argument("--node", action="append",
                        help="machine name the run may touch; repeat for several. A name "
                             "the site does not list refuses the run before any stop")
    parser.add_argument("--list-file",
                        help="JSON file that keeps the services this run guards. A later "
                             "run whose list dropped one is refused")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="accept a shorter service list than the one in --list-file")
    parser.add_argument("--self-test", action="store_true",
                        help="run the offline assertion suite and exit")
    return parser


def _resolve_user(explicit):
    if explicit:
        return explicit
    from_env = os.environ.get("ARCGIS_SERVER_USER")
    if from_env:
        return from_env
    try:
        return input("ArcGIS Server admin user: ").strip()
    except EOFError:  # no terminal (scheduler, cron): same as a blank answer
        return ""


def _resolve_password():
    from_env = os.environ.get("ARCGIS_SERVER_PASSWORD")
    if from_env:
        return from_env
    try:
        return getpass.getpass("ArcGIS Server admin password: ")
    except EOFError:
        return ""


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    if args.command is not None:
        args.command = args.command.strip()  # a blank command would run an empty shell
    missing = [name for name in ("server_url", "service", "command")
               if not getattr(args, name)]
    if missing:
        sys.stderr.write("error: missing required option(s): %s\n"
                         % ", ".join("--" + m.replace("_", "-") for m in missing))
        return 64
    services = args.service
    problem = validate_target(args.server_url, services)
    if problem:
        sys.stderr.write("error: %s\n" % problem)
        return 64
    repeated = sorted(set(name for name in services if services.count(name) > 1))
    if repeated:
        sys.stderr.write("error: --service given more than once: %s\n" % ", ".join(repeated))
        return 64
    if args.retries < 1 or args.timeout < 1:
        sys.stderr.write("error: --retries and --timeout must be >= 1\n")
        return 64
    nodes = args.node or []
    problem = validate_nodes(nodes)
    if problem:
        sys.stderr.write("error: %s\n" % problem)
        return 64
    if args.allow_shrink and not args.list_file:
        sys.stderr.write("error: --allow-shrink needs --list-file\n")
        return 64
    verdict = note = None
    if args.list_file:
        try:
            saved = read_list(args.list_file)
        except SvcGuardError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 64
        verdict, note = list_verdict(saved, services, args.allow_shrink)
    if verdict == "shrunk" and args.apply:
        sys.stderr.write("REFUSED: %s\n" % note)
        return 1

    if not args.apply:
        print("DRY RUN. No HTTP call was made. Re-run with --apply to execute.")
        print("  server   : %s" % args.server_url)
        print("  stop     : %s (halts at the first that does not confirm STOPPED)"
              % " -> ".join(services))
        print("  run      : %r, only if every service confirmed STOPPED" % args.command)
        print("  start    : %s (each with a fresh token; a failed start skips no other)"
              % " -> ".join(restart_order(services, len(services) - 1)))
        if nodes:
            print("  nodes    : %s (checked against the site's machine list before any stop)"
                  % ", ".join(nodes))
        if verdict:
            print("  list     : %s" % note)
        print("  tls      : %s" % ("VERIFICATION DISABLED (--insecure)" if args.insecure
                                   else "verified"))
        print("  timeout  : %ss   retries: %s   poll: %ss" % (args.timeout, args.retries,
                                                              POLL_INTERVAL))
        if verdict == "shrunk":
            print("  WOULD REFUSE: the saved list shrank. Exit 1.")
            return 1
        return 0

    username = _resolve_user(args.user)
    password = _resolve_password()
    if not username or not password:
        sys.stderr.write("error: a username and password are required for --apply\n")
        return 64

    try:
        log_path = _setup_logging(LOG_DIR)
    except OSError as exc:
        sys.stderr.write("error: cannot write the log directory %s: %s\n" % (LOG_DIR, exc))
        return 64
    log.info("=" * 60)
    log.info("svcguard %s starting | services=%s | log=%s", __version__, ", ".join(services),
             log_path)
    if args.insecure:
        log.warning("TLS verification is DISABLED for this run (--insecure).")
    if args.server_url.lower().startswith("http://"):
        log.warning("--server-url is http://: the admin password travels in clear text.")
    try:
        if verdict:
            log.info("saved list: %s", note)
        if verdict in ("first", "grew", "allowed"):
            try:
                write_list(args.list_file, services)
            except OSError as exc:
                sys.stderr.write("error: cannot write the saved list %s: %s\n"
                                 % (args.list_file, exc))
                return 64
        code = guard(args.server_url, services, args.command, username, password,
                     args.insecure, args.retries, args.timeout, nodes)
    finally:
        log.info("svcguard finished. Log: %s", log_path)
        _close_logging()
    return code


# ==========================================================================
# SELF-TEST. No credentials, no arcpy, no network beyond a loopback stub on 127.0.0.1.
# ==========================================================================

class _Checker(object):
    def __init__(self):
        self.total = 0
        self.failed = 0

    def __call__(self, condition, label):
        self.total += 1
        if condition:
            print("PASS %3d  %s" % (self.total, label))
        else:
            self.failed += 1
            print("FAIL %3d  %s" % (self.total, label))


class _Clock(object):
    """Deterministic clock: only _sleep advances it."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _FakeServer(object):
    """Scripted stand-in for _http_json. Records every call."""

    def __init__(self, states=None, token_failures=0, stop_body=None,
                 start_body=None, token_body=None, status_body=None):
        self.calls = []
        self.states = list(states or ["STOPPED", "STARTED"])
        self.token_failures = token_failures
        self.token_calls = 0
        self.stop_body = stop_body or {"status": "success"}
        self.start_body = start_body or {"status": "success"}
        self.token_body = token_body
        self.status_body = status_body

    def __call__(self, url, data, insecure=False, http_timeout=None):
        self.calls.append(url)
        if url.endswith("/generateToken"):
            self.token_calls += 1
            if self.token_calls <= self.token_failures:
                raise TransientError("simulated connection reset")
            return self.token_body or {"token": "SECRET-TOKEN-abc123", "expires": 1}
        if url.endswith("/stop"):
            return self.stop_body
        if url.endswith("/start"):
            return self.start_body
        if url.endswith("/status"):
            if self.status_body is not None:
                return self.status_body
            state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
            return {"configuredState": state, "realTimeState": state}
        raise AssertionError("unexpected URL in self-test: %s" % url)

    def count(self, suffix):
        return len([u for u in self.calls if u.endswith(suffix)])


class _Collect(logging.Handler):
    """Keeps every formatted log message, so a test can read the final summary line."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _explode(*_args, **_kwargs):
    raise AssertionError("HTTP call attempted without --apply")


def _quiet(func, *args):
    """Call func, capturing stdout/stderr. Returns (result, stdout, stderr)."""
    import io
    out, err = io.StringIO(), io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        result = func(*args)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    return result, out.getvalue(), err.getvalue()


def _raises(check, fn, exc_type, label, text=None):
    """Record one assertion: fn() raises exc_type, and its message passes text.

    text is a substring or a predicate on the message. Returns the exception, or
    None when the assertion failed.
    """
    try:
        fn()
    except exc_type as exc:
        message = str(exc)
        ok = text is None or (text(message) if callable(text) else text in message)
        check(ok, label)
        return exc
    except Exception as exc:
        check(False, "%s (wrong exception %r)" % (label, exc))
        return None
    check(False, "%s (no error raised)" % label)
    return None


class _StubAdmin(object):
    """A loopback http.server that plays the admin API, so the real urllib path runs.

    It keeps one realTimeState per service and logs every (operation, service) in
    arrival order. A service in fail_start answers its start with the documented
    error body; a service in never_stops accepts a stop and stays STARTED.
    """

    TOKEN = "STUB-TOKEN-q7w8e9"

    CUT = object()

    def __init__(self, services, fail_start=(), never_stops=(), cut_status=0, machines=(),
                 machines_reply=None):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        self.state = dict((name, "STARTED") for name in services)
        self.fail_start = set(fail_start)
        self.never_stops = set(never_stops)
        self.cut_status = cut_status
        self.ops = []
        self.forms = []
        self.gets = []
        self.get_forms = []
        self.machines_reply = machines_reply if machines_reply is not None else {
            "machines": [{"machineName": name, "adminURL": "https://%s:6443/arcgis/admin" % name}
                         for name in machines]}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length"))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                stub.forms.append(form)
                reply = stub.answer(self.path)
                if reply is stub.CUT:
                    raw = b'{"configuredState": "STA'
                    declared = len(raw) + 40  # promise more bytes than are sent, then hang up
                else:
                    raw = (reply if isinstance(reply, str) else json.dumps(reply)).encode("utf-8")
                    declared = len(raw)
                self.send_response(200)
                self.send_header("Content-Length", str(declared))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                # The one GET resource of the admin API this tool reads: machines.
                stub.gets.append(self.path)
                stub.get_forms.append(urllib.parse.parse_qs(self.path.partition("?")[2]))
                raw = json.dumps(stub.machines_reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/x" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                         kwargs={"poll_interval": 0.01})
        self.thread.daemon = True
        self.thread.start()

    def answer(self, path):
        if path.endswith("/admin/generateToken"):
            return {"token": self.TOKEN, "expires": 1}
        if path.endswith("/admin/machines"):
            # The machines resource is documented as GET only; a POST here is a regression.
            return {"error": {"code": 405, "message": "machines accepts GET only"}}
        if path.endswith("/proxy"):
            return "<html>proxy sign-in page</html>"
        if path.endswith("/bom"):
            return "\ufeff" + json.dumps({"status": "success"})
        service, op = path.split("/")[-2:]
        self.ops.append((op, service))
        if op == "status" and self.cut_status > 0:
            self.cut_status -= 1
            return self.CUT
        if op == "status":
            return {"configuredState": self.state[service], "realTimeState": self.state[service]}
        if op == "start" and service in self.fail_start:
            # The documented shape of a start the server refuses.
            return {"status": "error", "code": 500, "messages": ["cannot start"]}
        if op == "start":
            self.state[service] = "STARTED"
        elif service not in self.never_stops:
            self.state[service] = "STOPPED"
        return {"status": "success"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def self_test():
    module = sys.modules[__name__]
    check = _Checker()
    real_http = _http_json
    real_run = _run_command
    real_token = get_token
    real_now, real_sleep = _now, _sleep
    # urllib sends the loopback stub through a configured proxy unless told not to.
    original_env = dict(os.environ)
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"
    log.addHandler(logging.NullHandler())
    log.propagate = False
    log.setLevel(logging.CRITICAL)

    def install(http=None, run=None, clock=None):
        module._http_json = http if http is not None else real_http
        module._run_command = run if run is not None else real_run
        clock = clock or _Clock()
        module._now = clock.now
        module._sleep = clock.sleep
        return clock

    def raises(fn, exc_type, label, text=None):
        return _raises(check, fn, exc_type, label, text)

    def run_guard(fake, run=None, retries=3, timeout=120, clock=None):
        """Run guard() with counted get_token; returns (exit_code, token_call_count)."""
        counter = [0]

        def counting(*a, **k):
            counter[0] += 1
            return real_token(*a, **k)

        install(http=fake, run=run, clock=clock)
        module.get_token = counting
        try:
            code = guard("https://s.example.org/x", ["Svc.MapServer"], "noop",
                         "u", "p", False, retries, timeout)
        finally:
            module.get_token = real_token
        return code, counter[0]

    try:
        # ---- group A: redaction, response validation, ssl, backoff -------
        red = _safe({"token": "abc", "keep": 1, "nested": {"password": "p", "ok": 2}})
        check(red["token"] == "***REDACTED***", "_safe masks a token value")
        check(red["keep"] == 1, "_safe preserves non-secret values")
        check(red["nested"]["password"] == "***REDACTED***", "_safe masks nested secrets")
        check(red["nested"]["ok"] == 2, "_safe recurses without damaging nested data")
        check(_safe([{"Token": "x"}])[0]["Token"] == "***REDACTED***",
              "_safe is case-insensitive and walks lists")
        check(_safe("plain") == "plain", "_safe passes scalars through")

        check(_check_ags({"status": "success"}, "w") == {"status": "success"},
              "_check_ags accepts a success body")
        check(_check_ags({"realTimeState": "STOPPED"}, "w")["realTimeState"] == "STOPPED",
              "_check_ags accepts a status body with no 'status' key")
        exc = raises(lambda: _check_ags({"status": "failed", "messages": ["nope"]}, "stop"),
                     SvcGuardError, "HTTP 200 + status=failed is a failure", "status=failed")
        check("stop" in str(exc), "failure message names the operation")
        raises(lambda: _check_ags({"error": {"code": 498, "message": "Invalid token"}}, "status"),
               SvcGuardError, "HTTP 200 + error{code:498} is a failure", "498")
        # AGS's real word for a failed admin operation is "error", not "failed".
        raises(lambda: _check_ags({"status": "error", "messages": ["boom"]}, "start"),
               SvcGuardError, "HTTP 200 + status=error is a failure  <-- pinned defect",
               "status=error")
        raises(lambda: _check_ags({"status": "whoKnows"}, "w"), SvcGuardError,
               "_check_ags whitelists success (any other status is a failure)")
        raises(lambda: _check_ags("<html>proxy</html>", "w"), SvcGuardError,
               "_check_ags rejects a non-dict body")

        check(_ssl_context(True).verify_mode == ssl.CERT_NONE,
              "--insecure yields CERT_NONE")
        check(_ssl_context(True).check_hostname is False,
              "--insecure disables hostname checking")
        check(_ssl_context(False).verify_mode == ssl.CERT_REQUIRED,
              "default verifies certificates (source hardcoded verify=False)")
        check(_ssl_context(False).check_hostname is True,
              "default checks hostname")

        check(_backoff(1) == RETRY_BASE_DELAY, "backoff starts at RETRY_BASE_DELAY")
        check(_backoff(2) > _backoff(1), "backoff grows")
        check(_backoff(30) == RETRY_MAX_DELAY, "backoff is capped at RETRY_MAX_DELAY")

        # ---- group B: retry ---------------------------------------------
        calls = [0]

        def flaky(url, data, insecure=False, http_timeout=None):
            calls[0] += 1
            if calls[0] < 3:
                raise TransientError("boom")
            return {"status": "success"}

        install(http=flaky)
        check(_request("u", {}, False, 3, "flaky")["status"] == "success",
              "retry succeeds on the third attempt after two transient failures")
        check(calls[0] == 3, "retry made exactly 3 attempts")

        calls[0] = 0

        def always_fail(url, data, insecure=False, http_timeout=None):
            calls[0] += 1
            raise TransientError("boom")

        install(http=always_fail)
        raises(lambda: _request("u", {}, False, 3, "doomed"), SvcGuardError,
               "retry raises when attempts are exhausted", "after 3 attempt")
        check(calls[0] == 3, "exhausted retry stopped at --retries attempts")

        calls[0] = 0

        def refused(url, data, insecure=False, http_timeout=None):
            calls[0] += 1
            return {"status": "failed", "messages": ["denied"]}

        install(http=refused)
        raises(lambda: _request("u", {}, False, 3, "refused"), SvcGuardError,
               "a server refusal raises")
        check(calls[0] == 1, "a server refusal is NOT retried")

        # HTTP-status classification, exercised through the real _http_json by
        # replacing urlopen. Nothing is dialled: the fake raises before any socket.
        real_urlopen = urllib.request.urlopen
        opened = [0]

        def _raise_http(code):
            def opener(*_a, **_k):
                opened[0] += 1
                raise urllib.error.HTTPError("u", code, "msg", {}, None)
            return opener

        try:
            for code in (401, 403, 404):
                opened[0] = 0
                urllib.request.urlopen = _raise_http(code)
                install()  # restore the real _http_json under the fake urlopen
                raises(lambda: _request("https://s.example.org/x", {}, False, 3, "perm"),
                       SvcGuardError,
                       "HTTP %d is a refusal, not a transient  <-- pinned defect" % code,
                       lambda msg: "not retryable" in msg and "attempt" not in msg)
                check(opened[0] == 1, "HTTP %d made exactly 1 request" % code)

            for code in (429, 500, 503):
                opened[0] = 0
                urllib.request.urlopen = _raise_http(code)
                install()
                raises(lambda: _request("https://s.example.org/x", {}, False, 3, "temp"),
                       SvcGuardError, "HTTP %d is retried as transient" % code,
                       "after 3 attempt")
                check(opened[0] == 3, "HTTP %d used all 3 attempts" % code)
        finally:
            urllib.request.urlopen = real_urlopen

        # ---- group C: wait_for_state ------------------------------------
        fake = _FakeServer(states=["STARTED", "STARTED", "STOPPED"])
        install(http=fake)
        check(wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 3, 120) is True,
              "wait_for_state returns True once the target state is seen")
        check(fake.count("/status") == 3, "wait_for_state polled until the state changed")

        fake = _FakeServer(states=["STARTED"])
        install(http=fake)
        raises(lambda: wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 3, 20),
               StateTimeout, "wait_for_state raises StateTimeout when the state never changes",
               "STOPPED")
        check(fake.count("/status") >= 2, "the timeout path polled more than once")

        fake = _FakeServer(status_body={"error": {"code": 498, "message": "expired"}})
        install(http=fake)
        raises(lambda: wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 1, 10),
               StateTimeout, "a 498 during polling does not count as reaching the state")

        fake = _FakeServer(states=["UNKNOWN"])
        install(http=fake)
        raises(lambda: wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 1, 10),
               StateTimeout, "UNKNOWN is not treated as STOPPED")

        # The deadline test is >=: a poll at exactly the deadline is the last one, and a
        # state seen on that poll still counts.
        fake = _FakeServer(states=["STARTED"])
        install(http=fake)
        raises(lambda: wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 3, 10),
               StateTimeout, "a timeout of 10s polls at 0, 5 and 10 seconds and no later")
        check(fake.count("/status") == 3, "the poll at exactly the deadline is the last poll")
        fake = _FakeServer(states=["STARTED", "STARTED", "STOPPED"])
        install(http=fake)
        check(wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 3, 10) is True,
              "a state that arrives on the poll at exactly the deadline still counts")

        # A status body with no realTimeState must never read as STOPPED.
        fake = _FakeServer(status_body={"configuredState": "STOPPED"})
        install(http=fake)
        check(get_service_state("b", "S.MapServer", "t", False, 1) == "UNKNOWN",
              "a status body with no realTimeState reads UNKNOWN  <-- pinned defect")
        raises(lambda: wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 1, 10),
               StateTimeout, "a configuredState of STOPPED alone does not confirm STOPPED"
               "  <-- pinned defect")

        # ---- group D: happy path ----------------------------------------
        ran = []
        fake = _FakeServer(states=["STOPPED", "STARTED"])
        code, tokens = run_guard(fake, run=lambda cmd: ran.append(cmd) or 0)
        check(code == 0, "happy path exits 0")
        check(tokens == 2, "get_token called exactly twice (fresh token for restart)")
        check(fake.count("/stop") == 1, "happy path stopped the service once")
        check(fake.count("/start") == 1, "happy path started the service once")
        check(ran == ["noop"], "happy path ran the maintenance command once")
        check(fake.calls.index("https://s.example.org/x/admin/services/Svc.MapServer/stop")
              < fake.calls.index("https://s.example.org/x/admin/services/Svc.MapServer/start"),
              "stop is ordered before start")

        # ---- group E: command exits non-zero ----------------------------
        fake = _FakeServer(states=["STOPPED", "STARTED"])
        code, tokens = run_guard(fake, run=lambda cmd: 7)
        check(fake.count("/start") == 1, "restart fires when the command exits non-zero")
        check(code == 2, "exit 2 when the command failed but the restart succeeded")
        check(tokens == 2, "a failed command still gets a fresh restart token")

        # ---- group F: command raises / binary missing --------------------
        def missing_binary(cmd):
            raise FileNotFoundError("no such file or directory: rebuild.exe")

        fake = _FakeServer(states=["STOPPED", "STARTED"])
        code, tokens = run_guard(fake, run=missing_binary)
        check(fake.count("/start") == 1, "restart fires when the command binary is missing")
        check(code == 2, "a raised command still exits 2, not 0")

        def exploding(cmd):
            raise RuntimeError("command blew up")

        fake = _FakeServer(states=["STOPPED", "STARTED"])
        code, _ = run_guard(fake, run=exploding)
        check(fake.count("/start") == 1, "restart fires when the command raises")
        check(code == 2, "a raising command exits 2")

        # ---- group G: stop-phase token throws ----------------------------
        ran = []
        fake = _FakeServer(states=["STARTED"], token_failures=3)
        code, tokens = run_guard(fake, run=lambda cmd: ran.append(cmd) or 0, retries=3)
        check(ran == [], "command never runs when the stop phase's token call throws")
        check(fake.count("/stop") == 0, "no stop was issued after the token failure")
        check(fake.count("/start") == 0,
              "nothing is started when nothing was stopped  <-- pinned defect")
        check(tokens == 1, "no restart token is requested for a stop that never happened")
        check(code == 1, "exit 1: the command was refused and there is nothing to restart")

        ran = []
        fake = _FakeServer(token_body={"error": {"code": 400,
                                                 "message": "Invalid username or password."}})
        code, tokens = run_guard(fake, run=lambda cmd: ran.append(cmd) or 0)
        check(code == 1 and ran == [],
              "a refused login exits 1, not 3: no service was touched  <-- pinned defect")
        check(tokens == 1 and fake.count("/start") == 0 and fake.count("/stop") == 0,
              "a refused login is sent once and starts nothing")

        # ---- group H: THE PINNED DEFECT ---------------------------------
        # A naive rewrite trusts the async stop response and runs the command
        # against a still-live service. These four must fail if that regresses.
        ran = []
        fake = _FakeServer(states=["STARTED"])
        code, _ = run_guard(fake, run=lambda cmd: ran.append(cmd) or 0, timeout=20)
        check(ran == [],
              "command NEVER runs when wait-for-STOPPED times out  <-- pinned defect")
        check(fake.count("/stop") == 1, "the stop request was still issued")
        check(fake.count("/start") == 1, "the service is started again after the refusal")
        check(code == 1, "a refused command exits 1, distinct from a failed command (2)")

        # ---- group I: restart failure ------------------------------------
        fake = _FakeServer(states=["STOPPED"], start_body={"status": "failed",
                                                           "messages": ["cannot start"]})
        code, _ = run_guard(fake, run=lambda cmd: 0, timeout=20)
        check(code == 3, "exit 3 when the restart itself fails")
        check(code != 2, "restart failure is distinct from command failure")

        fake = _FakeServer(states=["STOPPED", "STARTING"])
        code, _ = run_guard(fake, run=lambda cmd: 0, timeout=20)
        check(code == 3, "exit 3 when the service never reaches STARTED")

        # ---- group J: no --apply, no HTTP --------------------------------
        # _explode raises on ANY http call or command execution.
        install(http=_explode, run=_explode)
        code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                       "--service", "Svc.MapServer", "--command", "noop"])
        check(code == 0, "dry run exits 0 and makes zero HTTP calls")
        check("DRY RUN" in out, "dry run says so on stdout")
        check("--apply" in out, "dry run names the flag that would execute it")
        code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                       "--service", "Svc.MapServer", "--command", "noop",
                                       "--insecure"])
        check(code == 0, "dry run with --insecure still makes zero HTTP calls")
        check("VERIFICATION DISABLED" in out, "dry run warns that --insecure was passed")
        code, out, err = _quiet(main, ["--command", "noop"])
        check(code == 64, "missing required options exit 64")
        check("--server-url" in err, "the usage error names the missing option")
        code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--service", "NoDot",
                                       "--command", "c"])
        check(code == 64, "--service must be NAME.TYPE")
        raises(lambda: _quiet(build_parser().parse_args, ["--ap"]), SystemExit,
               "a unique prefix of --apply is refused by the parser  <-- pinned defect", "64")
        raises(lambda: _quiet(main, ["--bogus"]), SystemExit,
               "an unknown flag exits 64, not argparse's 2  <-- pinned defect", "64")
        raises(lambda: _quiet(main, ["--timeout", "abc"]), SystemExit,
               "a non-integer --timeout exits 64", "64")
        raises(lambda: _quiet(main, ["--help"]), SystemExit, "--help still exits 0", "0")
        check(build_parser().parse_args([]).apply is False, "--apply defaults to OFF")
        check(build_parser().parse_args([]).insecure is False, "--insecure defaults to OFF")
        check(build_parser().parse_args([]).timeout == 120, "--timeout defaults to 120")
        check(build_parser().parse_args([]).retries == 3, "--retries defaults to 3")
        flags = set()
        for action in build_parser()._actions:
            flags.update(action.option_strings)
        check("--password" not in flags, "there is no --password flag")
        check("--apply" in flags and "--insecure" in flags, "the documented flags exist")
        check(len(flags) == 14, "exactly 12 flags plus -h/--help are defined")

        # ---- group K: the token never reaches the log file ---------------
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="svcguard-selftest-")
        try:
            log.propagate = False
            path = _setup_logging(tmp, retention=LOG_RETENTION)
            log.setLevel(logging.DEBUG)
            for handler in log.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(
                        handler, logging.FileHandler):
                    handler.setLevel(logging.CRITICAL)
            fake = _FakeServer(states=["STOPPED", "STARTED"])
            run_guard(fake, run=lambda cmd: 0)
            _close_logging()
            with open(path, "r", encoding="utf-8") as handle:
                contents = handle.read()
            check("SECRET-TOKEN-abc123" not in contents,
                  "the token is never written to the log file")
            check("token generation succeeded" in contents,
                  "the log did record the token response (so the redaction is load-bearing)")
            check("REDACTED" in contents, "the log shows the redaction marker instead")
            check("realTimeState" in contents, "the log records polled service states")

            for index in range(LOG_RETENTION + 4):
                open(os.path.join(tmp, "svcguard_old_%03d.log" % index), "w").close()
            before = len(glob.glob(os.path.join(tmp, "svcguard_*.log")))
            _prune_logs(tmp, LOG_RETENTION)
            after = len(glob.glob(os.path.join(tmp, "svcguard_*.log")))
            check(before > LOG_RETENTION, "the retention test created more logs than the cap")
            check(after == LOG_RETENTION, "log pruning keeps exactly LOG_RETENTION files")
            check(len(_prune_logs(tmp, 0)) == LOG_RETENTION
                  and glob.glob(os.path.join(tmp, "svcguard_*.log")) == [],
                  "a keep of 0 removes every log")
        finally:
            _close_logging()
            shutil.rmtree(tmp, ignore_errors=True)

        # From here on, more than one service. A loopback http.server plays the
        # admin API, so these runs go through the real urllib path, not a fake.
        def hush():
            _close_logging()
            log.addHandler(logging.NullHandler())
            log.setLevel(logging.CRITICAL)

        def ops(stub, op):
            return [name for (seen, name) in stub.ops if seen == op]

        def tokens(stub):
            return len([form for form in stub.forms if "username" in form])

        hush()
        work = tempfile.mkdtemp(prefix="svcguard-selftest-")
        saved_env = dict(os.environ)
        real_log_dir = LOG_DIR
        real_getpass = getpass.getpass
        try:
            # ---- group L: the pure decision core ------------------------
            three = ["A.MapServer", "B.GeocodeServer", "C.FeatureServer"]
            check(restart_order(three, 2) == ["C.FeatureServer", "B.GeocodeServer",
                                              "A.MapServer"],
                  "every service the stop phase reached starts again, in reverse order")
            check(restart_order(three, 1) == ["B.GeocodeServer", "A.MapServer"],
                  "a stop phase that failed on the second service restarts the first two")
            check(restart_order(three, 0) == ["A.MapServer"],
                  "a stop phase that failed on the first service restarts that one only")
            check(restart_order(["A.MapServer"], 0) == ["A.MapServer"],
                  "one service restarts that service, as before")
            check(exit_code(True, 0, []) == 0, "all stopped, command 0, all started is exit 0")
            check(exit_code(True, 0, ["B.GeocodeServer"]) == 3,
                  "one failed start is exit 3 even when everything else worked")
            check(exit_code(False, None, ["B.GeocodeServer"]) == 3,
                  "a failed start outranks a refused command")
            check(exit_code(True, 7, ["B.GeocodeServer"]) == 3,
                  "a failed start outranks a failed command")
            check(exit_code(False, None, []) == 1, "a refused command with all started is exit 1")
            check(exit_code(True, 7, []) == 2, "a failed command with all started is exit 2")
            check(exit_code(True, -1, []) == 2, "a command that could not run is exit 2")

            # ---- group M: one failed start skips no other ---------------
            ran = []
            stub = _StubAdmin(three, fail_start=["B.GeocodeServer"])
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20)
            finally:
                stub.close()
            check(code == 3, "a failed start of the middle service exits 3")
            check(stub.state["A.MapServer"] == "STARTED",
                  "the service started AFTER the failed one is running again  <-- pinned defect")
            check(stub.state["C.FeatureServer"] == "STARTED",
                  "the service started before the failed one is running again")
            check(stub.state["B.GeocodeServer"] == "STOPPED",
                  "the failed service is reported, not imagined running")
            check(ops(stub, "start") == ["C.FeatureServer", "B.GeocodeServer", "A.MapServer"],
                  "starts run in reverse stop order and the failed one was still attempted")
            check(ops(stub, "stop") == three, "stops ran in the order given")
            check(ran == ["noop"], "the command ran once, after every service confirmed STOPPED")
            check(tokens(stub) == 4, "one stop token plus one fresh token per start")
            check(all(form.get("token") == [_StubAdmin.TOKEN] for form in stub.forms
                      if "username" not in form),
                  "every admin request carried the token in its POST body")
            logins = [form for form in stub.forms if "username" in form]
            check(all(form.get("expiration") == [str(TOKEN_EXPIRATION)]
                      and form.get("client") == ["requestip"] and form.get("f") == ["json"]
                      for form in logins) and TOKEN_EXPIRATION == 60,
                  "every login asks for a 60 minute token tied to the caller's IP")

            # ---- group N: a stop that never lands halts the stop phase --
            ran = []
            stub = _StubAdmin(three, never_stops=["B.GeocodeServer"])
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 10)
            finally:
                stub.close()
            check(ran == [], "the command never runs when one of three never confirms STOPPED"
                  "  <-- pinned defect")
            check(code == 1, "and the run exits 1, refused, with every start good")
            check("C.FeatureServer" not in [name for (_op, name) in stub.ops],
                  "the stop phase halts at the failure: the third service is never touched"
                  "  <-- pinned defect")
            check(ops(stub, "start") == ["B.GeocodeServer", "A.MapServer"],
                  "the services the stop phase reached are started again, newest first")
            check(stub.state["A.MapServer"] == "STARTED"
                  and stub.state["C.FeatureServer"] == "STARTED",
                  "every service ends the run started")

            # ---- group O: a token failure costs one start only ----------
            token_calls = [0]

            def third_token_fails(url, data, insecure=False, http_timeout=None):
                if url.endswith("/generateToken"):
                    token_calls[0] += 1
                    if token_calls[0] == 3:
                        raise TransientError("simulated connection reset")
                return real_http(url, data, insecure, http_timeout)

            stub = _StubAdmin(three)
            try:
                install(http=third_token_fails, run=lambda cmd: 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 1, 20)
            finally:
                stub.close()
            check(code == 3, "a token failure while starting one service exits 3")
            check(ops(stub, "start") == ["C.FeatureServer", "A.MapServer"],
                  "the start after the token failure still happened  <-- pinned defect")
            check(stub.state["B.GeocodeServer"] == "STOPPED"
                  and stub.state["A.MapServer"] == "STARTED",
                  "only the service whose token failed is left down")

            stub = _StubAdmin(["A.MapServer"])
            try:
                install(run=lambda cmd: 0)
                code = guard(stub.url, ["A.MapServer"], "noop", "u", "p", False, 3, 20)
            finally:
                stub.close()
            check(code == 0 and stub.ops == [("stop", "A.MapServer"), ("status", "A.MapServer"),
                                             ("start", "A.MapServer"), ("status", "A.MapServer")],
                  "one --service still runs stop, poll, start, poll and nothing else")

            # The final line counts the configured services, not the ones reached.
            collected = _Collect()
            log.addHandler(collected)
            stub = _StubAdmin(three, fail_start=["A.MapServer"], never_stops=["A.MapServer"])
            try:
                install(run=lambda cmd: 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 10)
            finally:
                stub.close()
                log.removeHandler(collected)
            check(code == 3 and "1 of 3 service(s) are NOT running: A.MapServer"
                  in collected.lines,
                  "a stop that failed on the first of three still reports 1 of 3  <-- pinned"
                  " defect")

            # ---- group P: the CLI end to end over real HTTP -------------
            marker = os.path.join(work, "command-ran")
            make_marker = '"%s" -c "open(r\'%s\', \'w\').close()"' % (sys.executable, marker)
            exit_five = '"%s" -c "import sys; sys.exit(5)"' % sys.executable
            os.environ["ARCGIS_SERVER_USER"] = "stub-admin"
            os.environ["ARCGIS_SERVER_PASSWORD"] = "stub-pass-n3m4"
            module.LOG_DIR = os.path.join(work, "logs-a")
            stub = _StubAdmin(three, fail_start=["B.GeocodeServer"])
            argv = ["--server-url", stub.url, "--command", make_marker, "--apply", "--insecure",
                    "--timeout", "20"]
            for name in three:
                argv += ["--service", name]
            try:
                install()
                code, out, err = _quiet(main, argv)
            finally:
                stub.close()
                hush()
            check(code == 3, "main exits 3 when one of three starts fails")
            check(os.path.exists(marker), "the real maintenance command ran through the shell")
            check(stub.state["A.MapServer"] == "STARTED"
                  and stub.state["C.FeatureServer"] == "STARTED",
                  "and the other two services are running again")
            check("RESTART FAILED, B.GeocodeServer" in err, "stderr names the failed start")
            logs = glob.glob(os.path.join(module.LOG_DIR, "svcguard_*.log"))
            with open(logs[0], "r", encoding="utf-8") as handle:
                contents = handle.read()
            check("1 of 3 service(s) are NOT running: B.GeocodeServer" in contents,
                  "the log ends with the one service still down, out of how many")
            check(_StubAdmin.TOKEN not in contents and "stub-pass-n3m4" not in contents,
                  "neither the token nor the password reaches the log file")
            check("TLS verification is DISABLED" in contents, "the log records --insecure")

            module.LOG_DIR = os.path.join(work, "logs-b")
            stub = _StubAdmin(three[:2])
            try:
                install()
                code, out, err = _quiet(main, ["--server-url", stub.url, "--command", exit_five,
                                               "--service", three[0], "--service", three[1],
                                               "--apply"])
            finally:
                stub.close()
                hush()
            check(code == 2, "a real command that exits 5 gives exit 2")
            check(sorted(stub.state.values()) == ["STARTED", "STARTED"],
                  "and both services are started again")
            check("TLS verification is DISABLED" not in err, "without --insecure, no TLS warning")

            # ---- group Q: CLI validation for several services -----------
            install(http=_explode, run=_explode)
            check(build_parser().parse_args(["--service", "B.MapServer", "--service",
                                             "A.MapServer"]).service
                  == ["B.MapServer", "A.MapServer"],
                  "--service repeats and keeps the order given")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A.MapServer", "--service", "B.MapServer"])
            check(code == 0 and "stop     : A.MapServer -> B.MapServer" in out,
                  "the dry run prints the stop order and makes zero HTTP calls")
            check("start    : B.MapServer -> A.MapServer" in out,
                  "the dry run prints the start order, reversed")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A.MapServer", "--service", "A.MapServer"])
            check(code == 64 and "more than once: A.MapServer" in err,
                  "a service named twice is a usage error")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A.MapServer", "--service", "NoDot"])
            check(code == 64, "every --service must be NAME.TYPE, not only the first")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A.MapServer", "--retries", "0"])
            check(code == 64, "--retries 0 is a usage error")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A.MapServer", "--timeout", "0"])
            check(code == 64, "--timeout 0 is a usage error")
            for bad, text in (("gis.example.org/server", "must start with https://"),
                              ("http://", "must start with https://"),
                              ("http://[bad", "must start with https://"),
                              ("https://admin:pw@s.example.org/x", "must not carry credentials")):
                code, out, err = _quiet(main, ["--server-url", bad, "--command", "c",
                                               "--service", "A.MapServer", "--apply"])
                check(code == 64 and text in err,
                      "--server-url %r is a usage error before anything is sent  <-- pinned"
                      " defect" % bad)
            check("admin:pw" not in err, "a URL with credentials is not echoed back")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                           "--service", "A B.MapServer", "--apply"])
            check(code == 64 and "NAME.TYPE" in err,
                  "a service name with a space is a usage error  <-- pinned defect")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                           "--command", "   ", "--service", "A.MapServer"])
            check(code == 64 and "--command" in err, "a blank --command is a usage error")
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                           "--service", "A.MapServer"])
            check(code == 64 and "--command" in err, "an absent --command is a usage error")
            raises(lambda: _quiet(build_parser().parse_args, ["--appl"]), SystemExit,
                   "the longest prefix --appl of --apply is refused too  <-- pinned defect",
                   "64")
            del os.environ["ARCGIS_SERVER_USER"]
            module.input = lambda prompt: "   "
            try:
                code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x", "--command", "c",
                                               "--service", "A.MapServer", "--apply"])
            finally:
                del module.input
            check(code == 64 and "username and password are required" in err,
                  "--apply with a blank typed user is a usage error, before any HTTP")

            # ---- group R: credentials, transport and helper edges -------
            check(_resolve_user("flag-user") == "flag-user", "--user wins over everything")
            os.environ["ARCGIS_SERVER_USER"] = "env-user"
            check(_resolve_user(None) == "env-user", "the environment user is next")
            del os.environ["ARCGIS_SERVER_USER"]
            module.input = lambda prompt: "  typed-user  "
            try:
                check(_resolve_user(None) == "typed-user", "the prompt is last, and trimmed")
            finally:
                del module.input
            check(_resolve_password() == "stub-pass-n3m4", "the environment password wins")
            del os.environ["ARCGIS_SERVER_PASSWORD"]
            getpass.getpass = lambda prompt: "typed-secret"
            check(_resolve_password() == "typed-secret", "else getpass prompts for it")

            def no_terminal(prompt):
                raise EOFError("EOF when reading a line")

            getpass.getpass = no_terminal
            module.input = no_terminal
            try:
                check(_resolve_user(None) == "" and _resolve_password() == "",
                      "with no terminal, a prompt reads as a blank answer, not a crash")
                code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                               "--command", "c", "--service", "A.MapServer",
                                               "--apply"])
            finally:
                del module.input
            check(code == 64 and "username and password are required" in err,
                  "--apply with no credentials and no terminal exits 64, not a traceback"
                  "  <-- pinned defect")

            blocker = os.path.join(work, "not-a-directory")
            open(blocker, "w").close()
            module.LOG_DIR = os.path.join(blocker, "logs")
            os.environ["ARCGIS_SERVER_USER"] = "u"
            os.environ["ARCGIS_SERVER_PASSWORD"] = "p"
            install(http=_explode, run=_explode)
            code, out, err = _quiet(main, ["--server-url", "https://s.example.org/x",
                                           "--command", "c", "--service", "A.MapServer",
                                           "--apply"])
            check(code == 64 and "cannot write the log directory" in err,
                  "an unwritable log directory exits 64 before any service is touched"
                  "  <-- pinned defect")
            install(run=lambda cmd: 0)
            del os.environ["ARCGIS_SERVER_USER"], os.environ["ARCGIS_SERVER_PASSWORD"]
            module.LOG_DIR = os.path.join(work, "logs-c")
            os.environ["ARCGIS_SERVER_USER"] = "u"
            os.environ["ARCGIS_SERVER_PASSWORD"] = "p"
            stub = _StubAdmin(["A.MapServer"])
            try:
                code, out, err = _quiet(main, ["--server-url", stub.url, "--command", "c",
                                               "--service", "A.MapServer", "--apply"])
            finally:
                stub.close()
                hush()
            logs = glob.glob(os.path.join(module.LOG_DIR, "svcguard_*.log"))
            with open(logs[0], "r", encoding="utf-8") as handle:
                contents = handle.read()
            check("clear text" in contents,
                  "an http:// --server-url is warned about in the log")
            del os.environ["ARCGIS_SERVER_USER"], os.environ["ARCGIS_SERVER_PASSWORD"]

            stub = _StubAdmin([])
            try:
                raises(lambda: real_http(stub.url + "/proxy", {}), TransientError,
                       "an HTML page with HTTP 200 is a transient fault, not success", "non-JSON")
            finally:
                stub.close()
            import socket
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
            closed.close()
            raises(lambda: real_http("http://127.0.0.1:%d/x" % port, {}), TransientError,
                   "a refused connection is a transient fault")
            # An unreachable server: nothing was stopped, so nothing is started, and the
            # run is a refusal (1), not an outage (3). https, so no clear-text warning.
            os.environ["ARCGIS_SERVER_USER"] = "u"
            os.environ["ARCGIS_SERVER_PASSWORD"] = "p"
            module.LOG_DIR = os.path.join(work, "logs-d")
            install(run=_explode)
            try:
                code, out, err = _quiet(main, ["--server-url", "https://127.0.0.1:%d/x" % port,
                                               "--command", "c", "--service", "A.MapServer",
                                               "--apply", "--retries", "1"])
            finally:
                hush()
            logs = glob.glob(os.path.join(module.LOG_DIR, "svcguard_*.log"))
            with open(logs[0], "r", encoding="utf-8") as handle:
                contents = handle.read()
            check(code == 1 and "RESTART FAILED" not in contents,
                  "an unreachable server exits 1 and starts nothing  <-- pinned defect")
            check("clear text" not in contents, "an https:// --server-url draws no clear-text warning")
            del os.environ["ARCGIS_SERVER_USER"], os.environ["ARCGIS_SERVER_PASSWORD"]

            stub = _StubAdmin(["A.MapServer"], cut_status=1)
            try:
                check(get_service_state(stub.url, "A.MapServer", "t", False, 3) == "STARTED",
                      "a reply cut short is retried, not read as a failed start  <-- pinned"
                      " defect")
                check(ops(stub, "status") == ["A.MapServer", "A.MapServer"],
                      "the cut reply cost exactly one retry")
                check(real_http(stub.url + "/bom", {}) == {"status": "success"},
                      "a reply that starts with a byte order mark is read as JSON")
            finally:
                stub.close()
            raises(lambda: real_http("gis.example.org/x", {}), SvcGuardError,
                   "a URL with no scheme is a refusal, not a retry  <-- pinned defect",
                   "unknown url type")
            install(http=_FakeServer(token_body={"expires": 1}))
            raises(lambda: get_token("b", "u", "p", False, 1), SvcGuardError,
                   "a token answer without a token is refused", "no token")
            install(http=_FakeServer(stop_body={"messages": ["queued"]}))
            raises(lambda: manage_service("b", "S.MapServer", "t", "stop", False, 1),
                   SvcGuardError, "a stop answer with no status is not taken as success",
                   "unexpected body")
            raises(lambda: _FakeServer()("https://s.example.org/x/other", {}), AssertionError,
                   "the fake server refuses a URL it does not know")
            raises(_explode, AssertionError, "_explode refuses any call")

            stuck = os.path.join(work, "svcguard_stuck.log")
            os.mkdir(stuck)
            check(_prune_logs(work, 0) == [] and os.path.isdir(stuck),
                  "a log entry that cannot be deleted is skipped, not fatal")

            # ---- group T: node names ------------------------------------
            check("--node" in flags and "--list-file" in flags and "--allow-shrink" in flags,
                  "the node and saved-list flags exist")
            check(node_drift(["a.example.org"], ["a.example.org"]) == ([], []),
                  "a node the site lists, and nothing else, leaves no drift")
            check(node_drift(["GIS-A"], ["gis-a"]) == ([], []),
                  "node names compare case-insensitively")
            check(node_drift(["gis-typo"], ["gis-a", "gis-b"]) == (["gis-typo"],
                                                                  ["gis-a", "gis-b"]),
                  "a node the site does not list is unknown, and every site machine is unnamed")
            check(node_drift(["gis-a"], ["gis-a", "gis-b"]) == ([], ["gis-b"]),
                  "a site machine --node leaves out is reported as unnamed")
            check(validate_nodes(["gis-a", "gis-b.example.org", "n_1"]) is None,
                  "plain machine names pass")
            check(validate_nodes([""]) is not None and validate_nodes(["a b"]) is not None
                  and validate_nodes(["a/b"]) is not None,
                  "an empty node, a space and a slash are usage errors")
            check("more than once: gis-a" in validate_nodes(["gis-a", "GIS-A"]),
                  "a node named twice, in any case, is a usage error")

            for body, what in (({"machines": "gis-a"}, "a machines value that is not a list"),
                               ({"hosts": []}, "a body with no machines key"),
                               ({"machines": [{"adminURL": "u"}]}, "a machine with no name"),
                               ({"machines": [{"machineName": ""}]}, "a machine with a blank name"),
                               ({"machines": ["gis-a"]}, "a machine that is not an object")):
                def machines_fake(url, data, insecure=False, http_timeout=None, method="POST",
                                  body=body):
                    return body
                install(http=machines_fake)
                raises(lambda: get_machines("https://s.example.org/x", "t", False, 1),
                       SvcGuardError, "get_machines refuses %s" % what, "unexpected body")
            install(http=lambda url, data, insecure=False, http_timeout=None, method="POST":
                    {"error": {"code": 498, "message": "Invalid token"}})
            raises(lambda: get_machines("https://s.example.org/x", "t", False, 1),
                   SvcGuardError, "get_machines refuses an error body", "498")

            stub = _StubAdmin(three, machines=["gis-a.example.org", "gis-b.example.org"])
            try:
                install()
                check(get_machines(stub.url, "TOK-1", False, 1)
                      == ["gis-a.example.org", "gis-b.example.org"],
                      "get_machines reads the machine names over real HTTP")
                check(len(stub.gets) == 1 and stub.forms == [],
                      "the machines resource is read with one GET and no POST body")
                check(stub.get_forms[0] == {"token": ["TOK-1"], "f": ["json"]},
                      "the GET carries the token and f=json in its query")
                check(stub.gets[0].startswith("/x/admin/machines?"),
                      "the machines request goes to /admin/machines")
                check(real_http(stub.url + "/admin/machines", {"token": "TOK-1"})["error"]["code"]
                      == 405, "the stub, like the documented resource, refuses a POST to machines")
            finally:
                stub.close()

            secret = "T0K/EN+x=9"
            check("T0K" not in _scrub("a T0K/EN+x=9 b T0K%2FEN%2Bx%3D9 c", {"token": secret})
                  and _scrub("keep", {"token": secret, "f": "json"}) == "keep",
                  "_scrub masks a token in its raw and its form-encoded spelling")
            check(_scrub("json", {"f": "json"}) == "json" and _scrub("x", {"token": ""}) == "x",
                  "_scrub leaves a form with no secret alone")
            install()
            exc = raises(lambda: real_http("http://127.0.0.1:9/a b", {"token": secret, "f": "json"},
                                           method="GET"),
                         SvcGuardError, "a GET to a URL http.client rejects is a refusal",
                         "bad request URL")
            check(exc is not None and "T0K" not in str(exc),
                  "that refusal does not carry the token from the query  <-- pinned defect")

            # The guard over real HTTP: a matching node runs, a typo refuses before any stop.
            names = ["gis-a.example.org", "gis-b.example.org"]
            ran = []
            collected = _Collect()
            log.addHandler(collected)
            log.setLevel(logging.WARNING)
            stub = _StubAdmin(three, machines=names)
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20, names)
            finally:
                stub.close()
                log.removeHandler(collected)
                log.setLevel(logging.CRITICAL)
            check(code == 0 and ran == ["noop"], "every node named and listed: the run goes ahead")
            check(len(stub.gets) == 1 and ops(stub, "stop") == three,
                  "the machine list is read once, before the stops")
            check(not [line for line in collected.lines if "does not name" in line],
                  "no warning when --node names every machine")

            ran = []
            collected = _Collect()
            log.addHandler(collected)
            log.setLevel(logging.WARNING)
            stub = _StubAdmin(three, machines=names)
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20, [names[0]])
            finally:
                stub.close()
                log.removeHandler(collected)
                log.setLevel(logging.CRITICAL)
            check(code == 0 and ran == ["noop"],
                  "a machine --node leaves out does not refuse the run")
            check([line for line in collected.lines if "does not name: gis-b.example.org" in line],
                  "it is warned about instead, by name")

            ran = []
            collected = _Collect()
            log.addHandler(collected)
            log.setLevel(logging.WARNING)
            stub = _StubAdmin(three, machines=names)
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20, ["gis-typo"])
            finally:
                stub.close()
                log.removeHandler(collected)
                log.setLevel(logging.CRITICAL)
            check(code == 1 and ran == [],
                  "a mistyped --node refuses the run instead of using every node"
                  "  <-- pinned defect")
            check(stub.ops == [], "a mistyped --node sends no stop, no start and no status poll"
                  "  <-- pinned defect")
            check(tokens(stub) == 1, "a refused node asks for no restart token")
            check([line for line in collected.lines if "--node not in this site: gis-typo. "
                   "The site lists: gis-a.example.org, gis-b.example.org" in line],
                  "the refusal names the typo and lists the site's machines")

            ran = []
            stub = _StubAdmin(three, machines_reply={"machines": "broken"})
            try:
                install(run=lambda cmd: ran.append(cmd) or 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20, names)
            finally:
                stub.close()
            check(code == 1 and ran == [] and stub.ops == [],
                  "a machine list svcguard cannot read refuses the run, it does not guess"
                  "  <-- pinned defect")

            stub = _StubAdmin(three, machines=[])
            try:
                install(run=lambda cmd: 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20, names)
            finally:
                stub.close()
            check(code == 1 and stub.ops == [], "a site that lists no machines refuses every node")

            stub = _StubAdmin(three, machines=names)
            try:
                install(run=lambda cmd: 0)
                code = guard(stub.url, three, "noop", "u", "p", False, 3, 20)
            finally:
                stub.close()
            check(code == 0 and stub.gets == [],
                  "without --node no machine call is made, as before")

            # ---- group U: the saved service list -------------------------
            ab = ["A.MapServer", "B.GeocodeServer"]
            check(list_drift(ab, ab) == ([], []), "an unchanged list has no drift")
            check(list_drift(ab, ["b.geocodeserver", "a.mapserver"]) == ([], []),
                  "a reordered or retyped-case list is not a change")
            check(list_drift(three, ab) == (["C.FeatureServer"], []),
                  "a service that dropped out is reported as removed")
            check(list_drift(ab, three) == ([], ["C.FeatureServer"]),
                  "a service that joined is reported as added")
            check(list_verdict(None, ab, False)[0] == "first", "no saved list is a first run")
            check(list_verdict(ab, ab, False)[0] == "same", "the same list is same")
            check(list_verdict(ab, three, False)[0] == "grew", "a longer list grew")
            verdict, note = list_verdict(three, ab, False)
            check(verdict == "shrunk" and "C.FeatureServer" in note and "--allow-shrink" in note,
                  "a shorter list is refused and the note names the dropped service"
                  "  <-- pinned defect")
            check(list_verdict(three, ["A.MapServer", "B.GeocodeServer", "D.MapServer"],
                               False)[0] == "shrunk",
                  "a swap is a shrink: one service added does not hide one dropped"
                  "  <-- pinned defect")
            check(list_verdict(three, ab, True)[0] == "allowed",
                  "--allow-shrink turns the refusal into an accepted shrink")

            lst = os.path.join(work, "list.json")
            check(read_list(lst) is None, "a list file that does not exist reads as no list")
            write_list(lst, three)
            check(read_list(lst) == three and not os.path.exists(lst + ".tmp"),
                  "write_list round-trips and leaves no temp file")
            with open(lst, "w", encoding="utf-8-sig") as handle:
                json.dump({"services": ab}, handle)
            check(read_list(lst) == ab, "a list saved by an editor with a byte order mark reads")
            for text, what in (("{not json", "invalid JSON"), ("[]", "a bare list"),
                               ('{"services": []}', "an empty service list"),
                               ('{"services": "A.MapServer"}', "a services value that is a string"),
                               ('{"services": ["NoDot"]}', "a name with no type"),
                               ('{"services": ["A B.MapServer"]}', "a name with a space"),
                               ('{"services": [7]}', "a name that is not a string")):
                with open(lst, "w", encoding="utf-8") as handle:
                    handle.write(text)
                raises(lambda: read_list(lst), SvcGuardError,
                       "read_list refuses %s, it never reads it as no list  <-- pinned defect"
                       % what, "Fix it or delete it")
            raises(lambda: read_list(work), SvcGuardError,
                   "read_list refuses a path that is a directory", "cannot be read")
            raises(lambda: write_list(os.path.join(work, "no-such-dir", "list.json"), ab), OSError,
                   "write_list reports an unwritable path")

            # The CLI: dry run, then real runs over HTTP against the stub.
            os.environ["ARCGIS_SERVER_USER"] = "u"
            os.environ["ARCGIS_SERVER_PASSWORD"] = "stub-pass-t5"
            lst = os.path.join(work, "saved.json")
            base = ["--server-url", "https://s.example.org/x", "--command", "c"]
            install(http=_explode, run=_explode)
            code, out, err = _quiet(main, base + ["--service", "A.MapServer", "--list-file", lst,
                                                  "--node", "gis-a", "--node", "gis-b"])
            check(code == 0 and "list     : no saved list yet" in out
                  and "nodes    : gis-a, gis-b" in out,
                  "the dry run shows the node and list lines and makes zero HTTP calls")
            check(not os.path.exists(lst), "a dry run never writes the saved list  <-- pinned defect")
            for argv, text, what in (
                    (["--service", "A.MapServer", "--node", "a b"], "--node must be", "a bad --node"),
                    (["--service", "A.MapServer", "--node", ""], "--node must be", "an empty --node"),
                    (["--service", "A.MapServer", "--node", "a", "--node", "A"], "more than once",
                     "a repeated --node"),
                    (["--service", "A.MapServer", "--allow-shrink"], "needs --list-file",
                     "--allow-shrink with no --list-file")):
                code, out, err = _quiet(main, base + argv)
                check(code == 64 and text in err, "%s is a usage error" % what)

            def marker_command(name):
                path = os.path.join(work, "ran-" + name)
                return path, '"%s" -c "open(r\'%s\', \'w\').close()"' % (sys.executable, path)

            def cli_run(stub_services, services, extra, name, machines=()):
                """One real --apply run against a stub. Returns (code, err, ran, stub)."""
                path, command = marker_command(name)
                module.LOG_DIR = os.path.join(work, "logs-" + name)
                stub = _StubAdmin(stub_services, machines=machines)
                argv = ["--server-url", stub.url, "--command", command, "--apply",
                        "--timeout", "20"] + extra
                for service in services:
                    argv += ["--service", service]
                try:
                    install()
                    code, out, err = _quiet(main, argv)
                finally:
                    stub.close()
                    hush()
                return code, err, os.path.exists(path), stub

            code, err, ran_it, stub = cli_run(three, three, ["--list-file", lst], "first")
            check(code == 0 and ran_it and read_list(lst) == three,
                  "a first --apply run saves the list it guarded")
            logs = glob.glob(os.path.join(work, "logs-first", "svcguard_*.log"))
            with open(logs[0], "r", encoding="utf-8") as handle:
                contents = handle.read()
            check("saved list: no saved list yet" in contents, "the log records the list decision")
            before = os.path.getmtime(lst)
            code, err, ran_it, stub = cli_run(three, three, ["--list-file", lst], "same")
            check(code == 0 and ran_it and os.path.getmtime(lst) == before,
                  "a run with the same list does not rewrite the file")

            code, err, ran_it, stub = cli_run(three, three[:2], ["--list-file", lst], "shrunk")
            check(code == 1 and not ran_it and stub.ops == [] and stub.forms == [],
                  "a shorter list refuses the run before the server or the command is touched"
                  "  <-- pinned defect")
            check("REFUSED: the saved list shrank: C.FeatureServer" in err,
                  "stderr names the service the list lost")
            check(read_list(lst) == three,
                  "a refused shrink leaves the saved list as it was  <-- pinned defect")
            code, out, err = _quiet(main, base + ["--service", three[0], "--service", three[1],
                                                  "--list-file", lst])
            check(code == 1 and "WOULD REFUSE" in out and "stop     :" in out,
                  "the dry run shows the plan, says it would refuse, and exits 1")
            code, err, ran_it, stub = cli_run(three, three[:2], ["--list-file", lst,
                                                                 "--allow-shrink"], "allowed")
            check(code == 0 and ran_it and read_list(lst) == three[:2],
                  "--allow-shrink runs, and the saved list becomes the shorter one")
            code, err, ran_it, stub = cli_run(three, three, ["--list-file", lst], "grew")
            check(code == 0 and ran_it and read_list(lst) == three,
                  "a longer list runs, and the saved list grows")

            with open(lst, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            code, err, ran_it, stub = cli_run(three, three, ["--list-file", lst], "broken")
            check(code == 64 and not ran_it and stub.ops == [] and "Fix it or delete it" in err,
                  "a damaged saved list is a usage error, not an empty list  <-- pinned defect")
            code, out, err = _quiet(main, base + ["--service", "A.MapServer", "--list-file", lst])
            check(code == 64, "the dry run refuses a damaged saved list too")
            os.remove(lst)
            code, err, ran_it, stub = cli_run(three, three, ["--list-file",
                                                             os.path.join(work, "no-dir", "l.json")],
                                              "nowrite")
            check(code == 64 and not ran_it and stub.ops == [] and "cannot write the saved list" in err,
                  "a saved list that cannot be written exits 64 before any service is touched"
                  "  <-- pinned defect")

            code, err, ran_it, stub = cli_run(three, three, ["--node", "gis-a.example.org"],
                                              "node-ok", machines=["gis-a.example.org"])
            check(code == 0 and ran_it and len(stub.gets) == 1,
                  "main --node runs when the site lists the node")
            logs = glob.glob(os.path.join(work, "logs-node-ok", "svcguard_*.log"))
            with open(logs[0], "r", encoding="utf-8") as handle:
                contents = handle.read()
            check(_StubAdmin.TOKEN not in contents and "stub-pass-t5" not in contents
                  and stub.get_forms[0]["token"] == [_StubAdmin.TOKEN],
                  "the token sent in the machines query never reaches the log file")
            code, err, ran_it, stub = cli_run(three, three, ["--node", "gis-typo"], "node-bad",
                                              machines=["gis-a.example.org"])
            check(code == 1 and not ran_it and stub.ops == [] and "--node not in this site" in err,
                  "main refuses a mistyped --node with exit 1 and starts nothing")
            del os.environ["ARCGIS_SERVER_USER"], os.environ["ARCGIS_SERVER_PASSWORD"]

            # ---- group S: the harness can fail ---------------------------
            scratch = _Checker()
            _quiet(scratch, False, "x")
            check(scratch.failed == 1, "check() records a failure  <-- pinned defect")
            for fn, exc_type, text, what in (
                    (lambda: None, ValueError, None, "a call that raised nothing"),
                    (lambda: int("x"), KeyError, None, "the wrong exception type"),
                    (lambda: int("x"), ValueError, "absent", "a message without the text")):
                scratch = _Checker()
                _quiet(_raises, scratch, fn, exc_type, "x", text)
                check(scratch.failed == 1, "raises() records %s" % what)
        finally:
            getpass.getpass = real_getpass
            module.LOG_DIR = real_log_dir
            os.environ.clear()
            os.environ.update(saved_env)
            _close_logging()
            shutil.rmtree(work, ignore_errors=True)

    finally:
        module._http_json = real_http
        module._run_command = real_run
        module.get_token = real_token
        module._now, module._sleep = real_now, real_sleep
        os.environ.clear()
        os.environ.update(original_env)
        _close_logging()
        log.propagate = True

    print("-" * 60)
    print("%d assertions, %d failed" % (check.total, check.failed))
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
