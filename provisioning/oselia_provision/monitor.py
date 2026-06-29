"""Stream the board's firmware log over USB + classify bring-up from the serial.

Two capture techniques (this board has a dual-core cold-boot USB-wedge quirk -- see
firmware/BRINGUP.md):

  * stream_held()  -- relaunch the firmware over a HELD `mpremote ... resume exec` session
    and relay its stdout. USB is already enumerated by that session, so it survives
    net_task's boot; a cold reset would wedge it. Used by `monitor` (default) and the
    best-effort bring-up stream during provisioning.
  * stream_passive() -- only LISTEN to the current USB-CDC serial without interrupting the
    running firmware (pyserial preferred, raw-tty fallback). Used by `monitor --passive`.

Ported from the original single-file provisioning wizard.
"""
import os
import subprocess
import threading
import time

from . import board, console

# ANSI colour per firmware log level prefix ([E]/[W]/[D] from src/log.py). INFO is left
# uncoloured (the common case) so only WARN/ERROR/DEBUG stand out.
_LOG_COLORS = {"E": "\x1b[31m", "W": "\x1b[33m", "D": "\x1b[2m"}

# Launch the app exactly as the OTA loader does (honouring slot selection / boot-confirm)
# but FROM a held mpremote session, so USB stays enumerated through net_task's boot.
_MONITOR_LAUNCH = (
    "import os\n"
    "_r = os.listdir('/')\n"
    "if 'boot.py' in _r:\n"
    "    exec(open('/boot.py').read())\n"
    "elif 'main.py' in _r:\n"
    "    __import__('main').main()\n"
    "else:\n"
    "    print('monitor: no boot.py/main.py on the board -- provision it first')\n"
)


def classify_bringup(text):
    """Inspect captured board serial -> (status, message). status in
    {pass, ethernet, mqtt, mcp, unknown}. Best-effort; the broker wait is authoritative."""
    if "HA discovery published" in text or "HA discovery skipped" in text:
        return ("pass", "Board reached HA bring-up and is online.")
    if "CH9120 TCP down" in text or "CH9120 re-bringup failed" in text:
        return ("ethernet",
                "Ethernet/TCP to the broker is down -- check the cable and that the broker "
                "IP is reachable from the board's network.")
    if "no MCP chips responding" in text or ("MCP@0x" in text and "init failed" in text):
        return ("mcp",
                "No input board is responding -- check the I2C wiring and that the board "
                "count matches the chips actually installed.")
    if "connect failed" in text or "MQTT connect" in text:
        return ("mqtt",
                "Reached the network but the MQTT session didn't complete -- check the "
                "broker IP/port and username/password.")
    return ("unknown", "Could not determine bring-up state from serial output.")


def colorize_log_line(line):
    """Colour a firmware serial log line by its level prefix ('[E] ...', etc). INFO lines
    and anything without a recognised prefix pass through unchanged."""
    if len(line) >= 3 and line[0] == "[" and line[2] == "]":
        col = _LOG_COLORS.get(line[1])
        if col:
            return col + line + "\x1b[0m"
    return line


# ---------------------------------------------------------------------------
# passive serial read
# ---------------------------------------------------------------------------
def _has_pyserial():
    try:
        import serial  # noqa: F401
        return True
    except ImportError:
        return False


def _read_serial_until_closed(port, on_chunk):
    """Open `port` and feed decoded text chunks to on_chunk(text) until the port errors or
    KeyboardInterrupt. PASSIVE -- never enters the raw REPL. pyserial preferred; raw POSIX
    read of the USB-CDC tty as a fallback (macOS/Linux)."""
    if _has_pyserial():
        import serial                              # type: ignore
        try:
            with serial.Serial(port, 115200, timeout=0.5) as s:
                while True:
                    data = s.read(256)
                    if data:
                        on_chunk(data.decode("utf-8", "replace"))
        except serial.SerialException:
            return
        return
    try:
        with open(port, "rb", buffering=0) as f:
            os.set_blocking(f.fileno(), False)
            while True:
                try:
                    chunk = f.read(256)
                except BlockingIOError:
                    time.sleep(0.05)
                    continue
                except OSError:
                    return
                if chunk:
                    on_chunk(chunk.decode("utf-8", "replace"))
                else:
                    time.sleep(0.05)
    except OSError:
        return


