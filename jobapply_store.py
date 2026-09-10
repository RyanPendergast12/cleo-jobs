"""Private worker database. Keep on a persistent volume, never commit to Git."""
import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, job TEXT NOT NULL, state TEXT NOT NULL,
                packet TEXT, resume BLOB, resume_docx BLOB, digest TEXT,
                message_id TEXT, approved_by TEXT, prepare_error TEXT,
                prepare_attempts INTEGER NOT NULL DEFAULT 0,
                prepare_after TEXT, revision_request TEXT,
                updated TEXT NOT NULL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(applications)')}
            migrations = {
                'resume_docx': 'BLOB',
                'prepare_error': 'TEXT',
                'prepare_attempts': 'INTEGER NOT NULL DEFAULT 0',
                'prepare_after': 'TEXT',
                'revision_request': 'TEXT',
                'auto_apply': 'INTEGER NOT NULL DEFAULT 0',
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE applications ADD COLUMN {name} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS audit (id TEXT, action TEXT, actor TEXT, at TEXT)')

    @contextmanager
    def connect(self):
        """Yield a connection inside a transaction, then always close it.

        `with db:` gives commit-on-success / rollback-on-exception; the outer
        finally guarantees the OS file handle is released (so tempdir cleanup
        on Windows doesn't hit WinError 32, and the long-lived service doesn't
        leak a handle per call)."""
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key, job):
        from types import SimpleNamespace
        from jobwatch_apply import eligible
        if not key or len(key) > 64 or not key.isalnum() or not eligible(SimpleNamespace(fit=job.get('fit'))):
            raise ValueError('Invalid candidate or below threshold / eligibility blocker')
        if not job.get('url', '').startswith('https://') or not job.get('title'):
            raise ValueError('Missing job URL/title')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO applications(id,job,state,updated) VALUES (?,?,?,?)',
                       (key, json.dumps(job), 'awaiting_skill', stamp()))
        return key

    def get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM applications WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('Unknown application')
            return dict(row)

    def pending_cards(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM applications WHERE message_id IS NULL AND state IN ('awaiting_skill','draft_ready','ready')")]

    def pending_skill(self, limit=1):
        now = stamp()
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM applications WHERE state IN ('awaiting_skill','changes_requested') "
                "AND prepare_attempts < 3 AND (prepare_after IS NULL OR prepare_after <= ?) "
                "ORDER BY updated LIMIT ?", (now, limit))]

    def lease_skill(self, key, until):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE applications SET prepare_after=?,prepare_attempts=prepare_attempts+1,updated=? "
                "WHERE id=? AND state IN ('awaiting_skill','changes_requested') AND prepare_attempts < 3",
                (until, stamp(), key)).rowcount
            return changed == 1

    def skill_failed(self, key, error, retry_after):
        safe = str(error).replace('\n', ' ')[:500]
        with self.connect() as db:
            db.execute(
                "UPDATE applications SET prepare_error=?,prepare_after=?,updated=? "
                "WHERE id=? AND state IN ('awaiting_skill','changes_requested')",
                (safe, retry_after, stamp(), key))
        return safe

    def retry_skill(self, key, actor, owner):
        if str(actor) != str(owner):
            raise ValueError('Only the configured owner can retry')
        with self.connect() as db:
            changed = db.execute(
                "UPDATE applications SET prepare_attempts=0,prepare_after=NULL,"
                "prepare_error=NULL,updated=? WHERE id=? "
                "AND state IN ('awaiting_skill','changes_requested') "
                "AND prepare_error IS NOT NULL",
                (stamp(), key)).rowcount
            if changed != 1:
                raise ValueError('This preparation is not waiting for a retry')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (key, 'retry_preparation', str(actor), stamp()))
        return self.get(key)['state']

    def skip_pending(self, key, actor, owner):
        if str(actor) != str(owner):
            raise ValueError('Only the configured owner can skip')
        with self.connect() as db:
            changed = db.execute(
                "UPDATE applications SET state='skipped',approved_by=?,updated=? "
                "WHERE id=? AND state IN ('awaiting_skill','changes_requested')",
                (str(actor), stamp(), key)).rowcount
            if changed != 1:
                raise ValueError('This preparation is outdated or already handled')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (key, 'skip_pending', str(actor), stamp()))
        return 'skipped'

    def mark_card(self, key, state, digest, message_id):
        with self.connect() as db:
            db.execute('UPDATE applications SET message_id=? WHERE id=? AND state=? AND digest IS ?',
                       (str(message_id), key, state, digest))

    def _validate_artifacts(self, packet, resume, resume_docx=None, require_complete=True):
        # Contract for the future skill adapter. Source references are mandatory;
        # they are not a substitute for the adapter checking the actual evidence.
        if not resume.startswith(b'%PDF-') or len(resume) > 7_000_000:
            raise ValueError('Provide a PDF resume under 7 MB')
        if resume_docx is not None:
            if len(resume_docx) > 7_000_000:
                raise ValueError('Provide a DOCX resume under 7 MB')
            try:
                with zipfile.ZipFile(io.BytesIO(resume_docx)) as archive:
                    if 'word/document.xml' not in archive.namelist():
                        raise ValueError('Invalid DOCX resume')
            except zipfile.BadZipFile as exc:
                raise ValueError('Invalid DOCX resume') from exc
        if not packet.get('skill_version') or not packet.get('evidence') or not packet.get('resume_claims'):
            raise ValueError('Skill version, evidence, and resume claims required')
        if require_complete and (packet.get('missing_fields') or packet.get('form_complete') is not True):
            raise ValueError('Resolve required fields before approval')
        if not require_complete and (packet.get('form_complete') is not False or not packet.get('missing_fields')):
            raise ValueError('Draft must disclose unresolved employer form fields')
        evidence = packet['evidence']
        if not isinstance(evidence, dict) or not all(isinstance(v, str) and v.strip() for v in evidence.values()):
            raise ValueError('Evidence must map IDs to source references')
        answers = packet.get('answers')
        if not isinstance(answers, dict):
            raise ValueError('Answers must be a dictionary')
        for item in list(answers.values()) + packet['resume_claims']:
            if not isinstance(item, dict) or not item.get('value') or not item.get('evidence_ids'):
                raise ValueError('Each answer and resume claim needs evidence')
            if any(ref not in evidence for ref in item['evidence_ids']):
                raise ValueError('Unknown evidence reference')

    def _digest(self, packet, resume, resume_docx=None):
        canonical = json.dumps(packet, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(canonical.encode() + b'\0' + resume + b'\0' + (resume_docx or b'')).hexdigest()
        return canonical, digest

    def save_draft(self, key, packet, resume, resume_docx):
        self._validate_artifacts(packet, resume, resume_docx, require_complete=False)
        current = self.get(key)
        if packet.get('job_url') != json.loads(current['job'])['url']:
            raise ValueError('Packet job URL mismatch')
        canonical, digest = self._digest(packet, resume, resume_docx)
        with self.connect() as db:
            changed = db.execute(
                "UPDATE applications SET packet=?,resume=?,resume_docx=?,digest=?,state='draft_ready',"
                "message_id=NULL,approved_by=NULL,prepare_error=NULL,prepare_after=NULL,"
                "prepare_attempts=0,revision_request=NULL,updated=? "
                "WHERE id=? AND state IN ('awaiting_skill','draft_ready','changes_requested')",
                (canonical, resume, resume_docx, digest, stamp(), key)).rowcount
            if not changed:
                raise ValueError('Cannot replace an approved or skipped packet')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (key, 'drafted:' + digest, 'skill-adapter', stamp()))
        return digest

    def prepare(self, key, packet, resume, resume_docx=None):
        self._validate_artifacts(packet, resume, resume_docx, require_complete=True)
        current = self.get(key)
        if packet.get('job_url') != json.loads(current['job'])['url']:
            raise ValueError('Packet job URL mismatch')
        canonical, digest = self._digest(packet, resume, resume_docx)
        with self.connect() as db:
            changed = db.execute("UPDATE applications SET packet=?,resume=?,resume_docx=?,digest=?,state='ready',message_id=NULL,approved_by=NULL,updated=? WHERE id=? AND state IN ('awaiting_skill','draft_ready','ready','changes_requested')",
                                 (canonical, resume, resume_docx, digest, stamp(), key)).rowcount
            if not changed:
                raise ValueError('Cannot replace an approved or skipped packet')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, 'prepared:' + digest, 'skill-adapter', stamp()))
        return digest

    def decide_draft(self, key, digest, action, actor, owner, changes=None, auto=False):
        if str(actor) != str(owner):
            raise ValueError('Only the configured owner can review')
        target = {'approve': 'resume_approved',
                  'changes': 'changes_requested',
                  'skip': 'skipped'}.get(action)
        if target is None:
            raise ValueError('Unknown action')
        if auto and action != 'approve':
            raise ValueError('Only a resume approval can authorize auto-apply')
        revision = None
        if action == 'changes':
            revision = (changes or '').strip()
            if not 5 <= len(revision) <= 1000:
                raise ValueError('Describe the requested changes in 5 to 1000 characters')
        approved_by = str(actor) if action in {'approve', 'skip'} else None
        with self.connect() as db:
            changed = db.execute(
                "UPDATE applications SET state=?,approved_by=?,revision_request=?,auto_apply=?,"
                "prepare_attempts=0,prepare_after=NULL,prepare_error=NULL,updated=? "
                "WHERE id=? AND digest=? AND state='draft_ready'",
                (target, approved_by, revision, int(auto), stamp(), key, digest)).rowcount
            if changed != 1:
                raise ValueError('This draft is outdated or already handled')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (key, 'draft_' + action + ('_auto' if auto else '') + ':' + digest,
                        str(actor), stamp()))
        return target

    def decide(self, key, digest, action, actor, owner):
        if str(actor) != str(owner):
            raise ValueError('Only the configured owner can approve')
        target = {'apply': 'approved_waiting_adapter', 'changes': 'changes_requested', 'skip': 'skipped'}.get(action)
        if target is None:
            raise ValueError('Unknown action')
        with self.connect() as db:
            changed = db.execute("UPDATE applications SET state=?,approved_by=?,updated=? WHERE id=? AND digest=? AND state='ready'",
                                 (target, str(actor), stamp(), key, digest)).rowcount
            if changed != 1:
                raise ValueError('This review is outdated or already handled')
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (key, action + ':' + digest, str(actor), stamp()))
        return target

