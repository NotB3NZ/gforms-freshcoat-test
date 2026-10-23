"""Aggregate-only production inspection. No raw participant data is printed/saved."""
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import ConfigurationError
from .header_init import HeaderTransport, classify, load_plan, require_checkpoint_settings
from .production import approved
from .production_sheets import APPROVAL_HEADERS
from .sheets import SheetIntegrityError, normalize_email
from .tiers import resolve_tier

STATUSES = {
    'state': {'PENDING', 'WAITING_APPROVAL', 'PROCESSING', 'READY', 'RENDER_ERROR', 'EMAIL_ERROR',
              'EMAIL_REVIEW', 'SENT', 'DUPLICATE', 'INVALID', 'REGISTRATION_REVIEW', 'CANCELLED'},
    'render_status': {'RENDERING', 'RENDERED', 'FAILED'},
    'email_status': {'DISABLED', 'RETRY', 'SENDING', 'SENT', 'FAILED', 'AMBIGUOUS'},
    'ticket_status': {'ACTIVE', 'CANCELLED'},
}
# Unknown free text can contain a name/address entered into the wrong field. Only
# bounded ticket vocabulary is allowed in terminal labels; other text is counted
# but withheld. This affects reporting only, never processing or tier normalization.
TIER_WORDS = set('lite regular pro ticket tickets tier type php peso pesos off discount discounted '
                 'student early bird promo promotion general admission access pass day full basic '
                 'standard premium vip gold silver bronze none n a unknown unspecified only '
                 'not or and aws scd mindanao'.split())


def present(value):
    return value is not None and value != ''


def valid_email(value):
    if not isinstance(value, str):
        return False
    value = normalize_email(value)
    return bool(value and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                and re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value))


def missing_name(value):
    return value is None or (isinstance(value, str) and not value.strip())


def valid_name(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= 160
            and not any(ord(c) < 32 or ord(c) == 127 for c in value.strip()))


def public_tier(value):
    if value is None or value == '':
        return '(blank)'
    if not isinstance(value, str):
        return '(unrecognized non-text value; withheld)'
    words = re.findall(r'[A-Za-z]+', value)
    safe = (len(value) <= 160 and set(w.casefold() for w in words) <= TIER_WORDS
            and not re.search(r'[@<>]|https?://|\d{7,}', value, re.IGNORECASE)
            and re.fullmatch(r'[A-Za-z0-9\s₱.,()%\[\]/:+–—-]*', value) is not None)
    return value if safe else '(unrecognized free-text label; withheld for privacy)'


def raw_tier_key(value):
    return (type(value).__name__, value) if isinstance(value, (str, bool, int, float, type(None))) else ('invalid', type(value).__name__)


def status_label(value, allowed):
    return '(blank)' if value in ('', None) else value if isinstance(value, str) and value in allowed else '(unrecognized)'


