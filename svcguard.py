#!/usr/bin/env python3
"""svcguard: run a maintenance command against a stopped ArcGIS Server service and
guarantee the service is started again afterwards, even when the command fails.

Stdlib only. Python 3.8+.

CONFIG PRECEDENCE (identical to the README):
    user      --user           >  env ARCGIS_SERVER_USER      >  interactive getpass prompt
    password  (no flag exists) >  env ARCGIS_SERVER_PASSWORD  >  interactive getpass prompt
    all other tunables:  CLI flag  >  CONFIGURATION constant below
The password is never accepted as a flag, never logged, and never written to disk.

EXIT CODES
    0   service stopped, command succeeded, service started
    1   service never reached STOPPED so the command was REFUSED; restart succeeded
    2   command failed (non-zero or raised); restart succeeded
    3   the service could not be started again. A human needs to look now
   64   usage error
"""

import argparse
import datetime
import getpass
import glob
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

__version__ = "1.0.0"

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


def _http_json(url, data, insecure=False, http_timeout=HTTP_TIMEOUT):
    """POST a form body, return the parsed JSON object. The one HTTP chokepoint."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "svcguard/%s" % __version__,
        },
    )
    try:
        opened = urllib.request.urlopen(
            request, timeout=http_timeout, context=_ssl_context(insecure)
        )
        try:
            raw = opened.read().decode("utf-8", "replace")
        finally:
            opened.close()
    except urllib.error.HTTPError as exc:
        # 5xx and 429 can clear on their own; 4xx (401 bad creds, 403, 404 wrong
        # service) is a permanent refusal. Retrying it only delays the page.
        if exc.code >= 500 or exc.code == 429:
            raise TransientError("HTTP %s from %s" % (exc.code, url))
        raise SvcGuardError("HTTP %s from %s (not retryable)" % (exc.code, url))
    except (urllib.error.URLError, OSError) as exc:
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


def _request(url, data, insecure, retries, what):
    """Retry transport failures with backoff. Never retry a server-side refusal."""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = _http_json(url, data, insecure, HTTP_TIMEOUT)
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

def guard(server_url, service, command, username, password, insecure, retries, timeout):
    """stop -> command -> ALWAYS start. Returns an exit code."""
    stopped = False
    command_rc = None
    restart_ok = False

    try:
        try:
            log.info("Requesting token for stop phase...")
            token = get_token(server_url, username, password, insecure, retries)
            log.info("Stopping %s ...", service)
            manage_service(server_url, service, token, "stop", insecure, retries)
            wait_for_state(server_url, service, token, "STOPPED", insecure, retries, timeout)
            stopped = True
            log.info("Service is STOPPED.")
        except Exception as exc:
            log.error("Stop phase failed: %s", exc)

        if stopped:
            try:
                command_rc = _run_command(command)
            except Exception as exc:
                log.error("Maintenance command could not run: %s", exc)
                command_rc = -1
        else:
            log.error(
                "REFUSING to run the maintenance command: the service is not confirmed "
                "STOPPED. Running it now would operate on live, in-use data."
            )
    finally:
        # Fresh token: the stop-phase token may have expired during a long command,
        # and it may never have existed if the stop phase's own token call threw.
        try:
            log.info("Requesting FRESH token for restart phase...")
            token = get_token(server_url, username, password, insecure, retries)
            log.info("Starting %s ...", service)
            manage_service(server_url, service, token, "start", insecure, retries)
            wait_for_state(server_url, service, token, "STARTED", insecure, retries, timeout)
            restart_ok = True
            log.info("Service is STARTED.")
        except Exception as exc:
            log.critical("RESTART FAILED, service may be down: %s", exc)

    if not restart_ok:
        return 3
    if not stopped:
        return 1
    if command_rc != 0:
        return 2
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="svcguard",
        description="Guarantee an ArcGIS Server service restarts around risky maintenance, "
                    "even on failure.",
        epilog="Password: env ARCGIS_SERVER_PASSWORD, else prompt. There is no --password flag.",
    )
    parser.add_argument("--server-url",
                        help="AGS admin REST base, e.g. https://gis.example.org/server")
    parser.add_argument("--service", help="NAME.TYPE, e.g. Parcels_Locator.GeocodeServer")
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
    parser.add_argument("--self-test", action="store_true",
                        help="run the offline assertion suite and exit")
    return parser


def _resolve_user(explicit):
    if explicit:
        return explicit
    from_env = os.environ.get("ARCGIS_SERVER_USER")
    if from_env:
        return from_env
    return input("ArcGIS Server admin user: ").strip()


def _resolve_password():
    from_env = os.environ.get("ARCGIS_SERVER_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass("ArcGIS Server admin password: ")


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    missing = [name for name in ("server_url", "service", "command")
               if not getattr(args, name)]
    if missing:
        sys.stderr.write("error: missing required option(s): %s\n"
                         % ", ".join("--" + m.replace("_", "-") for m in missing))
        return 64
    if "." not in args.service:
        sys.stderr.write("error: --service must be NAME.TYPE, e.g. Parcels_Locator.GeocodeServer\n")
        return 64
    if args.retries < 1 or args.timeout < 1:
        sys.stderr.write("error: --retries and --timeout must be >= 1\n")
        return 64

    if not args.apply:
        print("DRY RUN. No HTTP call was made. Re-run with --apply to execute.")
        print("  server   : %s" % args.server_url)
        print("  service  : %s" % args.service)
        print("  would    : stop -> run %r -> start (start guaranteed via finally)" % args.command)
        print("  tls      : %s" % ("VERIFICATION DISABLED (--insecure)" if args.insecure
                                   else "verified"))
        print("  timeout  : %ss   retries: %s   poll: %ss" % (args.timeout, args.retries,
                                                              POLL_INTERVAL))
        return 0

    username = _resolve_user(args.user)
    password = _resolve_password()
    if not username or not password:
        sys.stderr.write("error: a username and password are required for --apply\n")
        return 64

    log_path = _setup_logging(LOG_DIR)
    log.info("=" * 60)
    log.info("svcguard %s starting | service=%s | log=%s", __version__, args.service, log_path)
    if args.insecure:
        log.warning("TLS verification is DISABLED for this run (--insecure).")
    try:
        code = guard(args.server_url, args.service, args.command, username, password,
                     args.insecure, args.retries, args.timeout)
    finally:
        log.info("svcguard finished. Log: %s", log_path)
        _close_logging()
    return code


# ==========================================================================
# SELF-TEST. No sockets, no credentials, no arcpy. urllib is never reached.
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


def self_test():
    module = sys.modules[__name__]
    check = _Checker()
    real_http = _http_json
    real_run = _run_command
    real_token = get_token
    real_now, real_sleep = _now, _sleep
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

    def run_guard(fake, run=None, retries=3, timeout=120, clock=None):
        """Run guard() with counted get_token; returns (exit_code, token_call_count)."""
        counter = [0]

        def counting(*a, **k):
            counter[0] += 1
            return real_token(*a, **k)

        install(http=fake, run=run, clock=clock)
        module.get_token = counting
        try:
            code = guard("https://s.example.org/x", "Svc.MapServer", "noop",
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
        try:
            _check_ags({"status": "failed", "messages": ["nope"]}, "stop")
            check(False, "HTTP 200 + status=failed is a failure")
        except SvcGuardError as exc:
            check("status=failed" in str(exc), "HTTP 200 + status=failed is a failure")
            check("stop" in str(exc), "failure message names the operation")
        try:
            _check_ags({"error": {"code": 498, "message": "Invalid token"}}, "status")
            check(False, "HTTP 200 + error{code:498} is a failure")
        except SvcGuardError as exc:
            check("498" in str(exc), "HTTP 200 + error{code:498} is a failure")
        try:
            # AGS's real word for a failed admin operation is "error", not "failed".
            _check_ags({"status": "error", "messages": ["boom"]}, "start")
            check(False, "HTTP 200 + status=error is a failure  <-- pinned defect")
        except SvcGuardError as exc:
            check("status=error" in str(exc),
                  "HTTP 200 + status=error is a failure  <-- pinned defect")
        try:
            _check_ags({"status": "whoKnows"}, "w")
            check(False, "_check_ags whitelists success (any other status is a failure)")
        except SvcGuardError:
            check(True, "_check_ags whitelists success (any other status is a failure)")
        try:
            _check_ags("<html>proxy</html>", "w")
            check(False, "_check_ags rejects a non-dict body")
        except SvcGuardError:
            check(True, "_check_ags rejects a non-dict body")

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
        try:
            _request("u", {}, False, 3, "doomed")
            check(False, "retry raises when attempts are exhausted")
        except SvcGuardError as exc:
            check("after 3 attempt" in str(exc), "retry raises when attempts are exhausted")
        check(calls[0] == 3, "exhausted retry stopped at --retries attempts")

        calls[0] = 0

        def refused(url, data, insecure=False, http_timeout=None):
            calls[0] += 1
            return {"status": "failed", "messages": ["denied"]}

        install(http=refused)
        try:
            _request("u", {}, False, 3, "refused")
            check(False, "a server refusal raises")
        except SvcGuardError:
            check(True, "a server refusal raises")
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
                try:
                    _request("https://s.example.org/x", {}, False, 3, "perm")
                    check(False, "HTTP %d is not retried  <-- pinned defect" % code)
                except SvcGuardError as exc:
                    check("not retryable" in str(exc) and "attempt" not in str(exc),
                          "HTTP %d is a refusal, not a transient  <-- pinned defect" % code)
                check(opened[0] == 1, "HTTP %d made exactly 1 request" % code)

            for code in (429, 500, 503):
                opened[0] = 0
                urllib.request.urlopen = _raise_http(code)
                install()
                try:
                    _request("https://s.example.org/x", {}, False, 3, "temp")
                    check(False, "HTTP %d is retried as transient" % code)
                except SvcGuardError as exc:
                    check("after 3 attempt" in str(exc),
                          "HTTP %d is retried as transient" % code)
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
        try:
            wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 3, 20)
            check(False, "wait_for_state raises StateTimeout when the state never changes")
        except StateTimeout as exc:
            check("STOPPED" in str(exc),
                  "wait_for_state raises StateTimeout when the state never changes")
        check(fake.count("/status") >= 2, "the timeout path polled more than once")

        fake = _FakeServer(status_body={"error": {"code": 498, "message": "expired"}})
        install(http=fake)
        try:
            wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 1, 10)
            check(False, "a 498 during polling does not count as reaching the state")
        except StateTimeout:
            check(True, "a 498 during polling does not count as reaching the state")

        fake = _FakeServer(states=["UNKNOWN"])
        install(http=fake)
        try:
            wait_for_state("b", "S.MapServer", "t", "STOPPED", False, 1, 10)
            check(False, "UNKNOWN is not treated as STOPPED")
        except StateTimeout:
            check(True, "UNKNOWN is not treated as STOPPED")

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
        check(fake.count("/start") == 1, "restart is attempted even when the stop token threw")
        check(tokens == 2, "the restart phase still requests its own token")
        check(code == 1, "exit 1 when the command was refused but the restart worked")

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
        code, out, err = _quiet(main, ["--server-url", "u", "--service", "NoDot",
                                       "--command", "c"])
        check(code == 64, "--service must be NAME.TYPE")
        check(build_parser().parse_args([]).apply is False, "--apply defaults to OFF")
        check(build_parser().parse_args([]).insecure is False, "--insecure defaults to OFF")
        check(build_parser().parse_args([]).timeout == 120, "--timeout defaults to 120")
        check(build_parser().parse_args([]).retries == 3, "--retries defaults to 3")
        flags = set()
        for action in build_parser()._actions:
            flags.update(action.option_strings)
        check("--password" not in flags, "there is no --password flag")
        check("--apply" in flags and "--insecure" in flags, "the documented flags exist")
        check(len(flags) == 11, "exactly 9 flags plus -h/--help are defined")

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
        finally:
            _close_logging()
            shutil.rmtree(tmp, ignore_errors=True)

    finally:
        module._http_json = real_http
        module._run_command = real_run
        module.get_token = real_token
        module._now, module._sleep = real_now, real_sleep
        _close_logging()
        log.propagate = True

    print("-" * 60)
    print("%d assertions, %d failed" % (check.total, check.failed))
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
