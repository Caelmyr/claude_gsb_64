"""代码模板 API：每个用户按语言保存自己的编辑器模板。

存储：data/templates/<user_id>.json，单文件存放该用户全部模板，
通过 storage.locked_update 做原子读-改-写。

模板完全私有：所有接口只访问当前登录用户自己的文件，
用户之间互相看不到、也改不了对方的模板。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.storage import read_json, locked_update
from backend.utils import now_iso, gen_id, sanitize_id

templates_bp = Blueprint("templates", __name__)

MAX_TEMPLATES_PER_USER = 50          # 每用户模板数量上限
MAX_NAME_LEN = 50                    # 模板名称长度上限
MAX_CODE_LEN = 64 * 1024             # 单份模板代码上限 64KB


def _tpl_path(user_id):
    return os.path.join(config.TEMPLATES_DIR, f"{sanitize_id(user_id)}.json")


def _load_templates(user_id):
    data = read_json(_tpl_path(user_id))
    return (data or {}).get("templates", []) if data else []


def _public(t):
    return {k: t[k] for k in ("id", "name", "language", "code",
                              "is_default", "created_at", "updated_at") if k in t}


def _validate(data, partial=False):
    """校验模板字段，返回错误响应或 None。partial=True 时只校验出现的字段。"""
    if not partial or "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return err("模板名称不能为空", 400)
        if len(name) > MAX_NAME_LEN:
            return err(f"模板名称不能超过 {MAX_NAME_LEN} 字", 400)
    if not partial or "language" in data:
        if data.get("language") not in config.LANGUAGES:
            return err("不支持的编程语言", 400)
    if not partial or "code" in data:
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            return err("模板代码不能为空", 400)
        if len(code) > MAX_CODE_LEN:
            return err("模板代码过长（上限 64KB）", 400)
    return None


def _clear_default(templates, language, keep=None):
    """清除某语言下其他模板的默认标记（保证同语言默认唯一）。"""
    for t in templates:
        if t is not keep and t.get("language") == language:
            t["is_default"] = False


@templates_bp.get("/templates")
@require_auth
def list_templates():
    language = request.args.get("language")
    templates = _load_templates(request.user["id"])
    if language:
        templates = [t for t in templates if t.get("language") == language]
    return ok({"total": len(templates), "items": [_public(t) for t in templates]})


@templates_bp.post("/templates")
@require_auth
def create_template():
    data = request.get_json(silent=True) or {}
    bad = _validate(data)
    if bad:
        return bad
    language = data["language"]
    created = [None]
    overflow = [False]

    def _upd(d):
        if d is None:
            d = {"user_id": request.user["id"], "templates": []}
        templates = d.setdefault("templates", [])
        if len(templates) >= MAX_TEMPLATES_PER_USER:
            overflow[0] = True
            return d
        # 显式指定默认，或该语言的第一份模板自动成为默认
        is_default = bool(data.get("is_default")) or \
            not any(t.get("language") == language for t in templates)
        tpl = {
            "id": gen_id("tpl"),
            "name": (data.get("name") or "").strip(),
            "language": language,
            "code": data["code"],
            "is_default": is_default,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        if is_default:
            _clear_default(templates, language)
        templates.append(tpl)
        created[0] = tpl
        return d

    locked_update(_tpl_path(request.user["id"]), _upd, default=None)
    if overflow[0]:
        return err(f"模板数量已达上限（{MAX_TEMPLATES_PER_USER} 份）", 400)
    return ok(_public(created[0]))


@templates_bp.put("/templates/<tpl_id>")
@require_auth
def update_template(tpl_id):
    data = request.get_json(silent=True) or {}
    bad = _validate(data, partial=True)
    if bad:
        return bad
    if not os.path.exists(_tpl_path(request.user["id"])):
        return err("模板不存在", 404)
    updated = [None]

    def _upd(d):
        if not d:
            return d
        templates = d.get("templates", [])
        for t in templates:
            if t.get("id") != tpl_id:
                continue
            if "name" in data:
                t["name"] = (data.get("name") or "").strip()
            if "language" in data:
                t["language"] = data["language"]
            if "code" in data:
                t["code"] = data["code"]
            if "is_default" in data:
                t["is_default"] = bool(data["is_default"])
            if t.get("is_default"):
                _clear_default(templates, t.get("language"), keep=t)
            t["updated_at"] = now_iso()
            updated[0] = t
            break
        return d

    locked_update(_tpl_path(request.user["id"]), _upd, default=None)
    if updated[0] is None:
        return err("模板不存在", 404)
    return ok(_public(updated[0]))


@templates_bp.delete("/templates/<tpl_id>")
@require_auth
def delete_template(tpl_id):
    if not os.path.exists(_tpl_path(request.user["id"])):
        return err("模板不存在", 404)
    removed = [False]

    def _upd(d):
        if not d:
            return d
        before = len(d.get("templates", []))
        d["templates"] = [t for t in d.get("templates", []) if t.get("id") != tpl_id]
        if len(d["templates"]) != before:
            removed[0] = True
        return d

    locked_update(_tpl_path(request.user["id"]), _upd, default=None)
    if not removed[0]:
        return err("模板不存在", 404)
    return ok()
