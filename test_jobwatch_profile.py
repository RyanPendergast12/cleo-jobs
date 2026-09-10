import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from jobapply_browser import REQUESTABLE, RECEIPT_PHRASES, allowed_url, execute_step, platform
from jobapply_forms import FormQueue, threshold
from jobapply_profile import load_profile, prefill, validate_profile
from jobapply_store import Store


PROFILE = {
    'identity': {'first_name': 'Alex', 'last_name': 'Rivera', 'email': 'alex@example.com',
                 'phone': '555-0100', 'location': 'Las Vegas, NV'},
    'links': {'linkedin': 'https://www.linkedin.com/in/example'},
    'attachments': {'resume': 'approved_resume.pdf'},
    'questions': [{'match': 'authorized to work', 'value': 'Yes'},
                  {'match': 'require sponsorship', 'value': 'No'}],
}


def field(identifier, label, kind='text', required=False, options=()):
    return {'id': identifier, 'label': label, 'type': kind,
            'required': required, 'options': list(options)}


def greenhouse():
    return {'url': 'https://job-boards.greenhouse.io/example/jobs/4001',
            'action': 'submit', 'button': 'Submit application',
            'fields': [field('first_name', 'First Name', required=True),
                       field('last_name', 'Last Name', required=True),
                       field('email', 'Email', 'email', required=True),
                       field('phone', 'Phone', 'tel'),
                       field('resume', 'Resume/CV', 'file', required=True),
                       field('q1', 'Will you now or in the future require sponsorship?',
                             'select', True, ('Yes', 'No')),
                       field('q2', 'Are you legally authorized to work in the United States?',
                             'select', True, ('Yes', 'No')),
                       field('q3', 'LinkedIn Profile'),
                       field('q4', 'I agree to the terms', 'checkbox'),
                       field('q5', 'Desired salary')]}


class ProfileTests(unittest.TestCase):
    def test_greenhouse_form_fills_from_the_owner_profile(self):
        answers, blank = prefill(greenhouse(), PROFILE)
        self.assertEqual(blank, [])
        self.assertEqual(answers['first_name']['value'], 'Alex')
        self.assertEqual(answers['email']['value'], 'alex@example.com')
        self.assertEqual(answers['resume']['value'], 'approved_resume.pdf')
        self.assertEqual(answers['q1']['value'], 'No')
        self.assertEqual(answers['q2']['value'], 'Yes')
        self.assertEqual(answers['q3']['value'], PROFILE['links']['linkedin'])
        self.assertEqual(answers['first_name']['source'], 'profile:identity.first_name')
        self.assertEqual(answers['q2']['source'], 'profile:questions')

    def test_sensitive_questions_are_never_inferred(self):
        bare = {'identity': PROFILE['identity'], 'links': PROFILE['links'],
                'attachments': PROFILE['attachments']}
        answers, blank = prefill(greenhouse(), bare)
        for identifier in ('q1', 'q2', 'q4', 'q5'):
            self.assertNotIn(identifier, answers)
        self.assertEqual(sorted(blank), ['Are you legally authorized to work in the United States?',
                                         'Will you now or in the future require sponsorship?'])

    def test_consent_checkbox_needs_an_explicit_answer(self):
        snapshot = {'url': 'https://job-boards.greenhouse.io/example/jobs/1', 'action': 'submit',
                    'button': 'Submit application',
                    'fields': [field('terms', 'I agree to the privacy policy', 'checkbox', True)]}
        self.assertEqual(prefill(snapshot, PROFILE), ({}, ['I agree to the privacy policy']))
        stated = dict(PROFILE, questions=[{'match': 'privacy policy', 'value': True}])
        answers, blank = prefill(snapshot, stated)
        self.assertEqual(blank, [])
        self.assertIs(answers['terms']['value'], True)

    def test_select_answer_must_match_an_employer_option(self):
        snapshot = {'url': 'https://job-boards.greenhouse.io/example/jobs/1', 'action': 'submit',
                    'button': 'Submit application',
                    'fields': [field('q', 'Do you require sponsorship?', 'select', True, ('Yes', 'No'))]}
        unusable = dict(PROFILE, questions=[{'match': 'require sponsorship', 'value': 'Maybe'}])
        self.assertEqual(prefill(snapshot, unusable), ({}, ['Do you require sponsorship?']))

    def test_only_a_resume_control_receives_the_locked_resume(self):
        snapshot = {'url': 'https://job-boards.greenhouse.io/example/jobs/1', 'action': 'submit',
                    'button': 'Submit application',
                    'fields': [field('cover', 'Cover Letter', 'file'),
                               field('resume', 'Resume/CV', 'file', True)]}
        answers, blank = prefill(snapshot, PROFILE)
        self.assertEqual(blank, [])
        self.assertNotIn('cover', answers)
        self.assertEqual(answers['resume']['value'], 'approved_resume.pdf')

    def test_absent_profile_leaves_every_personal_answer_to_discord(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / 'absent.json')
            self.assertEqual(load_profile(missing), {})
            answers, blank = prefill(greenhouse(), load_profile(missing))
        # The approved resume is the owner's own locked artifact, not profile data.
        self.assertEqual(set(answers), {'resume'})
        self.assertEqual(len(blank), 5)

    def test_profile_is_read_from_the_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'apply_profile.json'
            target.write_text(json.dumps(PROFILE), encoding='utf-8')
            with patch.dict('os.environ', {'JOB_APPLY_PROFILE': str(target)}):
                self.assertEqual(load_profile()['identity']['email'], 'alex@example.com')

    def test_a_deployed_service_can_supply_the_profile_as_one_variable(self):
        # Render deploys from the repo, where the real profile is gitignored.
        with patch.dict('os.environ', {'JOB_APPLY_PROFILE_JSON': json.dumps(PROFILE)}):
            self.assertEqual(load_profile()['identity']['email'], 'alex@example.com')
            answers, blank = prefill(greenhouse(), load_profile())
            self.assertEqual(blank, [])
            self.assertEqual(answers['q2']['value'], 'Yes')
        with patch.dict('os.environ', {'JOB_APPLY_PROFILE_JSON': '{not json'}):
            with self.assertRaisesRegex(ValueError, 'not valid JSON'):
                load_profile()


