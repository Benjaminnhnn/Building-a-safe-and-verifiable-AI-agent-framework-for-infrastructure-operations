from core.schema.event_schema import normalize_alertmanager_payload


def test_postgresql_chain_gets_one_replayable_correlation_id() -> None:
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "MoodleNodeRdsTcpFailed",
                    "instance": "moodle-app-b",
                    "service": "moodle",
                    "component": "postgres-staging",
                    "environment": "staging",
                    "severity": "critical",
                },
                "startsAt": "2026-08-24T09:00:00Z",
                "fingerprint": "pg-down-1",
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "MoodleRdsTcpProbeFailed",
                    "instance": "moodle-app-b",
                    "service": "moodle",
                    "environment": "staging",
                    "severity": "critical",
                },
                "startsAt": "2026-08-24T09:00:15Z",
                "fingerprint": "api-down-1",
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "MoodleWebContainerMissing",
                    "instance": "moodle-app-a",
                    "service": "frontend",
                    "environment": "staging",
                    "severity": "critical",
                },
                "startsAt": "2026-08-24T09:00:30Z",
                "fingerprint": "frontend-down-1",
            },
        ],
    }

    first = normalize_alertmanager_payload(payload, received_at="2026-08-24T09:00:31Z")
    second = normalize_alertmanager_payload(payload, received_at="2026-08-24T09:05:00Z")

    assert len(first["alerts"]) == 3
    assert len({event["correlation_id"] for event in first["alerts"]}) == 1
    assert [event["event_id"] for event in first["alerts"]] == [
        event["event_id"] for event in second["alerts"]
    ]
    assert first["alerts"][0]["component_id"] == "postgres-staging"
    assert first["alerts"][2]["host_id"] == "moodle-app-a"
