"""Opt-in, registry-routed employer application browser adapter.

Every transmitting step needs an approval: a Discord click, or the owner's
prior Approve & auto-apply on a match above the configured score threshold.
Install Chromium separately; profiles and receipts belong on the private disk.
Unsupported controls, authentication and challenges require human attention.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from jobapply_ats import REQUESTABLE, destination, platform_id, request_allowed
from jobapply_forms import digest, transmission_allowed
from jobapply_profile import load_profile, prefill
from jobapply_store import stamp


def platform(url):
    return platform_id(url, allow_source=True)


def allowed_intake_url(url):
    """Accept a registered direct ATS link or a resolvable source-board link."""
    destination(url, allow_source=True)
    return url


def allowed_url(url):
    """Accept only a registered direct ATS form, never an aggregator page."""
    destination(url, allow_source=False)
    return url


def resolve_linkedin(page):
    """Follow LinkedIn's external Apply link to a registered employer ATS.

    Easy Apply stays on LinkedIn and needs a separate authenticated adapter;
    this resolver never signs in, fills a field, or clicks an Easy Apply flow.
    """
    links = page.eval_on_selector_all(
        'a[href]',
        """links => links.map(link => ({
          href: link.href,
          label: (link.innerText || link.getAttribute('aria-label') || '').trim()
        }))""",
    )
    seen = set()
    for link in links:
        href = link.get('href', '')
        label = link.get('label', '').lower()
        if not href or href in seen or 'apply' not in label:
            continue
        seen.add(href)
        try:
            found = destination(href, allow_source=True)
        except ValueError:
            continue
        if found.kind == 'direct' or '/externalapply/' in urlsplit(href).path.lower():
            page.goto(href, wait_until='domcontentloaded')
            page.wait_for_timeout(1500)
            allowed_url(page.url)
            return page.url
    raise ValueError(
        'LinkedIn Easy Apply or login needs manual handling; provide the direct employer ATS URL'
    )


# Deliberately ignores hidden token controls and never reads existing answers.
SNAPSHOT_JS = r'''() => {
 const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
 const clean = text => (text || '').replace(/\s+/g, ' ').trim().slice(0, 300);
 const described = el => (el.getAttribute('aria-labelledby') || '').split(/\s+/)
   .map(id => id && document.getElementById(id) ? document.getElementById(id).innerText : '').join(' ');
 const labelFor = el => {
   const wrapper = el.closest('label');
   return clean(el.getAttribute('aria-label')) || clean([...(el.labels || [])].map(l=>l.innerText).join(' '))
     || clean(el.placeholder) || clean(described(el)) || clean(wrapper && wrapper.innerText);
 };
 // React combobox/autosize widgets leave decoy inputs behind (an off-screen
 // sizer, a search proxy pulled out of tab order); they carry no real answer.
 const decoy = el => el.getAttribute('aria-hidden') === 'true' || el.tabIndex === -1;
 const candidates = [...document.querySelectorAll('input,select,textarea')]
   // A resume control is routinely hidden behind a styled Attach button.
   .filter(el => (visible(el) || el.type === 'file') && !el.disabled
     && !['hidden','submit','button','image','reset'].includes(el.type) && !decoy(el));
 const groups = new Map();
 const singles = [];
 candidates.forEach(el => {
   if (el.type === 'checkbox' && el.name) {
     const peers = candidates.filter(o => o.type === 'checkbox' && o.name === el.name);
     if (peers.length > 1) { groups.set(el.name, peers); return; }
   }
   singles.push(el);
 });
 const fields = singles.map(el => {
   const label = labelFor(el) || (el.type === 'file' ? clean((el.name || el.id || '').replace(/[_-]+/g, ' ')) : '');
   const role = el.getAttribute('role');
   return {id: el.id || el.name || (label ? 'label:' + label : ''), label,
     type: role === 'combobox' ? 'combobox' : el.tagName === 'SELECT' ? 'select'
       : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text'),
     required: el.required || el.getAttribute('aria-required') === 'true',
     options: el.tagName === 'SELECT' ? [...el.options].filter(o=>!o.disabled && o.value).map(o=>o.value) : []};
 });
 groups.forEach((members, name) => {
   const fieldset = members[0].closest('fieldset');
   const legend = fieldset && fieldset.querySelector('legend');
   const label = clean(legend && legend.innerText) || clean(fieldset && fieldset.getAttribute('aria-label'))
     || clean(described(fieldset || members[0]));
   fields.push({id: 'group:' + name, label, type: 'checkbox-group',
     required: members.some(el => el.required || el.getAttribute('aria-required') === 'true'),
     options: members.map(el => ({value: el.id || el.value, label: labelFor(el)}))});
 });
 const buttons = [...document.querySelectorAll('button,input[type=submit]')].filter(visible)
   .map(el => (el.innerText || el.value || '').trim());
 return {fields, buttons};
}'''

NEXT_BUTTONS = ('continue', 'next', 'submit application', 'submit my application')
# Greenhouse and Ashby wording overlap heavily; Ashby's exact confirmation
# copy is unverified against a live posting, so this stays a superset until
# checked. Unlike NEXT_BUTTONS, an unmatched phrase here never misfires a
# click — it only leaves a real submission in the safer 'uncertain' state
# instead of recording a receipt, so widening this list carries no new risk.
RECEIPT_PHRASES = ('your application has been submitted', 'thank you for applying',
                   'thanks for applying', 'application successfully submitted',
                   'application submitted', 'application received',
                   'we have received your application', "we've received your application")

# Types this adapter can safely read and later fill. A combobox is Greenhouse's
# custom country/yes-no control (an input[role=combobox] paired with a listbox
# opened on demand); checkbox-group is a fieldset of related checkboxes such as
# "which clearances do you hold" that must be answered as one selection.
SUPPORTED_TYPES = ('text', 'email', 'tel', 'textarea', 'select', 'checkbox',
                   'file', 'combobox', 'checkbox-group')

# These are navigation-only actions used during inspection when an ATS job
# page precedes the actual form.  They are never used by execute_step(), so an
# approved transmission can click only the version-bound button in its plan.
ENTRY_ACTIONS = {
    'workday': ('apply', 'apply now', 'start your application'),
    'lever': ('apply for this job',),
    'smartrecruiters': ('apply', 'apply now'),
    'icims': ('apply for this job online', 'apply now'),
    'bamboohr': ('apply for this job', 'apply now'),
}


def enter_application_form(page):
    """Navigate from a registered ATS job page to its form, without filling."""
    ats = platform(page.url)
    labels = ENTRY_ACTIONS.get(ats)
    if not labels:
        return False
    raw = page.evaluate(SNAPSHOT_JS)
    if raw.get('fields'):
        return False
    matches = []
    for role in ('link', 'button'):
        for label in labels:
            control = page.get_by_role(
                role, name=re.compile(r'^' + re.escape(label) + r'$', re.IGNORECASE)
            )
            if control.count() == 1:
                matches.append(control)
            elif control.count() > 1:
                raise ValueError('Cannot unambiguously identify the employer Apply action')
    if len(matches) != 1:
        raise ValueError('Cannot unambiguously identify the employer Apply action')
    matches[0].click()
    page.wait_for_timeout(1500)
    allowed_url(page.url)
    return True


def field_locator(page, field):
    # Escape as a CSS string, never interpolate raw field IDs into executable JS.
    key = json.dumps(field['id'])
    locator = (page.get_by_label(field['label'], exact=True) if field['id'].startswith('label:')
               else page.locator(f'[id={key}], [name={key}]'))
    if locator.count() != 1:
        raise ValueError('Field locator is no longer unique')
    return locator


def checkbox_option_locator(page, name, value):
    name_key, value_key = json.dumps(name), json.dumps(value)
    locator = page.locator(f'[id={value_key}], input[type=checkbox][name={name_key}][value={value_key}]')
    if locator.count() != 1:
        raise ValueError('Checkbox group option is no longer unique')
    return locator


def combobox_options(page, control):
    """Open a Greenhouse-style combobox just long enough to read its choices.

    The listbox is frequently mounted on demand (a portal, not a descendant of
    the trigger), so it is located generically by role rather than by DOM
    position, and closed again before any value is chosen.
    """
    listbox_id = control.get_attribute('aria-controls') or control.get_attribute('aria-owns')
    control.click()
    scope = page.locator(f'[id={json.dumps(listbox_id)}] [role="option"]') if listbox_id else page.locator('[role="option"]')
    try:
        scope.first.wait_for(state='visible', timeout=5000)
        options = [text.strip() for text in scope.all_inner_texts() if text.strip()]
    finally:
        page.keyboard.press('Escape')
    if not options:
        raise ValueError('Combobox exposed no selectable options')
    return options


def inspect(page):
    allowed_url(page.url)
    text = page.locator('body').inner_text()
    if any(word in text.lower() for word in ('verify you are human', 'checking your browser', 'sign in to continue', 'verification code')):
        raise ValueError('Employer requires login or human verification; no automatic bypass')
    if page.locator('input[type=password]').count():
        raise ValueError('Employer login needs manual handling')
    raw = page.evaluate(SNAPSHOT_JS)
    fields = []
    for candidate in raw['fields']:
        if candidate['type'] not in SUPPORTED_TYPES:
            if candidate['required']:
                raise ValueError('Employer uses a required control this adapter does not support yet')
            continue  # An optional control we cannot fill is simply left alone.
        if not candidate['id'] or not candidate['label']:
            if candidate['required']:
                raise ValueError('Required control has no accessible label; manual inspection needed')
            continue  # An unlabeled optional control is almost always a decoy.
        fields.append(candidate)
    for candidate in fields:
        if candidate['type'] == 'combobox':
            candidate['options'] = combobox_options(page, field_locator(page, candidate))
    choices = [b for b in raw['buttons'] if b.lower() in NEXT_BUTTONS]
    if len(choices) != 1:
        raise ValueError('Cannot unambiguously identify the next employer action')
    button = choices[0]
    return {'url': page.url, 'fields': fields, 'button': button,
            'action': 'submit' if button.lower().startswith('submit') else 'continue'}


def select_combobox(page, control, value):
    """Open a Greenhouse combobox and click its matching option; never type-and-hope."""
    control.click()
    option = page.get_by_role('option', name=value, exact=True)
    if option.count() != 1:
        raise ValueError('Combobox option is no longer unique')
    option.click()
    if (control.input_value() or '').strip() != value.strip():
        raise ValueError('Combobox did not register the selected option')


def fill_checkbox_group(page, field, values):
    """Set every option in a related-checkbox group explicitly, selected or not."""
    name = field['id'].split(':', 1)[1]
    for option in field['options']:
        control = checkbox_option_locator(page, name, option['value'])
        control.set_checked(option['value'] in values)


def execute_step(page, plan, app):
    """Return receipt only after an explicit success message; otherwise next snapshot.

    Exceptions after this function begins are uncertain, not retryable.
    """
    if not transmission_allowed(plan.get('snapshot', {}).get('url')):
        raise ValueError('Employer transmission is disabled by deployment policy')
    current = inspect(page)
    if digest(current) != digest(plan['snapshot']):
        raise ValueError('Employer form changed since approval; inspect and approve again')
    for field in current['fields']:
        answer = plan['answers'].get(field['id'])
        if field['type'] == 'checkbox-group':
            # No answer still means every option gets an explicit, deliberate state.
            fill_checkbox_group(page, field, answer['value'] if answer else [])
            continue
        control = field_locator(page, field)
        if not answer:
            # Optional consent must not inherit an employer-selected default.
            if field['type'] == 'checkbox':
                control.set_checked(False)
            elif field['type'] != 'file':
                # Existing autofill must not transmit unreviewed personal data.
                if control.input_value():
                    raise ValueError('Unreviewed prefilled field; manual inspection required')
            continue
        value = answer['value']
        if field['type'] == 'file':
            docx = value == 'approved_resume.docx'
            payload = app['resume_docx'] if docx else app['resume']
            if not payload:
                raise ValueError('Approved resume attachment unavailable')
            control.set_input_files({'name': value,
                                    'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' if docx else 'application/pdf',
                                    'buffer': payload})
        elif field['type'] == 'checkbox':
            control.set_checked(value)
        elif field['type'] == 'select':
            control.select_option(value)
        elif field['type'] == 'combobox':
            select_combobox(page, control, value)
        else:
            control.fill(value)
    if digest(inspect(page)) != digest(current):
        raise ValueError('Answers revealed different questions; new approval required')
    # Do not interact with, solve or bypass a human-verification widget.
    challenge = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"]')
    if any(challenge.nth(i).is_visible() for i in range(challenge.count())):
        raise ValueError('Human verification requires manual handling')
    before_text = page.locator('body').inner_text().lower()
    page.get_by_role('button', name=current['button'], exact=True).click()
    page.wait_for_timeout(1500)
    allowed_url(page.url)
    text = page.locator('body').inner_text()
    if current['action'] == 'submit' and any(p in text.lower() and p not in before_text for p in RECEIPT_PHRASES):
        return {'receipt': {'url': page.url, 'at': stamp(), 'text': text[:12000], 'version': digest(plan)}}
    next_snapshot = inspect(page)
    if current['action'] == 'submit' or digest(next_snapshot) == digest(current):
        raise ValueError('Employer outcome is not confirmed; check before any further action')
    return {'snapshot': next_snapshot}


def record(queue, key, snapshot):
    """Store the inspected step with the owner's own profile answers filled in."""
    answers, _ = prefill(snapshot, load_profile())
    queue.save_inspection(key, snapshot, answers)
    queue.auto_advance(key)


