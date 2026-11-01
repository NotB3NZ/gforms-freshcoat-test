"""Request-scoped reviewed schema/data snapshots; no cross-boundary row cache."""
from copy import deepcopy
from dataclasses import dataclass
import math

from gspread.utils import rowcol_to_a1

from .diagnostics import diagnostic_context
from .header_init import classify
from .production_sheets import PRODUCTION_METADATA, source_mapping, column_number, validate_production_identities
from .sheets import Row, SheetIntegrityError


def require(condition):
    if not condition: raise SheetIntegrityError('Incomplete or changed reviewed Sheet snapshot; no write authorized.')


def value(cell, field):
    typed = cell.get(field, {})
    require(isinstance(typed, dict) and len(typed) <= 1)
    if not typed: return ''
    key, result = next(iter(typed.items()))
    require(key in ('stringValue', 'boolValue', 'numberValue', 'formulaValue'))
    require((key in ('stringValue', 'formulaValue') and isinstance(result, str))
            or (key == 'boolValue' and type(result) is bool)
            or (key == 'numberValue' and type(result) in (int, float) and math.isfinite(result)))
    require(key != 'formulaValue' or field == 'userEnteredValue')
    return result


@dataclass
class SourceSnapshot:
    source: dict
    header: dict
    positions: dict
    rows: list
    typed: dict

    def capture(self, number):
        return {'source': self.source['id'], 'row': number, 'headers': deepcopy(self.header),
                'cells': dict(self.positions),
                'values': {k: deepcopy(self.typed.get(number, {}).get(k, {})) for k in self.positions}}


def read_source(http, entry, *, first=2, last=None, data=True):
    require(type(first) is int and first >= 2 and (last is None or type(last) is int and last >= first))
    source = entry['source']
    reviewed = entry['original_headers'] + list(PRODUCTION_METADATA)
    positions = {key: column_number(col) for key, (_, col) in source_mapping(source).items()}
    positions.update({key: reviewed.index(key)+1 for key in PRODUCTION_METADATA})
    groups = []
    if data:
        for col in sorted(positions.values()):
            if groups and groups[-1][1]+1 == col: groups[-1] = (groups[-1][0], col)
            else: groups.append((col, col))
    prefix = "'" + source['tab_name'].replace("'", "''") + "'!"
    ranges = [prefix+'1:1']
    for lo, hi in groups:
        end = rowcol_to_a1(last, hi) if last is not None else rowcol_to_a1(1, hi)[:-1]
        ranges.append(prefix+rowcol_to_a1(first, lo)+':'+end)
    result = http.fetch_sheet_metadata(source['spreadsheet_id'], params={
        'ranges': ranges, 'includeGridData': 'true',
        'fields': 'spreadsheetId,sheets(properties(sheetId,title,gridProperties(columnCount,rowCount)),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))'})
    sheets = result.get('sheets', [])
    require(result.get('spreadsheetId') == source['spreadsheet_id'] and len(sheets) == 1)
    props = sheets[0].get('properties', {})
    require(props.get('sheetId') == source['tab_id'] and props.get('title') == source['tab_name'])
    grid_props = props.get('gridProperties', {})
    require(type(grid_props.get('rowCount')) is int and grid_props['rowCount'] >= (last or first))
    blocks = {}
    expected = {(0, 0)} | {(first-1, lo-1) for lo, hi in groups}
    for block in sheets[0].get('data', []):
        key = (block.get('startRow', 0), block.get('startColumn', 0))
        require(key in expected and key not in blocks)
        blocks[key] = block.get('rowData', [])
    require(set(blocks) == expected and len(blocks[(0, 0)]) <= 1)
    head_cells = blocks[(0, 0)][0].get('values', []) if blocks[(0, 0)] else []
    header = {'title': props['title'], 'tab_id': props['sheetId'], 'column_count': grid_props.get('columnCount'),
              'headers': [value(cell, 'userEnteredValue') for cell in head_cells]}
    # Validate the exact approved schema before interpreting participant values.
    require(classify(entry, header) == 'initialized')
    typed, effective = {}, {}
    keys_by_col = {col: key for key, col in positions.items()}
    for lo, hi in groups:
        rows = blocks[(first-1, lo-1)]
        require(len(rows) <= (last or grid_props['rowCount'])-first+1)
        for offset, row in enumerate(rows):
            cells = row.get('values', [])
            require(len(cells) <= hi-lo+1)
            number = first+offset
            for col in range(lo, hi+1):
                key = keys_by_col[col]
                cell = cells[col-lo] if col-lo < len(cells) else {}
                typed.setdefault(number, {})[key] = deepcopy(cell.get('userEnteredValue', {}))
                effective.setdefault(number, {})[key] = value(cell, 'effectiveValue')
    rows = []
    for number in sorted(effective):
        values = {k: effective[number].get(k, '') for k in positions}
        require(values.get('source_id') in ('', source['id']))
        if not any(v != '' for v in values.values()): continue
        values['_price_category'] = source['price_category']
        rows.append(Row(number, values, source['id']))
    return SourceSnapshot(source, header, positions, rows, typed)


class ReviewedSheets:
    live = True

    def __init__(self, settings, plan, headers):
        from .production import digest
        self.settings, self.plan, self.headers = settings, plan, headers
        self.read_scheduler = headers.read_scheduler
        self.dataset = digest([e['source'] for e in plan['sources']])

    def snapshot(self):
        # A successful retry after quota waiting invalidates every earlier source
        # in this sweep. Restart once; never submit using pre-backoff approvals.
        for sweep in range(2):
            generation = self.read_scheduler.generation
            initial = {}
            for entry in self.plan['sources']:
                with diagnostic_context('validate_header_snapshot_'+entry['source']['id'], entry['source']['id']):
                    initial[entry['source']['id']] = self.headers.read_reviewed(entry, data=False).header
            sources = {}
            for entry in self.plan['sources']:
                with diagnostic_context('read_production_source', entry['source']['id']):
                    item = self.headers.read_reviewed(entry)
                    require(item.header == initial[entry['source']['id']])
                    sources[entry['source']['id']] = item
            if generation == self.read_scheduler.generation:
                rows = [row for item in sources.values() for row in item.rows]
                validate_production_identities(rows)
                return rows, sources
            self.read_scheduler.progress('Google Sheets read waiting invalidated the snapshot; refreshing all source checks.')
        raise SheetIntegrityError('Repeated read waiting prevented a fresh all-source snapshot; stopped for review.')

    def rows(self):
        return self.snapshot()[0]

    def capture(self, source, number):
        entry = next(e for e in self.plan['sources'] if e['source']['id'] == source)
        return self.headers.read_reviewed(entry, first=number, last=number).capture(number)

    def read_for_update(self, source, number):
        for sweep in range(2):
            rows, sources = self.snapshot()
            generation = self.read_scheduler.generation
            expected = sources[source].capture(number)
            entry = next(e for e in self.plan['sources'] if e['source']['id'] == source)
            with diagnostic_context('read_selected_original_cell_values', source):
                current = self.headers.read_reviewed(entry, first=number, last=number)
            if generation != self.read_scheduler.generation:
                self.read_scheduler.progress('Google Sheets read waiting before tracking update; refreshing eligibility and original cells.')
                continue
            require(current.capture(number) == expected)
            before = [r for r in rows if r.source_id == source and r.number == number]
            require(len(before) == 1 and len(current.rows) == 1 and before[0].data == current.rows[0].data)
            return rows, current.capture(number)
        raise SheetIntegrityError('Read waiting prevented a fresh tracking update; no write attempted.')
