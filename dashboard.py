"""
dashboard.py -- render the Work Factor dashboard from one or more experiment databases.

    python dashboard.py runs/wf1.sqlite                       # -> runs/wf1.html
    python dashboard.py runs/wf1.sqlite -o out.html
    python dashboard.py runs/wf1_s0.sqlite runs/wf1_s1.sqlite  # shards of ONE run
    python dashboard.py runs/wf1.sqlite --serve 8765           # live: re-renders on every refresh

The page is a single self-contained HTML file: no build step, no CDN, no
tracking. The renderer lives in wf/report.py and reads only through the guarded
read API, so a page can never show data pooled across models, arms, or stages.
A synthetic (harness_test) database renders with a banner that says so.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from wf.report import ReportError, collect, render


def build_page(args, refresh: int | None) -> str:
    summary = collect(args.db, stage=args.stage, model_id=args.model, n_boot=args.boot)
    return render(summary, title=args.title, refresh=refresh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="+", help="experiment database(s); several only for shards of one run")
    ap.add_argument("-o", "--out", default=None, help="output HTML path (default: beside the first db)")
    ap.add_argument("--stage", default=None, choices=("screen", "confirm", "harness_test"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--title", default="Work Factor")
    ap.add_argument("--boot", type=int, default=600, help="bootstrap resamples per interval")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="serve the page on localhost:PORT, re-rendering on each request")
    ap.add_argument("--refresh", type=int, default=30, help="auto-refresh seconds when serving")
    args = ap.parse_args()

    for p in args.db:
        if not pathlib.Path(p).exists():
            raise SystemExit(f"no such database: {p}")

    if args.serve:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                try:
                    body = build_page(args, refresh=args.refresh).encode("utf-8")
                    code = 200
                except ReportError as e:
                    body = f"<pre>{e}</pre>".encode("utf-8")
                    code = 409
                except Exception as e:  # a crashed renderer should say so, not 500 silently
                    body = f"<pre>{type(e).__name__}: {e}</pre>".encode("utf-8")
                    code = 500
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *a):  # quiet
                return

        srv = HTTPServer(("127.0.0.1", args.serve), Handler)
        print(f"serving {', '.join(args.db)} at http://127.0.0.1:{args.serve}/  (Ctrl-C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    try:
        page = build_page(args, refresh=None)
    except ReportError as e:
        raise SystemExit(f"cannot render: {e}")
    out = pathlib.Path(args.out) if args.out else pathlib.Path(args.db[0]).with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
