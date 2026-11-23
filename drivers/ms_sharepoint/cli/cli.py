#!/usr/bin/env python3
"""SharePoint / OneDrive CLI over Microsoft Graph with the token of a signed-in Teams web tab.

Global options go before the command: --account <upn> (env SHAREPOINT_ACCOUNT), --cdp-url <url>
(env SHAREPOINT_CDP_URL), --json. Library selection (per command): --site <url|id> or --drive <id>; default = own OneDrive.
Targets: a web/sharing URL, a ref (f1..) from the last list/search, <driveId>:<itemId>, or a path (/Folder/file.xlsx).
Writes (upload, cell-update, link-create) only print a DRAFT unless --yes is given.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import extract  # noqa: E402
import files  # noqa: E402
import furl  # noqa: E402
import util  # noqa: E402


def cmd_fetch(a):
    print(json.dumps(furl.fetch(a.kind, json.loads(a.ident))))


def cmd_test(a):
    print(json.dumps(furl.run_test()))


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default=os.environ.get("SHAREPOINT_ACCOUNT"), help="UPN/email of the signed-in Teams account")
    p.add_argument("--json", action="store_true", help="JSON lines instead of table output")
    p.add_argument("--cdp-url", help="Browser CDP endpoint (env SHAREPOINT_CDP_URL, default http://localhost:9222)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, lib=False, top=None):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if lib:
            s.add_argument("--site", help="site URL (https://x.sharepoint.com/sites/Name) or site id; default own OneDrive")
            s.add_argument("--drive", help="document library id (b!...), from `drives`")
        if top:
            s.add_argument("--top", type=int, default=top)
        return s

    add("whoami", files.cmd_whoami, "show signed-in identity and file scopes")
    s = add("fetch", cmd_fetch, "furl card for a SharePoint link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=["file"])
    s.add_argument("--ident", required=True)
    add("test", cmd_test, "check CDP, Teams tab, token, Graph and OneDrive (JSON; exit 0 even when a step fails)")

    s = add("sites", files.cmd_sites, "search sites by name, or list followed sites", top=25)
    s.add_argument("query", nargs="?")
    s = add("drives", files.cmd_drives, "list document libraries of a site (default: own drives)", top=25)
    s.add_argument("--site")
    s = add("ls", files.cmd_ls, "list a folder (refs f1.. reusable in other commands)", lib=True, top=50)
    s.add_argument("target", nargs="?", default="/")
    s = add("search", files.cmd_search, "search files by name/content in a library", lib=True, top=25)
    s.add_argument("query")
    add("recent", files.cmd_recent, "recently used files", top=25)
    add("shared", files.cmd_shared, "files shared with me", top=25)
    s = add("info", files.cmd_info, "file/folder metadata", lib=True)
    s.add_argument("target")
    s = add("versions", files.cmd_versions, "version history", lib=True, top=25)
    s.add_argument("target")
    s = add("read", files.cmd_read, "file content as text (docx/pptx/xlsx/vtt/txt...), bounded", lib=True)
    s.add_argument("target")
    s.add_argument("--chars", type=int, default=6000, help="output limit (default 6000)")
    s.add_argument("--full", action="store_true", help="no output limit")
    s.add_argument("--sheets", action="store_true", help="xlsx: list sheets and used ranges")
    s.add_argument("--sheet", help="xlsx: sheet name (default first visible)")
    s.add_argument("--range", help="xlsx: A1:D20")
    s.add_argument("--max-rows", type=int, default=50)
    s.add_argument("--max-cols", type=int, default=20)
    s.add_argument("--timestamps", action="store_true", help="vtt: keep cue start times")
    s.add_argument("--max-bytes", type=int, default=api.MAX_DOWNLOAD)
    s = add("download", files.cmd_download, "save a file locally (default /tmp/sharepoint-dl)", lib=True)
    s.add_argument("target")
    s.add_argument("--out", help="output directory")
    s.add_argument("--max-bytes", type=int, default=api.MAX_DOWNLOAD)
    s = add("link", files.cmd_link, "web URL and existing sharing links", lib=True)
    s.add_argument("target")
    s = add("link-create", files.cmd_link_create, "create a sharing link (DRAFT unless --yes)", lib=True)
    s.add_argument("target")
    s.add_argument("--type", choices=["view", "edit"], default="view")
    s.add_argument("--scope", choices=["organization", "anonymous"], default="organization")
    s.add_argument("--allow-anonymous", action="store_true")
    s.add_argument("--yes", action="store_true")
    s = add("upload", files.cmd_upload, "upload a local file (DRAFT unless --yes)", lib=True)
    s.add_argument("local")
    s.add_argument("--to", required=True, help="remote path; ending with / keeps the local file name")
    s.add_argument("--replace", action="store_true", help="overwrite if it exists (old version is kept)")
    s.add_argument("--yes", action="store_true")
    s = add("cell-update", files.cmd_cell_update, "write a cell range of an xlsx (DRAFT with old/new unless --yes)", lib=True)
    s.add_argument("target")
    s.add_argument("--sheet", required=True)
    s.add_argument("--range", required=True)
    s.add_argument("--values", help="JSON 2D array matching the range")
    s.add_argument("--values-file")
    s.add_argument("--yes", action="store_true")
    return p


def main():
    a = build().parse_args()
    acct = a.account if a.account != "default" else None
    util.set_account(acct)
    try:
        api.configure(a.cdp_url, acct)
        a.fn(a)
    except (api.GraphError, extract.ExtractError) as e:
        util.die(api.redact(e))
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
