"""Opt-in Chromium smoke tests against local fixture HTML, never employers."""
import os
import unittest
from unittest.mock import patch
from jobapply_browser import inspect, execute_step, select_combobox, fill_checkbox_group, field_locator


@unittest.skipUnless(os.environ.get('JOB_APPLY_BROWSER_TESTS') == '1', 'Requires installed Chromium')
class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True)
        self.page = self.browser.new_page()
        # Fixtures use about:blank; production destinations remain allowlisted.
        self.url_patch = patch('jobapply_browser.allowed_url')
        self.url_patch.start()
        self.transmission_patch = patch(
            'jobapply_browser.transmission_allowed', return_value=True)
        self.transmission_patch.start()

    def tearDown(self):
        self.transmission_patch.stop()
        self.url_patch.stop()
        self.browser.close()
        self.runtime.stop()

    def form(self, button, outcome):
        self.page.set_content('''<form>
            <label for="email">Email</label><input id="email" type="email" required>
            <label><input type="checkbox" checked>Text me</label>
            <button type="submit">''' + button + '''</button></form>
            <script>document.querySelector('form').onsubmit = event => {
              event.preventDefault();
              window.sentEmail = document.querySelector('#email').value;
              window.sentConsent = document.querySelector('input[type=checkbox]').checked;
              document.body.innerHTML = ''' + repr(outcome) + ''';
            };</script>''')
        schema = inspect(self.page)
        return {'snapshot': schema, 'resume_digest':'test',
                'answers': {'email': {'value':'alex@example.com','source':'test owner'}}}

    def test_continue_requires_new_review_and_unchecks_optional_consent(self):
        plan = self.form('Continue', '<label for="q">Question</label><textarea id="q" required></textarea><button>Submit application</button>')
        result = execute_step(self.page, plan, {})
        self.assertEqual(result['snapshot']['action'], 'submit')
        self.assertEqual(self.page.evaluate('window.sentEmail'), 'alex@example.com')
        self.assertFalse(self.page.evaluate('window.sentConsent'))

    def test_final_submission_records_new_confirmation(self):
        plan = self.form('Submit application', '<p>Your application has been submitted</p>')
        result = execute_step(self.page, plan, {})
        self.assertIn('Your application has been submitted', result['receipt']['text'])

    def test_unchanged_page_is_not_treated_as_success(self):
        plan = self.form('Submit application', '<label for="email">Email</label><input id="email" type="email" required><button>Submit application</button>')
        with self.assertRaisesRegex(ValueError, 'not confirmed'):
            execute_step(self.page, plan, {})


