import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from jobapply_store import Store
from jobapply_forms import (FormQueue, digest, transmission_allowed,
                            validate_snapshot, valid_answer)
from jobapply_ats import destination, request_allowed
from jobapply_browser import (allowed_intake_url, allowed_url, execute_step,
                              enter_application_form, field_locator,
                              resolve_linkedin)


def snapshot():
    return {'url': 'https://us-3.fountain.com/apply/example/opening/test',
            'action': 'continue', 'button': 'Continue',
            'fields': [{'id':'email','label':'Email','type':'email','required':True,'options':[]},
                       {'id':'sms','label':'Send text messages','type':'checkbox','required':False,'options':[]}]}


class FormTests(unittest.TestCase):
    def setUp(self):
        self.transmit_env = patch.dict(
            os.environ,
            {'JOB_APPLY_TRANSMIT_ENABLED': 'true',
             'JOB_APPLY_TRANSMIT_ATS': 'fountain'},
            clear=False,
        )
        self.transmit_env.start()
        self.addCleanup(self.transmit_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'db.sqlite'))
        self.store.enqueue('test', {'url':'https://example.com/job','title':'Test role','fit':{'score':90}})
        with self.store.connect() as db:
            db.execute("UPDATE applications SET state='resume_approved',digest='resume-v1' WHERE id='test'")
        self.queue = FormQueue(self.store)
        self.queue.sync_approved()
        self.queue.claim('test','queued','inspecting')
        self.queue.save_inspection('test', snapshot())

    def answer(self):
        row = self.queue.get('test')
        return self.queue.answer('test', row['version'], 'email', 'alex@example.com', 1, 1)

    def test_owner_missing_fields_and_stale_versions(self):
        old = self.queue.get('test')['version']
        with self.assertRaises(ValueError):
            self.queue.decide('test', old, 'approve', 2, 1)
        with self.assertRaises(ValueError):
            self.queue.decide('test', old, 'approve', 1, 1)
        with self.assertRaises(ValueError):
            self.queue.answer('test', old, 'email', 'alex@example.com', 2, 1)
        new = self.answer()
        self.assertNotEqual(old, new)
        with self.assertRaises(ValueError):
            self.queue.decide('test', old, 'approve', 1, 1)
        self.assertEqual(self.queue.decide('test', new, 'approve', 1, 1), 'approved')
        with self.assertRaises(ValueError):
            self.queue.decide('test', new, 'approve', 1, 1)

    def test_resume_change_blocks_approval(self):
        version = self.answer()
        with self.store.connect() as db:
            db.execute("UPDATE applications SET digest='new-resume' WHERE id='test'")
        with self.assertRaises(ValueError):
            self.queue.decide('test', version, 'approve', 1, 1)

    def test_restart_does_not_repeat_transmission(self):
        version = self.answer()
        self.queue.decide('test', version, 'approve', 1, 1)
        self.assertTrue(self.queue.claim('test','approved','executing'))
        self.assertFalse(self.queue.claim('test','approved','executing'))
        restarted = FormQueue(self.store)
        restarted.recover()
        self.assertEqual(restarted.get('test')['state'], 'uncertain')
        self.assertEqual(restarted.list(('approved','queued')), [])
        with self.assertRaises(ValueError):
            restarted.set_target('test', snapshot()['url'], 1, 1)

    def test_changed_form_prevents_any_fill_or_click(self):
        self.answer()
        plan = json.loads(self.queue.get('test')['plan'])
        changed = snapshot()
        changed['button'] = 'Next'
        page = Mock()
        with patch('jobapply_browser.inspect', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'changed'):
                execute_step(page, plan, {})
        page.locator.assert_not_called()
        page.get_by_role.assert_not_called()

    def test_receipt_requires_executing_state(self):
        with self.assertRaises(ValueError):
            self.queue.finish('test', {'text':'Example confirmation'})
        version = self.answer()
        self.queue.decide('test', version, 'approve', 1, 1)
        self.queue.claim('test', 'approved', 'executing')
        self.queue.finish('test', {'text':'Example confirmation','version':version})
        self.assertEqual(self.queue.get('test')['state'], 'submitted')
        self.assertIn('Example confirmation', self.queue.get('test')['receipt'])

    def test_destination_and_checkbox_validation(self):
        for url in ('http://us-3.fountain.com', 'https://us-3.fountain.com.evil.test',
                    'https://127.0.0.1', 'https://user:pass@us-3.fountain.com',
                    'https://us-3.fountain.com:444'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                allowed_url(url)
        row = self.queue.get('test')
        with self.assertRaises(ValueError):
            self.queue.answer('test', row['version'], 'sms', 'yes', 1, 1)
        self.queue.answer('test', row['version'], 'sms', False, 1, 1)

    def test_registry_accepts_direct_ats_and_linkedin_source_links(self):
        direct = {
            'https://jobs.ashbyhq.com/acme/role': 'ashby',
            'https://jobs.lever.co/acme/role': 'lever',
            'https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/role': 'workday',
            'https://jobs.smartrecruiters.com/Acme/role': 'smartrecruiters',
            'https://careers-acme.icims.com/jobs/123/role/job': 'icims',
            'https://acme.bamboohr.com/careers/123': 'bamboohr',
        }
        for url, expected in direct.items():
            with self.subTest(url=url):
                self.assertEqual(destination(url).platform, expected)
                self.assertEqual(allowed_url(url), url)
        linkedin = 'https://www.linkedin.com/jobs/view/123456'
        self.assertEqual(allowed_intake_url(linkedin), linkedin)
        self.assertEqual(destination(linkedin).kind, 'source')
        with self.assertRaisesRegex(ValueError, 'direct employer ATS'):
            allowed_url(linkedin)

    def test_tenant_suffixes_and_source_hosts_fail_closed(self):
        rejected = (
            'https://evilmyworkdayjobs.com/job/1',
            'https://tenant.myworkdayjobs.com.evil.test/job/1',
            'https://linkedin.com.evil.test/jobs/view/1',
            'https://user:pass@jobs.lever.co/acme/1',
            'https://jobs.smartrecruiters.com:444/Acme/1',
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                allowed_intake_url(url)

    def test_newly_recognized_ats_cannot_bypass_transmission_readiness(self):
        workday = 'https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/role'
        with patch.dict(os.environ, {
                'JOB_APPLY_TRANSMIT_ENABLED': 'true',
                'JOB_APPLY_TRANSMIT_ATS': 'workday,linkedin,greenhouse',
        }, clear=False):
            self.assertFalse(transmission_allowed(workday))
            self.assertFalse(transmission_allowed('https://www.linkedin.com/jobs/view/123'))
            self.assertTrue(transmission_allowed('https://job-boards.greenhouse.io/acme/jobs/1'))

    def test_request_policy_is_scoped_to_active_platform(self):
        self.assertTrue(request_allowed('acme.wd5.myworkdayjobs.com', 'workday',
                                        'acme.wd5.myworkdayjobs.com'))
        self.assertTrue(request_allowed('static.licdn.com', 'linkedin', 'www.linkedin.com'))
        self.assertFalse(request_allowed('tracking.example.com', 'linkedin', 'www.linkedin.com'))
        self.assertFalse(request_allowed('jobs.lever.co', 'workday',
                                         'acme.wd5.myworkdayjobs.com'))
        self.assertFalse(request_allowed('uploads.s3.amazonaws.com', 'workday',
                                         'acme.wd5.myworkdayjobs.com'))

    def test_linkedin_external_apply_resolves_only_to_registered_ats(self):
        page = Mock()
        page.url = 'https://jobs.lever.co/acme/role'
        page.eval_on_selector_all.return_value = [{
            'href': 'https://www.linkedin.com/jobs/view/externalApply/123',
            'label': 'Apply on company website',
        }]
        self.assertEqual(resolve_linkedin(page), page.url)
        page.goto.assert_called_once()

    def test_workday_entry_navigation_is_read_only_and_unambiguous(self):
        page = Mock()
        page.url = 'https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/role'
        page.evaluate.return_value = {'fields': [], 'buttons': ['Apply Now']}
        apply = Mock()
        apply.count.return_value = 1
        absent = Mock()
        absent.count.return_value = 0
        page.get_by_role.side_effect = lambda role, name: (
            apply if role == 'button' and name.pattern == '^apply\\ now$' else absent
        )
        self.assertTrue(enter_application_form(page))
        apply.click.assert_called_once_with()

    def test_sync_deduplicates_approved_resume(self):
        self.queue.sync_approved()
        self.queue.sync_approved()
        self.assertEqual(len(self.queue.list(('review',))), 1)

    def test_unidentified_checkbox_uses_its_exact_label(self):
        page = Mock()
        page.get_by_label.return_value.count.return_value = 1
        field_locator(page, {'id':'label:Text me', 'label':'Text me'})
        page.get_by_label.assert_called_once_with('Text me', exact=True)
        page.locator.assert_not_called()

    def test_transmission_gate_blocks_manual_auto_and_direct_execution(self):
        version = self.answer()
        with self.store.connect() as db:
            db.execute("UPDATE form_steps SET auto=1 WHERE id='test'")
        page = Mock()
        plan = json.loads(self.queue.get('test')['plan'])
        with patch.dict(os.environ, {
                'JOB_APPLY_TRANSMIT_ENABLED': 'false',
                'JOB_APPLY_TRANSMIT_ATS': '',
        }, clear=False):
            self.assertFalse(transmission_allowed(snapshot()['url']))
            self.assertFalse(self.queue.auto_advance('test'))
            with self.assertRaisesRegex(ValueError, 'Transmission is disabled'):
                self.queue.decide('test', version, 'approve', 1, 1)
            with self.assertRaisesRegex(ValueError, 'transmission is disabled'):
                execute_step(page, plan, {})
        self.assertEqual(self.queue.get('test')['state'], 'review')
        page.locator.assert_not_called()
        page.get_by_role.assert_not_called()

    def test_transmission_requires_matching_ats_allowlist(self):
        version = self.answer()
        with patch.dict(os.environ, {
                'JOB_APPLY_TRANSMIT_ENABLED': 'true',
                'JOB_APPLY_TRANSMIT_ATS': 'greenhouse',
        }, clear=False):
            self.assertFalse(transmission_allowed(snapshot()['url']))
            with self.assertRaisesRegex(ValueError, 'Transmission is disabled'):
                self.queue.decide('test', version, 'approve', 1, 1)
        self.assertEqual(self.queue.get('test')['state'], 'review')

    def test_stale_approved_step_is_returned_to_review(self):
        version = self.answer()
        self.queue.decide('test', version, 'approve', 1, 1)
        with patch.dict(os.environ, {
                'JOB_APPLY_TRANSMIT_ENABLED': 'false',
                'JOB_APPLY_TRANSMIT_ATS': '',
        }, clear=False):
            self.assertTrue(self.queue.defer_transmission('test'))
        self.assertEqual(self.queue.get('test')['state'], 'review')
        with self.store.connect() as db:
            actions = [row['action'] for row in db.execute(
                "SELECT action FROM audit WHERE id='test'")]
        self.assertIn('form_transmission_blocked', actions)

    def test_each_new_step_invalidates_prior_approval(self):
        old = self.answer()
        self.queue.decide('test', old, 'approve', 1, 1)
        self.queue.claim('test', 'approved', 'executing')
        self.queue.claim('test', 'executing', 'inspecting')
        step2 = snapshot()
        step2['action'] = 'submit'
        step2['button'] = 'Submit application'
        self.queue.save_inspection('test', step2)
        self.assertNotEqual(self.queue.get('test')['version'], old)
        with self.assertRaises(ValueError):
            self.queue.decide('test', old, 'approve', 1, 1)


def clearance_group(required=True):
    return {'id': 'group:clearance', 'label': 'Which clearances do you hold?',
            'type': 'checkbox-group', 'required': required,
            'options': [{'value': 'secret', 'label': 'Secret'}, {'value': 'ts', 'label': 'Top Secret'}]}


class SnapshotValidationTests(unittest.TestCase):
    def base(self, **field_overrides):
        field = {'id': 'q', 'label': 'Question', 'type': 'text', 'required': False, 'options': []}
        field.update(field_overrides)
        return {'url': 'https://job-boards.greenhouse.io/example/jobs/1', 'action': 'submit',
                'button': 'Submit application', 'fields': [field]}

    def test_combobox_needs_an_option_list(self):
        with self.assertRaisesRegex(ValueError, 'Select options missing'):
            validate_snapshot(self.base(type='combobox', options=None))
        validate_snapshot(self.base(type='combobox', options=['Yes', 'No']))

    def test_checkbox_group_options_need_a_value_and_label(self):
        with self.assertRaisesRegex(ValueError, 'Checkbox group options missing'):
            validate_snapshot(self.base(type='checkbox-group', options=[]))
        with self.assertRaisesRegex(ValueError, 'value and a label'):
            validate_snapshot(self.base(type='checkbox-group', options=[{'value': 'secret'}]))
        with self.assertRaisesRegex(ValueError, 'Duplicate checkbox group option'):
            validate_snapshot(self.base(type='checkbox-group', options=[
                {'value': 'secret', 'label': 'Secret'}, {'value': 'secret', 'label': 'Secret again'}]))
        validate_snapshot(self.base(type='checkbox-group', options=[{'value': 'secret', 'label': 'Secret'}]))

    def test_valid_answer_for_combobox_matches_employer_options(self):
        field = {'type': 'combobox', 'required': True, 'options': ['United States', 'Canada']}
        self.assertTrue(valid_answer(field, 'United States'))
        self.assertFalse(valid_answer(field, 'Mexico'))

    def test_valid_answer_for_checkbox_group_requires_a_selection_when_required(self):
        group = clearance_group(required=True)
        self.assertTrue(valid_answer(group, ['secret']))
        self.assertTrue(valid_answer(group, ['secret', 'ts']))
        self.assertFalse(valid_answer(group, []))
        self.assertFalse(valid_answer(group, ['secret', 'secret']))
        self.assertFalse(valid_answer(group, ['top_secret']))
        self.assertFalse(valid_answer(group, 'secret'))
        self.assertTrue(valid_answer(clearance_group(required=False), []))


if __name__ == '__main__':
    unittest.main()

