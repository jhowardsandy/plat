import json
import os

import pytest
from typer.testing import CliRunner

from plat.cli import app
from plat.layline_source import read_story
from plat.lifecycle import WorkflowError

CONNECTION = '00000000-0000-4000-8000-000000000001'
CLOUD = '00000000-0000-4000-8000-000000000002'


@pytest.fixture
def exported(tmp_path):
    packet = {'schema': 'layline.plat-story.v1', 'source': {
        'workspace': 'mlg', 'workspace_identity': 'a' * 64, 'connection_id': CONNECTION,
        'cloud_id': CLOUD, 'issue_id': '7'}, 'mapping': 'Engineer-entered mapping',
        'exported_at': '2026-11-04T12:00:00Z', 'story': {
            'key': 'DEV-7', 'title': 'Source title', 'captain': 'unassigned',
            'source_ref': f'jira://{CLOUD}/DEV-7', 'source_revision': '2026-11-04T11:00:00Z',
            'requirements': 'Original description', 'outcomes': ['Correct dates'],
            'scope': 'Manual uploads', 'context_refs': [], 'estimate': ''}}
    path = tmp_path / f'plat-story-{CONNECTION}-DEV-7.json'
    path.write_text(json.dumps(packet))
    return tmp_path, path, packet


def test_cli_and_adapter_preserve_source_and_explain_freshness(exported):
    directory, path, _ = exported
    before = path.read_bytes()
    result = read_story(directory, CONNECTION, 'mlg', CLOUD, 'DEV-7')
    assert result['requirements'] == 'Original description'
    assert result['outcomes'] == ['Correct dates']
    assert result['source_revision'] == ('2026-11-04T11:00:00Z; Layline exported snapshot '
                                         '2026-11-04T12:00:00Z (not a live Jira read)')
    cli = CliRunner().invoke(app, ['workflow', 'layline-story', 'DEV-7', '--directory',
        str(directory), '--connection', CONNECTION, '--workspace', 'mlg', '--cloud-id', CLOUD])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == result
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['schema', 'key', 'workspace', 'cloud', 'timestamp', 'shape'])
def test_invalid_exports_fail_without_writes(exported, change):
    directory, path, packet = exported
    if change == 'schema':
        packet['schema'] = 'v2'
    if change == 'key':
        packet['story']['key'] = 'DEV-8'
    if change == 'workspace':
        packet['source']['workspace'] = 'other'
    if change == 'cloud':
        packet['source']['cloud_id'] = CONNECTION
    if change == 'timestamp':
        packet['exported_at'] = 'yesterday'
    if change == 'shape':
        packet = []
    path.write_text(json.dumps(packet))
    before = path.read_bytes()
    with pytest.raises(WorkflowError):
        read_story(directory, CONNECTION, 'mlg', CLOUD, 'DEV-7')
    assert path.read_bytes() == before


def test_missing_oversized_and_linked_are_distinct_failures(exported):
    directory, path, _ = exported
    with pytest.raises(WorkflowError, match='no Layline export'):
        read_story(directory, CONNECTION, 'mlg', CLOUD, 'DEV-8')
    path.write_bytes(b'x' * 48001)
    with pytest.raises(WorkflowError, match='exceeds'):
        read_story(directory, CONNECTION, 'mlg', CLOUD, 'DEV-7')
    path.unlink()
    other = directory / 'original'
    other.write_text('preserved')
    os.symlink(other, path)
    with pytest.raises(WorkflowError):
        read_story(directory, CONNECTION, 'mlg', CLOUD, 'DEV-7')
    assert other.read_text() == 'preserved'
