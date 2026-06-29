"""Output + interaction layer.

Crucial design rule (per the tool brief): every interactive prompt MUST be
bypassable so an automated caller -- me (Claude) or a CI script -- never blocks.
Two global switches, set once from the CLI root callback, govern this:

  * ASSUME_YES  (`--yes`/`-y`)        -> confirm() returns True without asking.
  * INTERACTIVE (default: stdin tty;  -> when False, confirm() returns the caller's
    forced off by `--non-interactive`)   `default`, and ask()/prompt_secret() return
                                          their default (or raise if it's required).

So `oselia <cmd> --yes` and `oselia <cmd> --non-interactive` both run end-to-end with
no human at the keyboard; a plain interactive run still prompts as a wizard would.
"""
import os
import sys

import typer

# --- global interaction state (set by cli.main_callback) -------------------
ASSUME_YES = False
INTERACTIVE = sys.stdin.isatty()
FORCE = False               # --force: override the serial-contention guard (board.py)


def configure(assume_yes: bool, non_interactive: bool, force: bool = False) -> None:
    global ASSUME_YES, INTERACTIVE, FORCE
    ASSUME_YES = bool(assume_yes)
    # --non-interactive forces it off; otherwise honour whether stdin is a real tty.
    INTERACTIVE = (not non_interactive) and sys.stdin.isatty()
    FORCE = bool(force)


def supports_color() -> bool:
    return (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") not in (None, "dumb"))


# --- styled output (thin wrappers over typer.secho) ------------------------
def info(msg: str = "") -> None:
    typer.echo(msg)


def step(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.CYAN, bold=True)


def ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def warn(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.YELLOW)


def err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


def die(msg: str, code: int = 1):
    """Print an error and raise typer.Exit (so Typer reports a clean non-zero exit)."""
    typer.secho("ERROR: " + msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# --- bypassable prompts ----------------------------------------------------
def confirm(prompt: str, default: bool = True) -> bool:
    if ASSUME_YES:
        return True
    if not INTERACTIVE:
        return default
    return typer.confirm(prompt, default=default)


def ask(prompt: str, default=None, required: bool = False):
    """Prompt for a line of text. Non-interactive -> the default (or die if required
    and no default)."""
    if not INTERACTIVE:
        if required and not default:
            die("%s: required, but running non-interactively (pass it as a flag)."
                % prompt)
        return default or ""
    suffix = " [%s]" % default if default else ""
    val = input("%s%s: " % (prompt, suffix)).strip()
    return val or (default or "")


def prompt_secret(prompt: str, default=None):
    """Masked prompt (e.g. a broker password). Non-interactive -> the default."""
    if not INTERACTIVE:
        return default
    import getpass
    return getpass.getpass(prompt + ": ") or default


def pick_one(items, label, fmt):
    """Auto-select when exactly one item, prompt a numbered menu when several, return
    None when zero. Non-interactive with several -> the first (deterministic)."""
    if not items:
        return None
    if len(items) == 1:
        info("  Using the only %s found: %s" % (label, fmt(items[0])))
        return items[0]
    if not INTERACTIVE:
        info("  Several %ss found; using the first (non-interactive): %s"
             % (label, fmt(items[0])))
        return items[0]
    info("Multiple %ss found:" % label)
    for i, it in enumerate(items):
        info("  [%d] %s" % (i, fmt(it)))
    while True:
        idx = ask("Which %s number" % label)
        if idx.isdigit() and 0 <= int(idx) < len(items):
            return items[int(idx)]
