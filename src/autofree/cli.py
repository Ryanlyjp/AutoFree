"""AutoFree CLI entry point — `autofree <subcmd>`.

Subcommands:
    api                   start the FastAPI web panel + HTTP API
    run [-R N -n N]       run a multi-round free-account batch (blocking)
    status                print master + storage summary
    push <email>          push a single saved auth.json to CPA
    push-all              push every un-pushed local auth.json
    import-token          read a session_token from stdin and verify
    set-account-id <id>   override the master workspace account_id
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _cmd_api(args: argparse.Namespace) -> int:
    """Boot uvicorn against autofree.api:app — api.py is in the next batch."""
    try:
        import uvicorn
    except ImportError:
        print("missing dependency: uvicorn. run `uv sync` first.", file=sys.stderr)
        return 2
    try:
        from autofree.api import app  # noqa: F401  (verifies module loads)
    except ImportError as exc:
        print(f"autofree.api 未实现 ({exc}) — 等下一批 api.py 写完再用 `autofree api`。", file=sys.stderr)
        return 2
    host, port = args.host, args.port
    print(f"AutoFree API listening on http://{host}:{port}", flush=True)
    uvicorn.run("autofree.api:app", host=host, port=port, reload=False, log_level="info")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from autofree import runner
    print(f"启动 run: rounds={args.rounds} per_round={args.per_round}")
    record = runner.run_blocking(args.rounds, args.per_round, mail_provider=args.mail_provider)
    print(f"\n=== 任务结束 status={record['status']} ===")
    summary = record.get("summary") or {}
    for k in ("registered", "oauthed", "kicked", "ok", "failed"):
        if k in summary:
            print(f"  {k}: {summary[k]}")
    if record.get("error"):
        print(f"  error: {record['error']}")
    return 0 if record.get("status") in ("done", "done_with_errors") else 1


def _cmd_status(args: argparse.Namespace) -> int:
    from autofree import admin_state, storage
    from autofree.settings import get_all

    print("=== AutoFree 状态 ===")
    print("\n[settings]")
    s = get_all()
    print(f"  proxy           : {s.get('proxy') or '(none)'}")
    print(f"  mail.provider   : {(s.get('mail') or {}).get('provider')}")
    cpa = s.get("cpa") or {}
    print(f"  cpa.base_url    : {cpa.get('base_url') or '(unset)'}")

    print("\n[admin]")
    a = admin_state.get_summary()
    for k, v in a.items():
        print(f"  {k:18s}: {v}")

    print("\n[auths]")
    auths = storage.list_auths()
    print(f"  total: {len(auths)}")
    for row in auths[:20]:
        pushed = row.get("pushed_to_cpa_at") or "(not pushed)"
        print(f"  {row['email']:40s} expired={row.get('expired')} pushed={pushed}")
    if len(auths) > 20:
        print(f"  ... +{len(auths) - 20} more")

    print("\n[runs] (latest 10)")
    for row in storage.list_runs(limit=10):
        print(
            f"  {row['id']} {row['status']:18s} R{row['current_round']}/{row['rounds']} "
            f"x{row['per_round']} created={row['created_at']}"
        )
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    from autofree import cpa_push
    res = cpa_push.push_one(args.email, overwrite=args.force)
    print(_fmt(res))
    return 0 if res.get("ok") or res.get("skipped") else 1


def _cmd_push_all(args: argparse.Namespace) -> int:
    from autofree import cpa_push, storage
    rows = storage.list_auths()
    todo = [r["email"] for r in rows if not r.get("pushed_to_cpa_at") or args.force]
    if not todo:
        print("nothing to push (use --force to re-push all)")
        return 0
    print(f"pushing {len(todo)} files...")
    res = cpa_push.push_many(todo, overwrite=args.force)
    print(_fmt(res))
    return 0 if not res.get("failed") else 1


def _cmd_import_token(args: argparse.Namespace) -> int:
    from autofree import master
    if args.token:
        token = args.token
    else:
        token = getpass.getpass("session_token (从浏览器 cookie 复制,留空取消): ").strip()
    if not token:
        print("cancelled")
        return 0
    res = master.import_session_token(token, account_id=args.account_id, email=args.email)
    print(_fmt(res))
    return 0 if res.get("ok") else 1


def _cmd_set_account(args: argparse.Namespace) -> int:
    from autofree import master
    res = master.set_account_id(args.account_id)
    print(_fmt(res))
    return 0 if res.get("ok") else 1


def _fmt(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)


# ============================================================ argparse


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autofree", description="ChatGPT Personal free-plan account producer")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    pa = sub.add_parser("api", help="start FastAPI web panel + HTTP API")
    pa.add_argument("--host", default="0.0.0.0")
    pa.add_argument("--port", type=int, default=8788)
    pa.set_defaults(func=_cmd_api)

    pr = sub.add_parser("run", help="run a multi-round free-account batch (blocking)")
    pr.add_argument("-R", "--rounds", type=int, default=1)
    pr.add_argument("-n", "--per-round", type=int, default=1)
    pr.add_argument("--mail-provider", default=None, help="override settings.mail.provider")
    pr.set_defaults(func=_cmd_run)

    ps = sub.add_parser("status", help="print master + storage summary")
    ps.set_defaults(func=_cmd_status)

    pp = sub.add_parser("push", help="push one local auth.json to CPA")
    pp.add_argument("email")
    pp.add_argument("--force", action="store_true", help="overwrite existing CPA file")
    pp.set_defaults(func=_cmd_push)

    pall = sub.add_parser("push-all", help="push every un-pushed local auth.json to CPA")
    pall.add_argument("--force", action="store_true")
    pall.set_defaults(func=_cmd_push_all)

    pi = sub.add_parser("import-token", help="import a master session_token (paste on stdin)")
    pi.add_argument("--token", default=None, help="(optional) pass directly; default = stdin prompt")
    pi.add_argument("--account-id", default=None)
    pi.add_argument("--email", default=None)
    pi.set_defaults(func=_cmd_import_token)

    psa = sub.add_parser("set-account-id", help="override the master workspace account_id")
    psa.add_argument("account_id")
    psa.set_defaults(func=_cmd_set_account)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