def run_one(queue):
    from playwright.sync_api import sync_playwright
    queue.sync_approved()
    tasks = queue.list(('approved','queued'))
    if not tasks:
        return
    row = tasks[0]
    sending = row['state'] == 'approved'
    if sending:
        plan = json.loads(row['plan'])
        if not transmission_allowed(plan.get('snapshot', {}).get('url')):
            queue.defer_transmission(row['id'])
            return
    claimed = 'executing' if sending else 'inspecting'
    if not queue.claim(row['id'], row['state'], claimed):
        return
    try:
        app = queue.store.get(row['id'])
        if app['state'] != 'resume_approved' or app['digest'] != row['resume_digest']:
            raise ValueError('Approved resume version changed')
        plan = json.loads(row['plan']) if sending else None
        job = json.loads(app['job'])
        target = plan['snapshot']['url'] if sending else row['target_url'] or job.get('application_url') or job['url']
        intake = destination(target, allow_source=not sending)
        if sending and intake.kind != 'direct':
            raise ValueError('A source-board URL can never be used for transmission')
        root = Path(os.environ.get('JOB_APPLY_BROWSER_DATA', str(Path(queue.store.path).parent / 'browser')))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(root / row['id']), headless=True,
                                                            accept_downloads=False)
            try:
                # Only the active source/ATS and its declared support origins
                # can receive requests.  LinkedIn resolution updates this
                # scope, then reloads the direct ATS page under its own policy.
                active = {'platform': intake.platform, 'host': intake.host}
                def guard(route):
                    host = urlsplit(route.request.url).hostname or ''
                    if request_allowed(host, active['platform'], active['host']):
                        route.continue_()
                    else:
                        route.abort()
                context.route('**/*', guard)
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(20000)
                page.goto(target, wait_until='domcontentloaded')
                page.wait_for_timeout(1500)
                if intake.kind == 'source':
                    direct_url = resolve_linkedin(page)
                    resolved = destination(direct_url, allow_source=False)
                    active.update(platform=resolved.platform, host=resolved.host)
                    # The redirect was loaded under LinkedIn's request policy;
                    # reload once so only the resolved ATS's declared assets run.
                    page.goto(direct_url, wait_until='domcontentloaded')
                    page.wait_for_timeout(1500)
                if platform(page.url) == 'subway':
                    link = page.get_by_role('link', name='Apply For This Role', exact=True)
                    destination = link.get_attribute('href')
                    allowed_url(destination)
                    page.goto(destination, wait_until='domcontentloaded')
                    page.wait_for_timeout(1500)
                if not sending:
                    enter_application_form(page)
                if sending:
                    outcome = execute_step(page, plan, app)
                    if 'receipt' in outcome:
                        queue.finish(row['id'], outcome['receipt'])
                    else:
                        queue.claim(row['id'], 'executing', 'inspecting')
                        record(queue, row['id'], outcome['snapshot'])
                else:
                    record(queue, row['id'], inspect(page))
            finally:
                context.close()
    except Exception as exc:
        # Do not copy browser exception URLs, cookies or submitted values into Discord.
        note = str(exc) if isinstance(exc, ValueError) else 'Browser unavailable or employer page could not be inspected'
        queue.stop(row['id'], note, uncertain=sending)

