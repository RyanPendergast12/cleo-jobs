"""Durable, version-bound approvals for each employer form step.

The browser never receives answers until an owner approves their exact packet.
An interrupted transmission is uncertain, never automatically retried.
"""
import hashlib
import json
import os
from urllib.parse import urlsplit
from jobapply_ats import TRANSMIT_READY, platform_id
from jobapply_store import stamp


def transmit_ats():
    """Explicit ATS transmission allowlist. Empty or unknown values fail closed."""
    configured = {
        value.strip().lower()
        for value in os.environ.get('JOB_APPLY_TRANSMIT_ATS', '').replace(';', ',').split(',')
        if value.strip()
    }
    return configured & TRANSMIT_READY


def transmission_enabled():
    return (os.environ.get('JOB_APPLY_TRANSMIT_ENABLED', '').strip().lower() == 'true'
            and bool(transmit_ats()))


def transmission_allowed(url):
    return transmission_enabled() and platform_id(url) in transmit_ats()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def threshold():
    """Match score an auto-apply step must reach before it runs unattended."""
    try:
        return float(os.environ.get('JOB_APPLY_AUTO_SUBMIT_MIN_SCORE', '90'))
    except ValueError:
        return 90.0


def validate_snapshot(snapshot):
    url = urlsplit(snapshot.get('url', ''))
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('Invalid employer form URL')
    if snapshot.get('action') not in ('continue', 'submit'):
        raise ValueError('Unknown form action')
    if not isinstance(snapshot.get('button'), str) or not snapshot['button']:
        raise ValueError('Missing form button')
    fields = snapshot.get('fields')
    if not isinstance(fields, list) or not fields or len(fields) > 100:
        raise ValueError('Form must contain 1 to 100 supported fields')
    ids = set()
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get('id'), str) or not field['id']:
            raise ValueError('Missing field identifier')
        if field['id'] in ids:
            raise ValueError('Duplicate field identifier')
        ids.add(field['id'])
        if not isinstance(field.get('label'), str) or not field['label']:
            raise ValueError('Unlabelled form control requires manual inspection')
        if field.get('type') not in ('text', 'email', 'tel', 'textarea', 'select', 'checkbox',
                                     'file', 'combobox', 'checkbox-group'):
            raise ValueError('Unsupported form control')
        if not isinstance(field.get('required'), bool):
            raise ValueError('Required status must be explicit')
        if field['type'] in ('select', 'combobox') and not isinstance(field.get('options'), list):
            raise ValueError('Select options missing')
        if field['type'] == 'checkbox-group':
            options = field.get('options')
            if not isinstance(options, list) or not options:
                raise ValueError('Checkbox group options missing')
            values = set()
            for option in options:
                if (not isinstance(option, dict) or not isinstance(option.get('value'), str)
                        or not option['value'] or not isinstance(option.get('label'), str)
                        or not option['label']):
                    raise ValueError('Checkbox group option needs a value and a label')
                if option['value'] in values:
                    raise ValueError('Duplicate checkbox group option')
                values.add(option['value'])


def valid_answer(field, value):
    if field['type'] == 'checkbox':
        return isinstance(value, bool) and (value or not field['required'])
    if field['type'] == 'checkbox-group':
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return False
        allowed = {option['value'] for option in field['options']}
        return (len(set(value)) == len(value) and set(value) <= allowed
                and (bool(value) or not field['required']))
    if field['type'] == 'file':
        return value in ('approved_resume.pdf', 'approved_resume.docx')
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        return False
    if field['type'] in ('select', 'combobox'):
        return value in field['options']
    return True


