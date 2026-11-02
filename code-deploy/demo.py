#!/usr/bin/env python3
"""Start the fake manager and fake Open WebUI for the code-deploy demo, on
fixed ports (4111 / 4112), with sample data.

Usage: python3 demo.py <path-to-write>

Writes the fake Open WebUI sqlite DB path to <path-to-write> so the service
(see demo.sh) can read the same database, then blocks until killed. The fake
manager accepts the bearer token "mgr-secret-token" (set FEATURES_TOKEN to
that in demo.sh).
"""

import os
import signal
import sys
import time

import fakes

TOKEN = "mgr-secret-token"
MGR_PORT = 4111
OW_PORT = 4112


def main():
    if len(sys.argv) < 2:
        print("usage: demo.py <path-to-write>", file=sys.stderr)
        return 2

    out_path = sys.argv[1]

    manager = fakes.FakeManager(TOKEN)
    manager.seed_demo()
    manager.start(MGR_PORT)

    owui = fakes.FakeOpenWebUI(key_file=None)
    owui.start(OW_PORT)

    with open(out_path, "w") as fh:
        # Write as a shell assignment so demo.sh can `source` it.
        fh.write("OWUI_DB=%s\n" % owui.db)

    sys.stderr.write(
        "[demo] fake manager on http://127.0.0.1:%d\n"
        "[demo] fake open webui on http://127.0.0.1:%d\n"
        "[demo] owui db: %s\n" % (MGR_PORT, OW_PORT, owui.db))
    sys.stderr.flush()

    def shutdown(*_):
        manager.stop()
        owui.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
