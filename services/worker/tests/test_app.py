import importlib

from manifest_worker.app import TASK_MODULES, celery_app
from manifest_worker.topology import DELIVERY_LIMIT, STAGES, dead_letter_queue, work_queue


def test_backpressure_settings() -> None:
    conf = celery_app.conf
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True


def test_every_stage_module_is_registered_and_imports() -> None:
    assert list(celery_app.conf.include) == TASK_MODULES
    for name in TASK_MODULES:
        importlib.import_module(name)
    registered = {name for name in celery_app.tasks if name.startswith("manifest.")}
    extra = {"manifest.reextract_document", "manifest.reconcile_shipment"}
    assert registered == {f"manifest.{stage}" for stage in STAGES} | extra


def test_each_stage_is_a_quorum_queue_dead_lettering_to_its_dlq() -> None:
    assert [queue.name for queue in celery_app.conf.task_queues] == list(STAGES)
    for stage in STAGES:
        arguments = work_queue(stage).queue_arguments
        assert arguments["x-queue-type"] == "quorum"
        assert arguments["x-delivery-limit"] == DELIVERY_LIMIT == 5
        assert arguments["x-dead-letter-exchange"] == dead_letter_queue(stage).exchange.name
        assert arguments["x-dead-letter-routing-key"] == f"{stage}.dlq"
        assert work_queue(stage).exchange.type == "topic"  # native delayed delivery needs topic
        assert dead_letter_queue(stage).routing_key == f"{stage}.dlq"


def test_every_task_routes_to_its_stage_queue() -> None:
    routes = celery_app.conf.task_routes
    for stage in STAGES:
        assert routes[f"manifest.{stage}"] == {"queue": stage}

    assert routes["manifest.reconcile_shipment"] == {"queue": "reconcile"}
    assert routes["manifest.reextract_document"] == {"queue": "classify"}