class FormQueue:
    @staticmethod
    def can_transmit(url):
        return transmission_allowed(url)

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS form_steps (
                id TEXT PRIMARY KEY, resume_digest TEXT NOT NULL,
                state TEXT NOT NULL, plan TEXT, version TEXT,
                message_id TEXT, error TEXT, receipt TEXT, target_url TEXT,
                auto INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL)''')
            if 'auto' not in {row[1] for row in db.execute('PRAGMA table_info(form_steps)')}:
                db.execute('ALTER TABLE form_steps ADD COLUMN auto INTEGER NOT NULL DEFAULT 0')

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE form_steps SET state='uncertain', message_id=NULL, "
                       "error='Worker restarted during transmission. Check employer before continuing.' "
                       "WHERE state='executing'")
            db.execute("UPDATE form_steps SET state='queued' WHERE state='inspecting'")

    def sync_approved(self):
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO form_steps(id,resume_digest,state,auto,updated) "
                       "SELECT id,digest,'queued',COALESCE(auto_apply,0),? FROM applications "
                       "WHERE state='resume_approved'", (stamp(),))

    def get(self, key):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM form_steps WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('Unknown form task')
        return dict(row)

    def set_target(self, key, url, actor, owner):
        self._owner(actor, owner)
        from jobapply_browser import allowed_intake_url
        allowed_intake_url(url)
        with self.store.connect() as db:
            changed = db.execute("UPDATE form_steps SET target_url=?,state='queued',plan=NULL,"
                                 "version=NULL,error=NULL,message_id=NULL,updated=? WHERE id=? "
                                 "AND state IN ('queued','needs_attention','review')",
                                 (url, stamp(), key)).rowcount
            if changed != 1:
                raise ValueError('Cannot change an approved, executing or uncertain application')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, 'set_form_destination', str(actor), stamp()))

    def list(self, states, undelivered=False):
        with self.store.connect() as db:
            sql = 'SELECT * FROM form_steps WHERE state IN (' + ','.join('?' for _ in states) + ')'
            if undelivered:
                sql += ' AND message_id IS NULL'
            return [dict(row) for row in db.execute(sql + ' ORDER BY updated', states)]

    def claim(self, key, before, after):
        with self.store.connect() as db:
            return db.execute('UPDATE form_steps SET state=?,updated=? WHERE id=? AND state=?',
                              (after, stamp(), key, before)).rowcount == 1

    def save_inspection(self, key, snapshot, answers=None):
        validate_snapshot(snapshot)
        row = self.get(key)
        app = self.store.get(key)
        if app['state'] != 'resume_approved' or app['digest'] != row['resume_digest']:
            raise ValueError('Resume approval is no longer current')
        # Only answers the owner wrote in the private profile are carried in;
        # no identity, legal, demographic or consent answer is ever guessed.
        prefilled = {}
        for field in snapshot['fields']:
            entry = (answers or {}).get(field['id'])
            if isinstance(entry, dict) and valid_answer(field, entry.get('value')):
                prefilled[field['id']] = {'value': entry['value'],
                                          'source': str(entry.get('source', 'profile'))}
        plan = {'resume_digest': row['resume_digest'], 'snapshot': snapshot, 'answers': prefilled}
        version = digest(plan)
        with self.store.connect() as db:
            changed = db.execute("UPDATE form_steps SET state='review',plan=?,version=?,"
                                 "message_id=NULL,error=NULL,updated=? WHERE id=? AND state='inspecting'",
                                 (json.dumps(plan), version, stamp(), key)).rowcount
            if changed != 1:
                raise ValueError('Inspection is no longer current')

    def answer(self, key, version, field_id, value, actor, owner):
        self._owner(actor, owner)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM form_steps WHERE id=? AND version=? AND state='review'",
                             (key, version)).fetchone()
            if not row:
                raise ValueError('This form review is outdated')
            plan = json.loads(row['plan'])
            field = next((f for f in plan['snapshot']['fields'] if f['id'] == field_id), None)
            if field is None or not valid_answer(field, value):
                raise ValueError('Answer does not match the field type or allowed choices')
            plan['answers'][field_id] = {'value': value, 'source': 'Discord owner ' + str(actor)}
            new_version = digest(plan)
            db.execute('UPDATE form_steps SET plan=?,version=?,message_id=NULL,updated=? WHERE id=?',
                       (json.dumps(plan), new_version, stamp(), key))
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, 'form_answer:' + new_version, str(actor), stamp()))
        return new_version

    def missing(self, plan):
        return [f['label'] for f in plan['snapshot']['fields'] if f['required'] and
                not valid_answer(f, plan['answers'].get(f['id'], {}).get('value'))]

    @staticmethod
    def _owner(actor, owner):
        if str(actor) != str(owner):
            raise ValueError('Only the configured owner can review this form')

    def decide(self, key, version, action, actor, owner):
        self._owner(actor, owner)
        if action not in ('approve', 'skip'):
            raise ValueError('Unknown form decision')
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM form_steps WHERE id=? AND version=? AND state='review'",
                             (key, version)).fetchone()
            if not row:
                raise ValueError('This form review is outdated or already handled')
            plan = json.loads(row['plan'])
            if action == 'approve' and self.missing(plan):
                raise ValueError('Answer all required fields before approval')
            if action == 'approve' and not transmission_allowed(plan['snapshot']['url']):
                raise ValueError(
                    'Transmission is disabled for this ATS. Inspection-only review remains active.')
            app = db.execute('SELECT * FROM applications WHERE id=?', (key,)).fetchone()
            if app['state'] != 'resume_approved' or app['digest'] != row['resume_digest']:
                raise ValueError('Resume approval changed')
            state = 'approved' if action == 'approve' else 'skipped'
            db.execute('UPDATE form_steps SET state=?,message_id=NULL,updated=? WHERE id=?', (state, stamp(), key))
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, 'form_' + action + ':' + version, str(actor), stamp()))
        return state

    def auto_advance(self, key):
        """Approve a step the owner pre-authorized, if nothing needs a human.

        Requires a complete plan and a match score at or above the configured
        threshold; anything weaker falls back to the ordinary review card.
        """
        row = self.get(key)
        if row['state'] != 'review' or not row['auto']:
            return False
        plan = json.loads(row['plan'])
        if not transmission_allowed(plan['snapshot']['url']):
            return False
        if self.missing(plan):
            return False
        score = (json.loads(self.store.get(key)['job']).get('fit') or {}).get('score')
        if not isinstance(score, (int, float)) or isinstance(score, bool) or score < threshold():
            return False
        with self.store.connect() as db:
            changed = db.execute("UPDATE form_steps SET state='approved',message_id=NULL,updated=? "
                                 "WHERE id=? AND version=? AND state='review'",
                                 (stamp(), key, row['version'])).rowcount
            if changed != 1:
                return False
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (key, 'form_auto_approve:' + row['version'], 'auto-apply', stamp()))
        return True

    def defer_transmission(self, key):
        """Return a stale approved step to review without touching the employer."""
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE form_steps SET state='review',message_id=NULL,"
                "error='Transmission disabled by deployment policy.',updated=? "
                "WHERE id=? AND state='approved'",
                (stamp(), key),
            ).rowcount
            if changed:
                db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                           (key, 'form_transmission_blocked', 'browser-worker', stamp()))
        return changed == 1

    def stop(self, key, error, uncertain=False):
        with self.store.connect() as db:
            db.execute('UPDATE form_steps SET state=?,error=?,message_id=NULL,updated=? WHERE id=?',
                       ('uncertain' if uncertain else 'needs_attention', error[:500], stamp(), key))

    def finish(self, key, receipt):
        with self.store.connect() as db:
            changed = db.execute("UPDATE form_steps SET state='submitted',receipt=?,message_id=NULL,"
                                 "updated=? WHERE id=? AND state='executing'",
                                 (json.dumps(receipt), stamp(), key)).rowcount
            if changed != 1:
                raise ValueError('No executing submission')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, 'submission_receipt', 'browser-worker', stamp()))

    def mark_card(self, row, message_id):
        with self.store.connect() as db:
            db.execute('UPDATE form_steps SET message_id=? WHERE id=? AND state=? AND version IS ?',
                       (str(message_id), row['id'], row['state'], row['version']))