class InspectionReader:
    """Read-only scope; ranges are single columns selected from the reviewed schema."""
    def __init__(self, settings, plan):
        self.plan = plan
        self.headers = HeaderTransport(settings, plan, apply=False)

    def read_all(self):
        snapshots = {}
        for entry in self.plan['sources']:
            snapshot = self.headers.read(entry)
            if classify(entry, snapshot) != 'initialized':
                raise SheetIntegrityError('Inspection requires all reviewed tracking headers to be initialized.')
            snapshots[entry['source']['id']] = snapshot
        result = {}
        for entry in self.plan['sources']:
            source = entry['source']
            snapshot = snapshots[source['id']]
            current = self.headers.read(entry)
            if current != snapshot:
                raise SheetIntegrityError('Source headers changed before inspection; no aggregate report produced.')
            columns = {key: selector['column'] for key, selector in source['fields'].items()}
            from .production_sheets import APPROVAL_COLUMNS
            columns.update(dict(zip(APPROVAL_HEADERS, APPROVAL_COLUMNS[source['id']])))
            from gspread.utils import rowcol_to_a1
            columns.update({key: rowcol_to_a1(1, snapshot['headers'].index(key) + 1)[:-1] for key in STATUSES})
            keys = list(columns)
            prefix = "'" + source['tab_name'].replace("'", "''") + "'!"
            ranges = [prefix + columns[key] + '2:' + columns[key] for key in keys]
            self.headers.pace()
            response = self.headers.http.values_batch_get(source['spreadsheet_id'], ranges, params={
                'majorDimension': 'COLUMNS', 'valueRenderOption': 'UNFORMATTED_VALUE'})
            values = response.get('valueRanges', [])
            if response.get('spreadsheetId') != source['spreadsheet_id'] or len(values) != len(keys):
                raise SheetIntegrityError('Incomplete inspection read; no aggregate report produced.')
            data = []
            for key, item in zip(keys, values):
                returned_tab, span = item.get('range', '').rsplit('!', 1)
                returned_tab = returned_tab[1:-1].replace("''", "'") if returned_tab.startswith("'") and returned_tab.endswith("'") else returned_tab
                if returned_tab != source['tab_name'] or not re.fullmatch(re.escape(columns[key]) + r'2:' + re.escape(columns[key]) + r'\d*', span):
                    raise SheetIntegrityError('Unexpected inspection range; no aggregate report produced.')
                cells = item.get('values', [])
                if item.get('majorDimension') != 'COLUMNS' or len(cells) > 1:
                    raise SheetIntegrityError('Unexpected inspection shape; no aggregate report produced.')
                data.append(cells[0] if cells else [])
            if self.headers.read(entry) != snapshot:
                raise SheetIntegrityError('Source headers changed during inspection; no aggregate report produced.')
            height = max((len(col) for col in data), default=0)
            result[source['id']] = [{key: col[i] if i < len(col) else '' for key, col in zip(keys, data)} for i in range(height)]
        return result


