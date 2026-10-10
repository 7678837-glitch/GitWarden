import re

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.i18n import msg, render
from gitwarden.runner import audit_target, run_task
from gitwarden.web import create_app
from tests.conftest import commit, make_source

PASSWORD = "pw-status"
CJK = re.compile(r"[一-鿿]")


def test_stored_message_renders_in_requested_language_and_legacy_text_is_unchanged():
    stored = msg("run.audit_alert", missing=2, modified=1)

    assert "2 missing" in render(stored, "en")
    assert "缺失" in render(stored, "zh_CN")
    assert render("历史仓缺失 [], 被改 []", "en") == "历史仓缺失 [], 被改 []"
    assert render(None, "en") == ""


@pytest.fixture
def client(tmp_path):
    config = Config(data_dir=tmp_path / "data", admin_password=PASSWORD)
    with TestClient(create_app(config, start_scheduler=False)) as c:
        assert c.post("/login", data={"password": PASSWORD}).status_code == 200
        yield c


def _main_text(client, path):
    html = client.get(path).text
    main = html.split("<main", 1)[1].split("</main>", 1)[0]
    return re.sub(r"<[^>]+>", " ", main)


def _synced_task(client, tmp_path):
    store = Store(tmp_path / "data")
    source = make_source(tmp_path / "source")
    commit(source, "initial")
    main, history = tmp_path / "main.git", tmp_path / "history.git"
    run(None, "init", "--bare", str(main))
    run(None, "init", "--bare", str(history))
    task_id = store.add_task("demo", str(source))
    target_id = store.add_target(task_id, "t", str(main), str(history))
    from gitwarden.runner import bind_target

    bind_target(store, tmp_path / "data", target_id)
    run_task(store, tmp_path / "data", task_id)
    return store, task_id, target_id


def test_english_pages_show_no_chinese_status_after_a_real_sync_and_audit(client, tmp_path):
    store, task_id, target_id = _synced_task(client, tmp_path)
    audit_target(store, tmp_path / "data", target_id)

    for path in (f"/tasks/{task_id}", "/logs", "/", "/tasks"):
        assert not CJK.search(_main_text(client, path)), path


def test_english_pages_translate_failure_messages_but_keep_legacy_chinese(client, tmp_path):
    store, task_id, target_id = _synced_task(client, tmp_path)
    store.set_target_status(target_id, "error", msg("run.target_failed"))
    store.finish_run(store.start_run(task_id), "failed", msg("run.archive_busy"))

    text = _main_text(client, f"/tasks/{task_id}")
    assert "Target run failed" in text and "The archive is busy" in text
    assert not CJK.search(text)

    store.set_target_status(target_id, "error", "目标执行失败")
    assert "目标执行失败" in _main_text(client, f"/tasks/{task_id}")
