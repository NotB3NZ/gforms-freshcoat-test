from copy import deepcopy
from pathlib import Path
from event_checkin.sheets import Row, validate_identities
from event_checkin.mailer import EmailNotSent, EmailAmbiguous


class FakeSheet:
    def __init__(self, rows):
        self.data = deepcopy(rows)
        self.fail_before = None
        self.fail_after = None
        self.writes = 0
        self.attempts = []

    def rows(self):
        rows = [Row(i, deepcopy(v)) for i, v in enumerate(self.data, 2)]
        validate_identities(rows)
        return rows

    def update(self, row, changes):
        self.attempts.append(deepcopy(changes))
        if self.fail_before and self.fail_before(changes):
            self.fail_before = None
            raise ConnectionError("Fake disconnected before write")
        if row.get("participant_id"):
            target = next(r for r in self.data if r.get("participant_id") == row.get("participant_id"))
        else:
            target = self.data[row.number - 2]
        target.update({k: str(v) for k, v in changes.items()})
        self.writes += 1
        if self.fail_after and self.fail_after(changes):
            self.fail_after = None
            raise ConnectionError("Fake response lost after write")
        row.data.update({k: str(v) for k, v in changes.items()})


class FakeRenderer:
    def __init__(self, settings):
        self.s = settings
        self.calls = []
        self.fail = False

    def render(self, pid, name, token):
        self.calls.append((pid, name, token))
        if self.fail:
            self.fail = False
            raise RuntimeError("Fake rendering failure")
        path = self.s.generated / pid / "card.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake card")
        return path


class FakeMailer:
    def __init__(self):
        self.calls = []
        self.failure = None

    def send(self, *args):
        self.calls.append(args)
        if self.failure:
            error = self.failure
            self.failure = None
            raise error("Fake email failure")


def registration(**changes):
    row = dict(event_id="test-event", name="Sample Participant", email="sample@example.test", state="PENDING")
    row.update(changes)
    return row


def ticket(**changes):
    row = registration(participant_id="P-test", ticket_token="a" * 43, normalized_email="sample@example.test",
                       ticket_name="Sample Participant", render_status="RENDERED", state="SENT", email_status="SENT")
    row.update(changes)
    return row
