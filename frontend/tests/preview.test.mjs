import test from 'node:test';
import assert from 'node:assert/strict';
import {participantReview} from '../review.js';

const person = {eligible: true, checked_in_at: null, name: 'Preview attendee'};
test('preview cannot offer admission for an otherwise valid ticket', () => {
  const result = participantReview(person, {readOnly: true});
  assert.equal(result.disabled, true);
  assert.equal(result.label, 'Read-only preview');
  assert.match(result.message, /attendance changes are disabled/);
  assert.doesNotMatch(result.message, /Confirm their arrival/);
});
test('preview preserves existing attendance and ineligible reasons', () => {
  const arrived = participantReview({...person, checked_in_at: 123, checked_in_by: 'Benz'}, {readOnly: true, stamp: () => '8:30 AM'});
  assert.match(arrived.message, /8:30 AM by Benz/);
  assert.equal(arrived.disabled, true);
  const invalid = participantReview({...person, eligible: false, reason: 'Cancelled'}, {readOnly: true});
  assert.match(invalid.message, /Cancelled/);
  assert.equal(invalid.disabled, true);
});
test('operational review still requires eligible unused ticket and no pending attempt', () => {
  assert.equal(participantReview(person).disabled, false);
  assert.equal(participantReview(person, {pending: true}).disabled, true);
  assert.equal(participantReview({...person, checked_in_at: 123}).disabled, true);
  assert.equal(participantReview({...person, eligible: false}).disabled, true);
});
