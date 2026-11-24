"""Command-line interface for V-Exporter.

`python v_exporter.py` still starts the GUI. Any argument is routed here:

    python v_exporter.py --list
    python v_exporter.py --scan --npv MyV
    python v_exporter.py --export --npv MyV --looks casual_01,outfit_01
    python v_exporter.py --export --npv MyV --no-proofs
    python v_exporter.py --detect --save
    python v_exporter.py --selftest

The CLI uses the same ``pipeline`` functions as the GUI. It never launches Tk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence

import pipeline as pl


class CliError(RuntimeError):
    """A user-facing CLI error (printed without a traceback)."""


# ---------------------------------------------------------------------------
# output / logging
# ---------------------------------------------------------------------------

def _echo(level: str, message: object, stream=None) -> None:
    prefix = {"ERROR": "[ERROR] ", "WARN": "[WARN] ", "STEP": "[STEP] "}.get(level, "")
    text = prefix + str(message)
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except UnicodeEncodeError:
        try:
            encoding = (stream or sys.stdout).encoding or "utf-8"
            print(str(text).encode(encoding, "replace").decode(encoding),
                  file=stream or sys.stdout, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _make_logger(args) -> pl.Logger:
    if args.quiet:
        def echo(level: str, message: object) -> None:
            if level in ("ERROR", "WARN"):
                _echo(level, message, sys.stderr)
    elif args.json:
        def echo(level: str, message: object) -> None:
            _echo(level, message, sys.stderr)
    else:
        def echo(level: str, message: object) -> None:
            _echo(level, message, sys.stdout)
    return pl.Logger(echo=echo)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v_exporter.py",
        description=(
            "Export Cyberpunk 2077 ACM NPV looks as GLB/FBX/OBJ. "
            "Run without arguments to open the GUI."
        ),
        epilog=(
            "Examples:\n"
            "  python v_exporter.py --list\n"
            "  python v_exporter.py --scan --npv MyV --force-uncook\n"
            "  python v_exporter.py --export --npv MyV --looks casual_01,outfit_01\n"
            "  python v_exporter.py --export --looks \"MyV=casual_01,outfit_01\" --looks \"OtherV=formal\"\n"
            "  python v_exporter.py --export --npv MyV --no-proofs\n"
            "  python v_exporter.py --detect --save\n"
            "  python v_exporter.py --selftest\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true",
                         help="list NPV folders and cached look names (no uncook)")
    actions.add_argument("--scan", action="store_true",
                         help="uncook selected NPV(s) and list their looks")
    actions.add_argument("--export", action="store_true",
                         help="export selected NPV(s) and looks")
    actions.add_argument("--detect", action="store_true",
                         help="auto-detect game/Blender/WolvenKit/NPV/output paths")
    actions.add_argument("--selftest", action="store_true",
                         help="validate settings and dependencies without exporting")
    actions.add_argument("--open-output", action="store_true",
                         help="open the configured output folder")

    parser.add_argument("--npv", action="append", metavar="NAME",
                        help="NPV name; repeat or use a comma-separated list")
    look_group = parser.add_mutually_exclusive_group()
    look_group.add_argument("--looks", action="append", metavar="LIST",
                            help="look names; repeat or use a comma-separated list. "
                                 "Use NPV=look1,look2 for per-NPV lists "
                                 "(default: all discovered looks)")
    look_group.add_argument("--all-looks", action="store_true",
                            help="export every discovered look (explicit form of the default)")

    parser.add_argument("--force-uncook", action="store_true",
                        help="ignore the uncook cache (scan/export)")
    proof_group = parser.add_mutually_exclusive_group()
    proof_group.add_argument("--proofs", dest="render_proofs", action="store_true",
                             help="render 1024x1024 proof images (default)")
    proof_group.add_argument("--no-proofs", dest="render_proofs", action="store_false",
                             help="skip the 1024x1024 proof render")
    parser.set_defaults(render_proofs=None)

    parser.add_argument("--wolvenkit-cli", metavar="PATH",
                        help="override wolvenkit_cli for this run")
    parser.add_argument("--blender", metavar="PATH",
                        help="override the Blender executable for this run")
    parser.add_argument("--game-root", metavar="PATH",
                        help="override the Cyberpunk 2077 game folder")
    parser.add_argument("--npv-root", metavar="PATH",
                        help="override the ACM NPV folder")
    parser.add_argument("--output-root", metavar="PATH",
                        help="override the export output folder")
    parser.add_argument("--save", action="store_true",
                        help="save path/proof overrides (or detected paths) to settings.json")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable JSON result on stdout; logs go to stderr")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress normal log lines (warnings/errors still print)")
    parser.add_argument("--verbose", action="store_true",
                        help="print tracebacks for per-NPV failures")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s - {pl.APP_TITLE}")
    return parser


# ---------------------------------------------------------------------------
# settings / selection helpers
# ---------------------------------------------------------------------------

def _is_file(value: str) -> bool:
    try:
        return bool(value) and Path(value).is_file()
    except OSError:
        return False


def _apply_overrides(settings: pl.Settings, args) -> pl.Settings:
    if args.wolvenkit_cli:
        settings.wolvenkit_cli = args.wolvenkit_cli
    if args.blender:
        settings.blender = args.blender
    if args.game_root:
        settings.game_root = args.game_root
    if args.npv_root:
        settings.npv_root = args.npv_root
    if args.output_root:
        settings.output_root = args.output_root
    if args.render_proofs is not None:
        settings.render_proofs = bool(args.render_proofs)
    return settings


def _autofill_missing(
    settings: pl.Settings,
    log: object,
    *,
    need_blender: bool,
    need_wolvenkit: bool,
) -> pl.Settings:
    """Fill invalid/blank paths the same way the GUI's Auto-detect does."""
    game = pl.normalize_game_root(settings.game_root)
    if game is None:
        found = pl.detect_game_root(log)
        if found is not None:
            settings.game_root = str(found)
            game = found
    else:
        settings.game_root = str(game)

    if not (settings.npv_root or "").strip():
        derived = pl.derive_npv_root(game) if game is not None else None
        try:
            if derived is not None and derived.is_dir():
                settings.npv_root = str(derived)
        except OSError:
            pass

    if not (settings.output_root or "").strip():
        settings.output_root = str(pl.default_output_root())

    if need_blender and not _is_file(settings.blender):
        found = pl.find_blender(log)
        if found is not None:
            settings.blender = str(found)

    if need_wolvenkit and not _is_file(settings.wolvenkit_cli):
        found = pl.find_wolvenkit(log)
        if found is not None:
            settings.wolvenkit_cli = str(found)

    return settings


