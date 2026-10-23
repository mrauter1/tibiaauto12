"""Error-only diagnostics shared by the Windows input implementations."""

from contextlib import contextmanager
import sys

from core import runtime


class InputDiagnostics:
    def _init_diagnostics(self, error_diagnostic=None):
        if error_diagnostic is not None and not callable(error_diagnostic):
            raise TypeError("error_diagnostic must be callable")
        self._error_diagnostic = error_diagnostic
        self.last_error_diagnostic = None
        self._native_failure = None

    def _call(self, method, *arguments, **keywords):
        try:
            return getattr(self.backend, method)(*arguments, **keywords)
        except runtime.CancelledError:
            raise
        except Exception as exc:
            if self._native_failure is None:
                self._native_failure = exc
            raise

    def _release(self, method, *arguments, **keywords):
        """Attempt UP even on failure, without replacing the original error."""
        pending = sys.exc_info()[1]
        try:
            self._call(method, *arguments, **keywords)
        except Exception:
            if pending is None:
                raise

    @contextmanager
    def _operation(self, reason):
        with runtime.input_transaction(reason):
            self._native_failure = None
            self._operation_hwnd = None
            self.last_error_diagnostic = None
            try:
                yield
            except runtime.CancelledError:
                raise
            except Exception as exc:
                if self._native_failure is not None:
                    self._diagnose_after_error(exc)
                raise

    def _diagnose_after_error(self, error):
        """Observe availability after an error; never resend the action."""
        hwnd = self._operation_hwnd
        if hwnd is None:
            try:
                hwnd = int(self._hwnd_resolver() or 0)
            except Exception:
                hwnd = 0
        try:
            if self._error_diagnostic is not None:
                detail = self._error_diagnostic(hwnd, error)
            elif callable(getattr(self.backend, "availability_diagnostic", None)):
                detail = self.backend.availability_diagnostic(hwnd)
            else:
                detail = {"hwnd": hwnd}
                for label, method, arguments in (
                    ("is_window", "is_window", (hwnd,)),
                    ("foreground_hwnd", "foreground_window", ()),
                    ("client_size", "client_size", (hwnd,)),
                ):
                    try:
                        detail[label] = getattr(self.backend, method)(*arguments)
                    except Exception as exc:
                        detail[label] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            detail = {"hwnd": hwnd, "diagnostic_error": f"{type(exc).__name__}: {exc}"}
        self.last_error_diagnostic = detail
        try:
            error.client_availability = detail
        except Exception:
            pass

    def diagnose_availability(self, error):
        """Explicit post-error entry point for capture/adapter error reporting."""
        self._operation_hwnd = None
        self._diagnose_after_error(error)
        return self.last_error_diagnostic
