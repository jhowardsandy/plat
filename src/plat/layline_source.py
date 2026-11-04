"""Read an explicitly exported Layline snapshot. No Jira or Keychain access."""
import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path

from . import intake
from .lifecycle import WorkflowError
from .spec import NAME


def read_story(directory, connection, workspace, cloud_id, key):
    try:
        if str(uuid.UUID(connection)) != connection or str(uuid.UUID(cloud_id)) != cloud_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise WorkflowError('connection and cloud identity must be canonical UUIDs') from exc
    if not isinstance(key, str) or len(key) > 160 or not NAME.fullmatch(key):
        raise WorkflowError('use a safe ticket key')
    path = Path(directory) / f'plat-story-{connection}-{key}.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkflowError('Layline export must be an unlinked regular file')
            raw = source.read(48001)
    except FileNotFoundError as exc:
        raise WorkflowError('no Layline export for this ticket; review and export it in Layline') from exc
    except OSError as exc:
        raise WorkflowError('Layline export unavailable; check its path and access') from exc
    if len(raw) > 48000:
        raise WorkflowError('Layline export exceeds 48000 bytes')
    try:
        packet = json.loads(raw)
        if not isinstance(packet, dict) or set(packet) != {'schema', 'source', 'story', 'mapping', 'exported_at'}:
            raise ValueError
        if packet['schema'] != 'layline.plat-story.v1':
            raise ValueError
        origin = packet['source']
        if (origin['connection_id'] != connection or origin['workspace'] != workspace
                or origin['cloud_id'] != cloud_id or not origin['issue_id']):
            raise ValueError
        exported = datetime.fromisoformat(packet['exported_at'])
        if exported.tzinfo is None:
            raise ValueError
        story = packet['story']
        if story['key'] != key or story['source_ref'] != f'jira://{cloud_id}/{key}':
            raise ValueError
        story = intake.preview(story)['story']
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise WorkflowError('malformed or mismatched Layline snapshot; export again') from exc
    story['source_revision'] += f"; Layline exported snapshot {packet['exported_at']} (not a live Jira read)"
    return intake.preview(story)['story']
