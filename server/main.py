"""web-muse: localhost bridge between a browser UI and `muse serve` (MSP v1).

Usage:
    python3 -m server.main [--port 8000] [--provider meta] [--model ID] ...
    python3 server/main.py [--port 8000]

Auth: the bridge spawns `muse serve` inheriting this process's environment,
so the user's existing subscription login (muse login) passes through
untouched. No API keys are accepted or stored by this wrapper.

Security: binds loopback only (127.0.0.1). Approval default stays onRequest;
the allowAll approval mode is selectable in the Session panel (with an
explicit warning) and runs every command without prompting.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.msp import MspClient  # noqa: E402
from server.sessions import SessionRouter, muse_sessions_base  # noqa: E402
from server.ws import HttpWsServer  # noqa: E402

LOG = logging.getLogger("web_muse")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="web-muse localhost bridge")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (loopback only is supported; the "
                         "bridge always listens on both 127.0.0.1 and ::1 "
                         "so tunnel origins using 'localhost' work "
                         "regardless of IPv4/IPv6 resolution)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", default=None,
                    help="serve provider: echo, meta, local (default: settings)")
    ap.add_argument("--model", default=None, help="model id override")
    ap.add_argument("--no-session-log", action="store_true",
                    help="memory-only sessions in the serve host")
    ap.add_argument("--trust-workspace",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="load each session workspace's skills and rules "
                         "(this is what lets GitHub-cloned sessions pick up "
                         "their seeded AGENTS.md; disable with "
                         "--no-trust-workspace)")
    ap.add_argument("--sandbox-network",
                    choices=("restricted", "enabled", "proxy-only"),
                    default=None,
                    help="serve sandbox network mode (host-lifetime, all "
                         "sessions; unset leaves the host default). "
                         "'enabled' lets in-session gh/git reach the "
                         "network, e.g. to push and open PRs.")
    ap.add_argument("--muse-bin", default="muse", help="muse binary path")
    ap.add_argument("--web-dir", default=str(ROOT / "web"))
    ap.add_argument("--allow-host", action="append", default=[],
                    metavar="NAME",
                    help="extra Host header to trust (repeatable; for "
                         "tunnel hostnames like a Cloudflare public "
                         "hostname). Also: WEB_MUSE_ALLOWED_HOSTS=a,b")
    ap.add_argument("--workspace-base", default=str(ROOT / "workspaces"),
                    help="base dir for per-session workspaces "
                         "(empty disables default workspaces)")
    ap.add_argument("--verbose", "-v", action="store_true")
    return ap.parse_args(argv)


# Loopback bind set: both families, so a tunnel origin pointing at
# "localhost" connects whether the tunnel resolves it to 127.0.0.1 or ::1.
# (cloudflared resolves localhost to ::1 first; binding IPv4-only made the
# tunnel fail with "dial tcp [::1]:8000: connect: connection refused" while
# direct port-forwarding to 127.0.0.1 worked.)
LOOPBACK_HOSTS = ("127.0.0.1", "::1")


class MultiLoopbackServer:
    """Composite over per-address loopback listeners (same surface as
    asyncio.Server for what amain needs: sockets/close/wait_closed)."""

    def __init__(self, servers):
        self._servers = list(servers)

    @property
    def sockets(self):
        return [s for srv in self._servers for s in (srv.sockets or [])]

    def close(self):
        for srv in self._servers:
            srv.close()

    async def wait_closed(self):
        for srv in self._servers:
            await srv.wait_closed()


async def start_loopback_server(handle, port):
    """Listen on every loopback address independently.

    One all-or-nothing bind would turn a single taken address (e.g. a stale
    bridge still holding 127.0.0.1:port) into a misleading total failure, so
    each family binds on its own: serve on whatever binds, warn about the
    rest, and raise the real error only when nothing binds at all.
    """
    servers = []
    errors = []
    for host in LOOPBACK_HOSTS:
        try:
            servers.append(await asyncio.start_server(handle, host, port))
        except OSError as e:
            errors.append(f"{host} ({e})")
    if not servers:
        raise OSError(f"no loopback address bindable on port {port}: "
                      + "; ".join(errors))
    for err in errors:
        LOG.warning("loopback bind partial, not serving %s", err)
    if len(servers) == 1:
        return servers[0]
    return MultiLoopbackServer(servers)


def extra_allowed_hosts(args):
    """--allow-host flags + WEB_MUSE_ALLOWED_HOSTS env, normalized."""
    from server.ws import normalize_allowed_hosts
    names = list(args.allow_host or [])
    env = os.environ.get("WEB_MUSE_ALLOWED_HOSTS", "")
    names += [n.strip() for n in env.split(",") if n.strip()]
    return normalize_allowed_hosts(names)


def build_serve_argv(args):
    argv = [args.muse_bin, "serve"]
    if args.provider:
        argv += ["--provider", args.provider]
    if args.model:
        argv += ["--model", args.model]
    if args.no_session_log:
        argv.append("--no-session-log")
    if args.trust_workspace:
        argv.append("--trust-workspace")
    if args.sandbox_network:
        argv += ["--sandbox-network", args.sandbox_network]
    return argv


async def amain(args):
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        LOG.error("refusing non-loopback bind %r (network wrapper = loopback only)",
                  args.host)
        return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.no_session_log:
        LOG.warning("--no-session-log: memory-only hosts emit no transcript "
                    "events (turns flip running->idle with no items); the UI "
                    "will show no streamed output")
    router_holder = {}

    def on_notification(method, params):
        router = router_holder.get("router")
        if router is not None:
            router.on_notification(method, params)

    def on_server_request(method, params):
        router = router_holder.get("router")
        if router is not None:
            router.on_server_request(method, params)

    msp = MspClient(build_serve_argv(args),
                    on_notification=on_notification,
                    on_server_request=on_server_request)
    ws_base = args.workspace_base or None
    if ws_base:
        LOG.info("session workspaces under %s", ws_base)
    router = SessionRouter(msp, workspace_base=ws_base,
                           sessions_base=str(muse_sessions_base()))
    router_holder["router"] = router

    def hello():
        return {"type": "hello",
                "server": msp.server_info,
                "schema": msp.schema,
                "mspAlive": msp.alive}

    # Start the serve host before accepting browsers so the first prompt
    # never waits on a cold spawn.
    await msp.start()

    extra_hosts = extra_allowed_hosts(args)
    if extra_hosts:
        LOG.info("trusting extra Host headers: %s", sorted(extra_hosts))
    srv = HttpWsServer(router, args.web_dir, on_ws_open=hello,
                       allowed_hosts=extra_hosts)
    server = await start_loopback_server(srv.handle, args.port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    LOG.info("web-muse listening on %s (web=%s)", addrs, args.web_dir)
    print(f"web-muse up: http://{args.host}:{args.port}/", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    LOG.info("shutting down")
    server.close()
    try:
        # Stray open browser tabs must not wedge shutdown: bound the drain.
        await asyncio.wait_for(server.wait_closed(), timeout=10)
    except asyncio.TimeoutError:
        LOG.warning("shutdown: %d connection(s) still open after 10s; "
                    "closing anyway", len(getattr(router, "_conns", ())))
    await msp.stop()
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