def aggregate(sources):
    reports, by_source, emails = {}, {}, defaultdict(list)
    for source, raw_rows in sources.items():
        rows = [r for r in raw_rows if any(present(r.get(k)) for k in ('name', 'email', 'tier', *STATUSES))]
        by_source[source] = rows
        excluded = sum(not any(present(r.get(k)) for k in ('name', 'email', 'tier', *STATUSES))
                       and any(present(r.get(k)) for k in APPROVAL_HEADERS) for r in raw_rows)
        labels = Counter(public_tier(r.get('tier', '')) for r in rows)
        all_true = sum(all(approved(r.get(k)) for k in APPROVAL_HEADERS) for r in rows)
        reports[source] = {
            'registrations': len(rows), 'checkbox_only_rows_excluded': excluded,
            'tier_labels': [{'label': label, 'count': count,
                             'recognized_by_current_processor': resolve_tier(source, label) is not None,
                             'mapped_tier': resolve_tier(source, label),
                             'review_required': resolve_tier(source, label) is None} for label, count in sorted(labels.items())],
            'blank_tiers': sum(r.get('tier') in ('', None) for r in rows),
            'unrecognized_by_current_processor': sum(resolve_tier(source, r.get('tier')) is None and r.get('tier') not in ('', None) for r in rows),
            'all_four_approvals_true': all_true, 'awaiting_approval': len(rows) - all_true,
            'true_by_approval': {key: sum(approved(r.get(key)) for r in rows) for key in APPROVAL_HEADERS},
            'malformed_approval_values': {key: sum(not (value is True or value is False or value is None or
                                                       isinstance(value, str) and value in ('', 'TRUE', 'FALSE'))
                                                   for value in (r.get(key) for r in rows)) for key in APPROVAL_HEADERS},
            'missing_names': sum(missing_name(r.get('name')) for r in rows),
            'invalid_nonblank_names': sum(not missing_name(r.get('name')) and not valid_name(r.get('name')) for r in rows),
            'missing_emails': sum(r.get('email') is None or isinstance(r.get('email'), str) and not r['email'].strip() for r in rows),
            'invalid_emails_including_missing': sum(not valid_email(r.get('email')) for r in rows),
            'processing_status': {key: dict(Counter(status_label(r.get(key), allowed) for r in rows)) for key, allowed in STATUSES.items()},
        }
        for row in rows:
            if valid_email(row.get('email')):
                emails[normalize_email(row['email'])].append((source, row))
    groups = [group for group in emails.values() if len(group) > 1]
    across = [group for group in groups if len({s for s, _ in group}) > 1]
    def tier_conflict(group):
        return len({resolve_tier(s, r.get('tier')) for s, r in group if resolve_tier(s, r.get('tier'))}) > 1
    within_source = {}
    for source in by_source:
        local = []
        for group in groups:
            matches = [(s, r) for s, r in group if s == source]
            if len(matches) > 1:
                local.append(matches)
        within_source[source] = {
            'email_groups': len(local),
            'registrations': sum(len(group) for group in local),
            'extra_registrations': sum(len(group) - 1 for group in local),
            'groups_with_conflicting_mapped_tiers': sum(tier_conflict(group) for group in local),
            'groups_with_ambiguous_or_blank_tiers': sum(any(resolve_tier(s, r.get('tier')) is None for s, r in group) for group in local),
        }
    duplicate_report = {
        'duplicate_email_groups_event_wide': len(groups),
        'registrations_in_event_wide_duplicate_groups': sum(len(group) for group in groups),
        'event_wide_extra_registrations': sum(len(group) - 1 for group in groups),
        'event_wide_groups_with_conflicting_mapped_tiers': sum(tier_conflict(group) for group in groups),
        'event_wide_groups_with_ambiguous_or_blank_tiers': sum(any(resolve_tier(s, r.get('tier')) is None for s, r in group) for group in groups),
        'within_source_duplicates': within_source,
        'cross_source_email_groups': len(across),
        'registrations_in_cross_source_groups': sum(len(group) for group in across),
        'cross_source_extra_registrations': sum(len(group) - 1 for group in across),
        'cross_source_groups_with_different_raw_tiers': sum(len({raw_tier_key(r.get('tier')) for _, r in group}) > 1 for group in across),
        'cross_source_groups_with_conflicting_mapped_tiers': sum(tier_conflict(group) for group in across),
        'cross_source_groups_with_ambiguous_or_blank_tiers': sum(any(resolve_tier(s, r.get('tier')) is None for s, r in group) for group in across),
        'cross_source_groups_with_different_names': sum(len({r.get('name', '').strip() if isinstance(r.get('name'), str) else '' for _, r in group}) > 1 for group in across),
        'cross_source_groups_with_multiple_all_approved': sum(sum(all(approved(r.get(k)) for k in APPROVAL_HEADERS) for _, r in group) > 1 for group in across),
        'cross_source_groups_with_conflicting_all_approved_tiers': sum(tier_conflict([(s, r) for s, r in group if all(approved(r.get(k)) for k in APPROVAL_HEADERS)]) for group in across),
        'cross_source_groups_by_source_combination': dict(Counter(' + '.join(sorted({s for s, _ in group})) for group in across)),
    }
    for source, rows in by_source.items():
        reports[source]['registrations_in_cross_source_duplicate_groups'] = sum(s == source for group in across for s, _ in group)
        def candidate(row):
            return (all(approved(row.get(k)) for k in APPROVAL_HEADERS) and valid_name(row.get('name'))
                    and valid_email(row.get('email')) and len(emails[normalize_email(row['email'])]) == 1
                    and all(not present(row.get(k)) for k in STATUSES))
        reports[source]['controlled_test_candidates_with_approved_tier'] = sum(candidate(r) and resolve_tier(source, r.get('tier')) is not None for r in rows)
    return {'sources': reports, 'total_registrations': sum(r['registrations'] for r in reports.values()),
            'duplicates': duplicate_report,
            'counting_basis': 'Rows with a name, email, tier or processing status; checkbox-only rows excluded. No timestamp or unrelated responses read.',
            'email_comparison': 'Trim and case-fold only; Gmail dots and plus tags remain distinct.',
            'mapping_status': 'Canonical tiers and the three approved source-specific aliases only; no guessed aliases or suffix stripping.'}


def run_command(settings, args):
    require_checkpoint_settings(settings)
    plan = load_plan(settings, args.plan or settings.root / 'config/production-header-init.approved.json')
    try:
        report = aggregate(InspectionReader(settings, plan).read_all())
    except Exception:
        # Provider errors and raw values must never reach terminal output or logs.
        raise SheetIntegrityError('Read-only inspection could not be fully confirmed. No participant details were logged and no writes were attempted.') from None
    print(json.dumps({'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                      'event_id': settings.event_id, 'email_enabled': settings.email_enabled,
                      'participant_operations': 'LOCKED', 'writes': 0, **report}, indent=2, ensure_ascii=False))
