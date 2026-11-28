"""Production registration reconciliation before the ASGI app accepts requests."""


def reconcile_startup(service, sheets):
    try:
        # Source I/O happens before the short database transaction. Import validates
        # the complete snapshot, preserves attendance, and opens a new event atomically.
        service.import_rows(sheets.rows(), initialize_admissions=service.s.auto_open_admissions)
    except Exception:
        # Google/SQL exceptions can contain credentials, URLs or registration data.
        raise RuntimeError(
            "Production startup blocked: registration import/reconciliation failed. "
            "Check WEB_EVENT_ID, every configured source and its headers/identities, Google access, "
            "and conflicting legacy attendance. No admissions were opened by this attempt; "
            "existing attendance and admission state were retained."
        ) from None
