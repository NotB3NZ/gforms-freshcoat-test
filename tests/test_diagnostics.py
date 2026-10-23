import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from event_checkin.diagnostics import diagnostic_operation, safe_diagnostic


class ProviderError(Exception):
    def __init__(self, code, error):
        super().__init__('DO_NOT_PRINT: credentials participant@example.test token-secret')
        self.response = SimpleNamespace(status_code=code)
        self.error = error


class SafeDiagnosticTests(unittest.TestCase):
    def test_underlying_exception_survives_suppressed_context_without_messages(self):
        @diagnostic_operation('read_selected_original_cell_values')
        def failing():
            try:
                raise ProviderError(400, {'code': 400, 'status': 'INVALID_ARGUMENT',
                                          'message': 'Invalid field selection DO_NOT_PRINT'})
            except ProviderError:
                raise RuntimeError('DO_NOT_PRINT wrapper') from None
        try:
            failing()
        except Exception as exc:
            result = safe_diagnostic(exc)
        self.assertEqual(result, {'stage': 'read_selected_original_cell_values', 'operation': None, 'exception_class': 'ProviderError',
                                  'http_status': 400, 'google_api_reason': 'invalid_field_mask'})
        self.assertNotIn('DO_NOT_PRINT', json.dumps(result))

    def test_untrusted_reason_message_status_and_payload_never_echoed(self):
        error = ProviderError(403, {'message': 'private participant name', 'status': 'private@example.test',
                                    'errors': [{'reason': 'token-secret'}], 'payload': {'password': 'private'}})
        self.assertEqual(safe_diagnostic(error)['google_api_reason'], 'unrecognized_reason_withheld')
        for private in ('private', '@', 'token-secret', 'password', 'payload'):
            self.assertNotIn(private, json.dumps(safe_diagnostic(error)))

    def test_known_reasons_and_non_http_exceptions_are_bounded(self):
        self.assertEqual(safe_diagnostic(ProviderError(429, {'status': 'RESOURCE_EXHAUSTED'}))['google_api_reason'],
                         'RESOURCE_EXHAUSTED')
        result = safe_diagnostic(KeyError('participant@example.test'))
        self.assertEqual(result['exception_class'], 'KeyError')
        self.assertIsNone(result['http_status'])
        self.assertIsNone(result['google_api_reason'])
        self.assertNotIn('@', json.dumps(result))

    def test_inner_stage_is_preserved_and_exception_is_not_replaced(self):
        original = ValueError('DO_NOT_PRINT')
        @diagnostic_operation('read_selected_original_cell_values')
        def inner():
            raise original
        @diagnostic_operation('validate_single_registration')
        def outer():
            inner()
        with self.assertRaises(ValueError) as captured:
            outer()
        self.assertIs(captured.exception, original)
        self.assertEqual(safe_diagnostic(captured.exception)['stage'], 'read_selected_original_cell_values')

    def test_malformed_provider_metadata_never_printed(self):
        for data in (None, [], {}, {'code': 'private', 'errors': None}, {'errors': 'private'}):
            error = ProviderError(None, data)
            result = safe_diagnostic(error)
            self.assertIsNone(result['http_status'])
            self.assertNotIn('private', json.dumps(result))

    def test_cli_reports_safe_metadata_instead_of_hiding_the_original_exception(self):
        from event_checkin import production_cli
        from event_checkin.config import ConfigurationError
        error = ProviderError(400, {'status': 'INVALID_ARGUMENT', 'message': 'DO_NOT_PRINT payload'})
        settings = SimpleNamespace(profile='production', event_id='aws-scd-mindanao-2026')
        args = SimpleNamespace(command='production-render-test')
        with patch('event_checkin.controlled_render.run_command', side_effect=error):
            with self.assertRaises(ConfigurationError) as captured:
                production_cli.run(settings, args)
        message = str(captured.exception)
        self.assertIn('ProviderError', message)
        self.assertIn('400', message)
        self.assertIn('INVALID_ARGUMENT', message)
        for private in ('DO_NOT_PRINT', 'participant@', 'token-secret', 'payload'):
            self.assertNotIn(private, message)
