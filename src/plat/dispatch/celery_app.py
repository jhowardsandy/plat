"""Host-routed workers. Repository paths and PIDs never move between hosts."""
from celery import Celery

from ..config import load

cfg = load()
app = Celery('plat', broker=cfg.broker_url, include=['plat.dispatch.tasks'])
app.conf.update(task_serializer='json', accept_content=['json'], result_serializer='json',
                task_acks_late=True, worker_prefetch_multiplier=1,
                task_default_queue='plat.' + cfg.origin_id)
