"""Owner-supplied answers for employer application forms.

Only values written in the private profile are offered. Nothing is inferred:
an unmatched question, and every sensitive one the owner did not name outright,
stays blank and still needs a Discord answer before approval.
"""
import json
import os
from pathlib import Path
from jobapply_forms import valid_answer

DEFAULT_PATH = 'profiles/apply_profile.json'

# Contact rules never answer these. Only an explicit `questions` entry can,
# so sponsorship, legal attestations and demographics are never guessed.
SENSITIVE = (
    'sponsor', 'visa', 'work authorization', 'authorized to work', 'right to work',
    'veteran', 'disability', 'race', 'ethnic', 'gender', 'hispanic', 'latino',
    'felony', 'convict', 'criminal', 'background check', 'clearance',
    'salary', 'compensation', 'desired pay', 'expected pay', 'notice period',
    'start date', 'available to start', 'reference',
    'consent', 'agree', 'certify', 'acknowledge', 'privacy', 'terms', 'opt in',
)

# Most specific first; the first hit wins.
RULES = (
    ('first_name', ('first name', 'given name', 'forename')),
    ('last_name', ('last name', 'surname', 'family name')),
    ('preferred_name', ('preferred name', 'preferred first name', 'nickname', 'goes by')),
    ('legal_name', ('legal name',)),
    ('full_name', ('full name', 'your name', 'candidate name')),
    ('email', ('email', 'e mail')),
    ('phone', ('phone', 'mobile number', 'telephone', 'cell number')),
    ('linkedin', ('linkedin',)),
    ('github', ('github',)),
    ('portfolio', ('portfolio', 'personal website', 'personal site', 'website', 'blog')),
    ('country', ('country',)),
    ('city', ('city', 'town')),
    ('state', ('state', 'province', 'region')),
    ('zip', ('zip', 'postal code', 'postcode')),
    ('location', ('location', 'where are you based', 'current residence')),
)


def normalize(text):
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in str(text).lower()).split())


def validate_profile(profile):
    """Fail loudly on a malformed profile instead of silently treating it as blank."""
    for section in ('identity', 'links', 'attachments'):
        value = profile.get(section)
        if value is not None and not (isinstance(value, dict) and
                all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
            raise ValueError(f'Apply profile "{section}" must be a flat object of strings')
    questions = profile.get('questions')
    if questions is not None:
        if not isinstance(questions, list):
            raise ValueError('Apply profile "questions" must be a list')
        for entry in questions:
            if (not isinstance(entry, dict) or not isinstance(entry.get('match'), str)
                    or not entry['match'].strip()):
                raise ValueError('Each apply profile question needs a non-empty "match" string')
            value = entry.get('value')
            usable = (isinstance(value, (str, bool)) or
                     (isinstance(value, list) and all(isinstance(v, str) for v in value)))
            if not usable:
                raise ValueError('Apply profile question values must be a string, boolean or list of strings')
    return profile


def load_profile(path=None):
    """Read the profile from a file, or whole from JOB_APPLY_PROFILE_JSON.

    A deployed service has no gitignored file to read, so the same JSON can be
    supplied as one secret environment variable instead.
    """
    raw = os.environ.get('JOB_APPLY_PROFILE_JSON', '').strip()
    if path is None and raw:
        try:
            profile = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError('JOB_APPLY_PROFILE_JSON is not valid JSON') from exc
    else:
        target = Path(path or os.environ.get('JOB_APPLY_PROFILE', DEFAULT_PATH))
        if not target.exists():
            return {}
        with target.open(encoding='utf-8') as handle:
            profile = json.load(handle)
    if not isinstance(profile, dict):
        raise ValueError('Apply profile must be a JSON object')
    return validate_profile(profile)


def contacts(profile):
    table = {key: value.strip() for key, value in
             ((profile.get('identity') or {}) | (profile.get('links') or {})).items()
             if isinstance(value, str) and value.strip()}
    if 'full_name' not in table and table.get('first_name') and table.get('last_name'):
        table['full_name'] = table['first_name'] + ' ' + table['last_name']
    if 'legal_name' not in table and table.get('full_name'):
        table['legal_name'] = table['full_name']
    if 'preferred_name' not in table and table.get('first_name'):
        table['preferred_name'] = table['first_name']
    return table


def stated(profile, label):
    """An answer the owner wrote for this exact question, or None."""
    for entry in profile.get('questions') or []:
        if not isinstance(entry, dict) or 'value' not in entry:
            continue
        needle = normalize(entry.get('match', ''))
        if needle and needle in label:
            return entry['value']
    return None


def candidate(field, profile, table):
    label = normalize(field.get('label') or '')
    if field['type'] == 'file':
        # Only the locked resume is ever attached, and only to a resume control.
        if 'resume' in label or 'cv' in label.split():
            return (profile.get('attachments') or {}).get('resume', 'approved_resume.pdf'), 'attachments.resume'
        return None, None
    answer = stated(profile, label)
    if answer is not None:
        return answer, 'questions'
    if any(word in label for word in SENSITIVE):
        return None, None
    if field['type'] in ('checkbox', 'checkbox-group'):
        return None, None
    for key, keywords in RULES:
        if key in table and any(word in label for word in keywords):
            return table[key], 'identity.' + key
    return None, None


def resolve(field, value):
    """Map a written answer onto exact employer option(s), or None."""
    if field['type'] in ('select', 'combobox'):
        if not isinstance(value, str):
            return None
        wanted = normalize(value)
        return next((option for option in field.get('options') or []
                     if normalize(option) == wanted), None)
    if field['type'] == 'checkbox-group':
        if not isinstance(value, list):
            return None
        resolved = []
        for item in value:
            if not isinstance(item, str):
                return None
            wanted = normalize(item)
            match = next((option['value'] for option in field.get('options') or []
                         if normalize(option['label']) == wanted or normalize(option['value']) == wanted), None)
            if match is None:
                return None
            resolved.append(match)
        return resolved
    return value


def prefill(snapshot, profile):
    """Return profile-sourced answers plus the required labels still blank."""
    table = contacts(profile)
    answers, blank = {}, []
    for field in snapshot['fields']:
        value, source = candidate(field, profile, table)
        if value is not None:
            value = resolve(field, value)
        if value is None or not valid_answer(field, value):
            if field['required']:
                blank.append(field['label'])
            continue
        answers[field['id']] = {'value': value, 'source': 'profile:' + source}
    return answers, blank

