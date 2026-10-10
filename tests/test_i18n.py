import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.web import create_app

PASSWORD = "pw-test-123"
I18N_DIR = Path(__file__).resolve().parent.parent / "gitwarden" / "i18n"


def _catalog(name):
    return json.loads((I18N_DIR / name).read_text(encoding="utf-8"))


def _csrf(client, path):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def test_language_catalogs_have_identical_keys_and_english_is_complete():
    english = _catalog("en.json")
    chinese = _catalog("zh_CN.json")
    assert english
    assert set(english) == set(chinese)
    assert all(isinstance(value, str) and value for value in english.values())
    assert all(isinstance(value, str) and value for value in chinese.values())


def test_anonymous_language_switch_uses_double_submit_csrf_and_falls_back_to_english(
    tmp_path,
):
    app = create_app(
        Config(data_dir=tmp_path / "data", admin_password=PASSWORD), start_scheduler=False
    )
    with TestClient(app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert "Log in" in page.text
        assert client.cookies.get("gw_lang") == "en"
        token = _csrf(client, "/login")
        csrf_cookie = client.cookies.get("gw_csrf")
        assert csrf_cookie
        assert token == csrf_cookie

        response = client.post(
            "/lang",
            data={"csrf": token, "lang": "zh_CN"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.cookies.get("gw_lang") == "zh_CN"
        assert "登录" in client.get("/login").text

        response = client.post(
            "/lang",
            data={"csrf": token, "lang": "fr"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Log in" in client.get("/login").text


def test_language_switch_without_csrf_is_rejected_for_anonymous_client(tmp_path):
    app = create_app(
        Config(data_dir=tmp_path / "data", admin_password=PASSWORD), start_scheduler=False
    )
    with TestClient(app) as client:
        assert client.post("/lang", data={"lang": "zh_CN"}).status_code == 403