class VoyagerControlTests(unittest.TestCase):
    """Coverage for the Greenhouse Voyager-style controls the adapter now handles."""

    def snapshot(self, *fields):
        return {'url': 'https://job-boards.greenhouse.io/example/jobs/1', 'action': 'submit',
                'button': 'Submit application', 'fields': list(fields)}

    def test_country_combobox_fills_from_identity(self):
        combobox = field('country', 'Country', 'combobox', True, ('United States', 'Canada'))
        answers, blank = prefill(self.snapshot(combobox), PROFILE | {'identity': PROFILE['identity'] | {'country': 'United States'}})
        self.assertEqual(blank, [])
        self.assertEqual(answers['country']['value'], 'United States')

    def test_yes_no_combobox_requires_an_explicit_question(self):
        combobox = field('auth', 'Are you legally authorized to work in the United States?', 'combobox', True, ('Yes', 'No'))
        answers, blank = prefill(self.snapshot(combobox), PROFILE)
        self.assertEqual(blank, [])
        self.assertEqual(answers['auth']['value'], 'Yes')
        bare = {'identity': PROFILE['identity']}
        answers, blank = prefill(self.snapshot(combobox), bare)
        self.assertEqual(answers, {})
        self.assertEqual(blank, [combobox['label']])

    def test_checkbox_group_resolves_labels_to_option_values(self):
        group = {'id': 'group:clearance', 'label': 'Which clearances do you hold?',
                 'type': 'checkbox-group', 'required': True,
                 'options': [{'value': 'secret', 'label': 'Secret'}, {'value': 'ts', 'label': 'Top Secret'}]}
        profile = dict(PROFILE, questions=PROFILE['questions'] +
                       [{'match': 'which clearances', 'value': ['Secret', 'top secret']}])
        answers, blank = prefill(self.snapshot(group), profile)
        self.assertEqual(blank, [])
        self.assertEqual(answers['group:clearance']['value'], ['secret', 'ts'])

    def test_checkbox_group_is_never_guessed_from_contact_rules(self):
        group = {'id': 'group:clearance', 'label': 'Which clearances do you hold?',
                 'type': 'checkbox-group', 'required': True,
                 'options': [{'value': 'secret', 'label': 'Secret'}]}
        answers, blank = prefill(self.snapshot(group), PROFILE)
        self.assertEqual(answers, {})
        self.assertEqual(blank, [group['label']])

    def test_unresolvable_checkbox_group_answer_stays_blank(self):
        group = {'id': 'group:clearance', 'label': 'Which clearances do you hold?',
                 'type': 'checkbox-group', 'required': True,
                 'options': [{'value': 'secret', 'label': 'Secret'}]}
        profile = dict(PROFILE, questions=[{'match': 'which clearances', 'value': ['Not A Real Option']}])
        answers, blank = prefill(self.snapshot(group), profile)
        self.assertEqual(answers, {})
        self.assertEqual(blank, [group['label']])

    def test_preferred_and_legal_name_fall_back_to_first_and_full_name(self):
        combobox = field('preferred', 'Preferred Name')
        legal = field('legal', 'Legal Name')
        answers, blank = prefill(self.snapshot(combobox, legal), PROFILE)
        self.assertEqual(blank, [])
        self.assertEqual(answers['preferred']['value'], 'Alex')
        self.assertEqual(answers['legal']['value'], 'Alex Rivera')

    def test_city_state_zip_and_country_fill_independently_of_combined_location(self):
        city, state, zip_ = field('city', 'City'), field('state', 'State'), field('zip', 'Postal Code')
        profile = dict(PROFILE, identity=PROFILE['identity'] | {'city': 'Las Vegas', 'state': 'NV', 'zip': '89101'})
        answers, blank = prefill(self.snapshot(city, state, zip_), profile)
        self.assertEqual(blank, [])
        self.assertEqual(answers['city']['value'], 'Las Vegas')
        self.assertEqual(answers['state']['value'], 'NV')
        self.assertEqual(answers['zip']['value'], '89101')