@unittest.skipUnless(os.environ.get('JOB_APPLY_BROWSER_TESTS') == '1', 'Requires installed Chromium')
class VoyagerControlTests(unittest.TestCase):
    """Greenhouse's Voyager-era React controls: comboboxes, checkbox groups,
    number inputs, and the decoy inputs those widgets leave behind."""

    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True)
        self.page = self.browser.new_page()
        self.url_patch = patch('jobapply_browser.allowed_url')
        self.url_patch.start()

    def tearDown(self):
        self.url_patch.stop()
        self.browser.close()
        self.runtime.stop()

    COMBOBOX = '''<div class="select-shell">
      <label id="{id}-label" for="{id}">{label}</label>
      <div>
        <input id="{id}" role="combobox" aria-haspopup="listbox" aria-expanded="false"
               aria-controls="{id}-listbox" aria-autocomplete="list" autocomplete="off" {required}>
        <input aria-hidden="true" tabindex="-1" readonly style="position:absolute;opacity:0;">
        <ul id="{id}-listbox" role="listbox" hidden>{options}</ul>
      </div>
    </div>'''

    def combobox(self, field_id, label, options, required=True):
        items = ''.join(f'<li role="option" id="{field_id}-opt-{i}">{o}</li>' for i, o in enumerate(options))
        return self.COMBOBOX.format(id=field_id, label=label, options=items,
                                    required='required' if required else '')

    def wire_comboboxes(self):
        return '''<script>
          document.querySelectorAll('input[role=combobox]').forEach(input => {
            const listbox = document.getElementById(input.getAttribute('aria-controls'));
            input.addEventListener('click', () => { listbox.hidden = false; input.setAttribute('aria-expanded', 'true'); });
            input.addEventListener('keydown', e => { if (e.key === 'Escape') { listbox.hidden = true; input.setAttribute('aria-expanded', 'false'); } });
            listbox.querySelectorAll('[role=option]').forEach(opt => {
              opt.addEventListener('click', () => {
                input.value = opt.innerText; listbox.hidden = true; input.setAttribute('aria-expanded', 'false');
              });
            });
          });
        </script>'''

    def test_optional_number_field_is_dropped_without_stopping_the_run(self):
        self.page.set_content('''<form>
          <label for="grad">Expected Graduation Year</label><input id="grad" type="number">
          <label for="e">Email</label><input id="e" type="email" required>
          <button>Continue</button></form>''')
        snapshot = inspect(self.page)
        self.assertNotIn('grad', [f['id'] for f in snapshot['fields']])

    def test_required_number_field_stops_the_run(self):
        self.page.set_content('''<form>
          <label for="grad">Expected Graduation Year</label><input id="grad" type="number" required>
          <button>Continue</button></form>''')
        with self.assertRaisesRegex(ValueError, 'does not support'):
            inspect(self.page)

    def test_required_unlabeled_control_stops_the_run(self):
        self.page.set_content('<form><input id="mystery" required><button>Continue</button></form>')
        with self.assertRaisesRegex(ValueError, 'no accessible label'):
            inspect(self.page)

    def test_country_combobox_is_read_and_filled_without_typing_free_text(self):
        self.page.set_content('<form>' + self.combobox('country', 'Country', ['United States', 'Canada']) +
                              '<button>Continue</button></form>' + self.wire_comboboxes())
        snapshot = inspect(self.page)
        field = next(f for f in snapshot['fields'] if f['id'] == 'country')
        self.assertEqual(field['type'], 'combobox')
        self.assertEqual(field['options'], ['United States', 'Canada'])
        # The listbox must be closed again after inspection reads it.
        self.assertEqual(self.page.locator('#country').get_attribute('aria-expanded'), 'false')
        control = field_locator(self.page, field)
        select_combobox(self.page, control, 'Canada')
        self.assertEqual(control.input_value(), 'Canada')

    def test_combobox_option_no_longer_present_is_refused(self):
        self.page.set_content('<form>' + self.combobox('auth', 'Authorized to work?', ['Yes', 'No']) +
                              '<button>Continue</button></form>' + self.wire_comboboxes())
        control = self.page.locator('#auth')
        with self.assertRaisesRegex(ValueError, 'no longer unique'):
            select_combobox(self.page, control, 'Maybe')

    def test_required_checkbox_group_is_read_as_one_field(self):
        self.page.set_content('''<form>
          <fieldset>
            <legend>Which of the following clearances do you currently hold? (select all that apply)</legend>
            <label for="cl_secret"><input type="checkbox" id="cl_secret" name="clearance[]" value="secret" required> Secret</label>
            <label for="cl_ts"><input type="checkbox" id="cl_ts" name="clearance[]" value="top_secret"> Top Secret</label>
          </fieldset>
          <button>Continue</button></form>''')
        snapshot = inspect(self.page)
        group = next(f for f in snapshot['fields'] if f['type'] == 'checkbox-group')
        self.assertTrue(group['required'])
        # Ids are stable and unique, so they are preferred over the raw "on"
        # value attribute browsers assign an un-valued checkbox by default.
        self.assertEqual({o['value'] for o in group['options']}, {'cl_secret', 'cl_ts'})
        fill_checkbox_group(self.page, group, ['cl_secret'])
        self.assertTrue(self.page.locator('#cl_secret').is_checked())
        self.assertFalse(self.page.locator('#cl_ts').is_checked())
        # An empty selection still explicitly clears every box rather than skipping them.
        fill_checkbox_group(self.page, group, [])
        self.assertFalse(self.page.locator('#cl_secret').is_checked())


