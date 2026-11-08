"""Instrument real gspread HTTP calls against synthetic in-memory Sheets only."""
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote

import gspread
import requests
from gspread.utils import a1_range_to_grid_range

from event_checkin.production import digest, private_json
from event_checkin.production_sheets import PRODUCTION_METADATA, source_mapping, column_number
from event_checkin.header_init import APPROVED_RANGES, EVENT_ID
from event_checkin import pilot


class FakeClock:
    def __init__(self):
        self.time, self.sleeps, self.on_sleep = 100.0, [], None

    def monotonic(self): return self.time

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.time += delay
        if self.on_sleep: self.on_sleep(delay)


class SheetsAPI:
    def __init__(self, fixture, clock):
        self.f, self.clock, self.calls = fixture, clock, []
        self.hook = None
        self.headers = {}
        self.sources = json.loads((Path(__file__).resolve().parents[1]/'config/sources.production.example.json').read_text())['sources']
        fixture.s.service_account = fixture.s.root/'synthetic-credentials.json'
        fixture.s.service_account.write_text('{}')
        self.entries = []
        for source in self.sources:
            source['spreadsheet_id'] = 'synthetic-' + source['id']
            count, span = APPROVED_RANGES[source['id']]
            headers = ['Synthetic question ' + str(i) for i in range(count)]
            for label, col in source_mapping(source).values(): headers[column_number(col)-1] = label
            self.entries.append({'source': source, 'range': span, 'original_headers': headers})
        self.plan = {'version': 1, 'event_id': EVENT_ID, 'headers': list(PRODUCTION_METADATA), 'sources': self.entries}
        fixture.s.sources_file.write_text(json.dumps({'version': 1, 'sources': self.sources}))
        private_json(fixture.s.root/'config/production-header-init.approved.json', self.plan)
        fixture.plan['dataset'] = digest(self.sources)
        private_json(fixture.path, fixture.plan)
        fixture.original_plan = fixture.path.read_bytes()
        fixture.policy.update(plan_sha256=pilot.checksum(fixture.path),
                              manifest_sha256=pilot.checksum(fixture.s.sources_file),
                              headers_sha256=pilot.checksum(fixture.s.root/'config/production-header-init.approved.json'))
        private_json(pilot.policy_path(fixture.s), fixture.policy)

    def client(self, **kwargs): return gspread.Client(auth=None, session=self)

    def entry(self, book): return next(e for e in self.entries if e['source']['spreadsheet_id'] == book)

    def positions(self, entry):
        positions = {key: column_number(col)-1 for key, (_, col) in source_mapping(entry['source']).items()}
        headers = entry['original_headers'] + list(PRODUCTION_METADATA)
        positions.update({key: headers.index(key) for key in PRODUCTION_METADATA})
        if len(entry['original_headers']) > 1 and entry['original_headers'][1] == 'Timestamp':
            positions['submission_timestamp'] = 1
        return positions

    def matrix(self, entry):
        header = entry['original_headers'] + list(PRODUCTION_METADATA)
        records = self.f.reader.sheet.data['sources'][entry['source']['id']]
        rows = [deepcopy(header)]
        for record in records:
            row = ['']*len(header)
            for key, col in self.positions(entry).items(): row[col] = record.get(key, '')
            rows.append(row)
        return rows

    @staticmethod
    def cell(value):
        if isinstance(value, dict): return deepcopy(value)
        if value == '' or value is None: return {}
        typed = ({'boolValue': value} if type(value) is bool else
                 {'numberValue': value} if isinstance(value, (int, float)) else {'stringValue': value})
        return {'userEnteredValue': typed, 'effectiveValue': deepcopy(typed)}

    def properties(self, entry):
        return {'sheetId': entry['source']['tab_id'], 'title': entry['source']['tab_name'],
                'gridProperties': {'columnCount': len(self.matrix(entry)[0]), 'rowCount': 1000}}

    def range_cells(self, entry, span):
        address = span.rsplit('!', 1)[-1]
        grid = a1_range_to_grid_range(address)
        matrix = self.matrix(entry)
        rs, cs = grid.get('startRowIndex', 0), grid.get('startColumnIndex', 0)
        re, ce = grid.get('endRowIndex', len(matrix)), grid.get('endColumnIndex', len(matrix[0]))
        # Include explicitly requested empty single cells for typed snapshots.
        height = min(re, max(len(matrix), rs+1))
        values = [[matrix[r][c] if r < len(matrix) and c < len(matrix[r]) else ''
                   for c in range(cs, ce)] for r in range(rs, height)]
        return rs, cs, values

    def request(self, method, url, params=None, json=None, **kwargs):
        path = unquote(url).split('/spreadsheets/', 1)[1]
        book = path.split('/', 1)[0].split(':', 1)[0]
        entry = self.entry(book)
        params = params or {}
        event = {'method': method.lower(), 'book': book, 'path': path, 'params': deepcopy(params),
                 'time': self.clock.monotonic()}
        self.calls.append(event)
        if self.hook: self.hook(event)
        if method.lower() != 'get':
            for request in json['requests']:
                update = request['updateCells']; area = update['range']
                key = next(k for k,c in self.positions(entry).items() if c == area['startColumnIndex'])
                assert key in PRODUCTION_METADATA
                row = self.f.reader.sheet.data['sources'][entry['source']['id']][area['startRowIndex']-1]
                row[key] = update['rows'][0]['values'][0]['userEnteredValue']['stringValue']
            body = {}
        elif '/values' in path:
            ranges = params.get('ranges') or [path.split('/values/', 1)[1]]
            items = []
            for span in ranges:
                _, _, values = self.range_cells(entry, span)
                if params.get('majorDimension') == 'COLUMNS': values = list(map(list, zip(*values))) if values else []
                items.append({'range': span, 'majorDimension': params.get('majorDimension', 'ROWS'), 'values': values})
            body = {'spreadsheetId': book, 'valueRanges': items} if ':batchGet' in path else items[0]
        else:
            sheet = {'properties': self.properties(entry)}
            if params.get('fields') and 'gridProperties' not in params['fields']:
                sheet['properties'].pop('gridProperties')
            if params.get('includeGridData') in ('true', True):
                sheet['data'] = []
                for span in params['ranges']:
                    rs, cs, values = self.range_cells(entry, span)
                    sheet['data'].append({'startRow': rs, 'startColumn': cs,
                                          'rowData': [{'values': [self.cell(v) for v in row]} for row in values]})
            body = {'spreadsheetId': book, 'properties': {'title': 'Synthetic workbook'}, 'sheets': [sheet]}
        response = requests.Response()
        response.status_code, response._content = 200, __import__('json').dumps(body).encode()
        return response

    def count(self): return sum(c['method'] == 'get' for c in self.calls)


def quota_error(code=429, reason='read_quota_exceeded'):
    from gspread.exceptions import APIError
    response = requests.Response()
    response.status_code = code
    response._content = json.dumps({'error': {'code': code, 'message': 'PRIVATE synthetic response',
                                            'errors': [{'reason': reason}]}}).encode()
    return APIError(response)
