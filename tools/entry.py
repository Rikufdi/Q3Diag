#!/usr/bin/env python3
"""entry.py - the packaged exe's entry point.

Starting the wizard is itself a failure mode: importing it pulls in cell -> analyze -> qsite, and
qsite.path() *exits* (SystemExit, which is not an Exception) when a required tool is missing or
site.json is unreadable. wizard.main()'s own handlers cannot see that, because main() has not been
reached yet -- and on a double-clicked exe the console window closes with the message still inside it,
which is exactly how issue #4 arrived: "a cmd window appears for 1 frame and immediately closes".

So the import happens inside the guard below, and any failure before the wizard can run prints and
waits for Enter, the same way the wizard already handles a fatal error of its own.

Development can use this too (`python tools/entry.py`), so the packaged and unpackaged paths cannot
drift apart. `python tools/wizard.py` still works and is what AGENTS.md documents; it simply lacks this
guard, which does not matter in an already-open terminal.
"""
import sys
import traceback


def _pause():
    """Hold the window open for a double-clicked exe. Harmless when a terminal is already open."""
    try:
        input("\nPress Enter to exit... ")
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    try:
        import wizard
    except SystemExit as exc:
        # qsite's own exits: a missing required tool, or a site.json that will not parse. Its message
        # already says what to do, so print it verbatim rather than wrapping it.
        print(f"\nerror: {exc}" if str(exc) else "\nstopped")
        _pause()
        return 1
    except BaseException:
        # Anything else before the wizard could start -- a broken install, a missing bundled module.
        print("\nfatal error before the session could start:")
        traceback.print_exc()
        _pause()
        return 1
    # Past this point wizard.main() owns the process: it prints and pauses on its own fatal errors, and
    # exits with the run's own status code.
    return wizard.main()


if __name__ == "__main__":
    raise SystemExit(main())
