"""Prepare private four-source candidates; never activate them or write Sheets."""
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from .header_init import HeaderTransport, SOURCE_RANGES, classify, load_plan, require_checkpoint_settings
from .production import private_json
from .production_sheets import APPROVAL_HEADERS, source_mapping, column_number
from .single_send import require
from .source_config import BASE_SOURCES


def special_source(spreadsheet_id, name_header):
    return {'id': 'special', 'spreadsheet_id': spreadsheet_id, 'tab_name': 'SPECIAL | REGIS',
            'tab_id': 2055698387, 'price_category': 'special',
            'fields': {'email': {'header': 'Email Address', 'column': 'C'},
                       'name': {'header': name_header, 'column': 'D'},
                       'tier': {'header': 'Please confirm', 'column': 'M'}},
            'approvals': dict(APPROVAL_HEADERS), 'approved_duplicate_headers': {'Email Address': ['C', 'F']}}


def require_disabled_authorizations(settings):
    paths = [settings.root/'config/single-entry-send.approved.json',
             settings.root/'config/pilot-batch.approved.json', settings.root/'config/pilot-resend.approved.json',
             *sorted((settings.root/'config/batch-authorizations').glob('*.json'))]
    require(all(json.loads(p.read_text()).get('enabled') is False for p in paths if p.exists()),
            'Disable sending authorizations before preparing a new source; existing bindings are never rewritten.')


def prepare(settings, directory, *, audit=False, transport_factory=HeaderTransport):
    require_checkpoint_settings(settings)
    require_disabled_authorizations(settings)
    original = load_plan(settings, settings.root/'config/production-header-init.approved.json')
    sources = [deepcopy(e['source']) for e in original['sources']]
    require(tuple(s['id'] for s in sources) == BASE_SOURCES,
            'Prepare special only from the unchanged three-source configuration.')
    require(len({s['spreadsheet_id'] for s in sources}) == 1, 'All four tabs must share the existing spreadsheet.')
    candidate = deepcopy(original)
    # The name header in a draft is provisional. Auditing replaces it with D1;
    # unobserved headers stay null and the draft cannot pass load_plan().
    source = special_source(sources[0]['spreadsheet_id'], sources[0]['fields']['name']['header'])
    entry = {'source': source, 'range': SOURCE_RANGES['special'][1], 'original_headers': [None]*21}
    for key, (label, column) in source_mapping(source).items():
        if key != 'name': entry['original_headers'][column_number(column)-1] = label
    entry['original_headers'][5] = 'Email Address'
    candidate['sources'].append(entry)
    candidate['requires_live_verification'] = True
    report = {'status': 'DRAFT; live header verification required', 'tracking_range': 'V1:AS1',
              'tracking_destination_blank': None, 'participant_rows_read': 0, 'sheet_writes': 0,
              'email_enabled': False, 'sending_authorizations_enabled': False}
    if audit:
        transport = transport_factory(settings, candidate, apply=False)
        snapshots = {e['source']['id']: transport.read(e) for e in candidate['sources']}
        for old in original['sources']:
            require(classify(old, snapshots[old['source']['id']]) == 'initialized',
                    'An existing source is not initialized; stop for review.')
        snapshot = snapshots['special']
        headers = list(snapshot['headers'])
        while headers and headers[-1] == '': headers.pop()
        require(len(headers) == 21 and all(isinstance(h, str) and h for h in headers),
                'Special must have exactly 21 populated headers through U and a blank V1:AS1 destination.')
        source['fields']['name']['header'] = headers[3]
        source_mapping(source)
        entry['original_headers'] = headers
        require(classify(entry, snapshot) == 'pending', 'Special tracking destination must be blank.')
        candidate['requires_live_verification'] = False
        report.update(status='VERIFIED CANDIDATE; active configuration unchanged',
                      tracking_destination_blank=True, snapshots=snapshots)
    directory = Path(directory).resolve()
    require(directory.is_relative_to((settings.runtime/'source-candidates').resolve()),
            'Keep private source candidates under runtime-production/source-candidates.')
    manifest_path, plan_path = directory/'sources.production.json', directory/'production-header-init.candidate.json'
    manifest = {'version': 1, 'sources': [e['source'] for e in candidate['sources']]}
    # An existing verified audit is never replaced by an unaudited draft.
    if plan_path.exists() and not audit:
        require(json.loads(plan_path.read_text()).get('requires_live_verification') is True,
                'Preserve the existing verified candidate; use a separate directory for a draft.')
    private_json(manifest_path, manifest)
    private_json(plan_path, candidate)
    if audit:
        load_plan(replace(settings, sources_file=manifest_path), plan_path)
    private_json(directory/'header-audit.json', report)
    return {**{k: v for k, v in report.items() if k != 'snapshots'},
            'manifest': str(manifest_path), 'header_plan': str(plan_path)}


def run_command(settings, args):
    directory = args.out_directory or settings.runtime/'source-candidates/special'
    print(json.dumps(prepare(settings, directory, audit=args.audit), indent=2))
