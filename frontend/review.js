export function participantReview(p, {readOnly = false, pending = false, stamp = String} = {}) {
  let message = 'Eligible ticket · Not yet arrived. Confirm their arrival below.';
  if (p.checked_in_at != null) message = `Already checked in at ${stamp(p.checked_in_at)} by ${p.checked_in_by}`;
  else if (!p.eligible) message = `Cannot check in · ${p.reason}`;
  else if (readOnly) message = 'Eligible ticket · Not yet arrived.';
  if (readOnly) message += ' Read-only preview: attendance changes are disabled.';
  return {message, disabled: readOnly || !p.eligible || p.checked_in_at != null || pending,
    label: readOnly ? 'Read-only preview' : 'Check in'};
}