def _split_names(values: Optional[Sequence[str]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        for part in str(value).split(","):
            name = part.strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    return names


def _parse_look_specs(
    values: Optional[Sequence[str]],
) -> tuple[list[str], dict[str, tuple[str, list[str]]]]:
    """Parse ``--looks`` into a global list plus ``NPV=look1,look2`` entries."""
    global_looks: list[str] = []
    per_npv: dict[str, tuple[str, list[str]]] = {}
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        if "=" in text:
            name, look_text = text.split("=", 1)
            name = name.strip()
            if not name:
                continue
            key = name.casefold()
            entry = per_npv.get(key)
            if entry is None:
                entry = (name, [])
                per_npv[key] = entry
            entry[1].extend(_split_names([look_text]))
        else:
            global_looks.extend(_split_names([text]))
    return global_looks, per_npv


def _npv_root(settings: pl.Settings, log: object) -> Path:
    root = pl.effective_npv_root(settings, log)
    if root is None:
        raise CliError(
            "The ACM NPV folder could not be located.\n"
            "Set --game-root or --npv-root, or run: python v_exporter.py --detect --save"
        )
    return root


def _select_npvs(
    settings: pl.Settings,
    names: Sequence[str],
    log: object,
    *,
    action: str,
) -> list[pl.NpvInfo]:
    root = _npv_root(settings, log)
    infos = pl.list_npvs(root, log)
    if names:
        by_name = {info.name.casefold(): info for info in infos}
        selected: list[pl.NpvInfo] = []
        missing: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            info = by_name.get(key)
            if info is None:
                missing.append(name)
            else:
                selected.append(info)
        if missing:
            available = ", ".join(info.name for info in infos) or "none"
            raise CliError(
                "NPV not found: " + ", ".join(missing) + f". Available: {available}"
            )
    else:
        if action == "list":
            return infos
        selected = list(infos)

    if action in ("scan", "export"):
        drafts = [info for info in selected if info.is_draft]
        if drafts:
            if names:
                raise CliError(
                    "Draft NPV(s) cannot be used yet:\n"
                    + "\n".join(f"  {info.name}: {pl.draft_message(info)}" for info in drafts)
                )
            for info in drafts:
                log.warn(f"[{info.name}] skipped: {pl.draft_message(info)}")
        selected = [info for info in selected if info.exportable]
        if not selected:
            raise CliError(
                f"No packed NPVs found under {root}.\n"
                "Create one in game with ACM's 'Create NPV', then run --list again."
            )
    return selected


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _cmd_list(args, settings: pl.Settings, log: object) -> int:
    root = _npv_root(settings, log)
    infos = _select_npvs(settings, _split_names(args.npv), log, action="list")
    rows: list[dict] = []
    for info in infos:
        cached = False
        looks: list[str] = []
        if info.exportable:
            scan = pl.cached_looks(info.name)
            if scan is not None:
                cached = True
                looks = list(scan.looks)
        elif info.is_draft:
            looks = list(info.draft_looks)
        rows.append({
            "name": info.name,
            "status": info.status,
            "directory": str(info.directory),
            "archives": [archive.name for archive in info.archives],
            "looks": looks,
            "looks_cached": cached,
            "draft_looks": list(info.draft_looks),
        })

    if args.json:
        _print_json({"npv_root": str(root), "npvs": rows})
        return 0

    print(f"NPV root: {root}")
    if not rows:
        print("  (no NPV folders found)")
        return 0
    for row in rows:
        if row["status"] == "draft":
            looks = ", ".join(row["draft_looks"]) or "none parsed"
            print(f"  {row['name']} [DRAFT] - looks saved: {looks}")
            continue
        archives = ", ".join(row["archives"]) or "no archive"
        if row["looks_cached"]:
            looks = ", ".join(row["looks"]) or "none parsed"
            print(f"  {row['name']} [packed] - {archives} - cached looks: {looks}")
        else:
            print(f"  {row['name']} [packed] - {archives} - no cached looks (run --scan)")
    return 0


def _cmd_scan(args, settings: pl.Settings, log: object) -> int:
    infos = _select_npvs(settings, _split_names(args.npv), log, action="scan")
    rows: list[dict] = []
    failures = 0
    for info in infos:
        try:
            scan = pl.scan_npv(info, settings, log, force=args.force_uncook)
            row = {
                "npv": info.name,
                "ok": scan.ok,
                "looks": list(scan.looks),
                "entity": str(scan.ent_json) if scan.ent_json else None,
                "raw_dir": str(scan.raw_dir) if scan.raw_dir else None,
                "candidates": [
                    {
                        "path": str(candidate.path),
                        "looks": list(candidate.looks),
                        "score": candidate.score,
                        "reason": candidate.reason,
                    }
                    for candidate in scan.candidates
                ],
                "notes": list(scan.notes),
            }
            if not scan.ok:
                failures += 1
                log.error(f"[{info.name}] no entity found; run --list / see the log.")
            rows.append(row)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.error(f"[{info.name}] scan failed: {exc}")
            if args.verbose:
                log.raw(traceback.format_exc())
            rows.append({"npv": info.name, "ok": False, "error": str(exc),
                         "looks": [], "candidates": [], "notes": []})

    if args.json:
        _print_json({"ok": failures == 0, "results": rows})
        return 0 if failures == 0 else 1

    for row in rows:
        if row["ok"]:
            looks = ", ".join(row["looks"]) or "no looks parsed"
            print(f"[{row['npv']}] {len(row['looks'])} look(s): {looks}")
            if row.get("entity"):
                print(f"  entity: {row['entity']}")
        else:
            print(f"[{row['npv']}] scan failed: {row.get('error', 'no entity found')}")
    return 0 if failures == 0 else 1


def _compact_summary(summary: dict) -> dict:
    looks: list[dict] = []
    for entry in summary.get("results") or []:
        report = entry.get("report") or {}
        for name, data in (report.get("looks") or {}).items():
            files = data.get("files") or {}
            looks.append({
                "look": name,
                "ok": bool(files),
                "files": files,
                "proof": data.get("proof"),
                "errors": list(data.get("errors") or []),
                "meshes": data.get("meshes"),
            })
    return {
        "npv": summary.get("npv"),
        "output_dir": summary.get("output_dir"),
        "requested_looks": list(summary.get("requested_looks") or []),
        "ok": bool(looks) and all(item["ok"] for item in looks),
        "looks": looks,
        "render_proofs": summary.get("render_proofs"),
    }


def _cmd_export(args, settings: pl.Settings, log: object) -> int:
    problems = pl.validate_settings(settings)
    if problems:
        raise CliError(
            "Export blocked: setup incomplete:\n"
            + "\n".join(f"  * {problem.replace(chr(10), ' ')}" for problem in problems)
            + "\n\nRun: python v_exporter.py --selftest"
        )

    npv_names = _split_names(args.npv)
    global_looks, per_npv = _parse_look_specs(args.looks)
    if not npv_names and per_npv:
        npv_names = [display for display, _looks in per_npv.values()]
    infos = _select_npvs(settings, npv_names, log, action="export")
    if args.all_looks:
        global_looks = []
        per_npv = {}
    elif per_npv:
        selected_keys = {info.name.casefold() for info in infos}
        extra = [display for key, (display, _looks) in per_npv.items()
                 if key not in selected_keys]
        if extra:
            raise CliError(
                "--looks refers to NPV(s) that are not selected: " + ", ".join(extra)
            )
    compact: list[dict] = []
    failures = 0

    for info in infos:
        wanted = list(global_looks)
        per_entry = per_npv.get(info.name.casefold())
        if per_entry is not None:
            wanted = list(per_entry[1])
        try:
            scan = pl.scan_npv(info, settings, log, force=args.force_uncook)
            if not scan.ok:
                raise CliError(
                    f"no entity found in the uncooked tree"
                    + (f" ({'; '.join(scan.notes)})" if scan.notes else "")
                )
            if wanted:
                available = {look.casefold(): look for look in scan.looks}
                missing = [look for look in wanted if look.casefold() not in available]
                if missing:
                    raise CliError(
                        "look(s) not found: " + ", ".join(missing)
                        + "; available: " + (", ".join(scan.looks) or "none")
                    )
                requested = [available[look.casefold()] for look in wanted]
            else:
                requested = list(scan.looks)
            summary = pl.export_npv(
                info, requested, settings, log,
                force_uncook=False,  # scan already refreshed the cache
            )
            row = _compact_summary(summary)
            row["ok"] = bool(row["looks"]) and all(item["ok"] for item in row["looks"])
            compact.append(row)
            if not row["ok"]:
                failures += 1
            if not args.json:
                print(f"[{info.name}] exported {len(row['looks'])} look(s) -> "
                      f"{row['output_dir']}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.error(f"[{info.name}] export failed: {exc}")
            if args.verbose:
                log.raw(traceback.format_exc())
            compact.append({"npv": info.name, "ok": False, "error": str(exc),
                            "output_dir": None, "looks": []})

    if args.json:
        _print_json({"ok": failures == 0, "npvs": compact})
        return 0 if failures == 0 else 1
    if failures:
        print(f"Export finished with {failures} failure(s).")
        return 1
    print("Export finished.")
    return 0


def _cmd_detect(args, settings: pl.Settings, log: object) -> int:
    saved = pl.save_settings(settings, logger=log) if args.save else False
    npv_root = pl.effective_npv_root(settings, log)
    game_valid = pl.normalize_game_root(settings.game_root) is not None
    payload = {
        "settings_saved": bool(saved),
        "wolvenkit_cli": settings.wolvenkit_cli,
        "blender": settings.blender,
        "game_root": settings.game_root,
        "game_valid": game_valid,
        "npv_root": str(npv_root) if npv_root else settings.npv_root,
        "output_root": settings.output_root,
        "render_proofs": bool(settings.render_proofs),
        "ok": bool(game_valid or npv_root),
    }
    if args.json:
        _print_json(payload)
        return 0 if payload["ok"] else 1
    print(f"WolvenKit CLI : {settings.wolvenkit_cli or '(not found)'}")
    print(f"Blender       : {settings.blender or '(not found)'}")
    print(f"Game folder   : {settings.game_root or '(not found)'}")
    print(f"NPV folder    : {payload['npv_root'] or '(not found)'}")
    print(f"Output folder : {settings.output_root or '(not set)'}")
    print(f"Proof images  : {'on' if settings.render_proofs else 'off'}")
    if args.save:
        print(f"Saved to      : {pl.SETTINGS_PATH}")
    return 0 if payload["ok"] else 1


def _cmd_open_output(args, settings: pl.Settings, log: object) -> int:
    output = Path(settings.output_root or "")
    if not output.is_dir():
        raise CliError(f"Output folder does not exist yet:\n{output}")
    try:
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(str(output))  # type: ignore[attr-defined]
        else:
            print(f"Open this folder: {output}")
    except OSError as exc:
        raise CliError(f"Could not open {output}: {exc}") from exc
    if args.json:
        _print_json({"opened": True, "path": str(output)})
    return 0


def _cmd_selftest(args, settings: pl.Settings, log: object) -> int:
    # Preserve the old behaviour: a selftest on a fresh install creates
    # settings.json with auto-detected values.
    use_settings = settings
    if not pl.SETTINGS_PATH.is_file() and not any((
        args.wolvenkit_cli, args.blender, args.game_root,
        args.npv_root, args.output_root,
    )):
        use_settings = None
    code = pl.run_selftest(use_settings, log)
    if args.json:
        _print_json({"ok": code == 0, "exit_code": code})
    return code


def _cmd_save(args, settings: pl.Settings, log: object) -> int:
    if not pl.save_settings(settings, logger=log):
        raise CliError(f"Could not write {pl.SETTINGS_PATH}")
    payload = {
        "saved": True,
        "settings": str(pl.SETTINGS_PATH),
        "wolvenkit_cli": settings.wolvenkit_cli,
        "blender": settings.blender,
        "game_root": settings.game_root,
        "npv_root": settings.npv_root,
        "output_root": settings.output_root,
        "render_proofs": bool(settings.render_proofs),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Saved settings: {pl.SETTINGS_PATH}")
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _configure_stdio() -> None:
    """Avoid UnicodeEncodeError on legacy Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    actions = (args.list, args.scan, args.export, args.detect, args.selftest, args.open_output)
    if sum(bool(action) for action in actions) > 1:
        parser.error("choose only one action")
    if not any(actions) and not args.save:
        parser.print_help()
        return 2

    if args.npv and not (args.list or args.scan or args.export):
        parser.error("--npv is only valid with --list, --scan or --export")
    if (args.looks or args.all_looks) and not args.export:
        parser.error("--looks/--all-looks are only valid with --export")
    if args.force_uncook and not (args.scan or args.export):
        parser.error("--force-uncook is only valid with --scan or --export")
    if args.render_proofs is not None and not (args.export or args.save):
        parser.error("--proofs/--no-proofs require --export or --save")

    log = _make_logger(args)
    try:
        settings = _apply_overrides(pl.load_settings(logger=log), args)
        if args.detect:
            _autofill_missing(settings, log, need_blender=True, need_wolvenkit=True)
        elif args.list:
            _autofill_missing(settings, log, need_blender=False, need_wolvenkit=False)
        elif args.scan:
            _autofill_missing(settings, log, need_blender=False, need_wolvenkit=True)
        elif args.export:
            _autofill_missing(settings, log, need_blender=True, need_wolvenkit=True)
        elif args.selftest:
            _autofill_missing(settings, log, need_blender=True, need_wolvenkit=True)

        if args.save and not args.detect:
            if not pl.save_settings(settings, logger=log):
                raise CliError(f"Could not write {pl.SETTINGS_PATH}")

        if args.detect:
            return _cmd_detect(args, settings, log)
        if args.list:
            return _cmd_list(args, settings, log)
        if args.scan:
            return _cmd_scan(args, settings, log)
        if args.export:
            return _cmd_export(args, settings, log)
        if args.open_output:
            return _cmd_open_output(args, settings, log)
        if args.selftest:
            return _cmd_selftest(args, settings, log)
        return _cmd_save(args, settings, log)
    except CliError as exc:
        log.error(str(exc))
        if args.json:
            _print_json({"ok": False, "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        log.warn("Cancelled.")
        if args.json:
            _print_json({"ok": False, "error": "cancelled"})
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error(f"{type(exc).__name__}: {exc}")
        if args.verbose:
            log.raw(traceback.format_exc())
        if args.json:
            _print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
