from ..config import load
from .celery_app import app
from .inline import InlineDispatcher


@app.task(name='plat.execute', acks_late=True, reject_on_worker_lost=True)
def execute(plat_id, installation, workers=1):
    cfg = load()
    if cfg.origin_id != installation:
        raise ValueError('job belongs to a different installation; local paths cannot be reassigned')
    return InlineDispatcher().run(cfg, plat_id, workers=workers)