class ProfileValidationTests(unittest.TestCase):
    def test_identity_and_links_must_be_flat_string_maps(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            validate_profile({'identity': {'first_name': 1}})
        with self.assertRaisesRegex(ValueError, 'links'):
            validate_profile({'links': ['not', 'a', 'map']})
        validate_profile({'identity': {'first_name': 'Alex'}})

    def test_questions_need_a_match_string_and_usable_value(self):
        with self.assertRaisesRegex(ValueError, 'questions'):
            validate_profile({'questions': 'not a list'})
        with self.assertRaisesRegex(ValueError, 'match'):
            validate_profile({'questions': [{'value': 'Yes'}]})
        with self.assertRaisesRegex(ValueError, 'string, boolean or list'):
            validate_profile({'questions': [{'match': 'clearance', 'value': 42}]})
        with self.assertRaisesRegex(ValueError, 'string, boolean or list'):
            validate_profile({'questions': [{'match': 'clearance', 'value': ['ok', 3]}]})
        validate_profile({'questions': [{'match': 'clearance', 'value': ['Secret', 'Top Secret']}]})
        validate_profile({'questions': [{'match': 'clearance', 'value': []}]})

    def test_malformed_profile_json_is_rejected_at_load(self):
        with patch.dict('os.environ', {'JOB_APPLY_PROFILE_JSON': json.dumps({'identity': {'first_name': 1}})}):
            with self.assertRaisesRegex(ValueError, 'identity'):
                load_profile()


class AutoApplyTests(unittest.TestCase):
    def setUp(self):
        self.transmit_env = patch.dict(
            'os.environ',
            {'JOB_APPLY_TRANSMIT_ENABLED': 'true',
             'JOB_APPLY_TRANSMIT_ATS': 'greenhouse'},
        )
        self.transmit_env.start()
        self.addCleanup(self.transmit_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.store.enqueue('test', {'url': 'https://example.com/job', 'title': 'Detection engineer',
                                    'fit': {'score': 95}})

    def approve(self, auto=True, score=95):
        with self.store.connect() as db:
            db.execute("UPDATE applications SET state='draft_ready',digest='resume-v1',"
                       "job=json_set(job,'$.fit.score',?) WHERE id='test'", (score,))
        self.store.decide_draft('test', 'resume-v1', 'approve', 1, 1, auto=auto)
        queue = FormQueue(self.store)
        queue.sync_approved()
        queue.claim('test', 'queued', 'inspecting')
        return queue

    def inspect(self, queue, profile=PROFILE, snapshot=None):
        snapshot = snapshot or greenhouse()
        answers, _ = prefill(snapshot, profile)
        queue.save_inspection('test', snapshot, answers)
        return queue.auto_advance('test')

    def test_one_click_carries_through_to_an_approved_step(self):
        queue = self.approve()
        self.assertEqual(queue.get('test')['auto'], 1)
        self.assertTrue(self.inspect(queue))
        row = queue.get('test')
        self.assertEqual(row['state'], 'approved')
        self.assertEqual(len(json.loads(row['plan'])['answers']), 8)
        with self.store.connect() as db:
            actions = [r['action'] for r in db.execute("SELECT action FROM audit WHERE id='test'")]
        self.assertIn('draft_approve_auto:resume-v1', actions)
        self.assertTrue(any(a.startswith('form_auto_approve:') for a in actions))

    def test_plain_approval_still_waits_for_a_human(self):
        queue = self.approve(auto=False)
        self.assertEqual(queue.get('test')['auto'], 0)
        self.assertFalse(self.inspect(queue))
        self.assertEqual(queue.get('test')['state'], 'review')

    def test_below_threshold_match_falls_back_to_review(self):
        queue = self.approve(score=72)
        self.assertFalse(self.inspect(queue))
        self.assertEqual(queue.get('test')['state'], 'review')

    def test_threshold_is_configurable(self):
        queue = self.approve(score=72)
        with patch.dict('os.environ', {'JOB_APPLY_AUTO_SUBMIT_MIN_SCORE': '70'}):
            self.assertEqual(threshold(), 70)
            self.assertTrue(self.inspect(queue))
        self.assertEqual(queue.get('test')['state'], 'approved')

    def test_a_question_the_profile_cannot_answer_stops_the_run(self):
        queue = self.approve()
        bare = {'identity': PROFILE['identity'], 'attachments': PROFILE['attachments']}
        self.assertFalse(self.inspect(queue, profile=bare))
        row = queue.get('test')
        self.assertEqual(row['state'], 'review')
        self.assertEqual(len(queue.missing(json.loads(row['plan']))), 2)

    def test_prefilled_answers_are_revalidated_before_storage(self):
        queue = self.approve()
        snapshot = greenhouse()
        forged = {'q1': {'value': 'Perhaps', 'source': 'profile:questions'},
                  'ghost': {'value': 'x', 'source': 'profile:questions'},
                  'email': {'value': 'alex@example.com', 'source': 'profile:identity.email'}}
        queue.save_inspection('test', snapshot, forged)
        stored = json.loads(queue.get('test')['plan'])['answers']
        self.assertEqual(set(stored), {'email'})

    def test_auto_apply_cannot_ride_along_with_a_skip(self):
        with self.store.connect() as db:
            db.execute("UPDATE applications SET state='draft_ready',digest='resume-v1' WHERE id='test'")
        with self.assertRaises(ValueError):
            self.store.decide_draft('test', 'resume-v1', 'skip', 1, 1, auto=True)
        self.assertEqual(self.store.get('test')['auto_apply'], 0)


class GreenhouseTests(unittest.TestCase):
    def setUp(self):
        self.transmit_env = patch.dict(
            'os.environ',
            {'JOB_APPLY_TRANSMIT_ENABLED': 'true',
             'JOB_APPLY_TRANSMIT_ATS': 'greenhouse'},
        )
        self.transmit_env.start()
        self.addCleanup(self.transmit_env.stop)

    def test_greenhouse_boards_are_accepted(self):
        for host in ('job-boards.greenhouse.io', 'boards.greenhouse.io'):
            url = f'https://{host}/example/jobs/4001'
            self.assertEqual(allowed_url(url), url)
            self.assertEqual(platform(url), 'greenhouse')

    def test_lookalike_and_aggregator_hosts_are_refused(self):
        for url in ('https://job-boards.greenhouse.io.evil.test/x',
                    'https://www.linkedin.com/jobs/view/1',
                    'http://job-boards.greenhouse.io/x',
                    'https://user:pass@job-boards.greenhouse.io/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                allowed_url(url)

    def test_greenhouse_uploads_reach_its_own_storage_only(self):
        suffixes = REQUESTABLE['greenhouse']
        for host in ('job-boards.greenhouse.io', 'prod-heroku.s3.amazonaws.com'):
            self.assertTrue(any(host.endswith(suffix) for suffix in suffixes))
        for host in ('tracker.example.com', 'facebook.com'):
            self.assertFalse(any(host.endswith(suffix) for suffix in suffixes))

    def test_greenhouse_confirmation_produces_a_receipt(self):
        snapshot = {'url': 'https://job-boards.greenhouse.io/example/jobs/4001',
                    'action': 'submit', 'button': 'Submit application',
                    'fields': [field('email', 'Email', 'email', required=True)]}
        plan = {'snapshot': snapshot,
                'answers': {'email': {'value': 'alex@example.com', 'source': 'profile:identity.email'}}}
        body, control, quiet = Mock(), Mock(), Mock()
        body.inner_text.side_effect = ['Apply for this role', 'Application submitted. We will be in touch.']
        control.count.return_value = 1
        quiet.count.return_value = 0
        page = Mock(url=snapshot['url'])
        page.locator.side_effect = lambda selector: (
            body if selector == 'body' else quiet if 'iframe' in selector else control)
        with patch('jobapply_browser.inspect', return_value=snapshot):
            outcome = execute_step(page, plan, {})
        control.fill.assert_called_once_with('alex@example.com')
        self.assertIn('Application submitted', outcome['receipt']['text'])

    def test_receipt_phrases_cover_greenhouse_wording(self):
        self.assertIn('application submitted', RECEIPT_PHRASES)


class AshbyTests(unittest.TestCase):
    def setUp(self):
        self.transmit_env = patch.dict(
            'os.environ',
            {'JOB_APPLY_TRANSMIT_ENABLED': 'true',
             'JOB_APPLY_TRANSMIT_ATS': 'ashby'},
        )
        self.transmit_env.start()
        self.addCleanup(self.transmit_env.stop)

    def test_ashby_boards_are_accepted(self):
        url = 'https://jobs.ashbyhq.com/example/11111111-2222-3333-4444-555555555555'
        self.assertEqual(allowed_url(url), url)
        self.assertEqual(platform(url), 'ashby')

    def test_lookalike_ashby_hosts_are_refused(self):
        for url in ('https://jobs.ashbyhq.com.evil.test/x',
                    'http://jobs.ashbyhq.com/x',
                    'https://user:pass@jobs.ashbyhq.com/x',
                    'https://ashbyhq.com/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                allowed_url(url)

    def test_ashby_uploads_stay_on_its_own_domain(self):
        suffixes = REQUESTABLE['ashby']
        self.assertTrue(any('jobs.ashbyhq.com'.endswith(suffix) for suffix in suffixes))
        for host in ('tracker.example.com', 'facebook.com', 's3.amazonaws.com'):
            self.assertFalse(any(host.endswith(suffix) for suffix in suffixes))

    def test_ashby_confirmation_produces_a_receipt(self):
        snapshot = {'url': 'https://jobs.ashbyhq.com/example/1',
                    'action': 'submit', 'button': 'Submit Application',
                    'fields': [field('email', 'Email', 'email', required=True)]}
        plan = {'snapshot': snapshot,
                'answers': {'email': {'value': 'alex@example.com', 'source': 'profile:identity.email'}}}
        body, control, quiet = Mock(), Mock(), Mock()
        body.inner_text.side_effect = ['Apply for this role', "We've received your application. Thanks!"]
        control.count.return_value = 1
        quiet.count.return_value = 0
        page = Mock(url=snapshot['url'])
        page.locator.side_effect = lambda selector: (
            body if selector == 'body' else quiet if 'iframe' in selector else control)
        with patch('jobapply_browser.inspect', return_value=snapshot):
            outcome = execute_step(page, plan, {})
        control.fill.assert_called_once_with('alex@example.com')
        self.assertIn('received your application', outcome['receipt']['text'])


if __name__ == '__main__':
    unittest.main()

