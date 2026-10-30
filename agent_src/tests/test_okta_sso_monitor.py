from core.okta_sso_monitor import failure_for_moodle, format_failure


APP_ID = "0oa18lbblzfwp43Oe698"


def _event(kind="user.authentication.sso", result="FAILURE", app_id=APP_ID):
    return {
        "uuid": "event-123",
        "eventType": kind,
        "actor": {"displayName": "Student Test", "alternateId": "student.test@example.com"},
        "outcome": {"result": result, "reason": "User is not assigned to the app"},
        "target": [{"type": "AppInstance", "id": app_id}],
    }


def test_only_failed_events_for_exact_moodle_app_alert():
    assert failure_for_moodle(_event(), APP_ID)
    assert failure_for_moodle(_event("app.generic.unauth_app_access_attempt"), APP_ID)
    assert failure_for_moodle(_event("application.policy.sign_on.deny_access"), APP_ID)
    assert not failure_for_moodle(_event(result="SUCCESS"), APP_ID)
    assert not failure_for_moodle(_event(app_id="0oa-other"), APP_ID)
    assert not failure_for_moodle(_event(kind="user.session.start"), APP_ID)


def test_alert_contains_user_error_event_and_human_guidance():
    message = format_failure(_event("app.generic.unauth_app_access_attempt"))
    assert "Student Test" in message
    assert "student.test@example.com" in message
    assert "User is not assigned to the app" in message
    assert "event-123" in message
    assert "Entra" in message
    assert "chỉ người" in message