def stream_passive(port, colorize=True):
    """Stream the board's USB-CDC serial line by line until Ctrl-C. Resilient to the board
    rebooting (re-detects the port and reconnects). Read-only throughout."""
    pending = [""]

    def emit(text):
        pending[0] += text
        while "\n" in pending[0]:
            line, pending[0] = pending[0].split("\n", 1)
            line = line.rstrip("\r")
            print(colorize_log_line(line) if colorize else line, flush=True)

    try:
        while True:
            _read_serial_until_closed(port, emit)
            if pending[0]:
                print(colorize_log_line(pending[0]) if colorize else pending[0], flush=True)
                pending[0] = ""
            print("  [monitor] link dropped (reset/unplug?) -- waiting for the board ...",
                  flush=True)
            newport = _wait_for_board(timeout=30)
            if not newport:
                print("  [monitor] board didn't re-appear -- stopping.", flush=True)
                return
            if newport != port:
                print("  [monitor] reconnected on %s" % newport, flush=True)
                port = newport
            else:
                print("  [monitor] reconnected.", flush=True)
    except KeyboardInterrupt:
        print("\n  [monitor] stopped.", flush=True)


def _wait_for_board(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        boards = board.find_boards()
        if boards:
            return boards[0][0]
        time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# held-session relaunch + stream
# ---------------------------------------------------------------------------
def stream_held(port, colorize=True):
    """Relay a held `mpremote ... resume exec` session that runs the loader. `resume` => NO
    soft reset (a soft reset auto-runs /boot.py and the firmware's non-returning main()
    blocks raw-REPL entry); we enter the raw REPL on the idle board and run the loader
    ourselves, streaming. Ctrl-C leaves the board at the REPL."""
    cmd = ["mpremote", "connect", port, "resume", "exec", _MONITOR_LAUNCH]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(colorize_log_line(line) if colorize else line, flush=True)
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        print("\n  [monitor] stopped -- the board is left at the REPL "
              "(`oselia board reset`, or re-provision, to resume autorun).", flush=True)


def stream_bringup(port, colorize=True, timeout=60.0):
    """During provisioning: run the firmware over a held session and stream its boot log
    live, returning once bring-up is confirmed, the session ends, `timeout` elapses, or
    Ctrl-C. Returns (status, text). FULLY non-fatal -- the broker is the authoritative
    check, so a flaky read never fails an otherwise-good provision."""
    try:
        # stderr->stdout so an attach failure (e.g. raw-REPL busy) is visible instead of a
        # silent empty stream; mpremote's own chatter is split out into `diag` below.
        proc = subprocess.Popen(
            ["mpremote", "connect", port, "resume", "exec", _MONITOR_LAUNCH],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        return ("error", "could not start stream: %s" % e)
    lines = []
    diag = []                 # mpremote/tooling lines (not firmware log) -- shown only if nothing else
    status = {"v": None}
    done = threading.Event()

    def reader():
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("mpremote:") or line.startswith("Traceback"):
                    diag.append(line)
                    continue
                print(colorize_log_line(line) if colorize else line, flush=True)
                lines.append(line)
                if "HA discovery published" in line or "HA discovery skipped" in line:
                    status["v"] = "pass"
                    return
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        done.wait(timeout)
    except KeyboardInterrupt:
        status["v"] = "interrupted"
        print("\n  [bring-up] log skipped -- checking the broker ...", flush=True)
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    # If the firmware printed nothing but mpremote complained, surface that one line so an
    # empty stream isn't a total mystery (the caller still treats the broker as authoritative).
    if not lines and diag:
        print("  [bring-up] %s" % diag[-1], flush=True)
    text = "\n".join(lines)
    return (status["v"] or classify_bringup(text)[0], text)


def has_pyserial():
    return _has_pyserial()
