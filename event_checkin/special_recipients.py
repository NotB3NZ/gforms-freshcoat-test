"""Private, row-bound recipient exceptions for the Special source."""
import re
import json
from email.headerregistry import Address
from pathlib import Path

from gspread.utils import absolute_range_name

from .sheets import SheetIntegrityError, normalize_email

POLICY = 'special-recipient-overrides.approved.json'
SOURCE = 'special'
TAB_ID = 2055698387
TAB = 'SPECIAL | REGIS'
ADDRESS = '_delivery_override_address'
BINDING = '_delivery_override_binding'
BLOCKED = '_delivery_override_review_required'


def normalize(value):
    return normalize_email(value) if isinstance(value, str) else ''


def valid_address(address):
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", address or ''):
        return False
    try:
        return Address(addr_spec=address).addr_spec == address
    except (ValueError, IndexError):
        return False


def binding(settings, row, address):
    from .production import digest
    return digest([settings.event_id, SOURCE, TAB_ID, row.number,
                   row.get('submission_timestamp'), row.get('name'), row.get('email'),
                   row.get('tier'), row.get('_price_category'), address])


def policy_rows(settings):
    path = Path(settings.runtime) / POLICY
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SheetIntegrityError('Special recipient approval file cannot be read.') from None
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('event_id') != settings.event_id or data.get('tab_id') != TAB_ID
            or not isinstance(data.get('rows'), dict) or not data['rows']):
        raise SheetIntegrityError('Invalid Special recipient approval policy.')
    result = {}
    for key, value in data['rows'].items():
        if not re.fullmatch(r'[1-9][0-9]{0,7}', key) or int(key) < 2 or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            raise SheetIntegrityError('Invalid Special recipient approval binding.')
        result[int(key)] = value
    return result


def read_addresses(http, source, numbers):
    """Read only the explicitly selected F cells, in one paced GET."""
    if not numbers:
        return {}
    if (source['id'], source['tab_name'], source['tab_id']) != (SOURCE, TAB, TAB_ID):
        raise SheetIntegrityError('Unexpected Special recipient tab.')
    ranges = [absolute_range_name(TAB, f'F{n}') for n in numbers]
    reply = http.values_batch_get(source['spreadsheet_id'], ranges,
                                 {'valueRenderOption': 'UNFORMATTED_VALUE'})
    blocks = reply.get('valueRanges', [])
    if len(blocks) != len(ranges):
        raise SheetIntegrityError('Incomplete Special recipient read.')
    found = {}
    for n, block in zip(numbers, blocks):
        if block.get('range') not in (f'{TAB}!F{n}', ranges[len(found)]):
            raise SheetIntegrityError('Unexpected Special recipient cell.')
        values = block.get('values', [])
        if values and (len(values) != 1 or len(values[0]) > 1):
            raise SheetIntegrityError('Unexpected Special recipient range.')
        found[n] = normalize(values[0][0]) if values and values[0] else ''
    return found


def apply_recipient_overrides(settings, rows, source, http):
    approved = policy_rows(settings)
    addresses = read_addresses(http, source, sorted(approved))
    for row in rows:
        if row.source_id != SOURCE:
            continue
        for key in (ADDRESS, BINDING, BLOCKED):
            row.data.pop(key, None)
        if row.number not in approved:
            continue
        address = addresses[row.number]
        signature = binding(settings, row, address)
        if not row.get('submission_timestamp') or not valid_address(address) or signature != approved[row.number]:
            row.data[BLOCKED] = True
        else:
            row.data[ADDRESS] = address
            row.data[BINDING] = signature
    return rows


def potential_repeat(row, other):
    """Hold same-C, same-name bookings even when an override changes delivery."""
    if not (row.get(BINDING) or other.get(BINDING)):
        return False
    captured = normalize(row.get('email'))
    name = ' '.join(str(row.get('name') or '').split()).casefold()
    return bool(captured and name and captured == normalize(other.get('email'))
                and name == ' '.join(str(other.get('name') or '').split()).casefold())