@unittest.skipUnless(os.environ.get('JOB_APPLY_BROWSER_TESTS') == '1', 'Requires installed Chromium')
class GreenhouseDryRunTests(unittest.TestCase):
    """Zero-transmission acceptance test for the complete pre-submit path."""

    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True)
        self.page = self.browser.new_page()

    def tearDown(self):
        self.browser.close()
        self.runtime.stop()

    def test_discovery_to_greenhouse_final_review_without_submission(self):
        import json
        import tempfile
        from datetime import date
        from io import BytesIO
        from pathlib import Path
        from types import SimpleNamespace
        from zipfile import ZipFile

        import jobwatch_match as matching
        from jobapply_forms import FormQueue
        from jobapply_profile import prefill
        from jobapply_store import Store

        url = 'https://job-boards.greenhouse.io/example/jobs/4001'
        candidate = SimpleNamespace(
            source='greenhouse-dry-run', title='Detection Engineer',
            company='Example Security', url=url,
            description='Requirements: Splunk, KQL, Python and incident response.',
            location='United States', work_mode='Remote',
            work_mode_source='dry-run fixture', remote=True,
            salary='$110,000-$130,000 per year', fit={})
        profile = matching.load_profile()
        candidate.fit = matching.score_job(candidate, profile, date(2026, 9, 7))
        online_text = (
            'This is a remote US Detection Engineer role at Example Security. '
            'Ability to obtain and maintain a security clearance. Requirements: '
            'Splunk, KQL, Python and incident response. One year of security '
            'experience required. Salary $110,000-$130,000 per year.'
        )
        candidate.fit = matching.rescore_after_online_enrichment(
            candidate, online_text, profile, date(2026, 9, 7))
        self.assertEqual(candidate.fit['post_enrichment']['status'], 'eligible')
        self.assertGreaterEqual(candidate.fit['score'], 70)

        job = {
            'source': candidate.source, 'title': candidate.title,
            'company': candidate.company, 'url': candidate.url,
            'description': candidate.description, 'location': candidate.location,
            'work_mode': candidate.work_mode, 'salary': candidate.salary,
            'fit': candidate.fit,
            'online_posting': {'status': 'retrieved', 'text': online_text},
        }
        packet = {
            'job_url': url, 'skill_version': 'greenhouse-dry-run-v1',
            'form_complete': False,
            'missing_fields': ['Employer form has not been inspected'],
            'evidence': {'E1': 'Synthetic dry-run evidence'},
            'resume_claims': [{
                'value': 'Detection engineering experience validated for dry run.',
                'evidence_ids': ['E1'],
            }],
            'answers': {},
        }
        docx = BytesIO()
        with ZipFile(docx, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document/>')

        html = '''<!doctype html><html><body>
          <form id="application">
            <label for="first_name">First Name</label>
            <input id="first_name" required>
            <label for="last_name">Last Name</label>
            <input id="last_name" required>
            <label for="email">Email</label>
            <input id="email" type="email" required>
            <label for="resume">Resume/CV</label>
            <input id="resume" type="file" required>
            <label for="sponsorship">Will you require sponsorship?</label>
            <select id="sponsorship" required><option></option><option>No</option><option>Yes</option></select>
            <label for="authorization">Are you authorized to work in the United States?</label>
            <select id="authorization" required><option></option><option>Yes</option><option>No</option></select>
            <label for="linkedin">LinkedIn Profile</label>
            <input id="linkedin">
            <button type="submit">Submit application</button>
          </form>
          <script>
            window.submissions = 0;
            document.querySelector('#application').addEventListener('submit', event => {
              event.preventDefault(); window.submissions += 1;
            });
          </script>
        </body></html>'''
        intercepted = []

        def local_greenhouse(route):
            intercepted.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=html)

        self.page.route(url, local_greenhouse)
        self.page.goto(url, wait_until='domcontentloaded')
        snapshot = inspect(self.page)
        owner_profile = {
            'identity': {
                'first_name': 'Alex', 'last_name': 'Rivera',
                'email': 'alex@example.com',
            },
            'links': {'linkedin': 'https://www.linkedin.com/in/example'},
            'attachments': {'resume': 'approved_resume.pdf'},
            'questions': [
                {'match': 'require sponsorship', 'value': 'No'},
                {'match': 'authorized to work', 'value': 'Yes'},
            ],
        }
        answers, missing = prefill(snapshot, owner_profile)
        self.assertEqual(missing, [])

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'dry-run.sqlite3'))
            store.enqueue('dryrun1', job)
            resume_digest = store.save_draft(
                'dryrun1', packet, b'%PDF-greenhouse-dry-run', docx.getvalue())
            store.decide_draft(
                'dryrun1', resume_digest, 'approve', 1, 1, auto=False)
            queue = FormQueue(store)
            queue.sync_approved()
            self.assertTrue(queue.claim('dryrun1', 'queued', 'inspecting'))
            queue.save_inspection('dryrun1', snapshot, answers)

            row = queue.get('dryrun1')
            plan = json.loads(row['plan'])
            self.assertEqual(row['state'], 'review')
            self.assertFalse(queue.auto_advance('dryrun1'))
            self.assertEqual(queue.missing(plan), [])
            self.assertIsNone(row['receipt'])
            self.assertEqual(store.get('dryrun1')['state'], 'resume_approved')
            with store.connect() as db:
                actions = [r['action'] for r in db.execute(
                    "SELECT action FROM audit WHERE id='dryrun1'")]
            self.assertFalse(any(action.startswith('form_approve:')
                                 or action == 'submission_receipt'
                                 for action in actions))

        self.assertEqual(intercepted, [url])
        self.assertEqual(self.page.evaluate('window.submissions'), 0)
        self.assertEqual(self.page.locator('#email').input_value(), '')
        self.assertEqual(self.page.locator('#resume').input_value(), '')

    def test_voyager_form_reaches_complete_final_review_without_submission(self):
        """The complex Voyager-style controls the plain fixture above lacks:

        an unsupported optional number field, Greenhouse's custom country and
        yes/no comboboxes, a decoy input the combobox widget leaves behind, and
        a required multi-checkbox clearance group. No answer is ever sent; this
        only proves the run can reach a complete review with nothing missing.
        """
        import json
        import tempfile
        from pathlib import Path
        from jobapply_forms import FormQueue
        from jobapply_profile import prefill
        from jobapply_store import Store

        url = 'https://job-boards.greenhouse.io/example/jobs/9001'
        job = {'source': 'greenhouse-dry-run', 'title': 'Detection Engineer',
               'company': 'Example Security', 'url': url, 'fit': {'score': 80}}

        html = '''<!doctype html><html><body>
          <form id="application">
            <label for="first_name">First Name</label>
            <input id="first_name" required>
            <label for="last_name">Last Name</label>
            <input id="last_name" required>
            <label for="email">Email</label>
            <input id="email" type="email" required>

            <fieldset>
              <legend>Education</legend>
              <label for="grad_year">Expected Graduation Year</label>
              <input id="grad_year" type="number" min="1950" max="2100">
            </fieldset>

            <div class="select-shell">
              <label id="country-label" for="country-input">Country</label>
              <div>
                <input id="country-input" role="combobox" aria-haspopup="listbox" aria-expanded="false"
                       aria-controls="country-listbox" aria-autocomplete="list" autocomplete="off" required>
                <input aria-hidden="true" tabindex="-1" readonly style="position:absolute;opacity:0;">
                <ul id="country-listbox" role="listbox" hidden>
                  <li role="option" id="country-opt-0">United States</li>
                  <li role="option" id="country-opt-1">Canada</li>
                </ul>
              </div>
            </div>

            <div class="select-shell">
              <label id="auth-label" for="auth-input">Are you legally authorized to work in the United States?</label>
              <div>
                <input id="auth-input" role="combobox" aria-haspopup="listbox" aria-expanded="false"
                       aria-controls="auth-listbox" aria-autocomplete="list" autocomplete="off" required>
                <ul id="auth-listbox" role="listbox" hidden>
                  <li role="option" id="auth-opt-0">Yes</li>
                  <li role="option" id="auth-opt-1">No</li>
                </ul>
              </div>
            </div>

            <fieldset>
              <legend>Which of the following security clearances do you currently hold? (select all that apply)</legend>
              <label for="cl_secret"><input type="checkbox" id="cl_secret" name="clearance[]" value="secret" required> Secret</label>
              <label for="cl_ts"><input type="checkbox" id="cl_ts" name="clearance[]" value="top_secret"> Top Secret</label>
              <label for="cl_scii"><input type="checkbox" id="cl_scii" name="clearance[]" value="ts_sci"> TS/SCI</label>
            </fieldset>

            <label for="resume">Resume/CV</label>
            <input id="resume" type="file" required>
            <button type="submit">Submit application</button>
          </form>
          <script>
            document.querySelectorAll('input[role=combobox]').forEach(input => {
              const listbox = document.getElementById(input.getAttribute('aria-controls'));
              input.addEventListener('click', () => { listbox.hidden = false; input.setAttribute('aria-expanded', 'true'); });
              input.addEventListener('keydown', e => { if (e.key === 'Escape') { listbox.hidden = true; input.setAttribute('aria-expanded', 'false'); } });
              listbox.querySelectorAll('[role=option]').forEach(opt => {
                opt.addEventListener('click', () => {
                  input.value = opt.innerText; listbox.hidden = true; input.setAttribute('aria-expanded', 'false');
                });
              });
            });
            window.submissions = 0;
            document.querySelector('#application').addEventListener('submit', event => {
              event.preventDefault(); window.submissions += 1;
            });
          </script>
        </body></html>'''
        intercepted = []

        def local_greenhouse(route):
            intercepted.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=html)

        self.page.route(url, local_greenhouse)
        self.page.goto(url, wait_until='domcontentloaded')
        snapshot = inspect(self.page)

        # The number field is dropped (optional/unsupported), and the
        # aria-hidden decoy input never surfaces as an unlabeled field.
        self.assertNotIn('grad_year', [f['id'] for f in snapshot['fields']])
        self.assertTrue(all(f['label'] for f in snapshot['fields']))
        country = next(f for f in snapshot['fields'] if f['id'] == 'country-input')
        self.assertEqual(country['type'], 'combobox')
        self.assertIn('United States', country['options'])
        group = next(f for f in snapshot['fields'] if f['type'] == 'checkbox-group')
        self.assertTrue(group['required'])

        owner_profile = {
            'identity': {'first_name': 'Alex', 'last_name': 'Rivera',
                        'email': 'alex@example.com', 'country': 'United States'},
            'attachments': {'resume': 'approved_resume.pdf'},
            'questions': [
                {'match': 'authorized to work', 'value': 'Yes'},
                {'match': 'which of the following security clearances', 'value': ['Secret', 'TS/SCI']},
            ],
        }
        answers, missing = prefill(snapshot, owner_profile)
        self.assertEqual(missing, [])
        self.assertEqual(answers['country-input']['value'], 'United States')
        self.assertEqual(set(answers[group['id']]['value']),
                         {o['value'] for o in group['options'] if o['label'] in ('Secret', 'TS/SCI')})

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'voyager-dry-run.sqlite3'))
            store.enqueue('voyager1', job)
            with store.connect() as db:
                db.execute("UPDATE applications SET state='resume_approved',digest='resume-v1' WHERE id='voyager1'")
            queue = FormQueue(store)
            queue.sync_approved()
            self.assertTrue(queue.claim('voyager1', 'queued', 'inspecting'))
            queue.save_inspection('voyager1', snapshot, answers)

            row = queue.get('voyager1')
            plan = json.loads(row['plan'])
            self.assertEqual(row['state'], 'review')
            self.assertEqual(queue.missing(plan), [])
            self.assertIsNone(row['receipt'])
            with store.connect() as db:
                actions = [r['action'] for r in db.execute(
                    "SELECT action FROM audit WHERE id='voyager1'")]
            self.assertFalse(any(action.startswith('form_approve:')
                                 or action == 'submission_receipt'
                                 for action in actions))

        self.assertEqual(intercepted, [url])
        self.assertEqual(self.page.evaluate('window.submissions'), 0)
        self.assertEqual(self.page.locator('#email').input_value(), '')
        self.assertEqual(self.page.locator('#resume').input_value(), '')
        self.assertFalse(self.page.locator('#cl_secret').is_checked())


