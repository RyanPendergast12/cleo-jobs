import os
import tempfile
import unittest
from pathlib import Path
from jobapply_store import Store
from jobapply_skill import (DRAFT_SCHEMA, MAX_OUTPUT_TOKENS, REQUIRED_EVIDENCE_CHECKS,
                            SkillDraftError, call_claude,
                            enrich_job_posting, load_skill, render_docx,
                            render_pdf, validate_result)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'private.db'))
        self.job = {'title': 'Patient Access Representative', 'url': 'https://example.com/job', 'fit': {'score': 70}}
        self.packet = {'job_url': self.job['url'], 'skill_version': 'test-only', 'form_complete': True,
            'missing_fields': [], 'evidence': {'e1': 'test evidence'},
            'resume_claims': [{'value': 'Test claim', 'evidence_ids': ['e1']}],
            'answers': {'name': {'value': 'Test', 'evidence_ids': ['e1']}}}
        self.store.enqueue('abc', self.job)

    def prepare(self):
        return self.store.prepare('abc', self.packet, b'%PDF-test fixture')

    def test_threshold_blockers_and_dedup(self):
        self.store.enqueue('abc', self.job)
        self.assertEqual(len(self.store.pending_cards()), 1)
        for score in [69.99, None, True, float('nan'), 101]:
            with self.assertRaises(ValueError):
                self.store.enqueue('other', dict(self.job, fit={'score': score}))
        with self.assertRaises(ValueError):
            self.store.enqueue('other', dict(self.job, fit={'score': 99, 'eligibility': ['BLOCKER: clearance']}))

    def test_incomplete_or_unsupported_answers_blocked(self):
        self.packet['missing_fields'] = ['authorization']
        with self.assertRaises(ValueError): self.prepare()
        self.packet['missing_fields'] = []
        self.packet['answers']['name']['evidence_ids'] = ['unknown']
        with self.assertRaises(ValueError): self.prepare()

    def test_owner_only_and_exact_version(self):
        first = self.prepare()
        with self.assertRaises(ValueError): self.store.decide('abc', first, 'apply', 2, 1)
        self.packet['answers']['name']['value'] = 'Revised'
        second = self.prepare()
        with self.assertRaises(ValueError): self.store.decide('abc', first, 'apply', 1, 1)
        self.assertEqual(self.store.decide('abc', second, 'apply', 1, 1), 'approved_waiting_adapter')
        with self.assertRaises(ValueError): self.store.decide('abc', second, 'apply', 1, 1)
        with self.assertRaises(ValueError): self.prepare()

    def test_restart_and_changes(self):
        digest = self.prepare()
        self.store = Store(self.store.path)
        self.store.decide('abc', digest, 'changes', 1, 1)
        self.prepare()
        self.assertEqual(self.store.get('abc')['state'], 'ready')

    def test_outbox_retries_after_alert_candidate_disappears(self):
        import os
        import sqlite3
        from contextlib import closing
        from dataclasses import dataclass
        from unittest.mock import patch, Mock
        import jobwatch_apply as intake
        @dataclass
        class Job:
            fit: dict
            def fingerprint(self): return 'outbox1'
        path = str(Path(self.tmp.name) / 'scanner.db')
        with patch.dict(os.environ, {'JOB_APPLY_SERVICE_URL': 'https://worker.test',
                                     'JOB_APPLY_INGEST_TOKEN': 'x' * 32}):
            with patch('requests.post', return_value=Mock(status_code=503)):
                intake.run_pass([Job({'score': 70})], path)
            with patch('requests.post', return_value=Mock(status_code=200)) as post:
                intake.run_pass([], path)
                self.assertEqual(post.call_count, 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT delivered FROM apply_outbox').fetchone()[0], 1)

    def test_resume_bytes_bound_to_approval(self):
        first = self.prepare()
        second = self.store.prepare('abc', self.packet, b'%PDF-different bytes')
        self.assertNotEqual(first, second)

    def skill_result(self):
        evidence = {'E1': 'Cleo Master Resume', 'E2': 'Private application profile'}
        return {
            'evidence_check': {name: 'checked, applicable evidence selected'
                               for name in REQUIRED_EVIDENCE_CHECKS},
            'disqualifier_screen': {'status': 'pass', 'items': [],
                                    'customer_overlap': 'exact list unavailable'},
            'analysis': {'priority_analysis': ['Patient-access operations fit'],
                         'match_score': {'overall': 82, 'ats': 80,
                                         'technical': 85, 'seniority': 81}},
            'evidence': evidence,
            'resume': {
                'contact': {'value': 'Alex Example | Example City', 'evidence_ids': ['E1']},
                'headline': {'value': 'Patient Access Representative', 'evidence_ids': ['E1']},
                'summary': {'value': 'Public health graduate with patient-facing medical office experience.',
                            'evidence_ids': ['E1']},
                'skills': [{'label': 'Patient Operations', 'value': 'Scheduling, intake, insurance verification',
                            'evidence_ids': ['E1']}],
                'experience': [{'heading': 'Medical Office Assistant | Example Clinic | 2025-Present',
                                'evidence_ids': ['E1'],
                                'bullets': [{'value': 'Managed patient scheduling, intake and insurance verification.',
                                             'evidence_ids': ['E1']}]}],
                'education': [{'value': 'B.S. Public Health, Example University',
                               'evidence_ids': ['E1']}],
                'certifications': [],
                'projects': [],
            },
            'answers': {
                'work_authorization': {'value': 'Example authorization answer', 'evidence_ids': ['E2']},
                'relocation': {'value': 'Example relocation preference',
                               'evidence_ids': ['E2']},
            },
            'form_complete': False,
            'missing_fields': ['Employer application form has not been inspected'],
        }

    def test_skill_is_versioned_and_draft_artifacts_are_bound(self):
        skill_path = Path(os.environ.get(
            'JOB_APPLY_SKILL_PATH',
            Path(__file__).parent / 'skills' / 'healthcare-career-strategist' / 'SKILL.md'))
        _, version = load_skill(str(skill_path))
        result = self.skill_result()
        packet = validate_result(result, self.job, version)
        pdf, docx = render_pdf(result['resume']), render_docx(result['resume'])
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertTrue(docx.startswith(b'PK'))
        digest = self.store.save_draft('abc', packet, pdf, docx)
        row = self.store.get('abc')
        self.assertEqual(row['state'], 'draft_ready')
        self.assertEqual(row['digest'], digest)
        with self.assertRaises(ValueError):
            self.store.decide('abc', digest, 'apply', 1, 1)

    def test_resume_renderer_matches_reference_architecture(self):
        import io
        import re
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        result = self.skill_result()
        docx_bytes = render_docx(result['resume'])
        pdf_bytes = render_pdf(result['resume'])
        doc = Document(io.BytesIO(docx_bytes))
        section = doc.sections[0]

        self.assertAlmostEqual(section.top_margin.inches, 31 / 72, places=2)
        self.assertAlmostEqual(section.bottom_margin.inches, 23.5 / 72, places=2)
        self.assertAlmostEqual(section.left_margin.inches, 31 / 72, places=2)
        self.assertAlmostEqual(section.right_margin.inches, 31 / 72, places=2)

        paragraphs = [p for p in doc.paragraphs if p.text.strip()]
        self.assertEqual(paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.CENTER)
        self.assertTrue(paragraphs[0].runs[0].bold)
        self.assertEqual(paragraphs[0].runs[0].font.size.pt, 15)
        self.assertEqual(paragraphs[1].alignment, WD_ALIGN_PARAGRAPH.CENTER)
        self.assertEqual(paragraphs[2].alignment, WD_ALIGN_PARAGRAPH.CENTER)

        by_text = {p.text: p for p in paragraphs}
        for heading in ('SUMMARY', 'CORE SKILLS',
                        'PROFESSIONAL EXPERIENCE',
                        'EDUCATION AND CERTIFICATIONS'):
            self.assertIn(heading, by_text)
            self.assertTrue(by_text[heading].runs[0].bold)
            from docx.oxml.ns import qn
            self.assertIsNotNone(by_text[heading]._p.pPr.find(qn("w:pBdr")))

        skill = next(p for p in paragraphs if p.text.startswith('Patient Operations:'))
        self.assertTrue(skill.runs[0].bold)
        role = next(p for p in paragraphs if p.text.startswith('Medical Office Assistant'))
        self.assertTrue(role.runs[0].bold)
        self.assertIn('\t', role.text)
        bullet = next(p for p in paragraphs if p.text.startswith('Managed patient scheduling'))
        self.assertEqual(bullet.style.name, 'List Bullet')
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", pdf_bytes)), 1)

    def test_skill_rejects_missing_evidence_audit_and_fake_completion(self):
        result = self.skill_result()
        result['evidence_check'].pop(REQUIRED_EVIDENCE_CHECKS[0])
        with self.assertRaises(ValueError):
            validate_result(result, self.job, 'sha256:test')
        result = self.skill_result()
        result['form_complete'] = True
        result['missing_fields'] = []
        with self.assertRaises(ValueError):
            validate_result(result, self.job, 'sha256:test')

    def test_failed_preparation_retry_is_owner_only(self):
        self.store.skill_failed('abc', 'temporary failure', '2999-01-01T00:00:00+00:00')
        with self.assertRaises(ValueError):
            self.store.retry_skill('abc', 2, 1)
        self.assertEqual(self.store.retry_skill('abc', 1, 1), 'awaiting_skill')
        row = self.store.get('abc')
        self.assertIsNone(row['prepare_error'])
        self.assertIsNone(row['prepare_after'])
        self.assertEqual(row['prepare_attempts'], 0)
        self.assertEqual(len(self.store.pending_skill()), 1)

    def test_owner_can_approve_or_request_a_new_resume_version(self):
        result = self.skill_result()
        packet = validate_result(result, self.job, 'sha256:test')
        pdf, docx = render_pdf(result['resume']), render_docx(result['resume'])
        first = self.store.save_draft('abc', packet, pdf, docx)
        with self.assertRaises(ValueError):
            self.store.decide_draft('abc', first, 'approve', 2, 1)

        self.assertEqual(
            self.store.decide_draft(
                'abc', first, 'changes', 1, 1,
                'Emphasize patient scheduling and shorten the summary.',
            ),
            'changes_requested',
        )
        row = self.store.get('abc')
        self.assertEqual(row['state'], 'changes_requested')
        self.assertIn('patient scheduling', row['revision_request'])
        self.assertEqual(len(self.store.pending_skill()), 1)

        result['resume']['summary']['value'] = (
            'Detection engineer with enterprise investigation experience.'
        )
        revised_packet = validate_result(result, self.job, 'sha256:test')
        revised_pdf = render_pdf(result['resume'])
        revised_docx = render_docx(result['resume'])
        second = self.store.save_draft(
            'abc', revised_packet, revised_pdf, revised_docx)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.store.get('abc')['revision_request'])
        with self.assertRaises(ValueError):
            self.store.decide_draft('abc', first, 'approve', 1, 1)
        self.assertEqual(
            self.store.decide_draft('abc', second, 'approve', 1, 1),
            'resume_approved',
        )
        with self.assertRaises(ValueError):
            self.store.decide('abc', second, 'apply', 1, 1)

    def test_online_posting_enrichment_supplies_full_public_text(self):
        from unittest.mock import Mock, patch
        response = Mock(status_code=200)
        response.headers = {'Content-Type': 'text/html; charset=utf-8'}
        response.encoding = 'utf-8'
        response.iter_content.return_value = [(
            b'<html><body><main><h1>Security Engineer</h1>'
            b'<p>This role is hybrid in Irvine, California and requires a '
            b'current Top Secret clearance. The engineer will manage identity, '
            b'endpoint security, incident response, vulnerability remediation, '
            b'cloud controls, and security automation across the environment.</p>'
            b'<p>Applicants need five years of relevant experience and strong '
            b'communication skills for cross-functional work.</p>'
            b'</main></body></html>'
        )]
        public_dns = [
            (2, 1, 6, '', ('93.184.216.34', 443)),
        ]
        job = {
            'url': 'https://jobs.example.com/security-engineer',
            'description': 'Saved scanner description remains primary.',
        }
        with patch('jobwatch_enrichment.socket.getaddrinfo',
                   return_value=public_dns), \
             patch('jobwatch_enrichment.requests.get',
                   return_value=response) as get:
            enriched = enrich_job_posting(job)

        self.assertEqual(enriched['description'], job['description'])
        self.assertEqual(enriched['online_posting']['status'], 'retrieved')
        self.assertIn('requires a current Top Secret clearance',
                      enriched['online_posting']['text'])
        self.assertIn('hybrid in Irvine', enriched['online_posting']['text'])
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_linkedin_posting_uses_public_job_detail_endpoint(self):
        from unittest.mock import patch
        job = {
            'url': 'https://www.linkedin.com/jobs/view/4464057272/',
            'title': 'Information Security Officer',
            'company': 'STOPSO',
            'description': 'Saved description.',
        }

        def fetched(url):
            if '/jobs-guest/jobs/api/jobPosting/4464057272' in url:
                return {
                    'status': 'retrieved',
                    'source_url': url,
                    'final_url': url,
                    'text': (
                        'Information Security Officer at STOPSO. Requirements, '
                        'responsibilities, location, qualifications, and benefits. '
                    ) * 4,
                    'retrieved_chars': 420,
                }
            return {'status': 'unavailable', 'source_url': url}

        with patch('jobwatch_enrichment.fetch_online_posting',
                   side_effect=fetched) as fetch:
            enriched = enrich_job_posting(job)

        self.assertIn('/jobs-guest/jobs/api/jobPosting/4464057272',
                      fetch.call_args_list[0].args[0])
        self.assertEqual(enriched['online_posting']['method'],
                         'linkedin_guest')
        self.assertEqual(enriched['online_posting']['retrieved_chars'], 420)

    def test_web_search_fallback_requires_matching_title_and_company(self):
        from unittest.mock import patch
        job = {
            'url': 'https://www.linkedin.com/jobs/view/4464057272/',
            'title': 'Information Security Officer',
            'company': 'STOPSO',
            'description': 'Saved description.',
        }
        company_url = 'https://careers.stopso.example/information-security-officer'

        def fetched(url):
            if url == company_url:
                text = (
                    'STOPSO Information Security Officer responsibilities include '
                    'security operations, governance, incident response, risk '
                    'management, and compliance. Required qualifications and '
                    'location details follow. '
                ) * 3
                return {
                    'status': 'retrieved',
                    'source_url': url,
                    'final_url': url,
                    'text': text,
                    'retrieved_chars': len(text),
                }
            return {'status': 'unavailable', 'source_url': url}

        with patch('jobwatch_enrichment.fetch_online_posting',
                   side_effect=fetched), \
             patch('jobwatch_enrichment._fetch_search_links',
                   return_value=[company_url]):
            enriched = enrich_job_posting(job)

        self.assertEqual(enriched['online_posting']['method'], 'web_search')
        self.assertEqual(enriched['online_posting']['matched_url'], company_url)
        self.assertIn('STOPSO Information Security Officer',
                      enriched['online_posting']['text'])

    def test_search_result_links_decode_redirect_and_ignore_search_engines(self):
        from jobwatch_enrichment import _search_result_links
        html = (
            '<a href="/l/?uddg=https%3A%2F%2Fcareers.example.com%2Fjob%2F123">'
            'result</a>'
            '<a href="https://www.google.com/search?q=ignored">ignored</a>'
        )
        self.assertEqual(
            _search_result_links(html),
            ['https://careers.example.com/job/123'],
        )

    def test_online_posting_enrichment_rejects_private_network_url(self):
        from unittest.mock import patch
        with patch('jobwatch_enrichment.requests.get') as get:
            enriched = enrich_job_posting({
                'url': 'http://127.0.0.1/private-job',
                'description': 'A complete saved description.',
            })
        self.assertEqual(enriched['online_posting']['status'], 'unavailable')
        get.assert_not_called()

    def test_online_posting_failure_keeps_saved_description(self):
        from unittest.mock import patch
        job = {
            'url': 'https://jobs.example.com/security-engineer',
            'description': 'Use this saved description when lookup fails.',
        }
        public_dns = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch('jobwatch_enrichment.socket.getaddrinfo',
                   return_value=public_dns), \
             patch('jobwatch_enrichment.requests.get', side_effect=OSError):
            enriched = enrich_job_posting(job)
        self.assertEqual(enriched['description'], job['description'])
        self.assertEqual(enriched['online_posting']['status'], 'unavailable')

    def test_anthropic_error_message_is_safe_and_actionable(self):
        from unittest.mock import Mock, patch
        response = Mock(status_code=400)
        response.json.return_value = {
            'error': {
                'type': 'invalid_request_error',
                'message': 'Your credit balance is too low to access the API',
            }
        }
        job = dict(self.job, description='x' * 201)
        with patch.dict(os.environ, {
                'ANTHROPIC_API_KEY': 'test-key',
                'ANTHROPIC_MODEL': 'claude-sonnet-5',
        }):
            with patch('jobapply_skill.requests.post', return_value=response):
                with self.assertRaisesRegex(SkillDraftError, 'credit balance'):
                    call_claude(job, 'test skill')

    def test_claude_uses_structured_output_and_normalizes_dynamic_fields(self):
        import json
        from unittest.mock import Mock, patch

        result = self.skill_result()
        result['evidence'] = [
            {'id': evidence_id, 'source': source}
            for evidence_id, source in result['evidence'].items()
        ]
        result['answers'] = [
            {'field': field, **answer}
            for field, answer in result['answers'].items()
        ]
        response = Mock(status_code=200)
        response.json.return_value = {
            'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(result)}],
        }
        job = dict(self.job, description='x' * 201)
        with patch.dict(os.environ, {
                'ANTHROPIC_API_KEY': 'test-key',
                'ANTHROPIC_MODEL': 'claude-sonnet-5',
        }):
            with patch('jobapply_skill.requests.post', return_value=response) as post:
                normalized = call_claude(job, 'test skill')

        request = post.call_args.kwargs['json']
        self.assertEqual(request['max_tokens'], 24000)
        self.assertIn('narrow resume-draft stage', request['system'])
        self.assertEqual(
            request['output_config']['format']['type'], 'json_schema')
        self.assertIs(request['output_config']['format']['schema'], DRAFT_SCHEMA)
        self.assertEqual(post.call_args.kwargs['timeout'], (10, 420))
        # Keep repeated regex constraints out of the actual API request.
        # Nonblank and evidence checks run locally before draft persistence.
        self.assertNotIn('"pattern":', json.dumps(request['output_config']['format']['schema']))
        self.assertEqual(normalized['evidence']['E1'], 'Cleo Master Resume')
        self.assertEqual(normalized['answers']['work_authorization']['value'],
                         'Example authorization answer')

    def test_claude_rejects_incomplete_structured_output(self):
        from unittest.mock import Mock, patch

        response = Mock(status_code=200)
        response.json.return_value = {
            'stop_reason': 'max_tokens',
            'content': [{'type': 'text', 'text': '{'}],
        }
        job = dict(self.job, description='x' * 201)
        with patch.dict(os.environ, {
                'ANTHROPIC_API_KEY': 'test-key',
                'ANTHROPIC_MODEL': 'claude-sonnet-5',
        }):
            with patch('jobapply_skill.requests.post', return_value=response):
                with self.assertRaisesRegex(SkillDraftError, f'{MAX_OUTPUT_TOKENS}-token limit'):
                    call_claude(job, 'test skill')

    def test_claim_errors_identify_path_without_disclosing_claim_text(self):
        cases = [
            ({'value': '', 'evidence_ids': ['E1']}, '.value: must be nonblank text'),
            ({'value': ' \n', 'evidence_ids': ['E1']}, '.value: must be nonblank text'),
            ({'value': 'PRIVATE CLAIM', 'evidence_ids': []}, '.evidence_ids: requires a nonempty list'),
            ({'value': 'PRIVATE CLAIM', 'evidence_ids': 'E1'}, '.evidence_ids: requires a nonempty list'),
            ({'value': 'PRIVATE CLAIM', 'evidence_ids': [{}]}, '.evidence_ids: IDs must be nonblank strings'),
            ({'value': 'PRIVATE CLAIM', 'evidence_ids': ['unknown']}, '.evidence_ids: references unknown evidence'),
        ]
        for item, suffix in cases:
            with self.subTest(item=item):
                result = self.skill_result()
                result['resume']['experience'][0]['bullets'][0] = item
                with self.assertRaises(SkillDraftError) as caught:
                    validate_result(result, self.job, 'test')
                self.assertEqual(str(caught.exception), 'resume.experience[0].bullets[0]' + suffix)
                self.assertNotIn('PRIVATE CLAIM', str(caught.exception))

    def test_uninspected_form_can_have_no_answers_and_remains_blocked(self):
        from jobapply_skill import _normalize_structured_result
        result = self.skill_result()
        result['answers'] = []
        result['resume']['education'] = []
        result['resume']['certifications'] = []
        result = _normalize_structured_result(result)
        packet = validate_result(result, self.job, 'test')
        self.assertEqual(packet['answers'], {})
        digest = self.store.save_draft('abc', packet, render_pdf(result['resume']),
                                       render_docx(result['resume']))
        self.assertEqual(self.store.get('abc')['state'], 'draft_ready')
        with self.assertRaises(ValueError):
            self.store.decide('abc', digest, 'apply', 1, 1)

    def test_invalid_claude_claim_never_reaches_draft_storage(self):
        from unittest.mock import Mock, patch
        from jobapply_skill import prepare_candidate
        import json
        for claim in ({'value': '   ', 'evidence_ids': ['E1']},
                      {'value': 'Unsupported claim', 'evidence_ids': ['missing']}):
            with self.subTest(claim=claim):
                result = self.skill_result()
                result['resume']['summary'] = claim
                store = Mock()
                store.get.return_value = {'job': json.dumps(self.job)}
                with patch('jobapply_skill.enrich_job_posting',
                           side_effect=lambda job: job), \
                     patch('jobapply_skill.load_skill',
                           return_value=('skill', 'test')), \
                     patch('jobapply_skill.call_claude', return_value=result):
                    with self.assertRaises(SkillDraftError):
                        prepare_candidate(store, 'abc', 'unused')
                store.save_draft.assert_not_called()

    def test_required_clinical_license_stops_draft_before_claude(self):
        from unittest.mock import Mock, patch
        from jobapply_skill import prepare_candidate
        import json
        enriched = dict(self.job, online_posting={
            'status': 'retrieved',
            'text': 'Remote in the United States. This role requires an active registered nurse license.',
        })
        store = Mock()
        store.get.return_value = {'job': json.dumps(self.job)}
        with patch('jobapply_skill.enrich_job_posting', return_value=enriched), \
             patch('jobapply_skill.load_skill', return_value=('skill', 'test')), \
             patch('jobapply_skill.call_claude') as call_claude:
            with self.assertRaisesRegex(SkillDraftError, 'clinical license'):
                prepare_candidate(store, 'abc', 'unused')
            call_claude.assert_not_called()
        store.save_draft.assert_not_called()

    def test_other_enrichment_blockers_stop_draft_before_claude(self):
        from unittest.mock import Mock, patch
        from jobapply_skill import prepare_candidate
        import json
        cases = {
            'minimum experience': (
                'Requires 8 years of experience. Patient scheduling, insurance verification, '
                'patient intake and Phreesia. Remote in the United States.',
                'required minimum'),
            'location/work mode': (
                'Hybrid role in Boston, MA. Requires 2 years of experience. '
                'Patient scheduling, insurance verification, patient intake and Phreesia.',
                'location'),
            'certification': (
                'Remote in the United States. Requires 1 year of experience. '
                'Patient scheduling and insurance verification. BLS certification is required.',
                'CPR/BLS'),
        }
        for label, (text, reason) in cases.items():
            with self.subTest(blocker=label):
                enriched = dict(self.job, online_posting={
                    'status': 'retrieved',
                    'text': text,
                })
                store = Mock()
                store.get.return_value = {'job': json.dumps(self.job)}
                with patch('jobapply_skill.enrich_job_posting',
                           return_value=enriched), \
                     patch('jobapply_skill.load_skill',
                           return_value=('skill', 'test')), \
                     patch('jobapply_skill.call_claude') as call_claude:
                    with self.assertRaisesRegex(SkillDraftError, reason):
                        prepare_candidate(store, 'abc', 'unused')
                    call_claude.assert_not_called()
                store.save_draft.assert_not_called()

    def test_unretrieved_online_posting_does_not_block_draft(self):
        from unittest.mock import Mock, patch
        from jobapply_skill import prepare_candidate
        import json
        enriched = dict(self.job, online_posting={'status': 'unavailable'})
        result = self.skill_result()
        store = Mock()
        store.get.return_value = {'job': json.dumps(self.job)}
        with patch('jobapply_skill.enrich_job_posting', return_value=enriched), \
             patch('jobapply_skill.load_skill', return_value=('skill', 'test')), \
             patch('jobapply_skill.call_claude', return_value=result):
            prepare_candidate(store, 'abc', 'unused')
        store.save_draft.assert_called_once()

    def test_blank_answer_is_rejected_instead_of_silently_removed(self):
        result = self.skill_result()
        result['answers'] = {'private question': {'value': '', 'evidence_ids': ['E1']}}
        with self.assertRaisesRegex(SkillDraftError, r'answers\[0\].value'):
            validate_result(result, self.job, 'test')

    def test_evidence_normalization_rejects_null_and_duplicate_sources(self):
        from jobapply_skill import _normalize_structured_result
        for rows in ([{'id': 'E1', 'source': None}],
                     [{'id': None, 'source': 'source'}],
                     [{'id': 'E1', 'source': 'source'}, {'id': 'E1', 'source': 'other'}]):
            with self.subTest(rows=rows), self.assertRaises(SkillDraftError):
                _normalize_structured_result({'evidence': rows})

if __name__ == '__main__': unittest.main()
