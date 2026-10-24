"""Bounded operation/error metadata, never exception messages or API bodies."""
from functools import wraps
from contextlib import contextmanager

SOURCES = frozenset(("regular", "discount_15", "discount_10"))


GOOGLE_REASONS = frozenset((
    "badRequest", "invalid", "invalidArgument", "INVALID_ARGUMENT", "rateLimitExceeded",
    "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded", "RESOURCE_EXHAUSTED",
    "PERMISSION_DENIED", "UNAUTHENTICATED", "NOT_FOUND", "UNAVAILABLE", "INTERNAL",
    "DEADLINE_EXCEEDED", "forbidden", "notFound", "accessNotConfigured",
    "insufficientPermissions", "authError", "backendError", "SERVICE_DISABLED",
    "invalid_field_mask", "invalid_ranges_parameter", "invalid_range", "read_quota_exceeded",
))
STAGES = frozenset(("controlled_preview", "open_production_sources_read_only",
                    "validate_headers_and_read_mapped_columns", "read_selected_original_cell_values",
                    "validate_single_registration", "validate_header_snapshot_regular",
                    "validate_header_snapshot_discount_15", "validate_header_snapshot_discount_10",
                    "single_entry_execution", "single_entry_email_settings", "single_entry_read_registration",
                    "single_entry_tracking_update", "single_entry_tracking_write", "single_entry_journal_read",
                    "single_entry_read_header", "single_entry_read_tracking", "open_production_source",
                    "read_production_source", "single_entry_message_build", "single_entry_smtp_connect",
                    "single_entry_smtp_before_submit", "single_entry_smtp_submit", "single_entry_smtp_accepted",
                    "single_entry_badge_verification"))


def diagnostic_operation(stage, source=None):
    """Keep the original exception; tag only a fixed caller-owned stage label."""
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                if not getattr(exc, "_safe_operation_stage", None):
                    exc._safe_operation_stage = stage
                if source in SOURCES and not getattr(exc, "_safe_source", None):
                    exc._safe_source = source
                raise
        return invoke
    return decorate


@contextmanager
def diagnostic_context(stage, source=None):
    try:
        yield
    except Exception as exc:
        if not getattr(exc, "_safe_operation_stage", None):
            exc._safe_operation_stage = stage
        if source in SOURCES and not getattr(exc, "_safe_source", None):
            exc._safe_source = source
        raise


def safe_diagnostic(exc, fallback_stage="controlled_preview", *, source=None):
    # `raise ... from None` hides display of a context, but its original exception
    # is still available. Follow that chain without formatting any exception.
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    root = chain[-1]
    operation = None
    frame = root.__traceback__
    trusted = {"event_checkin.controlled_render", "event_checkin.header_init", "event_checkin.production_sheets",
               "event_checkin.production", "event_checkin.sheets", "event_checkin.config",
               "event_checkin.single_send", "event_checkin.mailer"}
    while frame is not None:
        module = frame.tb_frame.f_globals.get("__name__")
        if module in trusted:
            operation = module.rsplit(".", 1)[-1] + "." + frame.tb_frame.f_code.co_name + ":" + str(frame.tb_lineno)
        frame = frame.tb_next
    stage = next((getattr(item, "_safe_operation_stage") for item in reversed(chain)
                  if getattr(item, "_safe_operation_stage", None) in STAGES),
                 fallback_stage if fallback_stage in STAGES else "controlled_preview")
    status, reason = None, None
    for item in reversed(chain):
        response = getattr(item, "response", None)
        code = getattr(response, "status_code", None)
        error = getattr(item, "error", None)
        if isinstance(error, dict):
            code = code if type(code) is int else error.get("code")
            candidates = [error.get("status")]
            errors = error.get("errors")
            candidates += [entry.get("reason") for entry in errors if isinstance(entry, dict)] if isinstance(errors, list) else []
            reason = next((candidate for candidate in candidates if isinstance(candidate, str) and candidate in GOOGLE_REASONS), None)
            # Classify known API messages internally, emitting only fixed codes.
            # Never echo their contents, which can contain a range or cell value.
            message = error.get("message", "")
            if isinstance(message, str):
                for fragment, category in (("Invalid field selection", "invalid_field_mask"),
                                           ("Invalid value at 'ranges'", "invalid_ranges_parameter"),
                                           ("Unable to parse range", "invalid_range"),
                                           ("Quota exceeded", "read_quota_exceeded")):
                    if fragment in message:
                        reason = category
                        break
        if type(code) is int and 100 <= code <= 599:
            status = code
            reason = reason or "unrecognized_reason_withheld"
            break
    result = {"stage": stage, "operation": operation, "exception_class": type(root).__name__,
              "http_status": status, "google_api_reason": reason}
    origin = next((getattr(item, "_safe_source") for item in reversed(chain)
                   if getattr(item, "_safe_source", None) in SOURCES), source if source in SOURCES else None)
    if origin:
        result["source"] = origin
    return result
