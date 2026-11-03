"""Capture from the user's temporary app inside a dedicated, unprivileged LLDB."""
import json
import os
from pathlib import Path
import re
import signal
import time

from db_export import candidate_matches


def emit(message):
    # LLDB replaces Python's sys.stdout during attach/continue. Use the inherited
    # pipe explicitly so protocol messages cannot be lost in its SBFile buffering.
    os.write(1, (message + "\n").encode("ascii"))


def run(debugger, config_path):
    import lldb

    config = json.loads(Path(config_path).read_text())
    process = None
    secret = None
    failure = "timeout"
    previous_async = debugger.GetAsync()
    old_handlers = {}

    def interrupted(*_):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
        # Launch as the current user and arm the breakpoint before the first login.
        # Attach mode is retained for isolated regression probes.
        debugger.SetAsync(False)
        executable = config.get("executable")
        target = debugger.CreateTarget(executable or "")
        # The command interpreter consumes debugger.GetListener() events itself.
        # A dedicated listener prevents it stealing our breakpoint notifications.
        listener = lldb.SBListener("wechat-export-capture")
        error = lldb.SBError()
        if executable:
            breakpoint = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
            process = target.Launch(listener, None, None, "/dev/null", "/dev/null", "/dev/null",
                                    str(Path(config_path).parent), 0, True, error)
        else:
            process = target.AttachToProcessWithID(listener, config["pid"], error)
        if error.Fail() or not process.IsValid():
            failure = "launch" if executable else "attach"
            if not executable:
                process = None
            return
        if not executable:
            breakpoint = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
        if not executable and breakpoint.GetNumLocations() == 0:
            failure = "symbol"
            return
        is_arm = "arm64" in target.GetTriple() or "aarch64" in target.GetTriple()
        pointer_reg, length_reg = ("x1", "x2") if is_arm else ("rsi", "rdx")
        # Pass normal broken-pipe/child notifications to the application unchanged.
        signals = process.GetUnixSignals()
        for sig in (signal.SIGPIPE, signal.SIGCHLD):
            signals.SetShouldSuppress(sig, False)
            signals.SetShouldStop(sig, False)
        stop_id = process.GetStopID()
        debugger.SetAsync(True)
        if process.Continue().Fail():
            failure = "continue"
            return
        emit("WCDB_READY")
        deadline = time.monotonic() + config["timeout"]
        seen = set()
        while time.monotonic() < deadline:
            event = lldb.SBEvent()
            if not listener.WaitForEvent(1, event) or not lldb.SBProcess.EventIsProcessEvent(event):
                continue
            state = lldb.SBProcess.GetStateFromEvent(event)
            if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
                failure = "exited"
                break
            if (state != lldb.eStateStopped or lldb.SBProcess.GetRestartedFromEvent(event)
                    or process.GetState() != lldb.eStateStopped or process.GetStopID() <= stop_id):
                continue
            stop_id = process.GetStopID()
            hits, unexpected = [], []
            for thread in process:
                reason = thread.GetStopReason()
                if reason == lldb.eStopReasonBreakpoint and any(
                        thread.GetStopReasonDataAtIndex(i) == breakpoint.GetID()
                        for i in range(0, thread.GetStopReasonDataCount(), 2)):
                    hits.append(thread)
                elif reason not in (lldb.eStopReasonInvalid, lldb.eStopReasonNone):
                    # Only enums / signal numbers / Mach exception types, never
                    # descriptions, registers, exception addresses, or memory.
                    if reason in (lldb.eStopReasonSignal, lldb.eStopReasonException):
                        kind = "signal" if reason == lldb.eStopReasonSignal else "exception"
                        unexpected.append(f"{kind}:{thread.GetStopReasonDataAtIndex(0)}")
                        if kind == "exception":
                            frame = thread.GetFrameAtIndex(0)
                            module = frame.GetModule().GetFileSpec().GetFilename() or "unknown"
                            if re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", module):
                                emit(f"WCDB_SITE:{module}:{frame.GetPCAddress().GetFileAddress():x}")
                    else:
                        unexpected.append(f"reason:{reason}")
            if unexpected or not hits:
                for detail in sorted(set(unexpected))[:8] or ["reason:0"]:
                    emit("WCDB_STOP:" + detail)
                failure = "unexpected_stop"
                break
            for thread in hits:
                frame = thread.GetFrameAtIndex(0)
                length = frame.FindRegister(length_reg).GetValueAsUnsigned()
                address = frame.FindRegister(pointer_reg).GetValueAsUnsigned()
                if length != 32 or not address:
                    continue
                error = lldb.SBError()
                candidate = process.ReadMemory(address, 32, error)
                if error.Fail() or len(candidate) != 32 or candidate in seen:
                    continue
                seen.add(candidate)
                if candidate_matches(candidate, [Path(p) for p in config["databases"]]):
                    secret = candidate
                    break
            if secret:
                break
            if process.Continue().Fail():
                failure = "continue"
                break
    except KeyboardInterrupt:
        failure = "cancelled"
    except Exception:
        failure = "driver"
    finally:
        detached = True
        if process is not None and process.IsValid():
            if process.GetState() not in (lldb.eStateExited, lldb.eStateDetached):
                # Detach clears this debugger's breakpoints and resumes the target.
                error = process.Detach(False)
                detached = error.Success()
        debugger.SetAsync(previous_async)
        for sig, handler in old_handlers.items():
            # LLDB may have installed a native handler that Python represents as
            # None; signal.signal cannot restore that value. This process exits next.
            if handler is not None:
                signal.signal(sig, handler)
        if not detached:
            emit("WCDB_ERROR:detach")
        elif secret:
            # Consumed by the parent via a private pipe; never echoed to the terminal.
            emit("WCDB_SECRET:" + secret.hex())
        else:
            emit("WCDB_ERROR:" + failure)
