from fastapi.testclient import TestClient

from app.main import app


def test_root_serves_html_ui() -> None:
    client = TestClient(app)
    response = client.get('/')
    assert response.status_code == 200
    content = response.text.lower()
    assert 'fomoprofletracker' in content
    assert '<html' in content

    app_script = client.get('/static/app.js')
    assert app_script.status_code == 200
    assert "hourCycle: 'h23'" in app_script.text
    assert "'date', 'delete'" in app_script.text
    assert "'dismiss-risk-token'" in app_script.text
    assert "method: config.action === 'dismiss' ? 'POST' : 'DELETE'" in app_script.text
    assert "let deleteRowsInProgress = false" in app_script.text
    assert "deleteRowsInProgress = false;" in app_script.text
    assert "label: 'FH', title: 'Fomo holders (FH)'" in app_script.text
    assert "multiple.toFixed" not in app_script.text

    health = client.get('/api/v1/health')
    assert health.status_code == 200
    monitor = health.json()['fomo_activity_monitor']
    assert monitor['state'] in {'disabled', 'starting', 'connecting', 'syncing', 'waiting', 'unavailable', 'stopped'}

    legacy = client.get('/legacy').text
    assert 'data-tab="discoveries"' not in legacy
    assert 'data-tab="market"' not in legacy