@unittest.skipUnless(os.environ.get('JOB_APPLY_BROWSER_TESTS') == '1', 'Requires installed Chromium')
class AshbyDryRunTests(unittest.TestCase):
    """Ashby was added by allow-listing its host only — no field-parsing code
    changed. This fixture deliberately uses different markup, wording and
    structure than every Greenhouse fixture above (different combobox wiring,
    a differently-labeled checkbox group, no fieldset legend) to prove the
    generic ARIA-based detection carries over to a second platform rather
    than having quietly been Greenhouse-specific. Zero-transmission, same as
    the Greenhouse dry runs: this only reaches a complete review, never a
    submission. Ashby's real button wording and confirmation copy are still
    unverified against a live posting — see jobapply_browser.py's comments.
    """

    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True)
        self.page = self.browser.new_page()

    def tearDown(self):
        self.browser.close()
        self.runtime.stop()

    def test_ashby_form_reaches_complete_final_review_without_submission(self):
        import json
        import tempfile
        from pathlib import Path
        from jobapply_forms import FormQueue
        from jobapply_profile import prefill
        from jobapply_store import Store

        url = 'https://jobs.ashbyhq.com/example/22222222-3333-4444-5555-666666666666'
        job = {'source': 'ashby-dry-run', 'title': 'Detection Engineer',
               'company': 'Example Security', 'url': url, 'fit': {'score': 80}}

        html = '''<!doctype html><html><body>
          <form id="application">
            <label for="_systemfield_name">Full name</label>
            <input id="_systemfield_name" required>
            <label for="_systemfield_email">Email</label>
            <input id="_systemfield_email" type="email" required>

            <div class="ashby-select-input">
              <label id="loc-label" for="loc-trigger">Location</label>
              <span>
                <input id="loc-trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false"
                       aria-owns="loc-popup" autocomplete="off" required>
                <input tabindex="-1" aria-hidden="true" style="width:0;height:0;border:0;">
                <div id="loc-popup" role="listbox" hidden>
                  <div role="option" id="loc-0">Remote - US</div>
                  <div role="option" id="loc-1">Remote - Canada</div>
                </div>
              </span>
            </div>

            <fieldset aria-label="Which of these certifications do you currently hold?">
              <div><label><input type="checkbox" id="cert_secplus" name="certs" required> Security+</label></div>
              <div><label><input type="checkbox" id="cert_gcih" name="certs"> GCIH</label></div>
              <div><label><input type="checkbox" id="cert_oscp" name="certs"> OSCP</label></div>
            </fieldset>

            <label for="resume">Resume</label>
            <input id="resume" type="file" required>
            <button type="submit">Submit Application</button>
          </form>
          <script>
            const trigger = document.getElementById('loc-trigger');
            const popup = document.getElementById('loc-popup');
            trigger.addEventListener('click', () => { popup.hidden = false; trigger.setAttribute('aria-expanded', 'true'); });
            trigger.addEventListener('keydown', e => { if (e.key === 'Escape') { popup.hidden = true; trigger.setAttribute('aria-expanded', 'false'); } });
            popup.querySelectorAll('[role=option]').forEach(opt => {
              opt.addEventListener('click', () => {
                trigger.value = opt.innerText; popup.hidden = true; trigger.setAttribute('aria-expanded', 'false');
              });
            });
            window.submissions = 0;
            document.querySelector('#application').addEventListener('submit', event => {
              event.preventDefault(); window.submissions += 1;
            });
          </script>
        </body></html>'''
        intercepted = []

        def local_ashby(route):
            intercepted.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=html)

        self.page.route(url, local_ashby)
        self.page.goto(url, wait_until='domcontentloaded')
        snapshot = inspect(self.page)

        self.assertTrue(all(f['label'] for f in snapshot['fields']))
        location = next(f for f in snapshot['fields'] if f['id'] == 'loc-trigger')
        self.assertEqual(location['type'], 'combobox')
        self.assertIn('Remote - US', location['options'])
        group = next(f for f in snapshot['fields'] if f['type'] == 'checkbox-group')
        self.assertTrue(group['required'])
        self.assertEqual(group['label'], 'Which of these certifications do you currently hold?')

        owner_profile = {
            'identity': {'full_name': 'Alex Rivera', 'email': 'alex@example.com', 'location': 'Remote - US'},
            'attachments': {'resume': 'approved_resume.pdf'},
            'questions': [
                {'match': 'which of these certifications', 'value': ['Security+', 'OSCP']},
            ],
        }
        answers, missing = prefill(snapshot, owner_profile)
        self.assertEqual(missing, [])

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'ashby-dry-run.sqlite3'))
            store.enqueue('ashby1', job)
            with store.connect() as db:
                db.execute("UPDATE applications SET state='resume_approved',digest='resume-v1' WHERE id='ashby1'")
            queue = FormQueue(store)
            queue.sync_approved()
            self.assertTrue(queue.claim('ashby1', 'queued', 'inspecting'))
            queue.save_inspection('ashby1', snapshot, answers)

            row = queue.get('ashby1')
            plan = json.loads(row['plan'])
            self.assertEqual(row['state'], 'review')
            self.assertEqual(queue.missing(plan), [])
            self.assertIsNone(row['receipt'])

        self.assertEqual(intercepted, [url])
        self.assertEqual(self.page.evaluate('window.submissions'), 0)
        self.assertEqual(self.page.locator('#_systemfield_email').input_value(), '')
        self.assertEqual(self.page.locator('#resume').input_value(), '')


if __name__ == '__main__':
    unittest.main()

